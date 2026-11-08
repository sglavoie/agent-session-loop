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

import { apiRequest } from "./api.js";
import { PLAN_LIVE_RUN_STATES, PLAN_REPLY_STATES } from "./constants.js";
import { state } from "./state.js";

/** The refusal reason the application raises for an operation a mode lacks. */
export const REASON_OPERATION_UNAVAILABLE = "operation-unavailable";

/** What a second click is told while the first is still unanswered. */
export const IN_FLIGHT_MESSAGE =
  "a submission is already in flight; wait for its answer";

/** What a second click is told while an earlier batch is still settling. */
export const HELD_MESSAGE =
  "an earlier submission is still settling; wait for it to finish";

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
 *
 * `paused`, when set, is the reason the action is shut while a paused run
 * waits for an answer: no run is in flight then, but the reply is the way on.
 */
export const ACTIONS = {
  launch: {
    operation: "launch",
    subject: "launch",
    requires: "idle",
    stale: "a run is already in flight for this plan; stop it before launching another",
    // Launching past the question would skip it; the reply is what resumes.
    paused: "the run is paused waiting for a reply; answering it resumes the run",
  },
  reply: {
    operation: "reply",
    subject: "the reply",
    requires: "reply",
    stale: "no run is waiting for an operator reply",
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

/** True when a paused run or a live in-turn question awaits an answer. */
export function planAwaitsReply(plan) {
  const context = state.detailReplyContext;
  if (context && context.context_kind === "waiting" && context.question_id) {
    return true;
  }
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
export function actionAvailability(name, withinHold) {
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
      if (action.paused && planAwaitsReply(plan)) {
        return { enabled: false, reason: action.paused };
      }
    }
  }
  if (operationInFlight(name)) {
    return { enabled: false, reason: IN_FLIGHT_MESSAGE };
  }
  if (!withinHold && state.operationsHeld.has(name)) {
    return { enabled: false, reason: HELD_MESSAGE };
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
  const remote = state.capabilities?.mode === "remote";
  const profile = document.getElementById("wb-profile-panel");
  if (profile) profile.hidden = remote;
  const launch = document.getElementById("wb-control-launch");
  if (launch) {
    launch.setAttribute("data-enabled-title", remote
      ? "Launch with the Mac default profile. The Mac checks readiness before starting."
      : "Launch this plan with the profile below.");
  }
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
    const disabled = !availability.enabled;
    if (node.disabled !== disabled) node.disabled = disabled;
    if (availability.reason) {
      if (node.getAttribute("data-disabled-reason") !== availability.reason) {
        node.setAttribute("data-disabled-reason", availability.reason);
      }
      if (node.title !== availability.reason) node.title = availability.reason;
    } else {
      if (node.hasAttribute("data-disabled-reason")) {
        node.removeAttribute("data-disabled-reason");
      }
      const enabledTitle = node.getAttribute("data-enabled-title") || "";
      if (node.title !== enabledTitle) node.title = enabledTitle;
    }
    // Recovery cards need an explanation readable on a phone as well as a
    // tooltip. Keep it in the same pass so a later capability read or an
    // in-flight submission cannot leave an old reason on the card.
    const reason = node.parentElement?.querySelector("[data-operation-reason]");
    if (reason) {
      const text = availability.reason || "";
      if (reason.textContent !== text) reason.textContent = text;
      reason.hidden = !text;
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

/**
 * Keep an action shut across a whole batch, not just one request.
 *
 * A restore or a remote archive is answered "pending" and then polled for up
 * to a minute; the per-request guard has long been released by then, and a
 * second click would start a second loop with fresh operation ids. The holder
 * passes `withinHold` to `runMutation` for its own submissions and must
 * release the hold in a `finally`. Returns false when already held or sent.
 */
export function holdOperation(name) {
  if (state.operationsHeld.has(name) || state.operationsInFlight.has(name)) {
    return false;
  }
  state.operationsHeld.add(name);
  applyActionAvailability();
  return true;
}

export function releaseOperation(name) {
  state.operationsHeld.delete(name);
  applyActionAvailability();
}

export {
  operationReceipts,
  receiptsForPlan,
  rememberReceipt,
  forgetReceipt,
  forgetAllReceipts,
  receiptIsOpen,
  receiptIsRetryable,
  retryableReceipt,
  describeReceipt,
  renderOperationReceipts,
} from "./operation_receipts.js";
export {
  answerKind,
  runMutation,
  captureFocus,
} from "./operation_mutations.js";
