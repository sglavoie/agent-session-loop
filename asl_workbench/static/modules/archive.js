/* Archive view — restore, permanent purge, retention.
 *
 * Reading the archive is a read: retention marks candidates and removes
 * nothing. Only the purge empties an entry, and it asks for a typed count
 * that matches the selection before it will.
 */

import { apiRequest, describeError } from "./api.js";
import { applyChipPressedState } from "./filters.js";
import { setField } from "./panels.js";
import {
  persistFilter,
  setHarnessFilter,
  setProjectFilter,
  setSearchText,
  setShowCompleted,
} from "./prefs.js";
import { displayName, formatLastActivity } from "./preview.js";
import { refresh, setRefreshStatus } from "./queue.js";
import {
  hydrateQueueControls,
  updateSearchClearVisibility,
} from "./queue_controls.js";
import { navigateToQueue, syncFilterHash } from "./router.js";
import { state } from "./state.js";

export function wireArchiveControls() {
  const back = document.getElementById("wb-archive-back");
  if (back) {
    back.addEventListener("click", navigateToQueue);
  }
  const refreshBtn = document.getElementById("wb-archive-refresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", loadArchiveView);
  }
  const retentionSave = document.getElementById("wb-archive-retention-save");
  if (retentionSave) {
    retentionSave.addEventListener("click", handleRetentionSave);
  }
  const selectAll = document.getElementById("wb-archive-select-all");
  if (selectAll) {
    selectAll.addEventListener("click", selectAllArchiveEntries);
  }
  const selectCandidatesBtn = document.getElementById(
    "wb-archive-select-candidates"
  );
  if (selectCandidatesBtn) {
    selectCandidatesBtn.addEventListener(
      "click",
      selectArchiveCleanupCandidates
    );
  }
  const restoreSelected = document.getElementById(
    "wb-archive-restore-selected"
  );
  if (restoreSelected) {
    restoreSelected.addEventListener("click", handleArchiveRestoreSelected);
  }
  const purgeSelected = document.getElementById("wb-archive-purge-selected");
  if (purgeSelected) {
    purgeSelected.addEventListener("click", () => {
      handleArchivePurge(Array.from(state.archiveSelection));
    });
  }
  const clearSel = document.getElementById("wb-archive-clear");
  if (clearSel) {
    clearSel.addEventListener("click", () => {
      state.archiveSelection.clear();
      renderArchiveEntries(state.archiveEntries);
      updateArchiveBulk();
    });
  }
}

export async function loadArchiveView() {
  if (state.archiveInFlight) return;
  state.archiveInFlight = true;
  setRefreshStatus("loading", "refreshing…");
  try {
    const body = await apiRequest("/api/archive");
    if (!body || body.ok !== true) {
      renderArchiveError((body && body.error) || "failed to load archive");
      setRefreshStatus("error", "error: archive");
      return;
    }
    state.archiveEntries = Array.isArray(body.entries) ? body.entries : [];
    state.archiveRetentionDays =
      typeof body.archive_retention_days === "number"
        ? body.archive_retention_days
        : null;
    state.archiveCleanup = body.archive_cleanup || null;
    reconcileArchiveSelection();
    renderArchiveView(body);
    setRefreshStatus("idle", "updated " + new Date().toLocaleTimeString());
  } catch (err) {
    renderArchiveError(describeError(err));
    setRefreshStatus("error", "error: " + describeError(err));
  } finally {
    state.archiveInFlight = false;
  }
}

export function reconcileArchiveSelection() {
  const present = new Set(
    state.archiveEntries.map((entry) => entry && entry.archive_id)
  );
  Array.from(state.archiveSelection).forEach((id) => {
    if (!present.has(id)) state.archiveSelection.delete(id);
  });
}

export function loadedArchiveIds() {
  // Valid archive IDs from the currently loaded entries, as strings so they
  // match the row-checkbox keys stored in state.archiveSelection.
  return state.archiveEntries
    .map((entry) => (entry && entry.archive_id ? String(entry.archive_id) : ""))
    .filter((id) => id !== "");
}

export function selectAllArchiveEntries() {
  // "select all" adds every loaded archive entry to the selection, then
  // re-renders so row checkboxes sync and refreshes bulk control state.
  loadedArchiveIds().forEach((id) => state.archiveSelection.add(id));
  renderArchiveEntries(state.archiveEntries);
  updateArchiveBulk();
}

export function renderArchiveError(message) {
  const list = document.getElementById("wb-archive-entries");
  if (!list) return;
  list.innerHTML = "";
  const li = document.createElement("li");
  li.className = "wb-empty";
  li.textContent = "archive unavailable: " + String(message);
  list.appendChild(li);
}

export function renderArchiveView(payload) {
  const retentionInput = document.getElementById("wb-archive-retention");
  if (retentionInput && typeof payload.archive_retention_days === "number") {
    retentionInput.value = String(payload.archive_retention_days);
  }
  renderArchiveCleanup(state.archiveCleanup);
  renderArchiveWarnings(payload.warnings || []);
  renderArchiveEntries(state.archiveEntries);
  updateArchiveBulk();
}

export function renderArchiveCleanup(cleanup) {
  // Reports what retention has *marked*, not what it removed. The line used
  // to read "auto-empty: N purged" and was the operator's first notice that
  // opening this tab had destroyed something; now it names candidates that
  // are all still on disk, and "select candidates" is how they act on it.
  const el = document.getElementById("wb-archive-cleanup");
  if (!el) return;
  const candidates =
    cleanup && typeof cleanup.candidate_count === "number"
      ? cleanup.candidate_count
      : 0;
  const undated =
    cleanup && typeof cleanup.undated_count === "number"
      ? cleanup.undated_count
      : 0;
  if (candidates === 0 && undated === 0) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  const parts = [];
  if (candidates) {
    const days =
      cleanup && typeof cleanup.retention_days === "number"
        ? cleanup.retention_days
        : state.archiveRetentionDays;
    const suffix = days ? " (older than " + days + "d)" : "";
    parts.push(
      candidates +
        (candidates === 1 ? " entry" : " entries") +
        " past retention" +
        suffix +
        " — still kept until you empty them"
    );
  }
  if (undated) {
    parts.push(undated + " with an unreadable archive date");
  }
  el.textContent = parts.join(" | ");
}

export function selectArchiveCleanupCandidates() {
  // Selects exactly what retention marked, so emptying the candidates is one
  // deliberate act rather than a checkbox hunt. The purge confirmation still
  // applies: this only fills the selection.
  const ids =
    state.archiveCleanup && Array.isArray(state.archiveCleanup.candidate_ids)
      ? state.archiveCleanup.candidate_ids
      : [];
  state.archiveSelection.clear();
  ids.forEach((id) => {
    if (id) state.archiveSelection.add(String(id));
  });
  renderArchiveEntries(state.archiveEntries);
  updateArchiveBulk();
}

export function renderArchiveWarnings(warnings) {
  const el = document.getElementById("wb-archive-warnings");
  if (!el) return;
  const items = Array.isArray(warnings)
    ? warnings.filter((w) => typeof w === "string" && w)
    : [];
  if (items.length === 0) {
    el.hidden = true;
    el.innerHTML = "";
    return;
  }
  el.hidden = false;
  el.innerHTML = "<ul></ul>";
  const ul = el.querySelector("ul");
  items.forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    ul.appendChild(li);
  });
}

export function describeCleanupState(value) {
  if (value === "eligible") return "cleanup: candidate";
  if (value === "undated") return "cleanup: unknown age";
  if (value === "retained") return "cleanup: within retention";
  return "";
}

export function renderArchiveEntries(entries) {
  const list = document.getElementById("wb-archive-entries");
  if (!list) return;
  list.innerHTML = "";
  if (!Array.isArray(entries) || entries.length === 0) {
    const li = document.createElement("li");
    li.className = "wb-empty";
    li.textContent = "no archived plans";
    list.appendChild(li);
    return;
  }
  const tmpl = document.getElementById("wb-archive-row-template");
  entries.forEach((entry) => {
    const fragment = tmpl.content.cloneNode(true);
    const li = fragment.querySelector("li");
    const archiveId =
      entry && entry.archive_id ? String(entry.archive_id) : "";
    li.setAttribute("data-archive-id", archiveId);
    setField(
      li,
      "label",
      entry.summary || displayName(entry.original_plan_dir)
    );
    setField(li, "path", entry.original_plan_dir || "");
    setField(
      li,
      "archived",
      "archived: " + formatLastActivity(entry.archived_at)
    );
    setField(
      li,
      "expires",
      "expires: " + formatLastActivity(entry.expires_at)
    );
    // `expires_at` is when the entry becomes a candidate, not when it goes
    // away; this line says which side of that it is on so the two are not
    // read as the same thing.
    const cleanupState =
      typeof entry.cleanup_state === "string" ? entry.cleanup_state : "";
    li.setAttribute("data-cleanup", cleanupState);
    setField(li, "cleanup", describeCleanupState(cleanupState));
    const box = li.querySelector('[data-field="archive-select"] input');
    if (box) {
      box.checked = state.archiveSelection.has(archiveId);
      box.addEventListener("change", () => {
        if (box.checked) {
          state.archiveSelection.add(archiveId);
        } else {
          state.archiveSelection.delete(archiveId);
        }
        updateArchiveBulk();
      });
    }
    const restoreBtn = li.querySelector('[data-action="archive-restore"]');
    if (restoreBtn) {
      restoreBtn.addEventListener("click", () =>
        handleArchiveRestore(archiveId)
      );
    }
    const purgeBtn = li.querySelector('[data-action="archive-purge"]');
    if (purgeBtn) {
      purgeBtn.addEventListener("click", () =>
        handleArchivePurge([archiveId])
      );
    }
    list.appendChild(li);
  });
}

export function updateArchiveBulk() {
  const count = state.archiveSelection.size;
  const loaded = loadedArchiveIds().length;
  const countEl = document.getElementById("wb-archive-count");
  if (countEl) {
    countEl.textContent = count + " of " + loaded + " entries selected";
  }
  const selectAllBtn = document.getElementById("wb-archive-select-all");
  if (selectAllBtn) selectAllBtn.disabled = loaded === 0 || count >= loaded;
  const candidatesBtn = document.getElementById(
    "wb-archive-select-candidates"
  );
  if (candidatesBtn) {
    const candidates =
      state.archiveCleanup &&
      typeof state.archiveCleanup.candidate_count === "number"
        ? state.archiveCleanup.candidate_count
        : 0;
    candidatesBtn.disabled = candidates === 0;
  }
  const restoreBtn = document.getElementById("wb-archive-restore-selected");
  if (restoreBtn) restoreBtn.disabled = count === 0;
  const purgeBtn = document.getElementById("wb-archive-purge-selected");
  if (purgeBtn) purgeBtn.disabled = count === 0;
  const clearBtn = document.getElementById("wb-archive-clear");
  if (clearBtn) clearBtn.disabled = count === 0;
}

export function requestArchiveRestore(archiveId) {
  // Shared single-entry restore request used by both row-level and bulk
  // restore so the endpoint and payload shape stay aligned.
  return apiRequest("/api/archive/restore", {
    method: "POST",
    body: { archive_id: archiveId },
  });
}

export async function handleArchiveRestore(archiveId) {
  if (!archiveId) return;
  showArchiveFeedback("", "");
  try {
    const body = await requestArchiveRestore(archiveId);
    if (body && body.ok) {
      showArchiveFeedback("info", "restored " + archiveId);
      loadArchiveView();
    } else {
      showArchiveFeedback("error", (body && body.error) || "restore failed");
    }
  } catch (err) {
    showArchiveFeedback("error", describeError(err));
  }
}

export async function handleArchiveRestoreSelected() {
  // Best-effort bulk restore: copy the selected IDs at click time, confirm
  // with a plain count prompt (no typed confirmation — that is reserved for
  // permanent purge), then restore each entry through the single-entry
  // endpoint and continue past per-entry failures.
  const ids = Array.from(state.archiveSelection).filter(Boolean);
  if (ids.length === 0) return;
  const confirmed = window.confirm(
    "Restore " + ids.length + " selected archived plan(s)?"
  );
  if (!confirmed) return;
  showArchiveFeedback("", "");
  let restored = 0;
  let failed = 0;
  for (const id of ids) {
    try {
      const body = await requestArchiveRestore(id);
      if (body && body.ok) {
        restored += 1;
        state.archiveSelection.delete(id);
      } else {
        failed += 1;
      }
    } catch (err) {
      failed += 1;
    }
  }
  showArchiveFeedback(
    failed > 0 ? "warning" : "info",
    "restored " + restored + (failed ? ", " + failed + " failed" : "")
  );
  loadArchiveView();
}

export async function handleArchivePurge(archiveIds) {
  const ids = Array.isArray(archiveIds) ? archiveIds.filter(Boolean) : [];
  if (ids.length === 0) return;
  const typed = window.prompt(
    "Permanently purge " +
      ids.length +
      " archived plan(s)? This cannot be undone. Type the number of entries to confirm:",
    ""
  );
  if (typed === null) return;
  const confirmCount = parseInt((typed || "").trim(), 10);
  if (!Number.isFinite(confirmCount) || confirmCount !== ids.length) {
    showArchiveFeedback(
      "error",
      "confirmation count must equal " + ids.length
    );
    return;
  }
  showArchiveFeedback("", "");
  try {
    const body = await apiRequest("/api/archive/delete", {
      method: "POST",
      body: { archive_ids: ids, confirm_count: confirmCount },
    });
    if (body && body.ok) {
      const removed =
        typeof body.removed_count === "number" ? body.removed_count : 0;
      ids.forEach((id) => state.archiveSelection.delete(id));
      showArchiveFeedback("info", "purged " + removed);
      loadArchiveView();
    } else {
      showArchiveFeedback("error", (body && body.error) || "purge failed");
    }
  } catch (err) {
    showArchiveFeedback("error", describeError(err));
  }
}

export async function handleRetentionSave() {
  const input = document.getElementById("wb-archive-retention");
  if (!input) return;
  const days = parseInt((input.value || "").trim(), 10);
  if (!Number.isFinite(days) || days < 1) {
    showArchiveFeedback(
      "error",
      "retention must be a positive number of days"
    );
    return;
  }
  showArchiveFeedback("", "");
  try {
    const body = await apiRequest("/api/archive/settings", {
      method: "POST",
      body: { archive_retention_days: days },
    });
    if (body && body.ok) {
      state.archiveRetentionDays =
        typeof body.archive_retention_days === "number"
          ? body.archive_retention_days
          : days;
      showArchiveFeedback(
        "info",
        "retention set to " + state.archiveRetentionDays + " days"
      );
      loadArchiveView();
    } else {
      showArchiveFeedback(
        "error",
        (body && body.error) || "failed to update retention"
      );
    }
  } catch (err) {
    showArchiveFeedback("error", describeError(err));
  }
}

export function showArchiveFeedback(kind, message) {
  const el = document.getElementById("wb-archive-feedback");
  if (!el) return;
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.setAttribute("data-kind", kind);
  el.textContent = message;
}

export function wireEmptyStateActions() {
  const clearSearch = document.getElementById("wb-empty-clear-search");
  if (clearSearch) {
    clearSearch.addEventListener("click", () => {
      if (setSearchText("")) {
        const input = document.getElementById("wb-search");
        if (input) input.value = "";
        updateSearchClearVisibility();
        refresh();
      }
    });
  }
  const showCompleted = document.getElementById("wb-empty-show-completed");
  if (showCompleted) {
    showCompleted.addEventListener("click", () => {
      if (setShowCompleted(true)) {
        const toggle = document.getElementById("wb-show-completed");
        if (toggle) toggle.checked = true;
        refresh();
      }
    });
  }
  const resetFilter = document.getElementById("wb-empty-reset-filter");
  if (resetFilter) {
    resetFilter.addEventListener("click", () => {
      // Everything the button offers to undo, not just the chip. The project
      // and harness facets narrow the same queue and are the reason it can be
      // empty, so a reset that left them set would be a reset that changed
      // nothing and an operator with no way back.
      let changed = false;
      if (state.activeFilter !== "all") {
        state.activeFilter = "all";
        persistFilter("all");
        applyChipPressedState();
        changed = true;
      }
      if (setProjectFilter("")) changed = true;
      if (setHarnessFilter("")) changed = true;
      if (!changed) return;
      hydrateQueueControls();
      syncFilterHash();
      refresh();
    });
  }
}
