"""Shared fixtures for the workflow metrics test modules.

Split from tests/test_asl_metrics.py."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import (
    assurance_models_findings,
    cli_trace,
    events,
    metrics,
    plans_files,
    plans_payloads,
    plans_session_ids,
    review,
    review_repair,
)
from agent_session_loop import assurance_projection as assurance_projection
from tests.support import write_plan_with_ids

AUDIT_RUN_ID = "20260810T101112123456Z-4242"


def _iso(now: datetime, *, hours_ago: float) -> str:
    return (now - timedelta(hours=hours_ago)).isoformat()


def _make_state_dir(state_root: Path, plan_dir: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    state_dir = state_root / plans_files.path_sha256(plan_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "state.json").write_text(
        json.dumps({"plan_dir": str(plan_dir), "version": 2}, sort_keys=True),
        encoding="utf-8",
    )
    return state_dir


def _write_turn_result(
    state_dir: Path,
    *,
    run_id: str,
    turn_index: int,
    session_id: str,
    started_at: str,
    finished_at: str,
    status_result: str = "complete",
    session_kind: str = "implementation",
    duration_seconds: float = 60.0,
    interrupted: bool = False,
    extra: dict | None = None,
) -> Path:
    turn_dir = (
        state_dir / "runs" / run_id / f"turn-{turn_index:04d}-session-{session_id}"
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session_kind": session_kind,
        "started_at": started_at,
        "finished_at": finished_at,
        "status_result": status_result,
        "duration_seconds": duration_seconds,
        "interrupted": interrupted,
    }
    payload.update(extra or {})
    result_path = turn_dir / "result.json"
    result_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result_path


def _append_event(
    state_dir: Path, *, created_at: str, name: str, payload: dict
) -> None:
    record = {
        "created_at": created_at,
        "name": name,
        "payload": payload,
        "version": 2,
    }
    with (state_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _publish_audit(state_root: Path, *, recorded_at: str, run_id: str = AUDIT_RUN_ID):
    finding = assurance_models_findings.Finding(
        category="lifecycle",
        component="agent_session_loop/runner.py",
        invariant_key="retry.budget-respected",
        lane="lifecycle-retry-resume",
        severity="high",
        summary="A retry ran after the budget was exhausted.",
        recommendation="Clamp the retry counter before relaunching.",
        evidence=("events.jsonl",),
    )
    return assurance_projection.publish_audit(
        assurance_projection.AuditPublication(
            run_id=run_id,
            state="findings",
            eligible=True,
            findings=(finding,),
            completed_at=recorded_at,
        ),
        state_dir=state_root,
        recorded_at=recorded_at,
    )


def _write_generated_review_repair_plan(
    plan_dir: Path,
    *,
    review_id: str = "2",
    child_status: str = "pending",
) -> None:
    child_id = f"{review_id}.1"
    write_plan_with_ids(
        plan_dir,
        (("1", "complete"), (review_id, "complete"), (child_id, child_status)),
    )
    review_path = plan_dir / plans_session_ids.session_id_to_filename(review_id)
    review_path.write_text(
        "\n".join(
            (
                review.REVIEW_SESSION_MARKER,
                review.review_session_version_marker(),
                review_path.read_text(encoding="utf-8"),
            )
        ),
        encoding="utf-8",
    )
    child_path = plan_dir / plans_session_ids.session_id_to_filename(child_id)
    child_path.write_text(
        "\n".join(
            (
                review_repair.REVIEW_REPAIR_SESSION_MARKER,
                review_repair.review_repair_session_version_marker(),
                child_path.read_text(encoding="utf-8"),
            )
        ),
        encoding="utf-8",
    )


class MetricsCollectionBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()
        self.now = datetime.now(UTC)

    def _collect(self, since_days: int = 7) -> metrics.MetricsReport:
        return metrics.collect_metrics(
            state_dir=self.state_root, since_days=since_days, now=self.now
        )


def _trace_record(
    *,
    event: str,
    invocation_id: str,
    timestamp: str,
    command_path: list[str] | None = None,
    options: list[str] | None = None,
    help_depth: str | None = None,
    harness: str | None = None,
    origin: str = cli_trace.ORIGIN_DIRECT,
    **end_fields: object,
) -> dict:
    record = {
        "schema_version": cli_trace.SCHEMA_VERSION,
        "event": event,
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "pid": 4242,
        "command_path": command_path or [],
        "options": options or [],
        "help_depth": help_depth,
        "harness": harness,
        "origin": origin,
    }
    record.update(end_fields)
    return record


def _timing_run(
    state_dir: Path,
    now: datetime,
    *,
    run_id: str,
    started: float,
    finished: float,
    dry_run: bool = False,
) -> None:
    """Write one run's terminal pair, `started`/`finished` in hours ago."""
    payload = {
        "run_id": run_id,
        "run_started_at": _iso(now, hours_ago=started),
    }
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=started),
        name=events.RUN_STARTED_EVENT,
        payload={**payload, "dry_run": dry_run, "harness": "claude"},
    )
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=finished),
        name=events.RUN_FINISHED_EVENT,
        payload={
            **payload,
            "ok": True,
            "paused": False,
            "reason": "completed",
            "run_finished_at": _iso(now, hours_ago=finished),
            "duration_seconds": (started - finished) * 3600.0,
        },
    )


def _timing_turn(
    state_dir: Path,
    now: datetime,
    *,
    run_id: str,
    turn_index: int,
    session_id: str,
    started: float,
    finished: float,
    session_kind: str | None = plans_payloads.SESSION_KIND_IMPLEMENTATION,
) -> None:
    payload = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session": session_id,
        "session_id": session_id,
        "started_at": _iso(now, hours_ago=started),
        "finished_at": _iso(now, hours_ago=finished),
        "duration_seconds": (started - finished) * 3600.0,
    }
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=finished),
        name=events.TURN_EXECUTION_FINISHED_EVENT,
        payload=dict(payload),
    )
    finished_payload = dict(payload)
    if session_kind is not None:
        finished_payload["session_kind"] = session_kind
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=finished),
        name=events.TURN_FINISHED_EVENT,
        payload=finished_payload,
    )


def _timing_check(
    state_dir: Path,
    now: datetime,
    *,
    run_id: str,
    turn_index: int,
    check_index: int,
    name: str,
    started: float,
    finished: float,
    argv: list[str] | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    status: str = "passed",
) -> None:
    """Write one verification phase carrying a single check."""
    identity = {"run_id": run_id, "turn_index": turn_index, "session": "1"}
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=started),
        name=events.VERIFICATION_STARTED_EVENT,
        payload={
            **identity,
            "check_count": 1,
            "started_at": _iso(now, hours_ago=started),
        },
    )
    started_payload = {
        **identity,
        "check_index": check_index,
        "name": name,
        "kind": "command",
    }
    if argv is not None:
        started_payload["argv"] = argv
    if repo is not None:
        started_payload["repo"] = repo
    if cwd is not None:
        started_payload["cwd"] = cwd
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=started),
        name=events.VERIFICATION_CHECK_STARTED_EVENT,
        payload=started_payload,
    )
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=finished),
        name=events.VERIFICATION_CHECK_FINISHED_EVENT,
        payload={
            **identity,
            "check_index": check_index,
            "name": name,
            "kind": "command",
            "repo": repo,
            "status": status,
            "duration_seconds": (started - finished) * 3600.0,
        },
    )
    _append_event(
        state_dir,
        created_at=_iso(now, hours_ago=finished),
        name=events.VERIFICATION_FINISHED_EVENT,
        payload={
            **identity,
            "passed": status == "passed",
            "finished_at": _iso(now, hours_ago=finished),
            "duration_seconds": (started - finished) * 3600.0,
        },
    )


class TimingSectionBase(unittest.TestCase):
    """The additive `execution_timing` section of `asl metrics`.

    Every fixture writes its own events, so the assertions are about what the
    section says when the evidence is thin, historical, or spread across
    invocations — not about a live corpus.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()
        self.now = datetime.now(UTC)

    def _section(self, since_days: int = 7) -> dict:
        return metrics.collect_metrics(
            state_dir=self.state_root, since_days=since_days, now=self.now
        ).execution_timing

    def _plan(self, name: str) -> Path:
        return _make_state_dir(self.state_root, self.tmp_path / name)

    def _text(self, since_days: int = 7) -> str:
        report = metrics.collect_metrics(
            state_dir=self.state_root, since_days=since_days, now=self.now
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            metrics._print_report(report)
        return stdout.getvalue()
