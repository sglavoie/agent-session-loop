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
import { setSearchText, setShowCompleted, setSortMode } from "./prefs.js";
import { refresh } from "./queue.js";
import { state } from "./state.js";

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
    refresh();
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
        refresh();
      }
    });
  }
}

export function wireSortControl() {
  const select = document.getElementById("wb-sort");
  if (!select) return;
  select.addEventListener("change", () => {
    if (setSortMode(select.value)) {
      refresh();
    }
  });
}

export function wireShowCompletedControl() {
  const toggle = document.getElementById("wb-show-completed");
  if (!toggle) return;
  toggle.addEventListener("change", () => {
    if (setShowCompleted(toggle.checked)) {
      refresh();
    }
  });
}
