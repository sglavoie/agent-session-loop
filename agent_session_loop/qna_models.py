"""Q&A context types and stable error vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

REASON_NO_PAUSED_RUN = "no-paused-run"
REASON_MISSING_QUESTION_CONTEXT = "missing-question-context"
REASON_REPLY_CONTEXT_MISMATCH = "reply-context-mismatch"
EXPIRED_WAIT_PAUSE_REASON = "wait-expired"

WAIT_CEILING_FIELD = "unsupervised_deadline_at"
LEGACY_WAIT_CEILING_FIELD = "hard_deadline"

QNA_ERROR_REASONS: tuple[str, ...] = (
    REASON_NO_PAUSED_RUN,
    REASON_MISSING_QUESTION_CONTEXT,
    REASON_REPLY_CONTEXT_MISMATCH,
)


class QnAError(Exception):
    """Raised when Q&A context cannot be loaded safely."""

    def __init__(
        self,
        reason: str,
        error: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.error = error
        self.details: dict[str, Any] = {} if details is None else dict(details)
        super().__init__(error)


@dataclass(frozen=True)
class QnAContext:
    """Structured latest-turn question context for resolver prompts."""

    plan_dir: Path
    state_dir: Path
    state_path: Path
    run_id: str | None
    run_dir: Path | None
    pause_reason: str | None
    pause_message: str | None
    pause_details: dict[str, Any]
    harness: str | None
    model: str | None
    model_source: str | None
    effort: str | None
    effort_source: str | None
    session_id: str
    next_pending_id: str
    turn_index: int | None
    artifact_dir: Path | None
    stdout_path: Path | None
    stderr_path: Path | None
    last_message_path: Path
    result_path: Path | None
    last_message: str
    state_dir_override: str | None = None
    context_kind: str = "pause"
    question_id: str | None = None
    waiting_for_reply: dict[str, Any] | None = None
