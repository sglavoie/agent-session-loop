/* Submission identity, retry protection and the shared mutation path. */

import {
  OUTCOME_PENDING,
  OUTCOME_REFUSED,
  OUTCOME_RESULT,
  OUTCOME_UNREACHABLE,
  describeAnswer,
  describeError,
  mintOperationId,
  submit,
} from "./api.js";
import { OPERATION_RECEIPT_LIMIT } from "./constants.js";
import { routePlanId } from "./identity.js";
import { state } from "./state.js";
import {
  REASON_OPERATION_UNAVAILABLE,
  IN_FLIGHT_MESSAGE,
  ACTIONS,
  loadCapabilities,
  actionAvailability,
  applyActionAvailability,
  beginOperation,
  endOperation,
} from "./operations.js";
import {
  rememberReceipt,
  retryableReceipt,
  renderOperationReceipts,
} from "./operation_receipts.js";

/* What each id this page submitted asked for, so a click that changed the
 * request -- another harness, another archive selection -- is not mistaken for
 * a retry of the last one. Memory only, like every other draft: after a reload
 * nothing is known about the body, and the receipt's id is reused as promised. */
const submittedBodies = new Map();

function bodyFingerprint(body) {
  const copy = Object.assign({}, body);
  delete copy.operation_id;
  delete copy.launch_id;
  try {
    return JSON.stringify(copy);
  } catch (err) {
    return null;
  }
}

function rememberSubmittedBody(operationId, fingerprint) {
  submittedBodies.delete(operationId);
  submittedBodies.set(operationId, fingerprint);
  while (submittedBodies.size > OPERATION_RECEIPT_LIMIT) {
    submittedBodies.delete(submittedBodies.keys().next().value);
  }
}

// ---------------------------------------------------------------------------
// The one mutation path
// ---------------------------------------------------------------------------

/* The routes whose local handler reads its idempotency key as `launch_id`, the
 * name `/api/launch` and `/api/reply` documented before every mutation carried
 * an `operation_id`. Both names carry the same id, so a resend is recognised
 * as one by whichever provider answers. */
const LAUNCH_ID_PATHS = ["/api/launch", "/api/reply"];

/**
 * The body a submission sends, with the identity a retry has to share.
 *
 * Callers build a fresh body on every click, so without this every retry the
 * feedback promised would reuse an id was a new id and, to the far side, a
 * second request. An id the caller chose itself (the reply composer keeps its
 * own) wins; otherwise the outstanding receipt for this action on this plan
 * lends its id to the same request, and only when there is none is a new one
 * minted.
 */
function bodyWithIdentity(spec, planId) {
  const body = spec.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) return body;
  const fingerprint = bodyFingerprint(body);
  let operationId = body.operation_id || (spec.options && spec.options.operationId);
  if (!operationId) {
    const receipt = retryableReceipt(spec.action, planId);
    const known = receipt ? submittedBodies.get(receipt.operation_id) : undefined;
    const sameRequest = known === undefined || known === fingerprint;
    operationId = receipt && sameRequest ? receipt.operation_id : mintOperationId();
  }
  rememberSubmittedBody(operationId, fingerprint);
  const next = Object.assign({}, body, { operation_id: operationId });
  if (LAUNCH_ID_PATHS.indexOf(spec.path) !== -1 && !next.launch_id) {
    next.launch_id = operationId;
  }
  return next;
}

/** Which feedback tone an answer earns. Pending is its own, deliberately. */
export function answerKind(answer) {
  if (!answer) return "error";
  if (answer.outcome === OUTCOME_RESULT) return "info";
  if (answer.outcome === OUTCOME_PENDING) return "pending";
  if (answer.outcome === OUTCOME_REFUSED) return "error";
  return "warning";
}

/**
 * Submit one mutation: guarded, recorded, and described in the words it earns.
 *
 * `spec` carries the action name (which decides availability and the guard),
 * the path and body, and a `feedback(kind, message)` the caller uses to draw
 * the answer wherever that panel draws things. Everything else -- the
 * double-submit guard, the receipt, the focus the browser's own confirm dialog
 * took away, and re-asking what this mode can do after an unavailable refusal
 * -- happens here so that no caller has to remember it. A caller that took
 * `holdOperation` for a batch sets `withinHold` so its own submissions pass.
 */
export async function runMutation(spec) {
  const name = spec.action;
  const action = ACTIONS[name] || {};
  const availability = actionAvailability(name, spec.withinHold === true);
  if (!availability.enabled) {
    spec.feedback("error", availability.reason || "this action is unavailable");
    return null;
  }
  if (!beginOperation(name)) {
    spec.feedback("error", IN_FLIGHT_MESSAGE);
    return null;
  }
  const restoreFocus = captureFocus();
  applyActionAvailability();
  const planId = spec.planId || routePlanId() || "";
  let answer = null;
  try {
    answer = await submit(spec.path, bodyWithIdentity(spec, planId), spec.options);
  } catch (err) {
    // `submit` classifies rather than throws, so reaching here means something
    // below it broke. Reported as no-answer, which is what it is.
    answer = {
      outcome: OUTCOME_UNREACHABLE,
      ok: false,
      status: 0,
      error: describeError(err),
      reason: "unreachable",
      operation: null,
      replay: false,
    };
  } finally {
    endOperation(name);
    applyActionAvailability();
    restoreFocus();
  }
  const identity = answer.operation && answer.operation.id;
  if (identity) {
    rememberReceipt({
      operation_id: identity,
      action: name,
      plan: planId,
      outcome: answer.outcome,
      status: answer.operation.status,
      reason: answer.reason,
      // A result is over. Everything else leaves a question that a reload has
      // to be able to see, which is the whole reason this is written down.
      settled: answer.outcome === OUTCOME_RESULT,
    });
  }
  const subject = spec.subject || action.subject || name;
  // A caller's own wording only replaces the default when it has something to
  // say. Returning nothing means "the standard sentence is the right one",
  // which is how a `describe` written for the completed case leaves the
  // pending and unreachable cases to the wording that will not overclaim.
  const custom = spec.describe ? spec.describe(answer) : null;
  const kind = (spec.kindFor && spec.kindFor(answer)) || answerKind(answer);
  spec.feedback(kind, custom || describeAnswer(answer, subject));
  renderOperationReceipts(state.detailOutstanding);
  if (
    answer.outcome === OUTCOME_REFUSED &&
    answer.reason === REASON_OPERATION_UNAVAILABLE
  ) {
    // The mode's answer changed under us. Re-reading it is what stops the next
    // click from asking the same impossible thing.
    await loadCapabilities();
    applyActionAvailability();
  }
  return answer;
}

/**
 * Remember what had the keyboard, and put it back.
 *
 * `window.confirm` and `window.prompt` take focus and give it to the document
 * body, so without this every confirmed control loses the keyboard exactly
 * when an operator who is using the keyboard needs it most.
 */
export function captureFocus() {
  const active = document.activeElement;
  const id = active && active.id ? active.id : null;
  return function restore() {
    const target = id ? document.getElementById(id) : null;
    const node = target || active;
    if (!node || typeof node.focus !== "function") return;
    if (!document.body || !document.body.contains(node)) return;
    if (node.disabled === true) return;
    node.focus();
  };
}
