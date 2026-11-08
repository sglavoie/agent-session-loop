/* Minimal durable operation receipts and their rendered outcomes. */

import {
  OUTCOME_PENDING,
  OUTCOME_REFUSED,
  OUTCOME_RESULT,
  OUTCOME_UNREACHABLE,
} from "./api.js";
import {
  OPERATION_RECEIPTS_STORAGE_KEY,
  OPERATION_RECEIPT_LIMIT,
} from "./constants.js";
import { routePlanId } from "./identity.js";
import { state } from "./state.js";
import { ACTIONS } from "./operations.js";

// ---------------------------------------------------------------------------
// Receipts that survive a reload
// ---------------------------------------------------------------------------
//
// What is written down here is deliberately minimal: an operation id, which
// action it belonged to, which plan it was aimed at, the outcome word, and the
// server's machine-readable reason code. No message, no draft, no target
// prose. The id is the only thing a retry needs and the only thing a reload
// cannot reconstruct, so it is the only thing stored.

function readReceipts() {
  try {
    const raw = window.sessionStorage.getItem(OPERATION_RECEIPTS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(isReceipt) : [];
  } catch (err) {
    return [];
  }
}

function isReceipt(entry) {
  return !!(
    entry &&
    typeof entry === "object" &&
    typeof entry.operation_id === "string" &&
    entry.operation_id
  );
}

function writeReceipts(entries) {
  try {
    window.sessionStorage.setItem(
      OPERATION_RECEIPTS_STORAGE_KEY,
      JSON.stringify(entries.slice(-OPERATION_RECEIPT_LIMIT))
    );
  } catch (err) {
    // Storage can be full or refused outright. The receipt is still on screen
    // for this page's life; losing it on reload is worse than losing the page.
  }
}

/** Every recorded receipt, newest last. */
export function operationReceipts() {
  return readReceipts();
}

/** The receipts recorded against one plan, newest first. */
export function receiptsForPlan(planId) {
  if (!planId) return [];
  return readReceipts()
    .filter((entry) => entry.plan === planId)
    .reverse();
}

/**
 * Write one answer down under its operation id.
 *
 * Keyed by id rather than appended, so a retry that reuses an id updates the
 * receipt it already has instead of implying two submissions happened.
 */
export function rememberReceipt(fields) {
  if (!fields || !fields.operation_id) return null;
  const entries = readReceipts().filter(
    (entry) => entry.operation_id !== fields.operation_id
  );
  const receipt = {
    operation_id: String(fields.operation_id),
    action: String(fields.action || ""),
    plan: String(fields.plan || ""),
    outcome: String(fields.outcome || ""),
    status: fields.status === null ? null : String(fields.status || ""),
    reason: fields.reason ? String(fields.reason) : null,
    settled: fields.settled === true,
    question_id: fields.question_id ? String(fields.question_id) : null,
  };
  entries.push(receipt);
  writeReceipts(entries);
  return receipt;
}

export function forgetReceipt(operationId) {
  writeReceipts(
    readReceipts().filter((entry) => entry.operation_id !== operationId)
  );
}

export function forgetAllReceipts() {
  writeReceipts([]);
}

/** True when this receipt still leaves a question open. */
export function receiptIsOpen(receipt) {
  if (!receipt) return false;
  if (receipt.settled === true) return false;
  return (
    receipt.outcome === OUTCOME_PENDING || receipt.outcome === OUTCOME_UNREACHABLE
  );
}

/**
 * Whether a retry of this receipt's action would resend its id.
 *
 * An unanswered submission always is: nobody knows whether it arrived, and a
 * fresh id would make a second request out of what may be the first one. A
 * recorded one is only while the service still reports it outstanding. This
 * tab never learns when a recorded control was carried out, so reusing its id
 * after that would turn the next deliberate stop into a replay of the old one.
 */
export function receiptIsRetryable(receipt) {
  if (!receiptIsOpen(receipt)) return false;
  if (receipt.outcome === OUTCOME_UNREACHABLE) return true;
  const outstanding = state.detailOutstanding;
  return !!(
    outstanding &&
    typeof outstanding === "object" &&
    outstanding.operation_id === receipt.operation_id
  );
}

/** The newest receipt a retry of one action on one plan must reuse. */
export function retryableReceipt(name, planId) {
  const plan = String(planId || "");
  const entries = readReceipts().reverse();
  return (
    entries.find(
      (entry) =>
        entry.action === name && entry.plan === plan && receiptIsRetryable(entry)
    ) || null
  );
}

/** The sentence one receipt earns, in the words its outcome allows. */
export function describeReceipt(receipt) {
  if (!receipt) return "";
  const what = ACTIONS[receipt.action]
    ? ACTIONS[receipt.action].subject
    : receipt.action || "the request";
  if (receipt.outcome === OUTCOME_RESULT) {
    return what + " completed";
  }
  if (receipt.outcome === OUTCOME_PENDING) {
    return (
      what +
      " was recorded and has not been confirmed as run" +
      (receiptIsRetryable(receipt) ? "; retrying reuses this id" : "")
    );
  }
  if (receipt.outcome === OUTCOME_REFUSED) {
    return what + " was refused" + (receipt.reason ? " (" + receipt.reason + ")" : "");
  }
  return (
    what +
    " got no answer, so it may or may not have run; retrying reuses this id"
  );
}

/**
 * Draw the outstanding receipts for the plan on screen.
 *
 * This is what makes a reload survivable for every mutation the way the
 * redirect journal already made it survivable for a correction. The ids come
 * from session storage; `outstanding` is what the *server* still holds, which
 * the remote mode reports on every plan read and is the authority when the two
 * disagree.
 */
export function renderOperationReceipts(outstanding) {
  const section = document.getElementById("wb-operations-panel");
  const list = document.getElementById("wb-operations-list");
  if (!section || !list) return;
  const planId = routePlanId();
  const receipts = receiptsForPlan(planId).filter(receiptIsOpen);
  const serverHeld =
    outstanding && typeof outstanding === "object" && outstanding.operation_id
      ? outstanding
      : null;
  if (receipts.length === 0 && !serverHeld) {
    section.hidden = true;
    list.textContent = "";
    return;
  }
  section.hidden = false;
  list.textContent = "";
  if (serverHeld) {
    list.appendChild(serverReceiptRow(serverHeld));
  }
  receipts.forEach((receipt) => {
    if (serverHeld && serverHeld.operation_id === receipt.operation_id) return;
    list.appendChild(receiptRow(receipt));
  });
}

function receiptRow(receipt) {
  const li = document.createElement("li");
  li.className = "wb-operations__row";
  li.setAttribute("data-operation-id", receipt.operation_id);
  li.setAttribute("data-outcome", receipt.outcome || "");
  li.setAttribute("data-source", "client");

  const id = document.createElement("code");
  id.className = "wb-operations__id";
  id.textContent = receipt.operation_id;
  li.appendChild(id);

  const text = document.createElement("span");
  text.className = "wb-operations__text";
  text.textContent = describeReceipt(receipt);
  li.appendChild(text);

  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "wb-button wb-button--ghost wb-operations__dismiss";
  dismiss.setAttribute("data-action", "operation-dismiss");
  dismiss.textContent = "dismiss";
  // Dismissing forgets this tab's note of the id; it never claims the
  // operation settled, because this front end is not in a position to say so.
  dismiss.addEventListener("click", () => {
    forgetReceipt(receipt.operation_id);
    renderOperationReceipts();
  });
  li.appendChild(dismiss);
  return li;
}

function serverReceiptRow(operation) {
  const li = document.createElement("li");
  li.className = "wb-operations__row";
  li.setAttribute("data-operation-id", String(operation.operation_id));
  li.setAttribute("data-outcome", String(operation.status || "outstanding"));
  li.setAttribute("data-source", "server");

  const id = document.createElement("code");
  id.className = "wb-operations__id";
  id.textContent = String(operation.operation_id);
  li.appendChild(id);

  const text = document.createElement("span");
  text.className = "wb-operations__text";
  text.textContent =
    "the service still holds this " +
    String(operation.method || "mutation") +
    " as " +
    String(operation.status || "outstanding") +
    "; it has not been confirmed as run";
  li.appendChild(text);
  return li;
}
