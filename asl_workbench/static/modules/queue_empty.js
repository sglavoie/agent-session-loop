/* Explain empty queues using the active filters and retained catalog. */

import { matchesFilter } from "./queue.js";
import { state } from "./state.js";

/** The facet narrowings in force, worded for the operator. */
function activeFacets() {
  const active = [];
  if (state.projectFilter) active.push("project: " + state.projectFilter);
  if (state.harnessFilter) active.push("harness: " + state.harnessFilter);
  return active;
}

export function classifyEmptyState(plansList) {
  const facets = activeFacets();
  if (!Array.isArray(plansList) || plansList.length === 0) {
    // An empty list is no longer evidence that the machine is empty. Since the
    // narrowing moved to the server, every filter that matches nothing arrives
    // here as zero rows -- the ones that did not match were never sent. Saying
    // "no plans discovered" under a project filter would tell an operator
    // their corpus is gone when they have merely filtered it away, and it
    // would say so with no button to undo.
    if (facets.length || state.searchText || state.activeFilter !== "all") {
      const tail = facets.slice();
      if (state.activeFilter !== "all") {
        tail.push("filter: " + state.activeFilter);
      }
      return {
        kind: state.searchText ? "no-search-results" : "no-filter-matches",
        primary: state.searchText
          ? 'no plans match search "' + state.searchText + '"'
          : "no plans match the current filters",
        secondary: tail.length ? "active: " + tail.join(" | ") : "",
        searchText: state.searchText,
        activeFilter: state.activeFilter,
        showCompleted: state.showCompleted,
        // Unknowable from here for the same reason: a completed plan hidden by
        // this request was never sent, so the page cannot say whether one
        // exists. Offering the toggle is the honest move -- it widens the
        // question rather than claiming an answer.
        completedHidden: false,
        canClearSearch: !!state.searchText,
        canShowCompleted:
          !state.showCompleted && state.activeFilter !== "complete",
        canResetFilter: facets.length > 0 || state.activeFilter !== "all",
      };
    }
    return {
      kind: "no-plans",
      primary: "no plans discovered",
      secondary: "",
      searchText: "",
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: false,
      canClearSearch: false,
      canShowCompleted: false,
      canResetFilter: false,
    };
  }
  const completedHiding =
    !state.showCompleted && state.activeFilter !== "complete";
  const completedExists = plansList.some(
    (plan) =>
      (plan && plan.urgency_group === "complete") ||
      (plan && plan.primary_state === "complete")
  );
  const filterMatchedAtAll = plansList.some((plan) =>
    matchesFilter(plan, state.activeFilter)
  );
  const completedHidden = completedHiding && completedExists;

  if (state.searchText) {
    const primary = 'no plans match search "' + state.searchText + '"';
    const tail = facets.slice();
    if (state.activeFilter !== "all") {
      tail.push("filter: " + state.activeFilter);
    }
    if (completedHidden) tail.push("completed hidden");
    return {
      kind: "no-search-results",
      primary: primary,
      secondary: tail.length ? "active: " + tail.join(" | ") : "",
      searchText: state.searchText,
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: completedHidden,
      canClearSearch: true,
      canShowCompleted: completedHidden,
      canResetFilter: facets.length > 0 || state.activeFilter !== "all",
    };
  }

  if (!filterMatchedAtAll && state.activeFilter !== "all") {
    const tail = facets.slice();
    if (completedHidden) tail.push("completed hidden");
    return {
      kind: "no-filter-matches",
      primary: 'no plans match filter "' + state.activeFilter + '"',
      secondary: tail.length ? "active: " + tail.join(" | ") : "",
      searchText: "",
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: completedHidden,
      canClearSearch: false,
      canShowCompleted: completedHidden,
      canResetFilter: true,
    };
  }

  if (completedHidden) {
    return {
      kind: "completed-hidden",
      primary: "no actionable plans — completed hidden",
      secondary: "toggle “show completed” to see the rest",
      searchText: "",
      activeFilter: state.activeFilter,
      showCompleted: state.showCompleted,
      completedHidden: true,
      canClearSearch: false,
      canShowCompleted: true,
      canResetFilter: facets.length > 0 || state.activeFilter !== "all",
    };
  }

  return {
    kind: "no-actionable",
    primary: "no actionable plans",
    secondary: "",
    searchText: "",
    activeFilter: state.activeFilter,
    showCompleted: state.showCompleted,
    completedHidden: false,
    canClearSearch: false,
    canShowCompleted: false,
    canResetFilter: state.activeFilter !== "all",
  };
}

export function describeEmptyState(plansList) {
  const info = classifyEmptyState(plansList);
  const parts = [info.primary];
  if (info.secondary) parts.push(info.secondary);
  return parts.join(" | ");
}

export function renderEmptyState(plansList) {
  const emptyEl = document.getElementById("wb-empty");
  if (!emptyEl) return;
  const info = classifyEmptyState(plansList);
  emptyEl.hidden = false;
  emptyEl.setAttribute("data-state", info.kind);

  const primaryEl = document.getElementById("wb-empty-primary");
  if (primaryEl) primaryEl.textContent = info.primary;

  const secondaryEl = document.getElementById("wb-empty-secondary");
  if (secondaryEl) {
    if (info.secondary) {
      secondaryEl.hidden = false;
      secondaryEl.textContent = info.secondary;
    } else {
      secondaryEl.hidden = true;
      secondaryEl.textContent = "";
    }
  }

  const actionsEl = document.getElementById("wb-empty-actions");
  const clearSearchBtn = document.getElementById("wb-empty-clear-search");
  const showCompletedBtn = document.getElementById("wb-empty-show-completed");
  const resetFilterBtn = document.getElementById("wb-empty-reset-filter");
  if (clearSearchBtn) clearSearchBtn.hidden = !info.canClearSearch;
  if (showCompletedBtn) showCompletedBtn.hidden = !info.canShowCompleted;
  if (resetFilterBtn) resetFilterBtn.hidden = !info.canResetFilter;
  if (actionsEl) {
    const anyVisible =
      info.canClearSearch || info.canShowCompleted || info.canResetFilter;
    actionsEl.hidden = !anyVisible;
  }
}
