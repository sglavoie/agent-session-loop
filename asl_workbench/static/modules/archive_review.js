/* Expired-entry review and its keyboard focus lifecycle. */

import { displayName, formatLastActivity } from "./preview.js";
import { state } from "./state.js";
import { handleArchivePurge } from "./archive_mutations.js";

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

/* The control that opened the purge review, so closing it can hand the
 * keyboard back there rather than dropping it on the document body. */
let reviewOpener = null;

function rememberReviewOpener() {
  const active = document.activeElement;
  if (!active || active === document.body) {
    reviewOpener = null;
    return;
  }
  const row = active.closest ? active.closest("[data-archive-id]") : null;
  reviewOpener = {
    element: active,
    archiveId: row ? row.getAttribute("data-archive-id") : null,
  };
}

function focusable(node) {
  return (
    !!node &&
    typeof node.focus === "function" &&
    document.body.contains(node) &&
    node.disabled !== true &&
    !node.closest("[hidden]")
  );
}

/* Back to the opener; if a re-render replaced it, to the same row's purge
 * button; failing both, to the bulk purge control. */
function restoreReviewOpener() {
  const opener = reviewOpener;
  reviewOpener = null;
  const candidates = [];
  if (opener) {
    candidates.push(opener.element);
    if (opener.archiveId) {
      const list = document.getElementById("wb-archive-entries");
      const rows = list ? list.querySelectorAll("[data-archive-id]") : [];
      rows.forEach((row) => {
        if (row.getAttribute("data-archive-id") === opener.archiveId) {
          candidates.push(row.querySelector('[data-action="archive-purge"]'));
        }
      });
    }
  }
  candidates.push(document.getElementById("wb-archive-purge-selected"));
  const target = candidates.find(focusable);
  if (target) target.focus();
}

/* Name every expired entry this purge would empty, and hold the request until
 * the operator says they have read it. Nothing is sent from here. */
export function openArchiveReview(ids, expired) {
  const panel = document.getElementById("wb-archive-review");
  const list = document.getElementById("wb-archive-review-list");
  const message = document.getElementById("wb-archive-review-message");
  if (!panel || !list || !message) return;
  rememberReviewOpener();
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
  const wasOpen = !!panel && !panel.hidden;
  if (panel) panel.hidden = true;
  state.archiveReviewPending = null;
  // Hiding the panel takes the focused acknowledge or cancel button with it;
  // the cleanup review hands the keyboard back the same way.
  if (wasOpen) restoreReviewOpener();
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
