/* Queue refresh loop and queue rendering.
 *
 * Rows are reused across polls, keyed by the plan's identity, so a refresh
 * does not rebuild the list under the operator's cursor. This is also where
 * catalog pages arrive, which makes it where a `#run=` notification link
 * finally learns which plan it names.
 */

import { apiRequest, describeError } from "./api.js";
import { FILTER_GROUPS, REFRESH_INTERVAL_MS } from "./constants.js";
import { planHint, planIdOf, planSearchText } from "./identity.js";
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

export async function refresh() {
  if (state.fetchInFlight) return;
  state.fetchInFlight = true;
  setRefreshStatus("loading", "refreshing…");
  try {
    const body = await apiRequest("/api/queue");
    if (!body || body.ok !== true) {
      const reason = body && body.reason ? body.reason : "unknown";
      setRefreshStatus("error", "error: " + reason);
      markQueueStale(reason);
      return;
    }
    const ts = new Date();
    renderQueue(body, ts);
    setRefreshStatus("idle", "updated " + ts.toLocaleTimeString());
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

export function renderQueue(payload, updatedAt) {
  const queueEl = document.getElementById("wb-queue");
  if (!queueEl) return;
  const scrollSnapshot = captureQueueScroll();
  const previewBody = document.getElementById("wb-preview-last-message-body");
  if (previewBody) scrollSnapshot.preview = previewBody.scrollTop;
  const plansList = Array.isArray(payload.plans) ? payload.plans : [];
  // A `#run=` deep link has no address until a catalog page names the run it
  // was minted for. This is where those pages arrive, so this is where such a
  // link becomes one the rest of the shell can read a plan with.
  if (resolveRouteFromPlans(plansList)) return;
  reconcileSelectedPlan(plansList);
  const sorted = plansList.slice().sort(comparePlanRows);
  const counts = computeCounts(sorted);
  renderCounts(counts);
  renderGlobalWarnings(payload);
  renderQueueMeta(payload, sorted.length, updatedAt);

  const seen = new Set();
  const visible = sorted.filter((plan) => isVisible(plan, state.activeFilter));

  const emptyEl = document.getElementById("wb-empty");
  if (visible.length === 0) {
    renderEmptyState(plansList);
  } else if (emptyEl) {
    emptyEl.hidden = true;
  }

  let previous = emptyEl;
  visible.forEach((plan) => {
    const row = upsertRow(plan);
    seen.add(planIdOf(plan));
    if (row.parentNode !== queueEl) {
      queueEl.appendChild(row);
    }
    if (previous && previous.nextSibling !== row) {
      queueEl.insertBefore(row, previous.nextSibling);
    }
    previous = row;
  });

  state.rowsByPlan.forEach((row, planId) => {
    if (!seen.has(planId)) {
      if (row.parentNode === queueEl) {
        queueEl.removeChild(row);
      }
    }
  });

  state.lastVisiblePlans = visible;
  updatePreview(visible);
  restoreQueueScroll(scrollSnapshot);
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
  state.lastQueueMeta = {
    stateRoot: stateRoot,
    stateRootExists: payload.state_root_exists !== false,
    planCount: planCount,
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
  el.textContent = message + " | stale " + formatQueueMeta(state.lastQueueMeta);
}

export function formatQueueMeta(meta) {
  const parts = [];
  if (meta.stateRoot) {
    parts.push("state root: " + meta.stateRoot);
    if (!meta.stateRootExists) parts.push("state root missing");
  }
  if (typeof meta.planCount === "number") {
    parts.push("plans: " + meta.planCount);
  }
  if (meta.updatedText) {
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
  // with workbench.URGENCY_RANK_BY_STATE ordering so visual tiers stay
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
  document.querySelectorAll(".wb-chip__count").forEach((el) => {
    const name = el.getAttribute("data-count");
    const value = counts[name];
    el.textContent = String(typeof value === "number" ? value : 0);
  });
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
  if (!matchesSearchText(plan, state.searchText)) return false;
  return true;
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
