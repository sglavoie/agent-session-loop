/* Archive view — restore, permanent purge, retention.
 *
 * Reading the archive is a read: retention marks candidates and removes
 * nothing. Only the purge empties an entry, and it asks for a typed count
 * that matches the selection before it will.
 *
 * A typed count is a good guard against pressing the wrong button and a poor
 * one against not having looked. "Select candidates" fills the selection with
 * everything retention marked, and the count that follows is a number the
 * operator can type without ever having seen which plans are behind it -- so
 * an expired entry now goes through an explicit review first, which names them
 * one by one. The count still applies afterwards; the review is what makes the
 * number mean something.
 */

import { apiRequest, describeError } from "./api.js";
import { applyChipPressedState } from "./filters.js";
import { applyActionAvailability } from "./operations.js";
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
import { hydrateQueueControls, updateSearchClearVisibility } from "./queue_controls.js";
import { navigateToQueue, syncFilterHash } from "./router.js";
import { state } from "./state.js";
import {
  handleArchiveRestore,
  handleArchiveRestoreSelected,
  handleArchivePurge,
  handleRetentionSave,
} from "./archive_mutations.js";
import { closeArchiveReview, handleArchiveReviewAck } from "./archive_review.js";

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
  const reviewAck = document.getElementById("wb-archive-review-ack");
  if (reviewAck) {
    reviewAck.addEventListener("click", handleArchiveReviewAck);
  }
  const reviewCancel = document.getElementById("wb-archive-review-cancel");
  if (reviewCancel) {
    reviewCancel.addEventListener("click", () => closeArchiveReview());
  }
}

export async function loadArchiveView() {
  if (state.archiveInFlight) {
    // A reload asked for while one is on the wire -- after a restore, a
    // purge, a settings change -- asks about an archive the read in flight may
    // have answered before that change. Dropping it would leave the view
    // describing the old archive, so it runs once the current read settles.
    state.archivePending = true;
    return;
  }
  state.archiveInFlight = true;
  state.archivePending = false;
  setRefreshStatus("loading", "refreshing…");
  try {
    const body = await apiRequest("/api/archive");
    // Superseded while on the wire: the follow-up read draws instead.
    if (state.archivePending) return;
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
    if (state.archivePending) return;
    renderArchiveError(describeError(err));
    setRefreshStatus("error", "error: " + describeError(err));
  } finally {
    state.archiveInFlight = false;
    if (state.archivePending) {
      state.archivePending = false;
      // Only while the archive is still what is on screen: a reload asked for
      // before the operator left is not a reason to read it from the queue.
      if (state.route && state.route.name === "archive") loadArchiveView();
    }
  }
}

export function reconcileArchiveSelection() {
  const present = new Set(
    state.archiveEntries.map((entry) => entry && entry.archive_id)
  );
  Array.from(state.archiveSelection).forEach((id) => {
    if (!present.has(id)) state.archiveSelection.delete(id);
  });
  // An acknowledgement belongs to the entry it was given for. Once the entry
  // is gone the acknowledgement has to go with it, or a later entry reusing
  // the id would arrive already reviewed.
  Array.from(state.archiveReviewed).forEach((id) => {
    if (!present.has(id)) state.archiveReviewed.delete(id);
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
      entry.summary || displayName(entry.original_plan_dir) || archiveId
    );
    setField(li, "path", entry.original_plan_dir || "");
    const pathField = li.querySelector('[data-field="path"]');
    if (pathField) pathField.hidden = !entry.original_plan_dir;
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
  // The bulk buttons have two reasons to be shut and they are different
  // sentences: an empty selection is the operator's to fix, a mode that does
  // not carry restores out is not. The nearer reason is written on the node
  // and the capability pass leaves it alone.
  setBlockedWhenEmpty(
    document.getElementById("wb-archive-restore-selected"),
    count
  );
  setBlockedWhenEmpty(
    document.getElementById("wb-archive-purge-selected"),
    count
  );
  const clearBtn = document.getElementById("wb-archive-clear");
  if (clearBtn) clearBtn.disabled = count === 0;
  applyActionAvailability(document.getElementById("wb-archive"));
}

function setBlockedWhenEmpty(node, count) {
  if (!node) return;
  if (count === 0) {
    node.setAttribute("data-blocked-reason", "no archived entries are selected");
  } else {
    node.removeAttribute("data-blocked-reason");
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

export {
  requestArchiveRestore,
  handleArchiveRestore,
  handleArchiveRestoreSelected,
  handleArchivePurge,
  handleRetentionSave,
} from "./archive_mutations.js";
export {
  unreviewedExpiredIds,
  openArchiveReview,
  closeArchiveReview,
  handleArchiveReviewAck,
} from "./archive_review.js";
