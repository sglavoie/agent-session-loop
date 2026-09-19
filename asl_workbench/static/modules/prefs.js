/* Token + URL bootstrap, and the preferences that outlive a reload.
 *
 * The filter, search text, sort mode, completed-visibility, selection, and
 * cleanup settings are remembered; the reply textarea and the urgent
 * correction draft never are. That split is the whole point of this module:
 * what an operator typed for an agent to read is not a preference.
 */

import {
  CLEANUP_DEFAULT_THRESHOLD_DAYS,
  CLEANUP_MODE_STORAGE_KEY,
  CLEANUP_THRESHOLD_STORAGE_KEY,
  COMPLETED_STORAGE_KEY,
  DEFAULT_SORT_MODE,
  FILTER_GROUPS,
  FILTER_STORAGE_KEY,
  HARNESS_STORAGE_KEY,
  OLDER_STORAGE_KEY,
  PROJECT_STORAGE_KEY,
  SEARCH_STORAGE_KEY,
  SELECTED_STORAGE_KEY,
  SORT_MODES,
  SORT_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "./constants.js";
import { state } from "./state.js";

export function resolveToken() {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get("token");
  if (fromUrl) {
    try {
      window.sessionStorage.setItem(TOKEN_STORAGE_KEY, fromUrl);
    } catch (err) {
      // sessionStorage unavailable; fall back to in-memory token.
    }
    url.searchParams.delete("token");
    const cleaned =
      url.pathname + (url.search ? url.search : "") + (url.hash || "");
    try {
      window.history.replaceState(null, "", cleaned || "/");
    } catch (err) {
      // ignore — older browsers, file:// loads, etc.
    }
    return fromUrl;
  }
  try {
    return window.sessionStorage.getItem(TOKEN_STORAGE_KEY) || null;
  } catch (err) {
    return null;
  }
}

export function restoreFilter() {
  try {
    const stored = window.sessionStorage.getItem(FILTER_STORAGE_KEY);
    if (stored && Object.prototype.hasOwnProperty.call(FILTER_GROUPS, stored)) {
      return stored;
    }
  } catch (err) {
    // ignore
  }
  return "all";
}

export function persistFilter(name) {
  try {
    window.sessionStorage.setItem(FILTER_STORAGE_KEY, name);
  } catch (err) {
    // ignore
  }
}

// Queue UI preferences (search/sort/completed visibility/selected plan).
// Reply textarea text is intentionally never persisted — see
// wb-reply-text handling and the privacy regression in
// WorkbenchStaticAssetTests.

export function restoreSearchText() {
  try {
    const raw = window.sessionStorage.getItem(SEARCH_STORAGE_KEY);
    return normalizeSearchText(raw);
  } catch (err) {
    return "";
  }
}

export function persistSearchText(value) {
  try {
    const normalized = normalizeSearchText(value);
    if (normalized) {
      window.sessionStorage.setItem(SEARCH_STORAGE_KEY, normalized);
    } else {
      window.sessionStorage.removeItem(SEARCH_STORAGE_KEY);
    }
  } catch (err) {
    // ignore
  }
}

export function normalizeSearchText(value) {
  if (typeof value !== "string") return "";
  return value.toLowerCase().replace(/\s+/g, " ").trim();
}

export function setSearchText(value) {
  const normalized = normalizeSearchText(value);
  if (normalized === state.searchText) return false;
  state.searchText = normalized;
  persistSearchText(normalized);
  return true;
}

export function restoreSortMode() {
  try {
    const stored = window.sessionStorage.getItem(SORT_STORAGE_KEY);
    if (stored && SORT_MODES.indexOf(stored) !== -1) {
      return stored;
    }
  } catch (err) {
    // ignore
  }
  return DEFAULT_SORT_MODE;
}

export function persistSortMode(name) {
  try {
    if (SORT_MODES.indexOf(name) === -1) return;
    window.sessionStorage.setItem(SORT_STORAGE_KEY, name);
  } catch (err) {
    // ignore
  }
}

export function setSortMode(name) {
  if (SORT_MODES.indexOf(name) === -1) return false;
  if (state.sortMode === name) return false;
  state.sortMode = name;
  persistSortMode(name);
  return true;
}

export function restoreShowCompleted() {
  try {
    const stored = window.sessionStorage.getItem(COMPLETED_STORAGE_KEY);
    if (stored === "true") return true;
    if (stored === "false") return false;
  } catch (err) {
    // ignore
  }
  return false;
}

export function persistShowCompleted(value) {
  try {
    window.sessionStorage.setItem(
      COMPLETED_STORAGE_KEY,
      value ? "true" : "false"
    );
  } catch (err) {
    // ignore
  }
}

export function setShowCompleted(value) {
  const next = value === true;
  if (state.showCompleted === next) return false;
  state.showCompleted = next;
  persistShowCompleted(next);
  return true;
}

// Project and harness are facets rather than fixed vocabularies: what exists
// is whatever the catalog holds, so neither is validated against a list here.
// A remembered value the catalog no longer contains is kept and offered as an
// option anyway — see `renderFacets` — because a filter that silently widened
// itself would show an operator more than they asked to see.

function restoreFacet(key) {
  try {
    const stored = window.sessionStorage.getItem(key);
    return typeof stored === "string" && stored ? stored : null;
  } catch (err) {
    return null;
  }
}

function persistFacet(key, value) {
  try {
    if (value) {
      window.sessionStorage.setItem(key, value);
    } else {
      window.sessionStorage.removeItem(key);
    }
  } catch (err) {
    // ignore
  }
}

export function normalizeFacet(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

export function restoreProjectFilter() {
  return restoreFacet(PROJECT_STORAGE_KEY);
}

export function setProjectFilter(value) {
  const next = normalizeFacet(value);
  if (state.projectFilter === next) return false;
  state.projectFilter = next;
  persistFacet(PROJECT_STORAGE_KEY, next);
  return true;
}

export function restoreHarnessFilter() {
  return restoreFacet(HARNESS_STORAGE_KEY);
}

export function setHarnessFilter(value) {
  const next = normalizeFacet(value);
  if (state.harnessFilter === next) return false;
  state.harnessFilter = next;
  persistFacet(HARNESS_STORAGE_KEY, next);
  return true;
}

export function restoreShowOlderRuns() {
  try {
    return window.sessionStorage.getItem(OLDER_STORAGE_KEY) === "true";
  } catch (err) {
    return false;
  }
}

export function setShowOlderRuns(value) {
  const next = value === true;
  if (state.showOlderRuns === next) return false;
  state.showOlderRuns = next;
  try {
    window.sessionStorage.setItem(OLDER_STORAGE_KEY, next ? "true" : "false");
  } catch (err) {
    // ignore
  }
  return true;
}

export function restoreSelectedPlan() {
  try {
    const stored = window.sessionStorage.getItem(SELECTED_STORAGE_KEY);
    if (typeof stored === "string" && stored) return stored;
  } catch (err) {
    // ignore
  }
  return null;
}

export function persistSelectedPlan(planDir) {
  try {
    if (planDir) {
      window.sessionStorage.setItem(SELECTED_STORAGE_KEY, planDir);
    } else {
      window.sessionStorage.removeItem(SELECTED_STORAGE_KEY);
    }
  } catch (err) {
    // ignore
  }
}

export function setSelectedPlan(planDir) {
  const next = typeof planDir === "string" && planDir ? planDir : null;
  if (state.selectedPlan === next) return false;
  state.selectedPlan = next;
  persistSelectedPlan(next);
  return true;
}

export function clearSelectedPlan() {
  return setSelectedPlan(null);
}

// Cleanup mode is a session-scoped UI preference (mode toggle + candidate
// threshold). The selected plan set is intentionally NOT persisted — a
// bulk archive is a deliberate, in-session action.

export function restoreCleanupMode() {
  try {
    return window.sessionStorage.getItem(CLEANUP_MODE_STORAGE_KEY) === "true";
  } catch (err) {
    return false;
  }
}

export function persistCleanupMode(value) {
  try {
    window.sessionStorage.setItem(
      CLEANUP_MODE_STORAGE_KEY,
      value ? "true" : "false"
    );
  } catch (err) {
    // ignore
  }
}

export function restoreCleanupThreshold() {
  try {
    const raw = window.sessionStorage.getItem(CLEANUP_THRESHOLD_STORAGE_KEY);
    return normalizeThresholdDays(raw);
  } catch (err) {
    return CLEANUP_DEFAULT_THRESHOLD_DAYS;
  }
}

export function persistCleanupThreshold(days) {
  try {
    window.sessionStorage.setItem(
      CLEANUP_THRESHOLD_STORAGE_KEY,
      String(normalizeThresholdDays(days))
    );
  } catch (err) {
    // ignore
  }
}

export function normalizeThresholdDays(value) {
  const parsed = parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return CLEANUP_DEFAULT_THRESHOLD_DAYS;
  }
  return parsed;
}
