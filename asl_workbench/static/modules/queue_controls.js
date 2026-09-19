/* Search / sort / completed-visibility controls.
 *
 * Each control writes the preference and re-renders from the plans already in
 * hand: none of them refetches, because none of them changes what the server
 * would answer.
 */

import { applyCleanupMode } from "./cleanup.js";
import {
  DEFAULT_SORT_MODE,
  SEARCH_DEBOUNCE_MS,
  SORT_MODES,
} from "./constants.js";
import {
  setHarnessFilter,
  setProjectFilter,
  setSearchText,
  setShowCompleted,
  setShowOlderRuns,
  setSortMode,
} from "./prefs.js";
import { refresh, rerenderQueue } from "./queue.js";
import { syncFilterHash } from "./router.js";
import { state } from "./state.js";

/* Every control below does the same two things in the same order: write the
 * preference, then put it in the URL. The second half is what makes a narrowed
 * queue a thing an operator can send to someone else, and it is done here
 * rather than in the render so a refresh that changes nothing does not rewrite
 * the address bar.
 */

function applyAndRefresh() {
  syncFilterHash();
  refresh();
}

export function hydrateQueueControls() {
  const searchInput = document.getElementById("wb-search");
  if (searchInput) searchInput.value = state.searchText || "";
  const sortSelect = document.getElementById("wb-sort");
  if (sortSelect) {
    // Defensive: only assign if the option exists in the markup.
    if (SORT_MODES.indexOf(state.sortMode) !== -1) {
      sortSelect.value = state.sortMode;
    } else {
      sortSelect.value = DEFAULT_SORT_MODE;
    }
  }
  const completedToggle = document.getElementById("wb-show-completed");
  if (completedToggle) completedToggle.checked = !!state.showCompleted;
  const projectSelect = document.getElementById("wb-project-filter");
  if (projectSelect) projectSelect.value = state.projectFilter || "";
  const harnessSelect = document.getElementById("wb-harness-filter");
  if (harnessSelect) harnessSelect.value = state.harnessFilter || "";
  const olderToggle = document.getElementById("wb-older-toggle");
  if (olderToggle) {
    olderToggle.setAttribute(
      "aria-pressed",
      state.showOlderRuns ? "true" : "false"
    );
  }
  const thresholdInput = document.getElementById("wb-cleanup-threshold");
  if (thresholdInput) thresholdInput.value = String(state.cleanupThresholdDays);
  updateSearchClearVisibility();
  applyCleanupMode();
}

export function updateSearchClearVisibility() {
  const clearBtn = document.getElementById("wb-search-clear");
  if (!clearBtn) return;
  clearBtn.hidden = !state.searchText;
}

export function scheduleSearchRefresh() {
  if (state.searchDebounceTimer !== null) {
    window.clearTimeout(state.searchDebounceTimer);
  }
  state.searchDebounceTimer = window.setTimeout(() => {
    state.searchDebounceTimer = null;
    applyAndRefresh();
  }, SEARCH_DEBOUNCE_MS);
}

export function wireSearchControl() {
  const input = document.getElementById("wb-search");
  if (input) {
    input.addEventListener("input", () => {
      if (setSearchText(input.value)) {
        updateSearchClearVisibility();
        scheduleSearchRefresh();
      }
    });
  }
  const clearBtn = document.getElementById("wb-search-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      if (setSearchText("")) {
        if (input) input.value = "";
        updateSearchClearVisibility();
        applyAndRefresh();
      }
    });
  }
}

export function wireSortControl() {
  const select = document.getElementById("wb-sort");
  if (!select) return;
  select.addEventListener("change", () => {
    if (setSortMode(select.value)) {
      applyAndRefresh();
    }
  });
}

export function wireShowCompletedControl() {
  const toggle = document.getElementById("wb-show-completed");
  if (!toggle) return;
  toggle.addEventListener("change", () => {
    if (setShowCompleted(toggle.checked)) {
      applyAndRefresh();
    }
  });
}

export function wireFacetControls() {
  const project = document.getElementById("wb-project-filter");
  if (project) {
    project.addEventListener("change", () => {
      if (setProjectFilter(project.value)) applyAndRefresh();
    });
  }
  const harness = document.getElementById("wb-harness-filter");
  if (harness) {
    harness.addEventListener("change", () => {
      if (setHarnessFilter(harness.value)) applyAndRefresh();
    });
  }
}

/**
 * The inactive-work toggle.
 *
 * It does not refetch: the window is a reading of rows already in hand against
 * a clock, so the server's answer to this request would be the same answer it
 * just gave. Only the layout changes, and the URL, so the view stays sendable.
 */
export function wireOlderRunsControl() {
  const toggle = document.getElementById("wb-older-toggle");
  if (!toggle) return;
  toggle.addEventListener("click", () => {
    setShowOlderRuns(!state.showOlderRuns);
    toggle.setAttribute("aria-pressed", state.showOlderRuns ? "true" : "false");
    syncFilterHash();
    rerenderQueue();
  });
}
