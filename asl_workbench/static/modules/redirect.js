/* Urgent correction (redirect) composer.
 *
 * A correction carries a client-chosen operation id, minted at the first
 * submit and kept through every uncertain, refused, or failed answer, so a
 * retry is the same correction rather than a second one. The draft survives
 * every refusal and lives in memory only.
 */

//
// Three rules shape everything below.
//
// The draft is memory only. It is never written to sessionStorage, never
// echoed into the hash, and never restored after a reload — a correction is
// the most sensitive text this UI handles, and the panel promises in prose
// that it lives in this tab alone.
//
// The operation id outlives the answer. It is minted once per draft and kept
// through every uncertain, refused, or failed response, because the journal
// treats a resubmission carrying the same id as the same request. Minting a
// fresh id on retry is how one correction becomes two.
//
// Only a durable answer clears the textarea. "Durable" means the server said
// ok — a 202 that persisted, or a 200 replay that recognized an id already in
// the journal carrying *this* correction. Everything else keeps the text
// exactly where the operator left it.
//
// A retry is only a retry while the draft is unchanged. Editing the textarea
// after a lost response makes the next submit a different correction under an
// id the journal already holds, which the server refuses. That refusal is the
// one case where the two rules above pull apart — the id has to move for the
// draft to be sendable — so it is the operator who moves it, through the
// conflict panel below, and never this code on their behalf.

import {
  OUTCOME_PENDING,
  OUTCOME_UNREACHABLE,
  apiRequest,
  describeError,
  mintOperationId,
} from "./api.js";
import {
  REDIRECT_ACTION,
  REDIRECT_CONFIRM,
  REDIRECT_CONFLICT_REASON,
  REDIRECT_MAX_FEEDBACK_BYTES,
  REDIRECT_MAX_OPERATION_ID_CHARS,
  REDIRECT_PHASES,
  REDIRECT_PHASE_LABELS,
  REDIRECT_RECONCILE_HINT,
  REDIRECT_STAGE_PHASES,
  REDIRECT_UNRESOLVED_STAGES,
} from "./constants.js";
import { showControlsFeedback } from "./controls.js";
import { planBody, planUrl, routePlanId } from "./identity.js";
import { rememberReceipt, renderOperationReceipts } from "./operations.js";
import { clearFormErrors, showFormError } from "./profile.js";
import { refresh } from "./queue.js";
import { emptyListItem } from "./recovery.js";
import { state } from "./state.js";

export function wireRedirectForm() {
  const form = document.getElementById("wb-redirect-form");
  if (!form) return;
  form.addEventListener("submit", handleRedirectSubmit);
  const clearBtn = document.getElementById("wb-redirect-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      // Discarding the body discards the request: an id with no text behind
      // it would silently attach the next correction to this one's identity.
      resetRedirectComposer();
    });
  }
  const newIdBtn = document.getElementById("wb-redirect-new-identity");
  if (newIdBtn) {
    newIdBtn.addEventListener("click", adoptNewRedirectIdentity);
  }
}

export function resetRedirectComposer() {
  const textarea = document.getElementById("wb-redirect-text");
  if (textarea) textarea.value = "";
  state.redirect.operationId = null;
  state.redirect.inFlight = false;
  clearFormErrors("wb-redirect-form-errors");
  hideRedirectConflict();
  renderRedirectOperationId();
}

export function isRedirectConflict(body) {
  return !!(body && body.ok === false && body.reason === REDIRECT_CONFLICT_REASON);
}

export function hideRedirectConflict() {
  const panel = document.getElementById("wb-redirect-conflict");
  if (panel) panel.hidden = true;
}

// The refused draft is still on screen and still the operator's. All this
// does is say what the journal holds and offer the only two ways out:
// reconcile the recorded id, or deliberately send the draft as its own
// correction.
export function showRedirectConflict(body, operationId) {
  const panel = document.getElementById("wb-redirect-conflict");
  const message = document.getElementById("wb-redirect-conflict-message");
  if (!panel || !message) return;
  const details = (body && body.details) || {};
  const recorded = details.redirect && details.redirect.operation;
  const changed =
    details.conflict === "target"
      ? "names a different turn than"
      : "carries different words from";
  message.textContent =
    "This draft " +
    changed +
    " correction " +
    operationId +
    ", which is already in the journal" +
    (recorded && recorded.feedback_digest
      ? " (recorded digest " + recorded.feedback_digest + ")"
      : "") +
    ". Nothing was resubmitted and the recorded correction is unchanged. " +
    REDIRECT_RECONCILE_HINT +
    ". Your draft is kept below.";
  panel.hidden = false;
}

// The "second correction" the reconcile hint warns about, made explicit. The
// draft is untouched and nothing is sent: this only mints the new identity a
// different correction needs, and the operator still has to press submit —
// which is what keeps one refused replay from becoming two corrections.
export function adoptNewRedirectIdentity() {
  const textarea = document.getElementById("wb-redirect-text");
  if (!textarea || !textarea.value.trim()) {
    showFormError(
      "wb-redirect-form-errors",
      "there is no draft to send as a new correction"
    );
    return;
  }
  state.redirect.operationId = mintRedirectOperationId();
  renderRedirectOperationId();
  hideRedirectConflict();
  clearFormErrors("wb-redirect-form-errors");
  showControlsFeedback(
    "info",
    "draft kept under new operation id " +
      state.redirect.operationId +
      "; it is a second correction and is sent only when you submit again"
  );
}

// Leaving the plan drops the attribution too: a correction id means nothing
// against a different journal, and carrying one across would mark an
// unrelated operation as this tab's.
export function resetRedirectForPlan() {
  state.redirect.submittedIds = [];
  state.redirect.status = null;
  state.redirect.actionToken = null;
  state.redirect.permitted = false;
  resetRedirectComposer();
}

export function rememberSubmittedRedirect(operationId) {
  if (state.redirect.submittedIds.indexOf(operationId) === -1) {
    state.redirect.submittedIds.push(operationId);
  }
}

export function isOwnRedirect(operationId) {
  if (!operationId) return false;
  return (
    operationId === state.redirect.operationId ||
    state.redirect.submittedIds.indexOf(operationId) !== -1
  );
}

export function mintRedirectOperationId() {
  // The identity itself is minted by the shared API layer, which attaches one
  // to every mutation. A redirect needs its own only because it shows the id
  // to the operator and keeps it across a retry, so it asks for the id early
  // rather than differently -- and the redirect journal's own, narrower bound
  // is applied here, where it is the rule that applies.
  return mintOperationId().slice(0, REDIRECT_MAX_OPERATION_ID_CHARS);
}

export function currentRedirectOperationId() {
  if (!state.redirect.operationId) {
    state.redirect.operationId = mintRedirectOperationId();
    renderRedirectOperationId();
  }
  return state.redirect.operationId;
}

export async function loadAndRenderRedirect() {
  try {
    const body = await apiRequest(
      planUrl("/api/redirect-status")
    );
    if (body && body.ok === true) {
      state.redirect.status = body;
      state.redirect.actionToken = body.action_token || null;
      // Availability is the server's answer, never this composer's guess. A
      // freshness proof derived locally would be a proof of nothing.
      const permitted = Array.isArray(body.permitted_actions)
        ? body.permitted_actions
        : [];
      state.redirect.permitted = permitted.indexOf(REDIRECT_ACTION) !== -1;
      const reasons = body.disabled_reasons || {};
      state.redirect.disabledReason = state.redirect.permitted
        ? null
        : reasons[REDIRECT_ACTION] || "redirect is unavailable right now";
      state.redirect.target = body.live_target || null;
      renderRedirect(body);
    } else {
      renderRedirectUnavailable(
        (body && body.error) || "redirect status unavailable"
      );
    }
  } catch (err) {
    renderRedirectUnavailable(describeError(err));
  }
}

export function renderRedirectUnavailable(message) {
  state.redirect.status = null;
  state.redirect.actionToken = null;
  state.redirect.permitted = false;
  state.redirect.disabledReason = message;
  setRedirectAvailability("unreadable", "unreadable", message);
  setRedirectSubmitDisabled(true);
  const target = document.getElementById("wb-redirect-target");
  if (target) target.textContent = "Cannot submit: " + message;
  // The progress groups keep whatever they last showed rather than emptying:
  // a failed poll is not evidence that a correction went away.
  renderRedirectOperationId();
}

export function renderRedirect(payload) {
  if (state.redirect.permitted) {
    setRedirectAvailability(
      "available",
      "ready",
      "A turn is in flight and can be interrupted."
    );
  } else {
    setRedirectAvailability(
      "unavailable",
      "unavailable",
      state.redirect.disabledReason || ""
    );
  }
  setRedirectSubmitDisabled(!state.redirect.permitted || state.redirect.inFlight);
  renderRedirectTarget(payload.live_target, state.redirect.disabledReason);
  renderRedirectOperationId();
  const operations = Array.isArray(payload.operations) ? payload.operations : [];
  renderRedirectPhases(operations);
  renderRedirectOperations(operations);
  renderRedirectUnresolved(operations);
  renderRedirectTruncation(payload);
}

export function setRedirectAvailability(stateName, label, title) {
  const panel = document.getElementById("wb-redirect-panel");
  if (panel) panel.setAttribute("data-available", stateName);
  const badge = document.getElementById("wb-redirect-availability");
  if (!badge) return;
  badge.setAttribute("data-state", stateName);
  badge.textContent = label;
  badge.title = title || "";
}

export function setRedirectSubmitDisabled(disabled) {
  const button = document.getElementById("wb-redirect-submit");
  if (button) button.disabled = disabled === true;
}

export function renderRedirectTarget(target, disabledReason) {
  const el = document.getElementById("wb-redirect-target");
  if (!el) return;
  // Why it is unavailable, in the line that otherwise says what a submission
  // would do. Leaving that explanation in the badge's tooltip would hide the
  // one thing an operator staring at a disabled button needs to read.
  if (disabledReason) {
    el.textContent = "Cannot submit: " + disabledReason;
    return;
  }
  if (!target) {
    el.textContent = "No live turn to interrupt.";
    return;
  }
  const parts = [];
  if (target.session_id) parts.push("session " + target.session_id);
  if (target.attempt !== undefined && target.attempt !== null) {
    parts.push("attempt " + target.attempt);
  }
  if (target.turn_id) parts.push("turn " + target.turn_id);
  el.textContent =
    "Would interrupt: " + (parts.length ? parts.join(" · ") : "the live turn");
}

export function renderRedirectOperationId() {
  const el = document.getElementById("wb-redirect-operation");
  if (!el) return;
  const id = state.redirect.operationId;
  if (!id) {
    el.textContent =
      "An operation id is minted when you submit; a retry of the same draft " +
      "reuses it, so a lost answer cannot become a second correction.";
    return;
  }
  el.textContent = "Operation id " + id + " — a retry reuses this id.";
}

export function redirectPhaseFor(stage) {
  return REDIRECT_STAGE_PHASES[stage] || "unresolved";
}

export function renderRedirectPhases(operations) {
  const list = document.getElementById("wb-redirect-phases");
  const section = document.getElementById("wb-redirect-progress-section");
  if (!list) return;
  if (section) section.hidden = operations.length === 0;
  list.textContent = "";
  // Acknowledgment is its own phase, deliberately not folded into resuming.
  // "The turn was stopped" and "a worker receipted the correction" are
  // different facts, and only the second one says the correction landed.
  const active = {};
  operations.forEach((operation) => {
    active[redirectPhaseFor(operation.stage)] = true;
  });
  REDIRECT_PHASES.forEach((phase) => {
    const li = document.createElement("li");
    li.className = "wb-redirect__phase";
    li.setAttribute("data-phase", phase);
    li.setAttribute("data-active", active[phase] ? "true" : "false");
    li.textContent = REDIRECT_PHASE_LABELS[phase];
    list.appendChild(li);
  });
}

export function redirectStageLine(operation) {
  const stage = operation.stage || "unknown";
  const description = operation.stage_description || "";
  return description ? stage + ": " + description : stage;
}

export function renderRedirectOperations(operations) {
  const list = document.getElementById("wb-redirect-operations");
  if (!list) return;
  list.textContent = "";
  if (operations.length === 0) {
    list.appendChild(emptyListItem("no corrections recorded for this plan"));
    return;
  }
  operations
    .slice()
    .reverse()
    .forEach((operation) => {
      list.appendChild(redirectOperationRow(operation));
    });
}

export function redirectOperationRow(operation) {
  const li = document.createElement("li");
  li.className = "wb-redirect__operation-row";
  const stage = operation.stage || "unknown";
  li.setAttribute("data-stage", stage);
  li.setAttribute("data-phase", redirectPhaseFor(stage));
  if (isOwnRedirect(operation.operation_id)) {
    li.setAttribute("data-mine", "true");
  }

  const id = document.createElement("span");
  id.className = "wb-redirect__operation-id";
  id.textContent = operation.operation_id || "(unknown id)";
  li.appendChild(id);

  const detail = document.createElement("span");
  detail.className = "wb-redirect__operation-detail";
  const phase = document.createElement("span");
  phase.className = "wb-redirect__operation-phase";
  phase.textContent = REDIRECT_PHASE_LABELS[redirectPhaseFor(stage)];
  detail.appendChild(phase);
  const line = document.createElement("span");
  line.className = "wb-redirect__operation-stage";
  line.textContent = redirectStageLine(operation);
  detail.appendChild(line);
  // The digest, never the text: the body is not stored by this UI, and
  // showing it back would undo that.
  const meta = redirectOperationMeta(operation);
  if (meta) {
    const metaEl = document.createElement("span");
    metaEl.className = "wb-redirect__operation-meta";
    metaEl.textContent = meta;
    detail.appendChild(metaEl);
  }
  li.appendChild(detail);
  return li;
}

export function redirectOperationMeta(operation) {
  const parts = [];
  if (operation.feedback_digest) {
    parts.push("digest " + String(operation.feedback_digest).slice(0, 12));
  }
  if (operation.feedback_bytes !== undefined && operation.feedback_bytes !== null) {
    parts.push(operation.feedback_bytes + " bytes");
  }
  if (operation.continuation_recovered === true) {
    parts.push("continuation recovered");
  }
  if (operation.reason) parts.push(String(operation.reason));
  return parts.join(" · ");
}

export function renderRedirectUnresolved(operations) {
  const section = document.getElementById("wb-redirect-unresolved-section");
  const list = document.getElementById("wb-redirect-unresolved");
  if (!section || !list) return;
  // This is what makes a reload survivable. The composer keeps nothing, so
  // after a refresh the journal is the only record of what was submitted;
  // anything still unanswered is surfaced by id, with the reconciliation the
  // CLI would give, instead of being resubmitted blind.
  const unresolved = operations.filter(
    (operation) => REDIRECT_UNRESOLVED_STAGES.indexOf(operation.stage) !== -1
  );
  if (unresolved.length === 0) {
    section.hidden = true;
    list.textContent = "";
    return;
  }
  section.hidden = false;
  const hint = document.getElementById("wb-redirect-reconcile-hint");
  if (hint) hint.textContent = REDIRECT_RECONCILE_HINT;
  list.textContent = "";
  unresolved.forEach((operation) => {
    list.appendChild(redirectOperationRow(operation));
  });
}

export function renderRedirectTruncation(payload) {
  const el = document.getElementById("wb-redirect-truncated");
  if (!el) return;
  const notes = [];
  if (payload.truncated === true) {
    notes.push("older corrections are not shown");
  }
  if (payload.journal_complete === false) {
    notes.push(
      "the journal is incomplete" +
        (payload.torn_tail === true ? " (torn tail)" : "") +
        "; a correction may be missing from this list"
    );
  }
  el.hidden = notes.length === 0;
  el.textContent = notes.join("; ");
}

export async function handleRedirectSubmit(event) {
  event.preventDefault();
  clearFormErrors("wb-redirect-form-errors");
  hideRedirectConflict();
  if (state.redirect.inFlight) {
    // The double-click guard. The button is disabled below too, but a second
    // submit can still arrive by keyboard before the first response lands.
    showFormError(
      "wb-redirect-form-errors",
      "a submission is already in flight; wait for its answer"
    );
    return;
  }
  if (!routePlanId()) return;
  const textarea = document.getElementById("wb-redirect-text");
  if (!textarea) return;
  const text = textarea.value;
  if (!text || !text.trim()) {
    showFormError("wb-redirect-form-errors", "correction must not be empty");
    return;
  }
  const bytes = redirectByteLength(text);
  if (bytes > REDIRECT_MAX_FEEDBACK_BYTES) {
    showFormError(
      "wb-redirect-form-errors",
      "correction is " +
        bytes +
        " bytes; the journal accepts at most " +
        REDIRECT_MAX_FEEDBACK_BYTES
    );
    return;
  }
  if (!state.redirect.permitted) {
    showFormError(
      "wb-redirect-form-errors",
      state.redirect.disabledReason || "redirect is unavailable right now"
    );
    return;
  }
  const token = state.redirect.actionToken;
  if (!token) {
    showFormError(
      "wb-redirect-form-errors",
      "no freshness proof yet; wait for the next refresh"
    );
    return;
  }
  if (!window.confirm(REDIRECT_CONFIRM)) return;

  const operationId = currentRedirectOperationId();
  state.redirect.inFlight = true;
  setRedirectSubmitDisabled(true);
  try {
    const body = await apiRequest("/api/control/redirect", {
      method: "POST",
      body: planBody({
        operation_id: operationId,
        action_token: token,
        text: text,
      }),
    });
    if (body && body.ok === true) {
      // Durable: either this call persisted it, or the journal already held
      // this id and the server replayed that record back.
      const replayed = !!(body.redirect && body.redirect.replayed);
      // Remembered before the draft is cleared: the retry identity ends here,
      // the attribution has to outlive it or the operator loses their own
      // correction in the progress list.
      rememberSubmittedRedirect(operationId);
      // A recorded correction is still work nobody has watched finish, so it
      // joins the outstanding list the rest of the matrix writes to. Only the
      // id goes in; the words never leave this tab.
      rememberReceipt({
        operation_id: operationId,
        action: "redirect",
        plan: routePlanId(),
        outcome: OUTCOME_PENDING,
        status: "accepted",
        reason: null,
        settled: false,
      });
      resetRedirectComposer();
      showControlsFeedback(
        "info",
        replayed
          ? "correction " + operationId + " was already recorded; nothing resubmitted"
          : "correction " + operationId + " recorded; watch Progress for acknowledgment"
      );
    } else {
      // The draft stays put on every refusal, and on this one the panel says
      // why and offers the new identity that sending it would need.
      if (isRedirectConflict(body)) showRedirectConflict(body, operationId);
      showFormError(
        "wb-redirect-form-errors",
        redirectFailureMessage(body, operationId)
      );
    }
  } catch (err) {
    // Nobody knows whether this arrived. The id is written down for exactly
    // that reason: a reload has to be able to show that the question is open.
    rememberReceipt({
      operation_id: operationId,
      action: "redirect",
      plan: routePlanId(),
      outcome: OUTCOME_UNREACHABLE,
      status: null,
      reason: "unreachable",
      settled: false,
    });
    showFormError(
      "wb-redirect-form-errors",
      "submission failed: " +
        describeError(err) +
        " — the draft is kept; retrying reuses operation id " +
        operationId
    );
  } finally {
    state.redirect.inFlight = false;
    setRedirectSubmitDisabled(!state.redirect.permitted);
    renderOperationReceipts(state.detailOutstanding);
  }
  await loadAndRenderRedirect();
}

export function redirectFailureMessage(body, operationId) {
  const error = (body && body.error) || "redirect was refused";
  const receipt = body && body.details ? body.details.redirect : null;
  const persisted = receipt && receipt.operation_id;
  if (persisted) {
    // Refused but recorded. Resubmitting the same id is the same request, so
    // the hint that stops a second correction belongs here.
    return error + " — " + REDIRECT_RECONCILE_HINT;
  }
  return (
    error +
    " — nothing was recorded; the draft is kept and a retry reuses operation id " +
    operationId
  );
}

export function redirectByteLength(text) {
  if (typeof TextEncoder === "function") {
    return new TextEncoder().encode(text).length;
  }
  return unescape(encodeURIComponent(text)).length;
}
