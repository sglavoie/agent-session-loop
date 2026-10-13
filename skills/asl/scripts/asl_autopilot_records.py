"""Progress records, supervision-end fields, and run-result payloads."""

from __future__ import annotations

import json

from asl_autopilot_constants import QUESTION_PREVIEW_MAX_CHARS, RUN_STAGE
from asl_autopilot_launch_policy import _assurance_payload, _review_resume_payload

from agent_session_loop import mailbox_models as _mailbox_models
from agent_session_loop import mailbox_vocabulary as _mailbox_vocabulary


def _escaped_question_preview(
    text: str,
    *,
    max_chars: int = QUESTION_PREVIEW_MAX_CHARS,
) -> str:
    """Return one bounded JSON string suitable for a progress record.

    JSON string encoding keeps newlines, control characters, quotes, and
    non-ASCII text from creating extra stderr records. Truncation is performed
    against the encoded representation so the advertised bound applies after
    escaping rather than only to the source text.
    """
    encoded = json.dumps(text.strip(), ensure_ascii=True)
    if len(encoded) <= max_chars:
        return encoded

    marker = "\u2026"
    low = 0
    high = len(text)
    best = json.dumps(marker, ensure_ascii=True)
    while low <= high:
        midpoint = (low + high) // 2
        candidate = json.dumps(
            text[:midpoint].rstrip() + marker,
            ensure_ascii=True,
        )
        if len(candidate) <= max_chars:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    return best


def _question_progress_record(question: _mailbox_models.MailboxMessage) -> str:
    """Render one question as a single bounded ``QUESTION`` progress record."""
    preview = _escaped_question_preview(question.text or "")
    return f"QUESTION id={question.id} preview={preview}"


def _notice_progress_record(notice: _mailbox_models.MailboxMessage) -> str:
    """Render one notice as a single bounded ``NOTICE`` progress record."""
    preview = _escaped_question_preview(notice.text or "")
    return f"NOTICE id={notice.id} preview={preview}"


def _wait_end_progress_record(message: _mailbox_models.MailboxMessage) -> str:
    return (
        f"WAIT_ENDED question={message.to} wait={message.wait_id} "
        f"reason={message.wait_end_reason}"
    )


def _receipt_progress_record(message: _mailbox_models.MailboxMessage) -> str:
    return (
        f"CLOSURE_CONSUMED question={message.to} "
        f"closure={message.closure_id} receipt={message.id}"
    )


SUPERVISION_END_OUTCOMES: dict[str, str] = {
    "complete": _mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
    "review_findings": _mailbox_vocabulary.SUPERVISION_OUTCOME_COMPLETE,
    "paused": _mailbox_vocabulary.SUPERVISION_OUTCOME_PAUSED,
    "needs_agent_reply": _mailbox_vocabulary.SUPERVISION_OUTCOME_PAUSED,
    "max-sessions": _mailbox_vocabulary.SUPERVISION_OUTCOME_STOPPED,
    "stopped": _mailbox_vocabulary.SUPERVISION_OUTCOME_STOPPED,
    "blocked": _mailbox_vocabulary.SUPERVISION_OUTCOME_BLOCKED,
    "user_required": _mailbox_vocabulary.SUPERVISION_OUTCOME_BLOCKED,
    "worker_hung": _mailbox_vocabulary.SUPERVISION_OUTCOME_BLOCKED,
}


# Every other terminal reason (`status-error`, `run-error`, `capped`,
# `empty-reply`, ...) means supervision stopped needing a human, not that the
# helper itself crashed; `error` stays reserved for a raised exception or a
# termination signal so the agent can tell the two apart.
DEFAULT_SUPERVISION_OUTCOME = _mailbox_vocabulary.SUPERVISION_OUTCOME_BLOCKED


def _supervision_end_fields(result: dict) -> tuple[str, str]:
    """Map a helper result onto the terminal record's ``(outcome, text)``.

    The raw ``reason`` is preserved verbatim in the text so a supervising agent
    reads the exact helper vocabulary without a schema round-trip, while the
    coarse enum stays stable.
    """
    reason = result.get("reason")
    reason = reason if isinstance(reason, str) and reason else "unknown"
    outcome = SUPERVISION_END_OUTCOMES.get(reason, DEFAULT_SUPERVISION_OUTCOME)
    return outcome, f"supervision ended: {reason}"


def _run_result(
    ok: bool,
    reason: str,
    *,
    status: dict | None = None,
    run: dict | None = None,
    chunks: int = 0,
    error: str | None = None,
    retries: dict | None = None,
    completed: int | None = None,
    pending: int | None = None,
    outbox: dict | None = None,
    hang: dict | None = None,
    quota_wait: dict | None = None,
    stop_kind: str | None = None,
    recovery: dict | None = None,
) -> dict:
    """Build a stable ``stage: run`` supervision result payload."""
    result: dict = {"ok": ok, "stage": RUN_STAGE, "reason": reason, "chunks": chunks}
    if stop_kind is not None:
        result["stop_kind"] = stop_kind
    if completed is not None:
        result["completed"] = completed
    if pending is not None:
        result["pending"] = pending
    if error is not None:
        result["error"] = error
    if hang is not None:
        result["hang"] = hang
    if status is not None:
        result["status"] = status
    if run is not None:
        result["run"] = run
    if retries is not None:
        result["retries"] = retries
    if quota_wait is not None:
        result["quota_wait"] = quota_wait
    if recovery is not None:
        result["recovery"] = recovery
    if outbox is not None:
        result["outbox"] = outbox
    # Runner JSON is the freshest projection after a chunk; status is the
    # durable source when no chunk launched.  Expose either at the top level so
    # callers do not have to know which supervision path produced the result.
    review_resume = _review_resume_payload(run) or _review_resume_payload(status)
    if review_resume is not None:
        result["review_resume"] = review_resume
    assurance = _assurance_payload(run) or _assurance_payload(status)
    if assurance is not None:
        result["assurance"] = assurance
    return result
