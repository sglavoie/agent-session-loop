/* Reply form — the operator's answer to a paused run.
 *
 * The text is streamed verbatim to the paused agent and is not retrievable
 * from the UI afterwards, so it is confirmed before it is sent and cleared
 * from the DOM as soon as it is.
 */

import { apiRequest, describeError, submit } from "./api.js";
import { showControlsFeedback } from "./controls.js";
import { planBody, routePlanId } from "./identity.js";
import { hideReplyPanel } from "./panels.js";
import { clearFormErrors, showFormError } from "./profile.js";

export function wireReplyForm() {
  const form = document.getElementById("wb-reply-form");
  if (!form) return;
  form.addEventListener("submit", handleReplySubmit);
  const clearBtn = document.getElementById("wb-reply-clear");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      clearReplyTextarea();
      clearFormErrors("wb-reply-form-errors");
    });
  }
}

export function clearReplyTextarea() {
  const textarea = document.getElementById("wb-reply-text");
  if (textarea) textarea.value = "";
}

export async function handleReplySubmit(event) {
  event.preventDefault();
  clearFormErrors("wb-reply-form-errors");
  if (!routePlanId()) return;
  const textarea = document.getElementById("wb-reply-text");
  if (!textarea) return;
  const replyText = textarea.value;
  if (!replyText || !replyText.trim()) {
    showFormError("wb-reply-form-errors", "reply must not be empty");
    return;
  }
  const confirmed = window.confirm(
    "Submit this reply to resume the paused session? The text below will be streamed verbatim to the paused agent and is not retrievable from the UI afterward."
  );
  if (!confirmed) return;
  try {
    const body = await apiRequest("/api/reply", {
      method: "POST",
      body: planBody({ reply_text: replyText }),
    });
    if (body && body.ok) {
      clearReplyTextarea();
      showControlsFeedback("info", "reply submitted; runner resumed");
      hideReplyPanel();
    } else {
      showFormError(
        "wb-reply-form-errors",
        (body && body.error) || "reply submission failed"
      );
    }
  } catch (err) {
    showFormError(
      "wb-reply-form-errors",
      "submission failed: " + describeError(err)
    );
  }
}
