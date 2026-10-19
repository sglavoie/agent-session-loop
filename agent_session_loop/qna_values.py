"""Low-level Q&A state readers and value coercion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .qna_models import (
    EXPIRED_WAIT_PAUSE_REASON,
    LEGACY_WAIT_CEILING_FIELD,
    REASON_MISSING_QUESTION_CONTEXT,
    REASON_NO_PAUSED_RUN,
    REASON_REPLY_CONTEXT_MISMATCH,
    WAIT_CEILING_FIELD,
    QnAError,
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
