/* Queue refresh loop and queue rendering.
 *
 * Rows are reused across polls, keyed by the plan's identity, so a refresh
 * does not rebuild the list under the operator's cursor. This is also where
 * catalog pages arrive, which makes it where a `#run=` notification link
 * finally learns which plan it names.
 */

import { apiRequest, describeError } from "./api.js";
import { showCleanupFeedback, updateCleanupReview } from "./cleanup.js";
import {
  FILTER_GROUPS,
  QUEUE_PAGE_LIMIT,
  REFRESH_INTERVAL_MS,
} from "./constants.js";
import { refreshHistory } from "./history.js";
import { planHint, planIdOf, planSearchText } from "./identity.js";
import {
  renderFacets,
  renderObservationBanners,
  renderSections,
} from "./overview.js";
import { setSelectedPlan } from "./prefs.js";
import {
  buildRow,
  populateRow,
  renderEmptyState,
  updatePreview,
} from "./preview.js";
import { resolveRouteFromPlans } from "./router.js";
import {
  captureQueueScroll,
  isDesktopShell,
  restoreQueueScroll,
} from "./shell.js";
import { state } from "./state.js";

export function scheduleRefresh() {
  stopQueueTimer();
  state.refreshTimer = window.setTimeout(() => {
    refresh().finally(() => {
      if (shouldPollQueue()) scheduleRefresh();
    });
  }, REFRESH_INTERVAL_MS);
}

export function shouldPollQueue() {
  return (
    state.route.name === "queue" ||
    (state.route.name === "detail" && isDesktopShell())
  );
}

export function stopQueueTimer() {
  if (state.refreshTimer !== null) {
    window.clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
  }
}

/**
 * The narrowing the queue read carries, as query parameters.
 *
 * Every one of these used to be applied in the browser, against whatever the
 * server happened to send. That is only correct while the catalog fits in one
 * answer: past the read limit, a browser-side filter describes the first page
 * rather than the catalog, and a count taken from it is a count of what was
 * fetched. So the narrowing goes to the server, which applies it before the
 * limit and counts over what matched.
 */
export function queueQuery(offset = 0) {
  const params = new URLSearchParams();
  if (state.activeFilter && state.activeFilter !== "all") {
    params.set("filter", state.activeFilter);
  }
  if (state.projectFilter) params.set("project", state.projectFilter);
  if (state.searchText) params.set("q", state.searchText);
  if (state.harnessFilter) params.set("harness", state.harnessFilter);
  if (state.sortMode) params.set("sort", state.sortMode);
  // Always sent, because the API's default is "narrow nothing": hiding
  // completed work is this view's opinion, so this view is what asks for it.
  // Cleanup mode reviews completed plans without changing the operator's
  // persisted preference, exactly as it did when the filter ran here.
  params.set(
    "include_completed",
    state.showCompleted || state.cleanupMode ? "1" : "0"
  );
  params.set("limit", String(QUEUE_PAGE_LIMIT));
  if (offset > 0) params.set("offset", String(offset));
  const query = params.toString();
  return query ? "/api/queue?" + query : "/api/queue";
}

export async function refresh() {
  if (state.fetchInFlight) return;
  state.fetchInFlight = true;
  setRefreshStatus("loading", "refreshing…");
  try {
    const body = await apiRequest(queueQuery());
    if (!body || body.ok !== true) {
      const reason = body && body.reason ? body.reason : "unknown";
      setRefreshStatus("error", "error: " + reason);
      markQueueStale(reason);
      return;
    }
    const ts = new Date();
    renderQueue(body, ts);
    setRefreshStatus("idle", "updated " + ts.toLocaleTimeString());
    // History has its own retention and its own read; it is refreshed from
    // here so the two panes on one screen never describe two different
    // moments, and it is not awaited so a slow history read cannot hold up
    // the work an operator is actually watching.
    refreshHistory();
  } catch (err) {
    setRefreshStatus("error", "error: " + describeError(err));
    markQueueStale(describeError(err));
  } finally {
    state.fetchInFlight = false;
  }
}

export function setRefreshStatus(stateName, text) {
  const el = document.getElementById("wb-refresh-status");
  if (!el) return;
  el.setAttribute("data-state", stateName);
  el.textContent = text;
}

/**
 * Draw the answer already in hand again.
 *
 * For the controls that change where a row goes rather than which rows there
 * are — the inactive-work toggle is the only one today. Refetching for those
 * would ask the server a question it has just answered, and would replace a
 * layout change with a network round trip the operator can see.
 */
export function rerenderQueue() {
  if (!state.lastQueuePayload) {
    refresh();
    return;
  }
  renderQueue(state.lastQueuePayload, state.lastQueuePayloadAt);
}

export function renderQueue(payload, updatedAt) {
  const queueEl = document.getElementById("wb-queue");
  if (!queueEl) return;
  state.lastQueuePayload = payload;
  state.lastQueuePayloadAt = updatedAt instanceof Date ? updatedAt : new Date();
  const scrollSnapshot = captureQueueScroll();
  const previewBody = document.getElementById("wb-preview-last-message-body");
  if (previewBody) scrollSnapshot.preview = previewBody.scrollTop;
  const plansList = Array.isArray(payload.plans) ? payload.plans : [];
  // A `#run=` deep link has no address until a catalog page names the run it
  // was minted for. This is where those pages arrive, so this is where such a
  // link becomes one the rest of the shell can read a plan with.
  //
  // Drawn from the page already in hand rather than left to the next poll: on
  // a phone the queue timer is stopped while a detail route is open, so
  // returning here would leave the sections as they were before the link
  // resolved -- and the heading that says "this run is old news" is precisely
  // what the operator opened the notification to find out.
  if (resolveRouteFromPlans(plansList)) {
    renderQueue(payload, state.lastQueuePayloadAt);
    return;
  }
  reconcileSelectedPlan(plansList);
  adoptQueuePayload(payload);
  if (state.cleanupSelection.size && state.cleanupSelectionRevision &&
      state.queueCatalogRevision !== state.cleanupSelectionRevision) {
    state.cleanupSelection.clear();
    state.cleanupCandidates.clear();
    state.cleanupSelectionRevision = null;
    showCleanupFeedback("warning", "the catalog changed; select plans again before archiving");
    updateCleanupReview();
  }
  const sorted = plansList.slice().sort(comparePlanRows);
  renderCounts(state.queueCounts || computeCounts(sorted));
  renderFacets(state.queueFacets);
  renderGlobalWarnings(payload);
  renderObservationBanners(payload);
  renderQueueMeta(payload, sorted.length, updatedAt);

  // The page arrived already narrowed, so this is not the filter a second
  // time: it is the one rule the server cannot apply, because cleanup mode is
  // a state of this browser rather than of the request.
  const visible = sorted.filter((plan) => isVisible(plan, state.activeFilter));

  const emptyEl = document.getElementById("wb-empty");
  if (visible.length === 0) {
    renderEmptyState(plansList);
  } else if (emptyEl) {
    emptyEl.hidden = true;
  }

  const seen = renderSections(queueEl, emptyEl, visible);
  state.rowsByPlan.forEach((row, planId) => {
    if (!seen.has(planId) && row.parentNode) {
      row.parentNode.removeChild(row);
    }
  });

  state.lastVisiblePlans = visible;
  updatePreview(visible);
  restoreQueueScroll(scrollSnapshot);
}

/**
 * Remember what the answer said about itself, as opposed to about plans.
 *
 * The counts, the facets and the matched total are the server's now, taken
 * over the complete result set rather than over the page, so they are kept
 * rather than recomputed: recomputing them here would be recomputing them from
 * 200 rows and calling the result a property of the catalog.
 */
export function adoptQueuePayload(payload) {
  state.queueFilters =
    payload.filters && typeof payload.filters === "object"
      ? payload.filters
      : null;
  state.queueCounts = isCountMap(payload.counts) ? payload.counts : null;
  state.queueFacets = {
    projects: Array.isArray(payload.projects) ? payload.projects : [],
    harnesses: Array.isArray(payload.harnesses) ? payload.harnesses : [],
  };
  state.queuePage = {
    matched:
      typeof payload.catalog_matched === "number"
        ? payload.catalog_matched
        : null,
    total:
      typeof payload.catalog_total === "number" ? payload.catalog_total : null,
    offset:
      payload.filters && typeof payload.filters.offset === "number"
        ? payload.filters.offset
        : 0,
    limit:
      payload.filters && typeof payload.filters.limit === "number"
        ? payload.filters.limit
        : QUEUE_PAGE_LIMIT,
    truncated: payload.truncated === true,
  };
  state.queueFreshness =
    typeof payload.freshness === "string" ? payload.freshness : null;
  state.queueConnectors = Array.isArray(payload.connectors)
    ? payload.connectors
    : [];
  state.queueIncidents = Array.isArray(payload.incidents)
    ? payload.incidents
    : [];
  state.queueObservedAt =
    typeof payload.observed_at === "number" ? payload.observed_at : null;
  state.queueCompleteness =
    typeof payload.catalog_completeness === "string"
      ? payload.catalog_completeness
      : null;
  state.queueCatalogRevision =
    typeof payload.catalog_revision === "string"
      ? payload.catalog_revision
      : null;
}

function isCountMap(value) {
  if (!value || typeof value !== "object") return false;
  return Object.keys(FILTER_GROUPS).every(
    (name) => typeof value[name] === "number"
  );
}

export function renderQueueMeta(payload, planCount, updatedAt) {
  const el = document.getElementById("wb-queue-meta");
  if (!el) return;
  const stateRoot =
    typeof payload.state_root === "string" && payload.state_root
      ? payload.state_root
      : null;
  const updatedText =
    updatedAt instanceof Date
      ? updatedAt.toLocaleTimeString()
      : new Date().toLocaleTimeString();
  const page = state.queuePage || {};
  state.lastQueueMeta = {
    stateRoot: stateRoot,
    stateRootExists: payload.state_root_exists !== false,
    planCount: planCount,
    // What the page is a page *of*. Without these two an operator reading
    // "plans: 200" cannot tell a catalog of 200 from the first fifth of one.
    matched: typeof page.matched === "number" ? page.matched : null,
    catalogTotal: typeof page.total === "number" ? page.total : null,
    truncated: page.truncated === true,
    narrowed: isNarrowed(),
    updatedText: updatedText,
  };
  el.hidden = false;
  el.removeAttribute("data-state");
  el.textContent = formatQueueMeta(state.lastQueueMeta);
}

export function markQueueStale(reason) {
  const el = document.getElementById("wb-queue-meta");
  if (!el) return;
  el.hidden = false;
  el.setAttribute("data-state", "stale");
  const message = reason ? "queue fetch failed: " + reason : "queue fetch failed";
  if (!state.lastQueueMeta) {
    el.textContent = message;
    return;
  }
  // Only a stale queue repeats the time: the header already says when a
  // healthy one was fetched, and a failed fetch leaves that header behind.
  el.textContent =
    message +
    " | stale " +
    formatQueueMeta(state.lastQueueMeta, { includeUpdated: true });
}

/** True when this view is narrower than the whole catalog. */
export function isNarrowed() {
  return !!(
    state.projectFilter ||
    state.harnessFilter ||
    state.searchText ||
    (state.activeFilter && state.activeFilter !== "all")
  );
}

export function formatQueueMeta(meta, options) {
  const includeUpdated = !!(options && options.includeUpdated);
  const parts = [];
  if (meta.stateRoot) {
    parts.push("state root: " + meta.stateRoot);
    if (!meta.stateRootExists) parts.push("state root missing");
  }
  if (typeof meta.planCount === "number") {
    parts.push("plans: " + meta.planCount);
  }
  if (typeof meta.matched === "number") {
    // "matching" rather than "matched": the number is about the catalog as it
    // is now, and it is the number the chips were counted over.
    const suffix =
      typeof meta.catalogTotal === "number" && meta.narrowed
        ? " of " + meta.catalogTotal
        : "";
    parts.push("matching: " + meta.matched + suffix);
  } else if (typeof meta.catalogTotal === "number") {
    parts.push("catalog: " + meta.catalogTotal);
  }
  if (meta.truncated) {
    parts.push("showing the first page — narrow the filters to see the rest");
  }
  if (includeUpdated && meta.updatedText) {
    parts.push("updated: " + meta.updatedText);
  }
  return parts.join(" | ");
}

export function comparePlanRows(a, b) {
  return comparePlanRowsFor(state.sortMode, a, b);
}

export function comparePlanRowsFor(sortMode, a, b) {
  if (sortMode === "label") {
    const cmp = compareLabels(a, b);
    if (cmp !== 0) return cmp;
    return compareByUrgencyRank(a, b);
  }
  if (sortMode === "recent") {
    const cmp = compareByActivity(a, b);
    if (cmp !== 0) return cmp;
    const rank = compareByUrgencyRank(a, b);
    if (rank !== 0) return rank;
    return compareLabels(a, b);
  }
  if (sortMode === "path") {
    const cmp = compareByPath(a, b);
    if (cmp !== 0) return cmp;
    const rank = compareByUrgencyRank(a, b);
    if (rank !== 0) return rank;
    return compareLabels(a, b);
  }
  if (sortMode === "oldest") {
    const cmp = compareByOldestActivity(a, b);
    if (cmp !== 0) return cmp;
    const rank = compareByUrgencyRank(a, b);
    if (rank !== 0) return rank;
    return compareLabels(a, b);
  }
  // Default: urgency tier first, freshest activity within tier, then label.
  const rank = compareByUrgencyRank(a, b);
  if (rank !== 0) return rank;
  const cmp = compareByActivity(a, b);
  if (cmp !== 0) return cmp;
  return compareLabels(a, b);
}

export function compareByUrgencyRank(a, b) {
  const rankA = urgencyRankFor(a);
  const rankB = urgencyRankFor(b);
  if (rankA !== rankB) return rankA - rankB;
  return 0;
}

export function compareByActivity(a, b) {
  // Newer activity first; missing timestamps sort last.
  const tsA = parseActivityTimestamp(a && a.last_activity_at);
  const tsB = parseActivityTimestamp(b && b.last_activity_at);
  if (tsA === tsB) return 0;
  if (tsA === null) return 1;
  if (tsB === null) return -1;
  return tsB - tsA;
}

export function compareByOldestActivity(a, b) {
  // Oldest first; missing timestamps sort as oldest (first) so never-run and
  // completion-less plans surface at the top of the cleanup candidate list.
  // Complete plans compare by completion time; all others by last activity.
  const tsA = oldestSortTimestampFor(a);
  const tsB = oldestSortTimestampFor(b);
  if (tsA === tsB) return 0;
  if (tsA === null) return -1;
  if (tsB === null) return 1;
  return tsA - tsB;
}

export function oldestSortTimestampFor(plan) {
  if (plan && plan.primary_state === "complete") {
    return parseActivityTimestamp(plan.completed_at);
  }
  return parseActivityTimestamp(plan && plan.last_activity_at);
}

export function compareLabels(a, b) {
  const labelA = (a.label || a.plan_dir || "").toLowerCase();
  const labelB = (b.label || b.plan_dir || "").toLowerCase();
  return labelA.localeCompare(labelB);
}

export function compareByPath(a, b) {
  const pathA = (a && a.plan_dir ? a.plan_dir : "").toLowerCase();
  const pathB = (b && b.plan_dir ? b.plan_dir : "").toLowerCase();
  return pathA.localeCompare(pathB);
}

export function urgencyRankFor(plan) {
  if (plan && typeof plan.urgency_rank === "number") {
    return plan.urgency_rank;
  }
  return stateOrder(plan && plan.primary_state);
}

export function parseActivityTimestamp(value) {
  if (typeof value !== "string" || !value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function stateOrder(primaryState) {
  // Legacy fallback when payload pre-dates triage fields. Keep aligned
  // with workbench_summary_model.URGENCY_RANK_BY_STATE ordering so visual tiers stay
  // stable across server upgrades.
  const ranking = {
    "paused-for-reply": 10,
    "failed": 20,
    "stale": 30,
    "invalid": 40,
    "verifying": 50,
    "running": 60,
    "recent": 70,
    "ready": 80,
    "complete": 90,
  };
  if (Object.prototype.hasOwnProperty.call(ranking, primaryState)) {
    return ranking[primaryState];
  }
  return 990;
}

export function computeCounts(plans) {
  const counts = { all: plans.length };
  Object.keys(FILTER_GROUPS).forEach((name) => {
    if (name === "all") return;
    counts[name] = 0;
  });
  plans.forEach((plan) => {
    Object.keys(FILTER_GROUPS).forEach((name) => {
      if (name === "all") return;
      if (FILTER_GROUPS[name].indexOf(plan.primary_state) !== -1) {
        counts[name] += 1;
      }
    });
  });
  return counts;
}

export function renderCounts(counts) {
  const hidden = completedHiddenCount(counts);
  document.querySelectorAll(".wb-chip__count").forEach((el) => {
    const name = el.getAttribute("data-count");
    let value = typeof counts[name] === "number" ? counts[name] : 0;
    // "all" counts what the "all" view shows. With completed plans hidden it
    // would otherwise promise rows the queue below does not draw.
    if (name === "all" && hidden > 0) {
      value = Math.max(0, value - hidden);
      el.title = hidden + " completed hidden";
    } else {
      el.removeAttribute("title");
    }
    el.textContent = String(value);
  });
}

/** How many completed plans the "show completed" toggle is keeping out of "all". */
export function completedHiddenCount(counts) {
  if (state.cleanupMode || state.showCompleted) return 0;
  return counts && typeof counts.complete === "number" ? counts.complete : 0;
}

export function renderGlobalWarnings(payload) {
  const el = document.getElementById("wb-warnings");
  if (!el) return;
  const items = [];
  appendWarnings(items, payload.registry_warnings, "registry");
  appendWarnings(items, payload.discovery_warnings, "discovery");
  if (items.length === 0) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = "<ul></ul>";
  const list = el.querySelector("ul");
  items.forEach((entry) => {
    const li = document.createElement("li");
    li.textContent = entry;
    list.appendChild(li);
  });
}

export function appendWarnings(items, source, prefix) {
  if (!Array.isArray(source)) return;
  source.forEach((warning) => {
    if (typeof warning === "string" && warning) {
      items.push("[" + prefix + "] " + warning);
    }
  });
}

export function isVisible(plan, activeFilter) {
  if (!matchesFilter(plan, activeFilter)) return false;
  if (!matchesCompletedVisibility(plan, activeFilter, state.showCompleted)) {
    return false;
  }
  // Only when the answer in hand did not already do it. A provider that
  // searched before its read limit searched the catalog; repeating it here
  // against the row's folded haystack can only subtract, and what it would
  // subtract is the rows the server matched on a title this row does not
  // carry. The echo is how the two are told apart, so a payload predating the
  // parameter still gets a working search box.
  if (!serverApplied("search", state.searchText)) {
    if (!matchesSearchText(plan, state.searchText)) return false;
  }
  return true;
}

/** True when the last answer says the server applied this filter itself. */
export function serverApplied(name, value) {
  const echoed = state.queueFilters;
  if (!echoed || typeof echoed !== "object") return false;
  const sent = value === undefined || value === null || value === "" ? null : value;
  const back = echoed[name] === undefined ? null : echoed[name];
  return back === sent;
}

export function matchesFilter(plan, activeFilter) {
  if (activeFilter === "all") return true;
  const group = FILTER_GROUPS[activeFilter] || [];
  return group.indexOf(plan.primary_state) !== -1;
}

export function matchesCompletedVisibility(plan, activeFilter, showCompleted) {
  // The "needs attention" default hides completed plans unless the
  // operator either toggles them back on or explicitly picks the
  // "complete" chip.
  //
  // Cleanup mode reveals completed plans for review without mutating the
  // persisted wb.completed preference, so stale completed plans surface in
  // the cleanup list even when the queue default would hide them.
  if (state.cleanupMode) return true;
  if (showCompleted) return true;
  if (activeFilter === "complete") return true;
  const group = plan && plan.urgency_group;
  if (group === "complete") return false;
  if (plan && plan.primary_state === "complete") return false;
  return true;
}

export function matchesSearchText(plan, searchText) {
  if (!searchText) return true;
  const haystack = searchableTextFor(plan);
  if (!haystack) return false;
  return haystack.indexOf(searchText) !== -1;
}

export function searchableTextFor(plan) {
  if (!plan) return "";
  // Both providers pre-fold this, under different names; `planSearchText`
  // knows which. The fallback below is for legacy payloads pre-dating
  // session 1 that carried neither.
  const published = planSearchText(plan);
  if (published) return published;
  const parts = [
    plan.label,
    plan.display_label,
    plan.title,
    plan.plan_dir,
    plan.plan_key,
    plan.primary_state,
    plan.recommended_action,
    planHint(plan),
  ];
  return parts
    .filter((part) => typeof part === "string" && part)
    .join(" ")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

export function upsertRow(plan) {
  const planId = planIdOf(plan);
  let row = state.rowsByPlan.get(planId);
  if (!row) {
    row = buildRow(planId);
    state.rowsByPlan.set(planId, row);
  }
  populateRow(row, plan);
  const isSelected = state.selectedPlan === planId;
  row.setAttribute("data-selected", isSelected ? "true" : "false");
  return row;
}

export function reconcileSelectedPlan(plansList) {
  if (!state.selectedPlan) return;
  const stillPresent = plansList.some(
    (plan) => plan && planIdOf(plan) === state.selectedPlan
  );
  if (!stillPresent) {
    // Plan vanished from the payload (deleted on disk, hidden by a
    // discovery scope change, etc.). Drop the stale selection so the
    // next click on a visible row can take over.
    setSelectedPlan(null);
  }
}
