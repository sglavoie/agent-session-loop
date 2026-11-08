/* Queue ordering, visibility, and filter counts. */

import { FILTER_GROUPS } from "./constants.js";
import { planHint, planLabel, planSearchText } from "./identity.js";
import { state } from "./state.js";
import { toEpochMs } from "./time.js";

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
  // The label the row displays, so a remote row (which carries neither
  // `label` nor `plan_dir`) sorts by the name the operator reads.
  const labelA = planLabel(a).toLowerCase();
  const labelB = planLabel(b).toLowerCase();
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

/** A published moment in epoch milliseconds, read the way the window reads it. */
export function parseActivityTimestamp(value) {
  return toEpochMs(value);
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

