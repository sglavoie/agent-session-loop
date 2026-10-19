"""Per-run execution timing reconciliation tests.

Every fixture here is synthetic and deterministic: the collector holds no
clock and reads no files, so a test only has to hand it records shaped like
the ones ``events.jsonl`` and ``result.json`` really carry.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import events, execution_timing, plans_payloads

BASE = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
RUN_ID = "20260901T000000000000Z-4242"
OTHER_RUN_ID = "20260901T010000000000Z-4243"
PLAN = "plan-a"


def _at(offset: float) -> datetime:
    return BASE + timedelta(seconds=offset)


def _iso(offset: float) -> str:
    return _at(offset).isoformat().replace("+00:00", "Z")


def _event(
    name: str,
    payload: dict[str, Any],
    *,
    created_at: float | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"name": name, "payload": payload, "version": 2}
    if created_at is not None:
        record["created_at"] = _iso(created_at)
    return record


def _run_started(*, dry_run: bool = False, run_id: str = RUN_ID) -> dict[str, Any]:
    return _event(
        events.RUN_STARTED_EVENT,
        {
            "run_id": run_id,
            "dry_run": dry_run,
            "harness": "claude",
            "run_started_at": _iso(0),
        },
        created_at=0,
    )


def _run_finished(
    *,
    run_id: str = RUN_ID,
    started: float = 0,
    finished: float = 250,
    duration: float | None = 250.0,
    paused: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "ok": not paused,
        "paused": paused,
        "reason": "paused" if paused else "completed",
        "run_started_at": _iso(started),
        "run_finished_at": _iso(finished),
        "duration_seconds": duration,
    }
    if extra:
        payload.update(extra)
    return _event(events.RUN_FINISHED_EVENT, payload, created_at=finished)


def _execution_finished(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    session_id: str = "1",
    omit_turn_index: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": finished - started if duration is None else duration,
    }
    if not omit_turn_index:
        payload["turn_index"] = turn_index
    return _event(events.TURN_EXECUTION_FINISHED_EVENT, payload, created_at=finished)


def _turn_finished(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    session_id: str = "1",
    session_kind: str | None = plans_payloads.SESSION_KIND_IMPLEMENTATION,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": finished - started if duration is None else duration,
    }
    if session_kind is not None:
        payload["session_kind"] = session_kind
    return _event(events.TURN_FINISHED_EVENT, payload, created_at=finished)


def _turn_started(
    *, turn_index: int, started: float, run_id: str = RUN_ID, session_id: str = "1"
) -> dict[str, Any]:
    return _event(
        execution_timing.TURN_STARTED_EVENT,
        {
            "run_id": run_id,
            "turn_index": turn_index,
            "session": session_id,
            "started_at": _iso(started),
        },
        created_at=started,
    )


def _turn_rejected(
    *, turn_index: int, run_id: str = RUN_ID, session_id: str = "1"
) -> dict[str, Any]:
    return _event(
        events.TURN_REJECTED_EVENT,
        {
            "run_id": run_id,
            "turn_index": turn_index,
            "session": session_id,
            "session_id": session_id,
            "guard": "handoff",
            "reason": "handoff-missing",
        },
    )


def _result(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    session_id: str = "1",
    session_kind: str | None = plans_payloads.SESSION_KIND_IMPLEMENTATION,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 2,
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "duration_seconds": finished - started if duration is None else duration,
        "status_result": "complete",
    }
    if session_kind is not None:
        payload["session_kind"] = session_kind
    return payload


def _verification(
    *,
    turn_index: int,
    started: float,
    finished: float,
    duration: float | None = None,
    run_id: str = RUN_ID,
    with_start: bool = True,
) -> list[dict[str, Any]]:
    identity = {"run_id": run_id, "turn_index": turn_index, "session": "1"}
    records: list[dict[str, Any]] = []
    if with_start:
        records.append(
            _event(
                events.VERIFICATION_STARTED_EVENT,
                {**identity, "check_count": 1, "started_at": _iso(started)},
                created_at=started,
            )
        )
    records.append(
        _event(
            events.VERIFICATION_FINISHED_EVENT,
            {
                **identity,
                "passed": True,
                "finished_at": _iso(finished),
                "duration_seconds": finished - started
                if duration is None
                else duration,
            },
            created_at=finished,
        )
    )
    return records


def _check_finished(
    *,
    turn_index: int,
    check_index: int,
    duration: float,
    run_id: str = RUN_ID,
    omit_check_index: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": "1",
        "name": f"check-{check_index}",
        "status": "passed",
        "duration_seconds": duration,
    }
    if not omit_check_index:
        payload["check_index"] = check_index
    return _event(events.VERIFICATION_CHECK_FINISHED_EVENT, payload)


def _commit(
    *,
    commit_index: int,
    started: float,
    finished: float,
    run_id: str = RUN_ID,
    repository_key: str = "root",
) -> list[dict[str, Any]]:
    identity = {
        "run_id": run_id,
        "commit_index": commit_index,
        "repository_key": repository_key,
    }
    return [
        _event(
            events.COMMIT_STARTED_EVENT,
            {**identity, "started_at": _iso(started)},
            created_at=started,
        ),
        _event(
            events.COMMIT_FINISHED_EVENT,
            {
                **identity,
                "success": True,
                "started_at": _iso(started),
                "finished_at": _iso(finished),
                "duration_seconds": finished - started,
            },
            created_at=finished,
        ),
    ]


def _finalization(
    *,
    started: float,
    finished: float,
    run_id: str = RUN_ID,
    phase: str = "assurance-audit",
) -> list[dict[str, Any]]:
    return [
        _event(
            events.FINALIZATION_STARTED_EVENT,
            {
                "run_id": run_id,
                "phase": phase,
                "started_at": _iso(started),
                "cancellable": True,
            },
            created_at=started,
        ),
        _event(
            events.FINALIZATION_FINISHED_EVENT,
            {
                "run_id": run_id,
                "phase": phase,
                "cancelled": False,
                "heartbeat_count": 2,
                "finished_at": _iso(finished),
                "duration_seconds": finished - started,
            },
            created_at=finished,
        ),
    ]


def _untimed_phase(
    start_name: str,
    finish_name: str,
    identity: dict[str, Any],
    *,
    run_id: str = RUN_ID,
) -> list[dict[str, Any]]:
    """A phase that ran — both records exist — that nothing ever timed.

    No ``duration_seconds`` and no timestamps: the shape a finished phase takes
    when its length was never recorded, which is not the shape of a phase that
    took no time.
    """
    return [
        _event(start_name, {"run_id": run_id, **identity}),
        _event(finish_name, {"run_id": run_id, **identity}),
    ]


#: ``(phase, start event, finish event, identity payload)`` for every non-turn
#: phase, so one missing-timing regression covers all three.
UNTIMED_PHASE_CASES: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        execution_timing.PHASE_VERIFICATION,
        events.VERIFICATION_STARTED_EVENT,
        events.VERIFICATION_FINISHED_EVENT,
        {"turn_index": 1, "session": "1"},
    ),
    (
        execution_timing.PHASE_COMMITS,
        events.COMMIT_STARTED_EVENT,
        events.COMMIT_FINISHED_EVENT,
        {"commit_index": 1, "repository_key": "root"},
    ),
    (
        execution_timing.PHASE_ASSURANCE,
        events.FINALIZATION_STARTED_EVENT,
        events.FINALIZATION_FINISHED_EVENT,
        {"phase": "assurance-audit"},
    ),
)


def _clean_run_events() -> list[dict[str, Any]]:
    """A run whose every phase reconciles: the trustworthy baseline.

    turn 1 [0, 100] · verification [100, 140] · commit [140, 150] ·
    turn 2 [150, 200] · assurance [200, 220] · run wall 250.
    """
    return [
        _run_started(),
        _turn_started(turn_index=1, started=0),
        _execution_finished(turn_index=1, started=0, finished=100),
        _turn_finished(turn_index=1, started=0, finished=100),
        *_verification(turn_index=1, started=100, finished=140),
        *_commit(commit_index=0, started=140, finished=150),
        _turn_started(turn_index=2, started=150, session_id="2"),
        _execution_finished(turn_index=2, started=150, finished=200, session_id="2"),
        _turn_finished(
            turn_index=2,
            started=150,
            finished=200,
            session_id="2",
            session_kind=plans_payloads.SESSION_KIND_REVIEW,
        ),
        *_finalization(started=200, finished=220),
        _run_finished(),
    ]


def _collect(
    evidence: list[execution_timing.RunEvidence],
    **kwargs: Any,
) -> execution_timing.ExecutionTimingReport:
    return execution_timing.collect_execution_timing(evidence, **kwargs)


def _one(
    event_records: list[dict[str, Any]],
    turn_results: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> execution_timing.RunTiming:
    report = _collect(
        [
            execution_timing.RunEvidence(
                plan_key=PLAN,
                event_records=event_records,
                turn_results=turn_results or [],
            )
        ],
        **kwargs,
    )
    assert len(report.runs) == 1, report.runs
    return report.runs[0]


def _codes(observations: tuple[execution_timing.Observation, ...]) -> list[str]:
    return [item.code for item in observations]


# fmt: off
__all__ = ("json", "unittest", "UTC", "datetime", "timedelta", "Path", "Any", "events", "execution_timing", "plans_payloads", "BASE", "RUN_ID", "OTHER_RUN_ID", "PLAN", "_at", "_iso", "_event", "_run_started", "_run_finished", "_execution_finished", "_turn_finished", "_turn_started", "_turn_rejected", "_result", "_verification", "_check_finished", "_commit", "_finalization", "_untimed_phase", "UNTIMED_PHASE_CASES", "_clean_run_events", "_collect", "_one", "_codes")
# fmt: on
