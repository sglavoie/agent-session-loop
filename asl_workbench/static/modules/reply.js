/* Reply form — the operator's answer to a paused run.
 *
 * The text is streamed verbatim to the paused agent and is not retrievable
 * from the UI afterwards, so it is confirmed before it is sent and cleared
 * from the DOM as soon as it is. "As soon as it is" is the whole difficulty:
 * the draft is the only copy, so clearing it on an answer that did not settle
 * would destroy the operator's words on the strength of a `202` — and keeping
 * it after one that did would invite the same reply twice.
 *
 * The four header rules are the redirect composer's, for the same reasons:
 *
 *   the draft is memory only — never storage, never the hash;
 *   the operation id outlives the answer, so a retry is the same reply;
 *   only a durable answer clears the textarea;
 *   an answer that settled ends the identity, because there is nothing left
 *   to retry under it.
 *
 * A recorded reply counts as durable. The far side has the words; resending
 * them would be a second reply to a question that already has one.
 */

import { OUTCOME_PENDING, OUTCOME_RESULT } from "./api.js";
import { planBody, routePlanId } from "./identity.js";
import { captureFocus, runMutation } from "./operations.js";
import {
  hideReplyPanel,
  setReplyFormDisabled,
  showReplyStatus,
} from "./panels.js";
import { clearFormErrors, showFormError } from "./profile.js";
import { state } from "./state.js";

export function wireReplyForm() {
  const form = document.getElementById("wb-reply-form");
  if (!form) return;
  form.addEventListener("submit", handleReplySubmit);
  const clearBtn = document.getElementById("wb-reply-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      // Discarding the words discards the request they belonged to: an id kept
      // past its draft would quietly attach the next reply to this one.
      clearReplyTextarea();
      clearFormErrors("wb-reply-form-errors");
    });
  }
}

export function clearReplyTextarea() {
  const textarea = document.getElementById("wb-reply-text");
  if (textarea) textarea.value = "";
  state.reply.operationId = null;
}

/** The identity this draft retries under, minted once and kept until it goes. */
export function currentReplyOperationId() {
  return state.reply.operationId;
}

export function rememberSubmittedReply(operationId) {
  if (!operationId) return;
  if (state.reply.submittedIds.indexOf(operationId) === -1) {
    state.reply.submittedIds.push(operationId);
  }
}

export function isOwnReply(operationId) {
  if (!operationId) return false;
  return (
    operationId === state.reply.operationId ||
    state.reply.submittedIds.indexOf(operationId) !== -1
  );
}

export async function handleReplySubmit(event) {
  event.preventDefault();
  clearFormErrors("wb-reply-form-errors");
  showReplyStatus("", "");
  if (!routePlanId()) return;
  const textarea = document.getElementById("wb-reply-text");
  if (!textarea) return;
  const replyText = textarea.value;
  if (!replyText || !replyText.trim()) {
    showFormError("wb-reply-form-errors", "reply must not be empty");
    return;
  }
  const context = state.detailReplyContext || {};
  const liveQuestion = context.context_kind === "waiting";
  const restore = captureFocus();
  const confirmed = window.confirm(
    liveQuestion
      ? "Send this reply to the running session? The text will be delivered verbatim and is not retrievable from the UI afterward."
      : "Submit this reply to resume the paused session? The text below will be streamed verbatim to the paused agent and is not retrievable from the UI afterward."
  );
  restore();
  if (!confirmed) return;
  const form = document.getElementById("wb-reply-form");
  const submitButton = form
    ? form.querySelector('button[type="submit"]')
    : null;
  if (form) form.setAttribute("aria-busy", "true");
  if (submitButton) submitButton.textContent = "sending reply…";
  showReplyStatus("pending", "Sending reply…");
  const body = planBody({
    reply_text: replyText,
    action_token: context.action_token,
    run_id: context.run_id,
  });
  if (liveQuestion && context.question_id) {
    body.question_id = context.question_id;
  }
  // A retry carries the id the first attempt used, which is what makes the
  // local runner answer `replayed` instead of streaming the words twice.
  if (state.reply.operationId) body.operation_id = state.reply.operationId;
  const answer = await runMutation({
    action: "reply",
    path: liveQuestion ? "/api/question-reply" : "/api/reply",
    body: body,
    subject: "the reply",
    feedback: (kind, message) => {
      // A refusal belongs beside the draft that is still on screen; anything
      // else belongs with the rest of the operation feedback.
      if (kind === "error") showFormError("wb-reply-form-errors", message);
      showReplyStatus(kind, message);
    },
  });
  if (form) form.removeAttribute("aria-busy");
  if (!answer) {
    if (submitButton) {
      submitButton.textContent = liveQuestion ? "send reply" : "reply and resume";
    }
    return;
  }
  if (answer.operation && answer.operation.id) {
    state.reply.operationId = answer.operation.id;
    rememberSubmittedReply(answer.operation.id);
  }
  if (
    answer.outcome === OUTCOME_PENDING &&
    !(answer.operation && answer.operation.id)
  ) {
    // A 202 is only a durable acceptance when it names the journal row the
    // service wrote.  Without that identity the browser cannot distinguish a
    // real Workbench receipt from an intermediary's generic accepted answer,
    // and claiming "Reply recorded" would destroy the operator's only copy of
    // the words without any row the connector can deliver.
    const message =
      "reply was not recorded: the server returned no durable operation receipt";
    showFormError("wb-reply-form-errors", message);
    showReplyStatus("error", message);
    if (submitButton) {
      submitButton.textContent = liveQuestion ? "send reply" : "reply and resume";
    }
    return;
  }
  const durable =
    answer.outcome === OUTCOME_RESULT || answer.outcome === OUTCOME_PENDING;
  if (!durable) {
    // The words are still the operator's and still the only copy. What is kept
    // with them is the identity, so pressing submit again is this reply rather
    // than another one.
    if (submitButton) {
      submitButton.textContent = liveQuestion ? "send reply" : "reply and resume";
    }
    return;
  }
  clearReplyTextarea();
  if (answer.outcome === OUTCOME_RESULT) {
    // Only a run that actually resumed gets the panel taken away; a recorded
    // reply leaves the question open until the Mac reads it. A fast consumer
    // can settle the operation before this POST answers, so retain the same
    // question identity the pending path uses. The following question read
    // will normally be a not-found refusal, and it must confirm delivery
    // rather than replace this terminal state with detail-refused.
    state.reply.recordedQuestionId = context.question_id || null;
    state.reply.recordedOperationId =
      answer.operation && answer.operation.id ? answer.operation.id : null;
    hideReplyPanel();
  } else {
    state.reply.recordedQuestionId = context.question_id || null;
    state.reply.recordedOperationId =
      answer.operation && answer.operation.id ? answer.operation.id : null;
    setReplyFormDisabled(
      true,
      "reply recorded; waiting for the running session"
    );
    if (submitButton) submitButton.textContent = "reply recorded";
    const recordedIdentity =
      context.question_id && state.reply.recordedOperationId
        ? " for question " +
          context.question_id +
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
}
