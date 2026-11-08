/* Archive restore, purge and retention submissions. */

import { OUTCOME_PENDING, OUTCOME_RESULT, apiRequest, describeError } from "./api.js";
import {
  HELD_MESSAGE,
  captureFocus,
  holdOperation,
  releaseOperation,
  runMutation,
} from "./operations.js";
import { state } from "./state.js";
import { loadArchiveView, showArchiveFeedback } from "./archive.js";
import { unreviewedExpiredIds, openArchiveReview } from "./archive_review.js";

export function requestArchiveRestore(archiveId) {
  // Shared single-entry restore used by both row-level and bulk restore, so
  // the endpoint, the payload shape, and the classification of the answer stay
  // aligned. It returns the classified answer rather than an envelope: a
  // restore the far side only recorded is not a restored plan. Its callers
  // hold "archive-restore" for as long as they poll, so it submits within it.
  return runMutation({
    action: "archive-restore",
    path: "/api/archive/restore",
    body: { archive_id: archiveId },
    subject: "restore of " + archiveId,
    withinHold: true,
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

/* Every restore button stays shut until the receipt settles, not just while
 * the request is out: `settledArchiveAnswer` polls for up to forty seconds,
 * and a second click in that window would start a second loop with new
 * operation ids. */
function holdRestores() {
  if (holdOperation("archive-restore")) return true;
  showArchiveFeedback("error", HELD_MESSAGE);
  return false;
}

export async function handleArchiveRestore(archiveId) {
  if (!archiveId) return;
  if (!holdRestores()) return;
  showArchiveFeedback("", "");
  let restored = false;
  try {
    const answer = await requestArchiveRestore(archiveId);
    restored = await settledArchiveAnswer(answer);
  } finally {
    releaseOperation("archive-restore");
  }
  if (restored) {
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
  if (!holdRestores()) return;
  showArchiveFeedback("", "");
  let restored = 0;
  let failed = 0;
  try {
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
  } finally {
    releaseOperation("archive-restore");
  }
  const parts = ["restored " + restored];
  if (failed) parts.push("stopped at the first unconfirmed or failed entry");
  showArchiveFeedback(
    failed > 0 ? "warning" : "info",
    parts.join(", ")
  );
  loadArchiveView();
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
