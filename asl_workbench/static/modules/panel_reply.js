/* Live question reads, reply context and durable receipt restoration. */

import { OUTCOME_PENDING, OUTCOME_RESULT, apiRequest, describeError } from "./api.js";
import {
  LINKED_QUESTION_OPEN_TIMEOUT_MS,
  REPLY_CONTEXT_ERROR_HINTS,
} from "./constants.js";
import { planUrl, routePlanId } from "./identity.js";
import { applyActionAvailability, receiptsForPlan } from "./operations.js";
import { clearFormErrors, showFormError } from "./profile.js";
import { state } from "./state.js";

export async function loadAndRenderReply() {
  const requestedPlan = routePlanId();
  const requestedQuestion = state.route && state.route.questionId;
  const panel = document.getElementById("wb-reply-panel");
  if (panel) panel.hidden = false;
  try {
    const body = await apiRequest(
      planUrl(
        "/api/questions",
        requestedQuestion ? { question_id: requestedQuestion } : null
      )
    );
    if (
      routePlanId() !== requestedPlan ||
      (state.route && state.route.questionId) !== requestedQuestion
    ) {
      return;
    }
    if (body && body.ok) {
      const questionId = body.question_id ? String(body.question_id) : null;
      const linkedQuestion = state.route && state.route.questionId;
      if (linkedQuestion && questionId !== linkedQuestion) {
        renderReplyPending(linkedQuestion);
        return;
      }
      state.reply.openingQuestionId = null;
      state.reply.openingStartedAt = null;
      const questionChanged = state.reply.questionId !== questionId;
      if (questionChanged) {
        state.reply.operationId = null;
        state.reply.questionId = questionId;
        state.reply.recordedQuestionId = null;
        state.reply.recordedOperationId = null;
        clearFormErrors("wb-reply-form-errors");
        showReplyStatus("", "");
      }
      restoreRecordedReply(questionId);
      if (
        questionId &&
        state.reply.deliveredQuestionId === questionId
      ) {
        // This read may have started before the reply settled. A late copy of
        // the formerly live question cannot downgrade the terminal delivery
        // state back to "recorded" or reopen its form.
        hideReplyPanel();
        return;
      }
      state.detailReplyContext = body;
      const form = document.getElementById("wb-reply-form");
      if (form) form.hidden = false;
      const recorded =
        questionId && state.reply.recordedQuestionId === questionId;
      setReplyFormDisabled(
        Boolean(recorded),
        recorded ? "reply recorded; waiting for the running session" : null
      );
      const submit = document.querySelector(
        '#wb-reply-form button[type="submit"]'
      );
      if (submit) {
        const label = recorded
          ? "reply recorded"
          : body.context_kind === "waiting"
            ? "send reply"
            : "reply and resume";
        if (submit.textContent !== label) submit.textContent = label;
      }
      if (recorded) {
        const recordedIdentity = state.reply.recordedOperationId
          ? " for question " +
            questionId +
            " as operation " +
            state.reply.recordedOperationId +
            "."
          : ".";
        showReplyStatus(
          "pending",
          "Reply recorded" +
            recordedIdentity +
            " Waiting for the running session to consume it."
        );
      }
      const promptEl = document.getElementById("wb-reply-prompt");
      const prompt = body.rendered_prompt || "";
      if (promptEl && promptEl.textContent !== prompt) promptEl.textContent = prompt;
      const promptDetails = document.getElementById("wb-reply-prompt-details");
      if (promptDetails && linkedQuestion) promptDetails.open = true;
      const hintEl = document.getElementById("wb-reply-hint");
      if (hintEl && linkedQuestion) {
        hintEl.textContent =
          "This notification opened the current question below. Reply sends the answer to the running session.";
      }
      if (linkedQuestion && !recorded && questionChanged) {
        showReplyStatus("info", "Current question loaded from this notification.");
      }
      if (
        linkedQuestion &&
        state.route &&
        state.route.focus === "reply" &&
        !recorded
      ) {
        const textarea = document.getElementById("wb-reply-text");
        if (textarea && typeof textarea.focus === "function") {
          try {
            textarea.focus({ preventScroll: true });
          } catch (err) {
            textarea.focus();
          }
        }
        // One-shot just like every other detail focus: a later question poll
        // must not steal the keyboard back from the operator.
        state.route.focus = null;
      }
      const lastEl = document.getElementById("wb-reply-last-message");
      const lastMessage = body.last_message || "";
      if (lastEl && lastEl.textContent !== lastMessage) {
        lastEl.textContent = lastMessage;
      }
    } else {
      const linkedQuestion = state.route && state.route.questionId;
      restoreRecordedReply(linkedQuestion);
      const recordedQuestion = state.reply.recordedQuestionId;
      const detailReason =
        body && body.details && typeof body.details === "object"
          ? body.details.reason
          : null;
      const questionNotFound =
        body &&
        body.reason === "detail-refused" &&
        detailReason === "not-found";
      const questionTimedOut = body && body.reason === "detail-timeout";
      const linkedNotFound = linkedQuestion && questionNotFound;
      if (
        recordedQuestion &&
        (!linkedQuestion || linkedQuestion === recordedQuestion) &&
        questionNotFound
      ) {
        // The recorded reply made this exact question disappear from the
        // connector.  That is its normal terminal state after ASL consumes the
        // reply, so preserve the durable submission feedback instead of
        // replacing it with the remote read's expected not-found refusal.
        hideReplyPanel();
        return;
      }
      if (linkedNotFound || (linkedQuestion && questionTimedOut)) {
        const startedAt = state.reply.openingStartedAt;
        if (
          state.reply.openingQuestionId !== linkedQuestion ||
          typeof startedAt !== "number" ||
          Date.now() - startedAt < LINKED_QUESTION_OPEN_TIMEOUT_MS
        ) {
          renderReplyPending(linkedQuestion);
          return;
        }
        renderReplyContextError({
          error:
            questionTimedOut
              ? "This alert's question could not be loaded before the deadline. Open the latest notification or return to the queue."
              : "This alert's question is no longer available. Open the latest notification or return to the queue.",
        });
        return;
      }
      renderReplyContextError(body);
    }
  } catch (err) {
    renderReplyContextError({
      reason: "request-failed",
      error: describeError(err),
    });
  }
}

/** Restore a durable live-reply receipt after Safari reloads the alert tab. */
export function restoreRecordedReply(questionId) {
  if (!questionId) return false;
  const outstanding = state.detailOutstanding;
  const target =
    outstanding && outstanding.target && typeof outstanding.target === "object"
      ? outstanding.target
      : {};
  const heldQuestion =
    outstanding && (outstanding.question_id || target.question_id);
  if (
    outstanding &&
    outstanding.operation_id &&
    outstanding.method === "question.reply" &&
    heldQuestion === questionId
  ) {
    state.reply.recordedQuestionId = questionId;
    state.reply.recordedOperationId = String(outstanding.operation_id);
    return true;
  }
  const receipt = receiptsForPlan(routePlanId()).find(
    (entry) =>
      entry.action === "reply" &&
      entry.question_id === questionId &&
      (entry.outcome === OUTCOME_PENDING || entry.outcome === OUTCOME_RESULT) &&
      entry.operation_id
  );
  if (!receipt) return false;
  state.reply.recordedQuestionId = questionId;
  state.reply.recordedOperationId = receipt.operation_id;
  return true;
}

/** Keep a question-bound notification on its exact question while data catches up. */
export function renderReplyPending(questionId) {
  if (state.reply.openingQuestionId !== questionId) {
    state.reply.openingQuestionId = questionId || null;
    state.reply.openingStartedAt = Date.now();
  }
  state.detailReplyContext = null;
  const panel = document.getElementById("wb-reply-panel");
  if (panel) panel.hidden = false;
  const form = document.getElementById("wb-reply-form");
  if (form) form.hidden = true;
  clearFormErrors("wb-reply-form-errors");
  const promptEl = document.getElementById("wb-reply-prompt");
  if (promptEl) promptEl.textContent = "Opening the question from this alert…";
  const promptDetails = document.getElementById("wb-reply-prompt-details");
  if (promptDetails) promptDetails.open = true;
  const hintEl = document.getElementById("wb-reply-hint");
  if (hintEl) {
    hintEl.textContent =
      "Waiting for the exact question carried by this notification. This page updates automatically.";
  }
  const lastEl = document.getElementById("wb-reply-last-message");
  if (lastEl) lastEl.textContent = questionId ? "question " + questionId : "";
  showReplyStatus("pending", "Opening the current question…");
}

export function renderReplyContextError(payload) {
  state.detailReplyContext = null;
  const reason = payload && payload.reason ? String(payload.reason) : null;
  const error =
    payload && payload.error
      ? String(payload.error)
      : "no paused-Q&A context available";
  const hint = reason ? REPLY_CONTEXT_ERROR_HINTS[reason] : null;
  const message = reason
    ? reason + ": " + (hint ? hint + " " + error : error)
    : error;
  // The same sentence goes under the shut button and into the form error. The
  // prompt box it used to live in alone is inside a collapsed `<details>`, so
  // an operator could otherwise meet a dead "submit reply" with nothing on it.
  setReplyFormDisabled(true, message);
  const promptEl = document.getElementById("wb-reply-prompt");
  if (promptEl) promptEl.textContent = message;
  const lastEl = document.getElementById("wb-reply-last-message");
  if (lastEl) lastEl.textContent = "";
  showFormError("wb-reply-form-errors", message);
}

export function hideReplyPanel() {
  const panel = document.getElementById("wb-reply-panel");
  if (state.reply.recordedQuestionId) {
    const firstDelivery =
      state.reply.deliveredQuestionId !== state.reply.recordedQuestionId;
    state.reply.deliveredQuestionId = state.reply.recordedQuestionId;
    if (panel) panel.hidden = false;
    const form = document.getElementById("wb-reply-form");
    if (form) form.hidden = true;
    showReplyStatus(
      "info",
      "Reply delivered. The question is closed and the run resumed."
    );
    state.detailReplyContext = null;
    if (firstDelivery) {
      // A reply can settle inside a detail poll. That poll normally restores
      // the operator's old scroll position after rendering, which would hide
      // this terminal state below the fold. Defer the one-time return to the
      // top until the poll's finally block; outside a poll, do it now.
      if (state.detailInFlight) {
        state.reply.scrollToTopAfterDelivery = true;
      } else {
        window.scrollTo(window.scrollX || 0, 0);
      }
    }
    return;
  }
  if (panel) panel.hidden = true;
  state.detailReplyContext = null;
  setReplyFormDisabled(false);
}

export function resetReplyForPlan() {
  state.reply.operationId = null;
  state.reply.questionId = null;
  state.reply.recordedQuestionId = null;
  state.reply.recordedOperationId = null;
  state.reply.deliveredQuestionId = null;
  state.reply.scrollToTopAfterDelivery = false;
  state.reply.openingQuestionId = null;
  state.reply.openingStartedAt = null;
  const form = document.getElementById("wb-reply-form");
  if (form) {
    form.hidden = false;
    form.removeAttribute("aria-busy");
  }
  clearFormErrors("wb-reply-form-errors");
  showReplyStatus("", "");
}

export function showReplyStatus(kind, message) {
  const status = document.getElementById("wb-reply-submit-status");
  if (!status) return;
  if (!message) {
    status.hidden = true;
    status.textContent = "";
    status.removeAttribute("data-kind");
    return;
  }
  status.hidden = false;
  status.setAttribute("data-kind", kind || "info");
  status.textContent = message;
}

/**
 * Shut or reopen the reply form, saying why on the button when it is shut.
 *
 * The reason is written as `data-blocked-reason` rather than left as a bare
 * `disabled`, for two reasons: the capability pass would otherwise re-enable
 * the button on its next sweep, and a greyed-out control with no sentence on
 * it is the thing this view exists not to show.
 */
export function setReplyFormDisabled(disabled, reason) {
  const form = document.getElementById("wb-reply-form");
  if (!form) return;
  const controls = form.querySelectorAll("textarea, button[type=\"submit\"]");
  controls.forEach((control) => {
    const next = Boolean(disabled);
    if (control.disabled !== next) control.disabled = next;
  });
  const button = form.querySelector("button[type=\"submit\"]");
  if (button) {
    if (disabled) {
      const blockedReason =
        reason || "there is no paused question on this plan to answer";
      if (button.getAttribute("data-blocked-reason") !== blockedReason) {
        button.setAttribute("data-blocked-reason", blockedReason);
      }
    } else if (button.hasAttribute("data-blocked-reason")) {
      button.removeAttribute("data-blocked-reason");
    }
  }
  applyActionAvailability(form);
}
