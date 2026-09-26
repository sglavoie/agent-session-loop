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

import { OUTCOME_PENDING, OUTCOME_RESULT, apiRequest, describeError } from "./api.js";
import { applyChipPressedState } from "./filters.js";
import {
  applyActionAvailability,
  captureFocus,
  runMutation,
} from "./operations.js";
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

export function requestArchiveRestore(archiveId) {
  // Shared single-entry restore used by both row-level and bulk restore, so
  // the endpoint, the payload shape, and the classification of the answer stay
  // aligned. It returns the classified answer rather than an envelope: a
  // restore the far side only recorded is not a restored plan.
  return runMutation({
    action: "archive-restore",
    path: "/api/archive/restore",
    body: { archive_id: archiveId },
    subject: "restore of " + archiveId,
    feedback: showArchiveFeedback,
  });
}

async function settledArchiveAnswer(answer) {
  if (!answer || answer.outcome !== OUTCOME_PENDING) return answer?.outcome === OUTCOME_RESULT;
  const id = answer.operation?.id;
  if (!id) return false;
  try {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const receipt = await apiRequest("/api/archive?operation_id=" + encodeURIComponent(id));
      const status = receipt?.operation?.status;
      if (status === "consumed") return true;
      if (["rejected", "expired", "superseded", "unconfirmed", "cancelled"].includes(status)) {
        showArchiveFeedback("error", "operation " + id + " is " + status);
        return false;
      }
    }
  } catch (error) {
    showArchiveFeedback("warning", "operation " + id + " is unconfirmed: " + describeError(error));
    return false;
  }
  showArchiveFeedback("warning", "operation " + id + " is still pending; check its receipt before retrying");
  return false;
}

export async function handleArchiveRestore(archiveId) {
  if (!archiveId) return;
  showArchiveFeedback("", "");
  const answer = await requestArchiveRestore(archiveId);
  if (await settledArchiveAnswer(answer)) {
    showArchiveFeedback("info", "restored " + archiveId);
    loadArchiveView();
  }
}

export async function handleArchiveRestoreSelected() {
  // Copy selected IDs at click time and restore one at a time. An uncertain
  // receipt stops the batch so the operator can inspect it before retrying.
  const ids = Array.from(state.archiveSelection).filter(Boolean);
  if (ids.length === 0) return;
  const restoreFocus = captureFocus();
  const confirmed = window.confirm(
    "Restore " + ids.length + " selected archived plan(s)?"
  );
  restoreFocus();
  if (!confirmed) return;
  showArchiveFeedback("", "");
  let restored = 0;
  let failed = 0;
  for (const id of ids) {
    const answer = await requestArchiveRestore(id);
    if (await settledArchiveAnswer(answer)) {
      restored += 1;
      state.archiveSelection.delete(id);
    } else {
      failed += 1;
      break;
    }
  }
  const parts = ["restored " + restored];
  if (failed) parts.push("stopped at the first unconfirmed or failed entry");
  showArchiveFeedback(
    failed > 0 ? "warning" : "info",
    parts.join(", ")
  );
  loadArchiveView();
}

/* The entries in a selection that retention has marked and the operator has
 * not yet looked at. Empty is the ordinary case: an entry still inside
 * retention is one the operator chose by name. */
export function unreviewedExpiredIds(ids) {
  const byId = new Map();
  state.archiveEntries.forEach((entry) => {
    if (entry && entry.archive_id) byId.set(String(entry.archive_id), entry);
  });
  return ids.filter((id) => {
    const entry = byId.get(String(id));
    if (!entry) return false;
    if (entry.cleanup_state !== "eligible") return false;
    return !state.archiveReviewed.has(String(id));
  });
}

/* Name every expired entry this purge would empty, and hold the request until
 * the operator says they have read it. Nothing is sent from here. */
export function openArchiveReview(ids, expired) {
  const panel = document.getElementById("wb-archive-review");
  const list = document.getElementById("wb-archive-review-list");
  const message = document.getElementById("wb-archive-review-message");
  if (!panel || !list || !message) return;
  state.archiveReviewPending = ids.slice();
  message.textContent =
    expired.length +
    (expired.length === 1 ? " entry is" : " entries are") +
    " past retention. Purging empties them permanently and there is no undo. Read the list, then confirm you have reviewed it — the typed count still follows.";
  list.textContent = "";
  const byId = new Map();
  state.archiveEntries.forEach((entry) => {
    if (entry && entry.archive_id) byId.set(String(entry.archive_id), entry);
  });
  expired.forEach((id) => {
    const entry = byId.get(String(id)) || {};
    const li = document.createElement("li");
    li.className = "wb-archive-review__row";
    li.setAttribute("data-archive-id", String(id));
    const label = document.createElement("span");
    label.className = "wb-archive-review__label";
    label.textContent =
      entry.summary || displayName(entry.original_plan_dir) || String(id);
    li.appendChild(label);
    const path = document.createElement("span");
    path.className = "wb-archive-review__path";
    path.textContent = entry.original_plan_dir || "";
    if (entry.original_plan_dir) li.appendChild(path);
    const expires = document.createElement("span");
    expires.className = "wb-archive-review__expires";
    expires.textContent = "expired: " + formatLastActivity(entry.expires_at);
    li.appendChild(expires);
    list.appendChild(li);
  });
  panel.hidden = false;
  const ack = document.getElementById("wb-archive-review-ack");
  if (ack && typeof ack.focus === "function") ack.focus();
}

export function closeArchiveReview() {
  const panel = document.getElementById("wb-archive-review");
  if (panel) panel.hidden = true;
  state.archiveReviewPending = null;
}

export async function handleArchiveReviewAck() {
  const ids = Array.isArray(state.archiveReviewPending)
    ? state.archiveReviewPending.slice()
    : [];
  unreviewedExpiredIds(ids).forEach((id) =>
    state.archiveReviewed.add(String(id))
  );
  closeArchiveReview();
  if (ids.length === 0) return;
  await handleArchivePurge(ids);
}

export async function handleArchivePurge(archiveIds) {
  const ids = Array.isArray(archiveIds) ? archiveIds.filter(Boolean) : [];
  if (ids.length === 0) return;
  const expired = unreviewedExpiredIds(ids);
  if (expired.length > 0) {
    // Nothing is asked and nothing is sent until the list has been seen. This
    // is the step that separates "I typed 12" from "I know which twelve".
    showArchiveFeedback(
      "warning",
      "review the " +
        expired.length +
        " expired " +
        (expired.length === 1 ? "entry" : "entries") +
        " before purging"
    );
    openArchiveReview(ids, expired);
    return;
  }
  const restoreFocus = captureFocus();
  const typed = window.prompt(
    "Permanently purge " +
      ids.length +
      " archived plan(s)? This cannot be undone. Type the number of entries to confirm:",
    ""
  );
  restoreFocus();
  if (typed === null) return;
  const typedCount = (typed || "").trim();
  const confirmCount = Number(typedCount);
  if (!/^\d+$/.test(typedCount) || confirmCount !== ids.length) {
    showArchiveFeedback(
      "error",
      "confirmation count must equal " + ids.length
    );
    return;
  }
  showArchiveFeedback("", "");
  const remote = state.capabilities?.mode === "remote";
  let removed = 0;
  for (const id of (remote ? ids : [null])) {
    const answer = await runMutation({
      action: "archive-purge",
      path: "/api/archive/delete",
      body: remote
        ? { archive_ids: [id], confirm_count: 1 }
        : { archive_ids: ids, confirm_count: confirmCount },
      subject: "the purge",
      feedback: showArchiveFeedback,
    });
    if (!(await settledArchiveAnswer(answer))) {
      showArchiveFeedback("warning", "purged " + removed + "; stopped at an unconfirmed or failed entry. Check its receipt before retrying.");
      break;
    }
    const completed = remote ? [id] : ids;
    completed.forEach((done) => {
      state.archiveSelection.delete(done);
      state.archiveReviewed.delete(String(done));
    });
    removed += remote ? 1 : (answer.payload?.removed_count || 0);
  }
  if (removed === ids.length) showArchiveFeedback("info", "purged " + removed);
  loadArchiveView();
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
  const answer = await runMutation({
    action: "archive-settings",
    path: "/api/archive/settings",
    body: { archive_retention_days: days },
    subject: "the retention change",
    feedback: showArchiveFeedback,
  });
  if (await settledArchiveAnswer(answer)) {
    state.archiveRetentionDays =
      typeof answer.payload.archive_retention_days === "number"
        ? answer.payload.archive_retention_days
        : days;
    showArchiveFeedback(
      "info",
      "retention set to " + state.archiveRetentionDays + " days"
    );
    loadArchiveView();
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
