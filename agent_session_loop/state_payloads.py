"""Runner-state payload assembly shared by status and persistence."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import pause_policy, plans_models, plans_session_ids
from .state import LockInfo, _coerce_pid

WAITING_FOR_REPLY_STATUS_LIVE = "live"


WAITING_FOR_REPLY_STATUS_EXPIRED = "expired"


WAITING_FOR_REPLY_STATUS_INVALID = "invalid"


WAITING_FOR_REPLY_STATUSES: tuple[str, ...] = (
    WAITING_FOR_REPLY_STATUS_LIVE,
    WAITING_FOR_REPLY_STATUS_EXPIRED,
    WAITING_FOR_REPLY_STATUS_INVALID,
)


def _status_counts(snapshot: plans_models.PlanSnapshot) -> dict[str, int]:
    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    return counts


def _session_number_alias(session_id: str) -> int | None:
    parsed = plans_session_ids.parse_session_id(session_id)
    return parsed.as_int() if parsed.is_integer else None


def _session_compat_value(session_id: str) -> int | str:
    number = _session_number_alias(session_id)
    return number if number is not None else session_id


def _session_payload(session: plans_models.SessionStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session.id,
        "status": session.status,
        "raw_value": session.raw_value,
        "line_number": session.line_number,
        "path": str(session.path),
    }
    number = _session_number_alias(session.id)
    if number is not None:
        payload["number"] = number
    return payload


def _session_label_from_payload(payload: dict[str, Any]) -> str:
    session_id = payload.get("session_id")
    if session_id is not None:
        return str(session_id)
    session = payload.get("session")
    if session is not None:
        return str(session)
    return "unknown"


def _lock_payload(lock_info: LockInfo | None, lock_path: Path) -> dict[str, Any]:
    if lock_info is None:
        return {
            "present": False,
            "active": False,
            "stale": False,
            "path": str(lock_path),
        }
    return {
        "present": True,
        "active": lock_info.active,
        "stale": lock_info.stale,
        "pid": lock_info.pid,
        "plan_dir": lock_info.plan_dir,
        "created_at": lock_info.created_at,
        "reason": lock_info.reason,
        "path": str(lock_info.path),
    }


def _parse_wait_moment(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def waiting_for_reply_ceiling(
    waiting_for_reply: dict[str, Any],
) -> datetime | None:
    """Project the instant a wait runs out of unsupervised budget.

    Records written before the ceiling became a rolling bound carry only a
    static ``hard_deadline`` and are read exactly as they were written. Newer
    records carry the ceiling span too, so the deadline rolls forward with the
    last lease activity the runner observed: while a supervisor keeps renewing,
    the wait has no terminal instant to compare against.
    """
    raw_deadline = waiting_for_reply.get("unsupervised_deadline_at")
    if raw_deadline is None:
        return _parse_wait_moment(waiting_for_reply.get("hard_deadline"))
    deadline = _parse_wait_moment(raw_deadline)
    if deadline is None:
        return None
    ceiling = waiting_for_reply.get("unsupervised_ceiling_seconds")
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, (int, float))
        or not math.isfinite(ceiling)
        or ceiling <= 0
    ):
        return deadline
    last_lease = _parse_wait_moment(waiting_for_reply.get("last_lease_activity_at"))
    if last_lease is None:
        return deadline
    return max(deadline, last_lease + timedelta(seconds=ceiling))


def waiting_for_reply_status(
    waiting_for_reply: dict[str, Any],
    *,
    lock_info: LockInfo | None,
    now: datetime | None = None,
) -> str:
    """Classify whether a persisted wait still has a runner to accept a reply."""
    ceiling_deadline = waiting_for_reply_ceiling(waiting_for_reply)
    if ceiling_deadline is None:
        return WAITING_FOR_REPLY_STATUS_INVALID
    resolved_now = now if now is not None else datetime.now(UTC)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=UTC)
    if ceiling_deadline <= resolved_now:
        return WAITING_FOR_REPLY_STATUS_EXPIRED
    if lock_info is None or not lock_info.active:
        return WAITING_FOR_REPLY_STATUS_EXPIRED
    return WAITING_FOR_REPLY_STATUS_LIVE


def _runner_state_payload(
    state_path: Path,
    *,
    lock_info: LockInfo | None,
    include_turn_activity: bool = False,
) -> dict[str, Any]:
    if not state_path.exists():
        return {
            "present": False,
            "path": str(state_path),
            "error": None,
            "active_turn": None,
            "active_verification": None,
            "active_finalization": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The file vanished between ``exists()`` and ``read_text()``; treat
        # it as absent rather than malformed so concurrent runner cleanup
        # does not surface a 500 to the Workbench UI.
        return {
            "present": False,
            "path": str(state_path),
            "error": None,
            "active_turn": None,
            "active_verification": None,
            "active_finalization": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }
    except (OSError, ValueError) as error:
        # ValueError rather than JSONDecodeError: bytes that are not UTF-8
        # raise UnicodeDecodeError before any JSON is parsed.
        return {
            "present": True,
            "path": str(state_path),
            "error": str(error),
            "active_turn": None,
            "active_verification": None,
            "active_finalization": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }

    if not isinstance(raw, dict):
        return {
            "present": True,
            "path": str(state_path),
            "error": "state file did not contain a JSON object",
            "active_turn": None,
            "active_verification": None,
            "active_finalization": None,
            "waiting_for_reply": None,
            "waiting_for_reply_status": None,
            "abandoned": None,
        }

    active_turn = raw.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

    active_verification = raw.get("active_verification")
    if not isinstance(active_verification, dict):
        active_verification = None

    active_finalization = raw.get("active_finalization")
    if not isinstance(active_finalization, dict):
        active_finalization = None

    control = raw.get("control")
    if not isinstance(control, dict):
        control = None

    abandoned = raw.get("abandoned")
    if not isinstance(abandoned, dict):
        abandoned = None

    waiting_for_reply = raw.get("waiting_for_reply")
    if not isinstance(waiting_for_reply, dict):
        waiting_for_reply = None
    waiting_status = (
        waiting_for_reply_status(waiting_for_reply, lock_info=lock_info)
        if waiting_for_reply is not None
        else None
    )

    # Prefer the pause_policy block the runner persisted alongside the pause;
    # fall back to classifying the reason for older state files that predate it,
    # and leave it absent when the run is not paused.
    runner_pause_policy = raw.get("pause_policy")
    if not isinstance(runner_pause_policy, dict) and raw.get("paused"):
        details = raw.get("pause_details")
        runner_pause_policy = pause_policy.classify_pause(
            raw.get("reason"), details if isinstance(details, Mapping) else None
        )
    if not isinstance(runner_pause_policy, dict):
        runner_pause_policy = None

    run_config = raw.get("run_config")
    if not isinstance(run_config, dict):
        run_config = {}

    assurance = raw.get("assurance")
    if not isinstance(assurance, dict):
        assurance = None

    return {
        "present": True,
        "path": str(state_path),
        "error": None,
        "run_id": raw.get("run_id"),
        "harness": raw.get("harness"),
        # The terminal window the run was launched from is the only durable
        # handle an external supervisor can focus, so it travels with the rest
        # of the run's binding rather than only in the run-started event.
        "kitty_window_id": _coerce_pid(raw.get("kitty_window_id")),
        "review_harness": run_config.get("review_harness"),
        "supervisor_audit": run_config.get("supervisor_audit"),
        "assurance": assurance,
        # A legacy single-repository run records only the directory it ran
        # in; that directory is still what names its project.
        "cwd": raw.get("cwd"),
        **(
            {
                "workspace_root": raw.get("workspace_root"),
                "registry_digest": raw.get("registry_digest"),
                "repositories": raw.get("repositories"),
            }
            if raw.get("workspace_root") is not None
            else {}
        ),
        "ok": raw.get("ok"),
        "paused": raw.get("paused"),
        "reason": raw.get("reason"),
        "pause_policy": runner_pause_policy,
        # What the runner told the operator when it paused, and when: the text
        # outcome line needs both, and a paused run is otherwise only a reason.
        "pause_message": raw.get("pause_message") or None,
        "paused_at": raw.get("paused_at"),
        "run_started_at": raw.get("run_started_at"),
        "run_finished_at": raw.get("run_finished_at"),
        # Body-free activity records from this runner state, not plan history.
        **(
            {
                "turns": [
                    {
                        key: turn.get(key)
                        for key in ("started_at", "finished_at", "last_activity_at")
                    }
                    for turn in raw.get("turns", [])
                    if isinstance(turn, dict)
                ]
                if isinstance(raw.get("turns"), list)
                else []
            }
            if include_turn_activity
            else {}
        ),
        "duration_seconds": raw.get("duration_seconds"),
        "active_turn": active_turn,
        "active_verification": active_verification,
        "active_finalization": active_finalization,
        # Projected so a supervisor can tell an operator-requested stop from an
        # unexplained one: ``control.last_action`` is the only place the run
        # records that an abort, rather than the plan running out, ended it.
        "control": control,
        "waiting_for_reply": waiting_for_reply,
        "waiting_for_reply_status": waiting_status,
        "abandoned": abandoned,
    }
