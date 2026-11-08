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
import { planIdOf } from "./identity.js";
import {
  renderFacets,
  renderObservationBanners,
  renderSections,
} from "./overview.js";
import { setSelectedPlan } from "./prefs.js";
import {
  buildRow,
  markRowSelected,
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
import {
  comparePlanRows,
  computeCounts,
  isVisible,
  renderCounts,
} from "./queue_selection.js";

// Keep the queue module's public entry points stable for callers.
export {
  comparePlanRows,
  comparePlanRowsFor,
  compareByUrgencyRank,
  compareByActivity,
  compareByOldestActivity,
  oldestSortTimestampFor,
  compareLabels,
  compareByPath,
  urgencyRankFor,
  parseActivityTimestamp,
  stateOrder,
  computeCounts,
  renderCounts,
  completedHiddenCount,
  isVisible,
  serverApplied,
  matchesFilter,
  matchesCompletedVisibility,
  matchesSearchText,
  searchableTextFor,
} from "./queue_selection.js";

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
  if (state.fetchInFlight) {
    // A filter, a search, a sort or a route change asks a different question
    // than the read already on the wire. Dropping it would leave the queue
    // answering the old one until the next poll, so it is run once that read
    // settles instead.
    state.refreshPending = true;
    return;
  }
  state.fetchInFlight = true;
  state.refreshPending = false;
  setRefreshStatus("loading", "refreshing…");
  try {
    const body = await apiRequest(queueQuery());
    // Superseded while it was on the wire: the follow-up read draws instead,
    // so the operator never sees the old filter's rows flash back.
    if (state.refreshPending) return;
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
    if (state.refreshPending) return;
    setRefreshStatus("error", "error: " + describeError(err));
    markQueueStale(describeError(err));
  } finally {
    state.fetchInFlight = false;
    if (state.refreshPending) {
      state.refreshPending = false;
      refresh();
    }
  }
}

export function setRefreshStatus(stateName, text) {
  const el = document.getElementById("wb-refresh-status");
  if (!el) return;
  // The line is a permanent polite live region so a failed read is announced,
  // but the "refreshing…" / "updated" pair repeats on every poll and would
  // talk over everything else. Routine text goes to an aria-hidden child, so
  // only an error -- written to the exposed child -- is spoken. Toggling
  // aria-live in the same tick as the text change instead would leave the
  // announcement to each screen reader's timing.
  el.setAttribute("data-state", stateName);
  let routine = document.getElementById("wb-refresh-routine");
  let error = document.getElementById("wb-refresh-error");
  if (!routine || !error) {
    el.textContent = "";
    routine = document.createElement("span");
    routine.id = "wb-refresh-routine";
    routine.setAttribute("aria-hidden", "true");
    error = document.createElement("span");
    error.id = "wb-refresh-error";
    el.append(routine, error);
  }
  if (stateName === "error") {
    routine.textContent = "";
    error.textContent = text;
  } else {
    error.textContent = "";
    routine.textContent = text;
  }
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

export function upsertRow(plan) {
  const planId = planIdOf(plan);
  let row = state.rowsByPlan.get(planId);
  if (!row) {
    row = buildRow(planId);
    state.rowsByPlan.set(planId, row);
  }
  populateRow(row, plan);
  markRowSelected(row, state.selectedPlan === planId);
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
