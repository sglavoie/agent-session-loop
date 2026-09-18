/* Every mutation this front end makes, and what it is allowed to make.
 *
 * Session 17's job was to put the whole capability matrix behind one shared
 * interface without letting either deployment lie about what happened. Three
 * facts make that hard, and each has a section below.
 *
 * **What a mode can do is the server's answer.** `GET /api/capabilities`
 * reports, per operation, whether this mode carries it out and -- in the same
 * words the refusal would have used -- why not. A control greyed out by a
 * guess would be a control greyed out for the wrong reason in whichever mode
 * the guess was not written for, so this module never guesses: the sentence
 * under a disabled button is the provider's own.
 *
 * **What a plan can do right now is a second question.** A launch against a
 * running plan and a reply to a plan with no question are both *available*
 * operations aimed at the wrong moment. That is staleness rather than
 * unavailability, and it is derived here from the plan summary both modes
 * publish, so the explanation names the run rather than the mode.
 *
 * **A mutation is not over when the answer arrives.** `202` means recorded and
 * not run; an unreachable answer means nobody knows. Both leave an operation
 * id that a retry must reuse, and both survive a reload -- so the id, and only
 * the id, is written to session storage. No prose goes with it: the reply text
 * and the correction draft stay in memory, and a receipt that carried them
 * would undo that promise on the first refresh.
 */

import {
  OUTCOME_PENDING,
  OUTCOME_REFUSED,
  OUTCOME_RESULT,
  OUTCOME_UNREACHABLE,
  apiRequest,
  describeAnswer,
  describeError,
  submit,
} from "./api.js";
import {
  OPERATION_RECEIPTS_STORAGE_KEY,
  OPERATION_RECEIPT_LIMIT,
  PLAN_LIVE_RUN_STATES,
  PLAN_REPLY_STATES,
} from "./constants.js";
import { routePlanId } from "./identity.js";
import { state } from "./state.js";

/** The refusal reason the application raises for an operation a mode lacks. */
export const REASON_OPERATION_UNAVAILABLE = "operation-unavailable";

/** What a second click is told while the first is still unanswered. */
export const IN_FLIGHT_MESSAGE =
  "a submission is already in flight; wait for its answer";

/* Every mutating action the shared interface offers, by the name the DOM uses
 * for it.
 *
 * `operation` is the word `/api/capabilities` answers about, so a mode that
 * cannot carry an action out disables it with that mode's own reason.
 * `requires` is the plan-level precondition, checked against the summary both
 * providers publish:
 *
 *   `live`   -- a run has to be in flight for this to reach anything
 *   `reply`  -- a run has to be *waiting for an operator*, which is narrower
 *   `idle`   -- a run must NOT be in flight, because this would collide with it
 *   `null`   -- nothing about the run makes this stale; unlock exists for
 *               exactly the case where the run state is not to be trusted
 */
export const ACTIONS = {
  launch: {
    operation: "launch",
    subject: "launch",
    requires: "idle",
    stale: "a run is already in flight for this plan; stop it before launching another",
  },
  reply: {
    operation: "reply",
    subject: "the reply",
    requires: "reply",
    stale: "no run is paused waiting for an operator reply",
  },
  stop: {
    operation: "control-stop-after-current",
    subject: "stop-after-current",
    requires: "live",
    stale: "no run is in flight to stop",
  },
  abort: {
    operation: "control-abort",
    subject: "the abort",
    requires: "live",
    stale: "no run is in flight to abort",
  },
  harness: {
    operation: "control-select-harness",
    subject: "the harness selection",
    requires: "live",
    stale: "no run is in flight to queue a harness change against",
  },
  redirect: {
    operation: "control-redirect",
    subject: "the correction",
    requires: "live",
    stale: "no run is in flight to interrupt",
  },
  unlock: {
    operation: "unlock",
    subject: "the unlock",
    requires: null,
    stale: "",
  },
  pin: {
    operation: "registry-pin",
    subject: "the pin",
    requires: null,
    stale: "",
  },
  unpin: {
    operation: "registry-unpin",
    subject: "the unpin",
    requires: null,
    stale: "",
  },
  "archive-plan": {
    operation: "plan-archive",
    subject: "the archive",
    requires: "idle",
    stale: "a run is in flight; stop it before archiving the plan",
  },
  "archive-plans": {
    operation: "plans-archive",
    subject: "the bulk archive",
    requires: null,
    stale: "",
  },
  "archive-restore": {
    operation: "archive-restore",
    subject: "the restore",
    requires: null,
    stale: "",
  },
  "archive-purge": {
    operation: "archive-delete",
    subject: "the purge",
    requires: null,
    stale: "",
  },
  "archive-settings": {
    operation: "archive-settings",
    subject: "the retention change",
    requires: null,
    stale: "",
  },
};

// ---------------------------------------------------------------------------
// What this mode can carry out
// ---------------------------------------------------------------------------

/**
 * Ask the deployment what it can do, and remember the answer.
 *
 * Read once at startup and again whenever a refusal says an operation is
 * unavailable, because the remote answer is not constant: a Pi with no paired
 * connector cannot carry a mutation, and the sentence saying so stops being
 * true the moment the Mac reconnects.
 */
export async function loadCapabilities() {
  try {
    const body = await apiRequest("/api/capabilities");
    if (!body || body.ok !== true) {
      state.capabilities = null;
      return null;
    }
    const byOperation = {};
    const entries = Array.isArray(body.operations) ? body.operations : [];
    entries.forEach((entry) => {
      if (!entry || typeof entry.operation !== "string") return;
      byOperation[entry.operation] = {
        available: entry.available !== false,
        reason: typeof entry.reason === "string" ? entry.reason : null,
        role: typeof entry.role === "string" ? entry.role : null,
      };
    });
    state.capabilities = {
      mode: typeof body.mode === "string" ? body.mode : null,
      byOperation: byOperation,
      unavailable: Array.isArray(body.unavailable_operations)
        ? body.unavailable_operations.slice()
        : [],
    };
    return state.capabilities;
  } catch (err) {
    // A capabilities read that did not answer is not evidence that anything is
    // unavailable. Clearing it returns every control to "unknown", which is
    // enabled: a failed poll must not be a way to lock the operator out.
    state.capabilities = null;
    return null;
  }
}

/** Load capabilities once; a later caller gets whatever the first one found. */
export function ensureCapabilities() {
  if (state.capabilities !== null) return Promise.resolve(state.capabilities);
  if (state.capabilitiesInFlight) return state.capabilitiesInFlight;
  const pending = loadCapabilities().finally(() => {
    state.capabilitiesInFlight = null;
  });
  state.capabilitiesInFlight = pending;
  return pending;
}

/**
 * What this mode says about one operation.
 *
 * An operation nobody has asked about yet is reported available with no
 * reason. Before the first answer arrives every control behaves as it always
 * did, which is the only safe direction: a UI that disabled everything until a
 * read completed would be a UI that disabled everything when the read failed.
 */
export function capabilityOf(operation) {
  const known = state.capabilities;
  if (!known || !operation) return { available: true, reason: null };
  const entry = known.byOperation[operation];
  if (!entry) return { available: true, reason: null };
  return { available: entry.available, reason: entry.reason };
}

// ---------------------------------------------------------------------------
// What the plan on screen can do right now
// ---------------------------------------------------------------------------

/** True when a run is in flight for this plan, in either mode's vocabulary. */
export function planHasLiveRun(plan) {
  if (!plan) return false;
  return PLAN_LIVE_RUN_STATES.indexOf(String(plan.primary_state || "")) !== -1;
}

/** True when a run is paused waiting for an operator to answer it. */
export function planAwaitsReply(plan) {
  if (!plan) return false;
  return PLAN_REPLY_STATES.indexOf(String(plan.primary_state || "")) !== -1;
}

/**
 * Whether one action can be taken now, and the sentence for why not.
 *
 * The three reasons are checked in the order an operator would rank them: a
 * mode that cannot do this at all, then a run this would not reach, then a
 * submission already on its way. Each returns the *specific* explanation --
 * "disabled" with no sentence is the state this replaces.
 */
export function actionAvailability(name) {
  const action = ACTIONS[name];
  if (!action) return { enabled: true, reason: null };
  const capability = capabilityOf(action.operation);
  if (!capability.available) {
    return {
      enabled: false,
      reason:
        capability.reason ||
        "this deployment does not carry out " + action.operation,
    };
  }
  const requires = action.requires;
  if (requires) {
    const plan = state.detailPlan;
    // Only judged once a plan is actually loaded. A control on an empty detail
    // view has nothing to be stale against, and saying otherwise would put a
    // false sentence under it for the second before the first read lands.
    if (plan) {
      const live = planHasLiveRun(plan);
      const stale =
        (requires === "live" && !live) ||
        (requires === "idle" && live) ||
        (requires === "reply" && !planAwaitsReply(plan));
      if (stale) return { enabled: false, reason: action.stale };
    }
  }
  if (operationInFlight(name)) {
    return { enabled: false, reason: IN_FLIGHT_MESSAGE };
  }
  return { enabled: true, reason: null };
}

/**
 * Disable every control whose action cannot be taken, and say why on it.
 *
 * The reason goes on `data-disabled-reason` as well as `title`, because a
 * tooltip is not readable on a phone and the responsive shell is one of the
 * two places this interface runs.
 */
export function applyActionAvailability(root) {
  const scope = root || document;
  const nodes = scope.querySelectorAll("[data-operation-action]");
  Array.prototype.forEach.call(nodes, (node) => {
    const name = node.getAttribute("data-operation-action");
    const availability = actionAvailability(name);
    // A view can hold a control shut for a reason of its own -- an empty
    // selection, a form with nothing in it, a plan that is not pinned -- and
    // it says so on the node rather than by setting `disabled`, which this
    // pass would overwrite on the next capability read.
    //
    // It is the *second* reason, not the first. "Select some plans" and "this
    // deployment does not carry bulk archives" are both true on a Pi with an
    // empty selection, and only one of them is something the operator can act
    // on: offering the fixable one for a situation that cannot be fixed sends
    // them hunting for a checkbox that was never going to help.
    const blocked = availability.enabled
      ? node.getAttribute("data-blocked-reason")
      : null;
    if (blocked) {
      availability.enabled = false;
      availability.reason = blocked;
    }
    node.disabled = !availability.enabled;
    if (availability.reason) {
      node.setAttribute("data-disabled-reason", availability.reason);
      node.title = availability.reason;
    } else {
      node.removeAttribute("data-disabled-reason");
      node.title = node.getAttribute("data-enabled-title") || "";
    }
  });
}

// ---------------------------------------------------------------------------
// One submission at a time
// ---------------------------------------------------------------------------

export function operationInFlight(name) {
  return state.operationsInFlight.has(name);
}

/** Take the guard for one action, or report that it is already taken. */
export function beginOperation(name) {
  if (state.operationsInFlight.has(name)) return false;
  state.operationsInFlight.add(name);
  return true;
}

export function endOperation(name) {
  state.operationsInFlight.delete(name);
}

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
      " was recorded and has not been confirmed as run; retrying reuses this id"
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

// ---------------------------------------------------------------------------
// The one mutation path
// ---------------------------------------------------------------------------

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
 * -- happens here so that no caller has to remember it.
 */
export async function runMutation(spec) {
  const name = spec.action;
  const action = ACTIONS[name] || {};
  const availability = actionAvailability(name);
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
  let answer = null;
  try {
    answer = await submit(spec.path, spec.body, spec.options);
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
      plan: spec.planId || routePlanId(),
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
