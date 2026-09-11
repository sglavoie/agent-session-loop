"""Paused and live-wait question-and-answer context loader.

The runner pauses a session when its harness ends without completing the
session, often because the agent is asking the human a clarifying question. A
mailbox-enabled run can instead remain live while waiting for a reply. This
module loads the latest completed turn's context from either state and exposes
a structured payload that downstream commands (``asl questions``, reply
producers, resolver prompt builders) can consume without duplicating state
parsing logic.

The plan directory remains the durable source of truth for completion. The
loader only reads state and never mutates it.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import events, mailbox, pause_policy, plans, responses, state

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


def load_qna_context(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> QnAContext:
    """Load and validate the latest paused or live-wait question context.

    ``reclaim=False`` classifies the lock without ever removing it, so a caller
    that has promised to write nothing observes the corpus rather than changing
    it.

    Raises :class:`QnAError` with a stable ``reason`` on every recognized
    failure path:

    - ``no-paused-run`` when the state file is missing, unreadable, neither
      paused nor waiting, or has no completed turn that recorded a session id.
    - ``missing-question-context`` when the selected turn or live-wait record
      is incomplete, or its ``last-message`` artifact is not readable/nonempty.
    - ``reply-context-mismatch`` when the selected turn's session id no longer
      matches the plan's current ``next_pending_id``.
    """
    snapshot = plans.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    payload = _read_state_payload(state_path)

    waiting_for_reply: dict[str, Any] | None = None
    if not _is_paused(payload):
        raw_waiting = payload.get("waiting_for_reply")
        if isinstance(raw_waiting, dict):
            waiting_for_reply = dict(raw_waiting)
        else:
            raise QnAError(
                REASON_NO_PAUSED_RUN,
                "latest runner state is not paused",
                details={
                    "state_path": str(state_path),
                    "paused": payload.get("paused"),
                },
            )

    return build_qna_context(
        snapshot,
        payload,
        plan_state=plan_state,
        state_path=state_path,
        waiting_for_reply=waiting_for_reply,
        reclaim=reclaim,
    )


def _wait_ceiling_field(waiting_for_reply: dict[str, Any]) -> str:
    """Name the key carrying this record's unsupervised ceiling.

    Waits persisted before the ceiling began rolling carry only the static
    ``hard_deadline``, so operator-facing output must keep naming that key for
    them rather than reporting a newer one as missing.
    """
    if WAIT_CEILING_FIELD in waiting_for_reply:
        return WAIT_CEILING_FIELD
    return LEGACY_WAIT_CEILING_FIELD


def _wait_ceiling_details(waiting_for_reply: dict[str, Any]) -> dict[str, Any]:
    field = _wait_ceiling_field(waiting_for_reply)
    details: dict[str, Any] = {field: waiting_for_reply.get(field)}
    if field == WAIT_CEILING_FIELD:
        # The persisted instant is only the anchor; the lease activity and the
        # ceiling span are what move it, so a bare deadline no longer explains
        # the classification on its own.
        details["unsupervised_ceiling_seconds"] = waiting_for_reply.get(
            "unsupervised_ceiling_seconds"
        )
        details["last_lease_activity_at"] = waiting_for_reply.get(
            "last_lease_activity_at"
        )
    return details


def build_qna_context(
    snapshot: plans.PlanSnapshot,
    payload: dict[str, Any],
    *,
    plan_state: Path,
    state_path: Path,
    waiting_for_reply: dict[str, Any] | None = None,
    reclaim: bool = True,
) -> QnAContext:
    """Build validated latest-turn context for a pause or proposed/live wait.

    Passing ``waiting_for_reply`` selects the nonterminal path and permits the
    caller to validate a proposed wait before it is persisted. Omitting it
    preserves the terminal-pause contract and requires ``paused is True``.
    ``reclaim=False`` keeps the wait classification free of the lock-hygiene
    side effect.
    """
    if waiting_for_reply is not None:
        lock_info = state._read_lock_path(
            plan_state / state.LOCK_FILE_NAME,
            snapshot.plan_dir,
            reclaim=reclaim,
        )
        wait_status = state.waiting_for_reply_status(
            waiting_for_reply,
            lock_info=lock_info,
        )
        if wait_status == state.WAITING_FOR_REPLY_STATUS_INVALID:
            raise QnAError(
                REASON_MISSING_QUESTION_CONTEXT,
                "waiting-for-reply state has an invalid unsupervised ceiling",
                details={
                    "state_path": str(state_path),
                    "question_id": waiting_for_reply.get("question_id"),
                    **_wait_ceiling_details(waiting_for_reply),
                },
            )
        if wait_status == state.WAITING_FOR_REPLY_STATUS_EXPIRED:
            payload = _expired_wait_recovery_payload(
                payload,
                waiting_for_reply,
            )
            waiting_for_reply = None

    next_pending = snapshot.next_pending
    if next_pending is None:
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            "plan has no pending session waiting on a reply",
            details={
                "plan_dir": str(snapshot.plan_dir),
                "state_path": str(state_path),
            },
        )

    context_kind = "waiting" if waiting_for_reply is not None else "pause"
    if context_kind == "pause" and not _is_paused(payload):
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            "latest runner state is not paused",
            details={
                "state_path": str(state_path),
                "paused": payload.get("paused"),
            },
        )

    turn = _latest_completed_turn(payload, state_path=state_path)

    session_id = _turn_session_id(turn, state_path=state_path)
    question_id: str | None = None
    if waiting_for_reply is not None:
        question_id = _validate_waiting_identity(
            waiting_for_reply,
            turn=turn,
            turn_session_id=session_id,
            state_path=state_path,
        )
    if session_id != next_pending.id:
        source_label = "waiting" if context_kind == "waiting" else "paused"
        session_detail_key = (
            "waiting_session_id" if context_kind == "waiting" else "paused_session_id"
        )
        raise QnAError(
            REASON_REPLY_CONTEXT_MISMATCH,
            (
                f"{source_label} session {session_id!r} no longer matches the "
                f"current pending session {next_pending.id!r}"
            ),
            details={
                session_detail_key: session_id,
                "next_pending_id": next_pending.id,
                "state_path": str(state_path),
            },
        )

    last_message_path = _coerce_path(turn.get("last_message_path"))
    if last_message_path is None:
        source_label = "waiting" if context_kind == "waiting" else "paused"
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            (
                f"latest {source_label} turn does not record a "
                "last-message artifact path"
            ),
            details={
                "state_path": str(state_path),
                "session_id": session_id,
            },
        )

    last_message = _read_last_message(last_message_path, session_id=session_id)

    return QnAContext(
        plan_dir=snapshot.plan_dir,
        state_dir=plan_state,
        state_path=state_path,
        run_id=_optional_str(payload.get("run_id")),
        run_dir=_coerce_path(payload.get("run_dir")),
        pause_reason=(
            _optional_str(payload.get("pause_reason") or payload.get("reason"))
            if context_kind == "pause"
            else None
        ),
        pause_message=(
            _optional_str(payload.get("pause_message"))
            if context_kind == "pause"
            else None
        ),
        pause_details=(
            _coerce_dict(payload.get("pause_details") or payload.get("details"))
            if context_kind == "pause"
            else {}
        ),
        harness=_optional_str(turn.get("harness") or payload.get("harness")),
        model=_optional_str(turn.get("model") or payload.get("model")),
        model_source=_optional_str(
            turn.get("model_source") or payload.get("model_source")
        ),
        effort=_optional_str(turn.get("effort") or payload.get("effort")),
        effort_source=_optional_str(
            turn.get("effort_source") or payload.get("effort_source")
        ),
        session_id=session_id,
        next_pending_id=next_pending.id,
        turn_index=_optional_int(turn.get("turn_index")),
        artifact_dir=_coerce_path(turn.get("artifact_dir")),
        stdout_path=_coerce_path(turn.get("stdout_path")),
        stderr_path=_coerce_path(turn.get("stderr_path")),
        last_message_path=last_message_path,
        result_path=_coerce_path(turn.get("result_path")),
        last_message=last_message,
        state_dir_override=_state_dir_override_from_payload(payload),
        context_kind=context_kind,
        question_id=question_id,
        waiting_for_reply=(
            dict(waiting_for_reply) if waiting_for_reply is not None else None
        ),
    )


def _expired_wait_recovery_payload(
    payload: dict[str, Any],
    waiting_for_reply: dict[str, Any],
) -> dict[str, Any]:
    """Project an orphaned expired wait as a resumable pause."""
    recovery = dict(payload)
    recovery["paused"] = True
    recovery["pause_reason"] = EXPIRED_WAIT_PAUSE_REASON
    recovery["pause_message"] = (
        "the live mailbox reply wait expired; resume the session to answer"
    )
    recovery["pause_details"] = {
        "question_id": waiting_for_reply.get("question_id"),
        **_wait_ceiling_details(waiting_for_reply),
    }
    return recovery


def _state_dir_override_from_payload(payload: dict[str, Any]) -> str | None:
    """Return the ``--state-dir`` override the paused run recorded, if any.

    The runner persists it under ``run_config.state_dir_override`` (added in the
    same session that introduced ``asl run --resume``). Resume guidance must
    re-emit ``--state-dir`` when the original run used one, because the override
    is what located this state file in the first place and cannot be recovered
    from the config it points at.
    """
    run_config = payload.get("run_config")
    if not isinstance(run_config, dict):
        return None
    return _optional_str(run_config.get("state_dir_override"))


def qna_context_payload(context: QnAContext) -> dict[str, Any]:
    """Render a :class:`QnAContext` as a JSON-friendly payload."""
    payload = {
        "plan_dir": str(context.plan_dir),
        "state_dir": str(context.state_dir),
        "state_path": str(context.state_path),
        "run_id": context.run_id,
        "run_dir": str(context.run_dir) if context.run_dir is not None else None,
        "pause_reason": context.pause_reason,
        "pause_message": context.pause_message,
        "pause_details": dict(context.pause_details),
        "harness": context.harness,
        "model": context.model,
        "model_source": context.model_source,
        "effort": context.effort,
        "effort_source": context.effort_source,
        "session_id": context.session_id,
        "next_pending_id": context.next_pending_id,
        "turn_index": context.turn_index,
        "artifact_dir": (
            str(context.artifact_dir) if context.artifact_dir is not None else None
        ),
        "stdout_path": (
            str(context.stdout_path) if context.stdout_path is not None else None
        ),
        "stderr_path": (
            str(context.stderr_path) if context.stderr_path is not None else None
        ),
        "last_message_path": str(context.last_message_path),
        "result_path": (
            str(context.result_path) if context.result_path is not None else None
        ),
        "last_message": context.last_message,
    }
    if context.context_kind == "waiting":
        payload.update(
            {
                "context_kind": "waiting",
                "question_id": context.question_id,
                "waiting_for_reply": (
                    dict(context.waiting_for_reply)
                    if context.waiting_for_reply is not None
                    else None
                ),
            }
        )
    return payload


def qna_error_payload(error: QnAError) -> dict[str, Any]:
    """Render a :class:`QnAError` as a JSON-friendly payload."""
    return {
        "reason": error.reason,
        "error": error.error,
        "details": dict(error.details),
    }


def _read_state_payload(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            f"runner state file does not exist: {state_path}",
            details={"state_path": str(state_path)},
        )
    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError as error:
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            f"runner state file is unreadable: {error}",
            details={"state_path": str(state_path), "error": str(error)},
        ) from error
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            f"runner state file is not valid JSON: {error}",
            details={"state_path": str(state_path), "error": str(error)},
        ) from error
    if not isinstance(payload, dict):
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            "runner state file did not contain a JSON object",
            details={"state_path": str(state_path)},
        )
    return payload


def _is_paused(payload: dict[str, Any]) -> bool:
    value = payload.get("paused")
    return value is True


def _latest_completed_turn(
    payload: dict[str, Any],
    *,
    state_path: Path,
) -> dict[str, Any]:
    turns = payload.get("turns")
    if not isinstance(turns, list) or not turns:
        raise QnAError(
            REASON_NO_PAUSED_RUN,
            "runner state has no completed turns",
            details={"state_path": str(state_path)},
        )
    for turn in reversed(turns):
        if isinstance(turn, dict):
            return turn
    raise QnAError(
        REASON_NO_PAUSED_RUN,
        "runner state turns array did not contain a turn object",
        details={"state_path": str(state_path)},
    )


def _turn_session_id(turn: dict[str, Any], *, state_path: Path) -> str:
    value = turn.get("session_id")
    if isinstance(value, str) and value:
        return value
    fallback = turn.get("session")
    if isinstance(fallback, str) and fallback:
        return fallback
    if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback > 0:
        return str(fallback)
    raise QnAError(
        REASON_NO_PAUSED_RUN,
        "latest paused turn does not record a session id",
        details={"state_path": str(state_path)},
    )


def _validate_waiting_identity(
    waiting: dict[str, Any],
    *,
    turn: dict[str, Any],
    turn_session_id: str,
    state_path: Path,
) -> str:
    question_id = _optional_str(waiting.get("question_id"))
    if question_id is None:
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            "waiting-for-reply state does not record a question id",
            details={"state_path": str(state_path)},
        )

    waiting_session_id = _session_id_from_payload(waiting)
    if waiting_session_id is None:
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            "waiting-for-reply state does not record a session id",
            details={
                "state_path": str(state_path),
                "question_id": question_id,
            },
        )
    if waiting_session_id != turn_session_id:
        raise QnAError(
            REASON_REPLY_CONTEXT_MISMATCH,
            (
                f"waiting session {waiting_session_id!r} does not match the "
                f"latest completed turn session {turn_session_id!r}"
            ),
            details={
                "state_path": str(state_path),
                "question_id": question_id,
                "waiting_session_id": waiting_session_id,
                "turn_session_id": turn_session_id,
            },
        )

    waiting_turn_index = _optional_int(waiting.get("turn_index"))
    turn_index = _optional_int(turn.get("turn_index"))
    if waiting_turn_index is None:
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            "waiting-for-reply state does not record a turn index",
            details={
                "state_path": str(state_path),
                "question_id": question_id,
            },
        )
    if turn_index is None or waiting_turn_index != turn_index:
        raise QnAError(
            REASON_REPLY_CONTEXT_MISMATCH,
            (
                f"waiting turn {waiting_turn_index!r} does not match the "
                f"latest completed turn {turn_index!r}"
            ),
            details={
                "state_path": str(state_path),
                "question_id": question_id,
                "waiting_turn_index": waiting_turn_index,
                "turn_index": turn_index,
            },
        )

    for field_name in (
        "started_at",
        "base_deadline",
        _wait_ceiling_field(waiting),
    ):
        if _optional_str(waiting.get(field_name)) is None:
            raise QnAError(
                REASON_MISSING_QUESTION_CONTEXT,
                f"waiting-for-reply state does not record {field_name}",
                details={
                    "state_path": str(state_path),
                    "question_id": question_id,
                },
            )
    lease_activity = waiting.get("last_lease_activity_at")
    if lease_activity is not None and _optional_str(lease_activity) is None:
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            ("waiting-for-reply state has an invalid last_lease_activity_at"),
            details={
                "state_path": str(state_path),
                "question_id": question_id,
            },
        )
    return question_id


def _session_id_from_payload(payload: dict[str, Any]) -> str | None:
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    session = payload.get("session")
    if isinstance(session, str) and session:
        return session
    if isinstance(session, int) and not isinstance(session, bool) and session > 0:
        return str(session)
    return None


def _read_last_message(path: Path, *, session_id: str) -> str:
    if not path.exists():
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            f"last-message artifact does not exist: {path}",
            details={
                "last_message_path": str(path),
                "session_id": session_id,
            },
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            f"last-message artifact is unreadable: {error}",
            details={
                "last_message_path": str(path),
                "session_id": session_id,
                "error": str(error),
            },
        ) from error
    if not text.strip():
        raise QnAError(
            REASON_MISSING_QUESTION_CONTEXT,
            f"last-message artifact is empty: {path}",
            details={
                "last_message_path": str(path),
                "session_id": session_id,
            },
        )
    return text


def _coerce_path(value: Any) -> Path | None:
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _coerce_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def artifact_paths_payload(context: QnAContext) -> dict[str, str | None]:
    """Render the selected turn's artifact paths as a JSON-friendly dict."""
    return {
        "run_dir": str(context.run_dir) if context.run_dir is not None else None,
        "artifact_dir": (
            str(context.artifact_dir) if context.artifact_dir is not None else None
        ),
        "stdout_path": (
            str(context.stdout_path) if context.stdout_path is not None else None
        ),
        "stderr_path": (
            str(context.stderr_path) if context.stderr_path is not None else None
        ),
        "last_message_path": str(context.last_message_path),
        "result_path": (
            str(context.result_path) if context.result_path is not None else None
        ),
    }


def requires_conversation_recovery(context: QnAContext) -> bool:
    """Report whether this pause is a lost conversation rather than a question.

    The distinction matters because every other paused context has a question
    in it. This one does not: the run stopped because it cannot resume the
    harness conversation at all, so an operator told to "ask the user the
    question the agent needed answered" would go looking for words nobody
    wrote.
    """
    return (
        context.context_kind != "waiting"
        and context.pause_reason == events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
    )


def recovery_command_text(context: QnAContext) -> str:
    """``asl run <plan> --resume [--state-dir X] --fresh-conversation`` guidance.

    The authorization is one-use and is never persisted, so this is rendered
    rather than performed: the operator has to decide, each time, that starting
    a fresh conversation on the retained files is the right trade.
    """
    return shlex.join([*_resume_command_base(context), "--fresh-conversation"])


def render_resolver_prompt(context: QnAContext) -> str:
    """Render a selected Q&A context as a ready-to-send resolver prompt.

    The output is plain Markdown suitable for handing to another AI agent.
    The agent is expected to ask the user the question(s) the original
    harness asked, then return a concise reply that the appropriate paused or
    live reply command can consume verbatim -- except for a lost conversation,
    which has no question and gets a recovery prompt instead.
    """
    if context.context_kind == "waiting":
        return _render_waiting_resolver_prompt(context)
    if requires_conversation_recovery(context):
        return _render_recovery_resolver_prompt(context)

    lines: list[str] = []
    lines.append("# Paused Agent Session - Resolver Prompt")
    lines.append("")
    lines.append(
        "An agent harness paused mid-session waiting on a clarifying answer. "
        "Read the context below, ask the user the question(s) the previous "
        "agent needed answered, then return a concise reply suitable for "
        "`asl run --reply`."
    )
    lines.append("")

    lines.append("## Plan")
    lines.append(f"- Plan directory: {context.plan_dir}")
    lines.append(f"- State file: {context.state_path}")
    lines.append(f"- Run ID: {_display(context.run_id)}")
    lines.append(f"- Session ID: {context.session_id}")
    lines.append(f"- Turn index: {_display(context.turn_index)}")
    lines.append("")

    lines.append("## Harness")
    lines.append(f"- Harness: {_display(context.harness)}")
    lines.append(
        f"- Model: {_display(context.model)}{_source_suffix(context.model_source)}"
    )
    lines.append(
        f"- Effort: {_display(context.effort)}{_source_suffix(context.effort_source)}"
    )
    lines.append("")

    lines.append("## Pause")
    lines.append(f"- Reason: {_display(context.pause_reason)}")
    lines.append(f"- Message: {_display(context.pause_message)}")
    if context.pause_details:
        details_json = json.dumps(context.pause_details, sort_keys=True)
        lines.append(f"- Details: {details_json}")
    else:
        lines.append("- Details: (none)")
    lines.append("")

    lines.append("## Artifacts")
    artifacts = artifact_paths_payload(context)
    lines.append(f"- Run dir: {_display(artifacts['run_dir'])}")
    lines.append(f"- Artifact dir: {_display(artifacts['artifact_dir'])}")
    lines.append(f"- Stdout: {_display(artifacts['stdout_path'])}")
    lines.append(f"- Stderr: {_display(artifacts['stderr_path'])}")
    lines.append(f"- Last message: {artifacts['last_message_path']}")
    lines.append(f"- Result: {_display(artifacts['result_path'])}")
    lines.append("")

    lines.append("## Previous Agent Message")
    lines.append("")
    lines.append(context.last_message.rstrip("\n"))
    lines.append("")

    lines.append("## Instructions")
    lines.append("")
    lines.append(
        "1. Ask the user the question(s) the previous agent needed answered. "
        "If the message contains multiple questions, ask each in order."
    )
    lines.append(
        "2. Keep the reply concise. One short answer per question is best "
        "(e.g., `1a, 2a, 3a`)."
    )
    lines.append(
        "3. Return ONLY the user's answer text. Do not add commentary, "
        "headings, or quoted context."
    )
    lines.append(
        "4. The answer will be passed verbatim to "
        "`asl run --resume --reply` to resume the paused session "
        "(--resume restores the original harness, model, and effort)."
    )
    lines.append("")

    return "\n".join(lines)


def _render_recovery_resolver_prompt(context: QnAContext) -> str:
    """Render the one pause that must not be read as a question.

    Deliberately different from the paused prompt in the only way that matters:
    it never tells the resolver to ask the user anything. There is no question
    here, so an instruction to find one produces an invented one, and a reply
    built from it would be handed to a session that cannot use it.
    """
    lines: list[str] = []
    lines.append("# Unresumable Conversation - Recovery Prompt")
    lines.append("")
    lines.append(
        "This run did not stop on a question. It stopped because the harness "
        "conversation it would resume can no longer be selected, so there is "
        "nothing for the user to answer and no reply that would restart it."
    )
    lines.append("")

    lines.append("## Plan")
    lines.append(f"- Plan directory: {context.plan_dir}")
    lines.append(f"- State file: {context.state_path}")
    lines.append(f"- Run ID: {_display(context.run_id)}")
    lines.append(f"- Session ID: {context.session_id}")
    lines.append(f"- Turn index: {_display(context.turn_index)}")
    lines.append("")

    lines.append("## Harness")
    lines.append(f"- Harness: {_display(context.harness)}")
    lines.append(
        f"- Model: {_display(context.model)}{_source_suffix(context.model_source)}"
    )
    lines.append(
        f"- Effort: {_display(context.effort)}{_source_suffix(context.effort_source)}"
    )
    lines.append("")

    lines.append("## Pause")
    lines.append(f"- Reason: {_display(context.pause_reason)}")
    lines.append(f"- Message: {_display(context.pause_message)}")
    if context.pause_details:
        details_json = json.dumps(context.pause_details, sort_keys=True)
        lines.append(f"- Details: {details_json}")
    else:
        lines.append("- Details: (none)")
    lines.append("")

    lines.append("## Artifacts")
    artifacts = artifact_paths_payload(context)
    lines.append(f"- Run dir: {_display(artifacts['run_dir'])}")
    lines.append(f"- Artifact dir: {_display(artifacts['artifact_dir'])}")
    lines.append(f"- Stdout: {_display(artifacts['stdout_path'])}")
    lines.append(f"- Stderr: {_display(artifacts['stderr_path'])}")
    lines.append(f"- Last message: {artifacts['last_message_path']}")
    lines.append(f"- Result: {_display(artifacts['result_path'])}")
    lines.append("")

    lines.append("## Recovery")
    lines.append("")
    lines.append(
        "1. Read the evidence first: "
        f"`{shlex.join(_recovery_inspect_command(context))}` reports whether a "
        "fresh conversation is required and which corrections a fresh start "
        "would still carry."
    )
    lines.append(
        "2. If a fresh start is the right trade, authorize exactly one: "
        f"`{recovery_command_text(context)}`. The session keeps its files, its "
        "recovery context, and any outstanding feedback; it loses only the "
        "prior conversation."
    )
    lines.append(
        "3. That authorization is one-use. It is never persisted, never "
        "rehydrated by a later `--resume`, and never replayed by a retry, so a "
        "second lost conversation has to be authorized again."
    )
    lines.append(
        "4. Do not ask the user to answer a question here, and do not resume "
        "with `--reply`: there is no pending question this session can consume."
    )
    lines.append("")

    return "\n".join(lines)


def _recovery_inspect_command(context: QnAContext) -> list[str]:
    parts = ["asl", "control", "recover-conversation", str(context.plan_dir)]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    return parts


def _render_waiting_resolver_prompt(context: QnAContext) -> str:
    waiting = (
        dict(context.waiting_for_reply) if context.waiting_for_reply is not None else {}
    )
    lines: list[str] = []
    lines.append("# Live Agent Session - Resolver Prompt")
    lines.append("")
    lines.append(
        "An agent harness is waiting in-process on a mailbox question. "
        "Read the context below and choose whether to submit a factual reply, "
        "continue without an answer, or end the live wait for ordinary "
        "paused-run recovery."
    )
    lines.append("")

    lines.append("## Plan")
    lines.append(f"- Plan directory: {context.plan_dir}")
    lines.append(f"- State file: {context.state_path}")
    lines.append(f"- Run ID: {_display(context.run_id)}")
    lines.append(f"- Session ID: {context.session_id}")
    lines.append(f"- Turn index: {_display(context.turn_index)}")
    lines.append("")

    lines.append("## Harness")
    lines.append(f"- Harness: {_display(context.harness)}")
    lines.append(
        f"- Model: {_display(context.model)}{_source_suffix(context.model_source)}"
    )
    lines.append(
        f"- Effort: {_display(context.effort)}{_source_suffix(context.effort_source)}"
    )
    lines.append("")

    lines.append("## Waiting for Reply")
    lines.append(f"- Question ID: {_display(context.question_id)}")
    lines.append(f"- Started: {_display(waiting.get('started_at'))}")
    lines.append(f"- Base deadline: {_display(waiting.get('base_deadline'))}")
    if _wait_ceiling_field(waiting) == LEGACY_WAIT_CEILING_FIELD:
        lines.append(f"- Hard deadline: {_display(waiting.get('hard_deadline'))}")
    else:
        lines.append(
            "- Unsupervised deadline: "
            f"{_display(waiting.get(WAIT_CEILING_FIELD))} "
            "(rolls forward with every lease that proves a supervisor alive)"
        )
        lines.append(
            "- Unsupervised ceiling: "
            f"{_display(waiting.get('unsupervised_ceiling_seconds'))}s"
        )
    lines.append(
        f"- Last lease activity: {_display(waiting.get('last_lease_activity_at'))}"
    )
    lines.append("")

    lines.append("## Artifacts")
    artifacts = artifact_paths_payload(context)
    lines.append(f"- Run dir: {_display(artifacts['run_dir'])}")
    lines.append(f"- Artifact dir: {_display(artifacts['artifact_dir'])}")
    lines.append(f"- Stdout: {_display(artifacts['stdout_path'])}")
    lines.append(f"- Stderr: {_display(artifacts['stderr_path'])}")
    lines.append(f"- Last message: {artifacts['last_message_path']}")
    lines.append(f"- Result: {_display(artifacts['result_path'])}")
    lines.append("")

    lines.append("## Resolution Choices")
    lines.append("")
    lines.append(
        "- Reply when the worker needs a factual operator answer: "
        f"`{mailbox_reply_command_text(context)}`"
    )
    lines.append(
        "- Continue without an answer only when the message is plainly "
        "nonblocking: "
        f"`{mailbox_continue_command_text(context)}`"
    )
    lines.append(
        "- Pause when the live wait should become ordinary paused-run "
        "recovery: "
        f"`{mailbox_pause_command_text(context)}`"
    )
    lines.append("")

    lines.append("## Previous Agent Message")
    lines.append("")
    lines.append(context.last_message.rstrip("\n"))
    lines.append("")

    lines.append("## Instructions")
    lines.append("")
    lines.append(
        "1. Choose reply, continue, or pause from the three outcomes above. "
        "Do not fabricate an answer merely to clear the live wait."
    )
    lines.append(
        "2. For reply, ask the user the question(s) the previous agent needed "
        "answered. If the message contains multiple questions, ask each in "
        "order. Keep the answer concise; one short answer per question is "
        "best (e.g., `1a, 2a, 3a`)."
    )
    lines.append(
        "3. For continue, proceed only when no factual answer or implementation "
        "choice is required. For pause, preserve the question for ordinary "
        "paused-run recovery."
    )
    lines.append(
        "4. Submit the selected command while the wait is still live. A reply "
        "continues with the operator's answer; continue supplies no answer; "
        "pause ends the live wait without inventing one."
    )
    lines.append("")

    return "\n".join(lines)


def _resume_command_base(context: QnAContext) -> list[str]:
    parts = ["asl", "run", str(context.plan_dir), "--resume"]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    return parts


def resume_command_text(context: QnAContext) -> str:
    """``asl run <plan> --resume [--state-dir X] --reply TEXT`` guidance.

    ``--resume`` rehydrates the paused run's harness/model/effort from the
    persisted run_config, so a non-default-harness run never resumes on the
    ``codex`` default.
    """
    return shlex.join([*_resume_command_base(context), "--reply", "TEXT"])


def reply_stdin_command_text(context: QnAContext) -> str:
    """``asl run <plan> --resume [--state-dir X] --reply-stdin`` guidance.

    Same rehydration as :func:`resume_command_text`, but the operator answer is
    piped on stdin so it never appears in argv.
    """
    return shlex.join([*_resume_command_base(context), "--reply-stdin"])


def _mailbox_reply_command_base(context: QnAContext) -> list[str]:
    if context.question_id is None:
        raise ValueError("live Q&A context does not record a question id")
    parts = [
        "asl",
        "inbox",
        "reply",
        str(context.plan_dir),
        "--to",
        context.question_id,
    ]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    return parts


def mailbox_reply_command_text(context: QnAContext) -> str:
    """Return live mailbox reply guidance with an inline text placeholder."""
    return shlex.join(
        [
            *_mailbox_reply_command_base(context),
            "--text",
            "TEXT",
            "--wait-consumed",
            "--wait-timeout",
            "300",
        ]
    )


def mailbox_reply_stdin_command_text(context: QnAContext) -> str:
    """Return live mailbox reply guidance that reads the answer from stdin."""
    return shlex.join(
        [
            *_mailbox_reply_command_base(context),
            "--text-stdin",
            "--wait-consumed",
            "--wait-timeout",
            "300",
        ]
    )


def _mailbox_resolution_command_base(
    context: QnAContext,
    action: mailbox.ResolutionAction,
) -> list[str]:
    if context.question_id is None:
        raise ValueError("live Q&A context does not record a question id")
    parts = [
        "asl",
        "inbox",
        "resolve",
        str(context.plan_dir),
        "--to",
        context.question_id,
        "--action",
        action,
    ]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    parts.extend(["--wait-consumed", "--wait-timeout", "300"])
    return parts


def mailbox_continue_command_text(context: QnAContext) -> str:
    """Return a ready-to-run answerless continue command."""
    return shlex.join(
        _mailbox_resolution_command_base(
            context,
            mailbox.RESOLUTION_ACTION_CONTINUE,
        )
    )


def mailbox_pause_command_text(context: QnAContext) -> str:
    """Return a ready-to-run answerless pause command."""
    return shlex.join(
        _mailbox_resolution_command_base(
            context,
            mailbox.RESOLUTION_ACTION_PAUSE,
        )
    )


def questions_payload(context: QnAContext) -> dict[str, Any]:
    """Render a paused or live-wait context as ``asl questions`` JSON."""
    if context.context_kind == "waiting":
        return responses.success_payload(
            "questions",
            context_kind="waiting",
            reply_command=mailbox_reply_command_text(context),
            reply_stdin_command=mailbox_reply_stdin_command_text(context),
            continue_command=mailbox_continue_command_text(context),
            pause_command=mailbox_pause_command_text(context),
            plan_dir=str(context.plan_dir),
            state_dir=str(context.state_dir),
            state_path=str(context.state_path),
            run_id=context.run_id,
            session_id=context.session_id,
            next_pending_id=context.next_pending_id,
            turn_index=context.turn_index,
            question_id=context.question_id,
            waiting_for_reply=(
                dict(context.waiting_for_reply)
                if context.waiting_for_reply is not None
                else None
            ),
            harness=context.harness,
            model=context.model,
            model_source=context.model_source,
            effort=context.effort,
            effort_source=context.effort_source,
            artifact_paths=artifact_paths_payload(context),
            last_message=context.last_message,
            rendered_prompt=render_resolver_prompt(context),
        )

    # The recovery keys are always present and ``None`` unless the pause is an
    # unresumable conversation: a consumer should branch on a key it can rely
    # on, not on whether the producer felt like emitting one. When recovery is
    # required the reply commands go to ``None`` for the same reason the prompt
    # stops asking for an answer -- a consumer that prints ``resume_command``
    # without reading ``recovery_required`` would otherwise hand the operator a
    # `--reply` for a question that does not exist.
    recovery_required = requires_conversation_recovery(context)
    return responses.success_payload(
        "questions",
        resume_command=None if recovery_required else resume_command_text(context),
        reply_stdin_command=(
            None if recovery_required else reply_stdin_command_text(context)
        ),
        recovery_required=recovery_required,
        recovery_command=recovery_command_text(context) if recovery_required else None,
        plan_dir=str(context.plan_dir),
        state_dir=str(context.state_dir),
        state_path=str(context.state_path),
        run_id=context.run_id,
        session_id=context.session_id,
        next_pending_id=context.next_pending_id,
        turn_index=context.turn_index,
        pause_reason=context.pause_reason,
        pause_message=context.pause_message,
        pause_details=dict(context.pause_details),
        pause_policy=pause_policy.classify_pause(context.pause_reason),
        harness=context.harness,
        model=context.model,
        model_source=context.model_source,
        effort=context.effort,
        effort_source=context.effort_source,
        artifact_paths=artifact_paths_payload(context),
        last_message=context.last_message,
        rendered_prompt=render_resolver_prompt(context),
    )


def questions_from_args(args: argparse.Namespace) -> int:
    """Run the ``questions`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    state_dir_arg = args.state_dir
    want_json = bool(args.json)

    try:
        context = load_qna_context(plan_dir_arg, state_dir=state_dir_arg)
    except QnAError as error:
        return _emit_questions_error(
            want_json=want_json,
            reason=error.reason,
            error=error.error,
            plan_dir=plan_dir_arg,
            details=error.details,
        )
    except plans.PlanError as error:
        return _emit_questions_error(
            want_json=want_json,
            reason="malformed-plan",
            error=str(error),
            plan_dir=plan_dir_arg,
            details={"plan_dir": str(plan_dir_arg)},
        )

    if want_json:
        responses.print_json(questions_payload(context))
    else:
        print(render_resolver_prompt(context))
    return 0


def _emit_questions_error(
    *,
    want_json: bool,
    reason: str,
    error: str,
    plan_dir: str | Path,
    details: dict[str, Any],
) -> int:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "questions",
                reason=reason,
                error=error,
                paused=False,
                details=dict(details),
                plan_dir=str(plan_dir),
            )
        )
    else:
        print(f"agent-session-loop questions: {error}", file=sys.stderr)
    return 1


def _display(value: Any) -> str:
    if value is None or value == "":
        return "(unknown)"
    return str(value)


def _source_suffix(source: str | None) -> str:
    if not source:
        return ""
    return f" ({source})"
