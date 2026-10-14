"""Shared fixtures for the storage maintenance test modules.

Split from tests/test_asl_maintenance.py."""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import (
    assurance_models_findings,
    maintenance_models,
    maintenance_run,
    plans_files,
    state,
)
from agent_session_loop import assurance_projection as projection

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
AUDIT_STAMP = "2026-06-01T00:00:00+00:00"

RUN_ANCIENT = "20260101T000000000000Z-11"
RUN_OLD = "20260601T000000000000Z-22"
RUN_NEWEST = "20260803T000000000000Z-33"

# Thresholds the tests choose deliberately instead of inheriting, so a real
# machine's free space can never decide whether an assertion holds.
NEVER_LOW = 0.0
ALWAYS_LOW = 1.0
HUGE_BUDGET = 1 << 40


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class MaintenanceTestBase(unittest.TestCase):
    """Builds a throwaway state root holding one or more plans' run directories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir(parents=True)
        self.plan_dir = self._make_plan("plan")

    def _make_plan(self, name: str) -> Path:
        plan_dir = self.tmp_path / name
        plan_dir.mkdir()
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True)
        payload = {"plan_dir": str(plans_files.canonicalize_path(plan_dir))}
        (plan_state / "state.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        return plan_dir

    def _write_lock(self, *, plan_dir: Path | None = None, **fields: object) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        lock_path = state.plan_state_dir(target, self.state_root) / state.LOCK_FILE_NAME
        payload = {
            "plan_dir": str(plans_files.canonicalize_path(target)),
            "created_at": "2026-08-01T00:00:00+00:00",
            **fields,
        }
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return lock_path

    def _write_run(
        self,
        run_id: str,
        *,
        finished_at: str | None,
        plan_dir: Path | None = None,
        bodies: dict[str, bytes] | None = None,
        turns: int = 1,
        write_result: bool = True,
    ) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        run_dir = (
            state.plan_state_dir(target, self.state_root) / state.RUNS_DIR_NAME / run_id
        )
        for index in range(1, turns + 1):
            turn_dir = run_dir / f"turn-{index:04d}-session-1"
            turn_dir.mkdir(parents=True)
            for name, data in (bodies or {"stderr.log": b""}).items():
                (turn_dir / name).write_bytes(data)
            if write_result:
                payload: dict[str, object] = {"status_result": "ok"}
                if finished_at is not None:
                    payload["finished_at"] = finished_at
                (turn_dir / "result.json").write_text(
                    json.dumps(payload, sort_keys=True), encoding="utf-8"
                )
        return run_dir

    def _run(self, **kwargs: Any) -> maintenance_models.MaintenanceResult:
        """Run one pass with thresholds pinned unless a test overrides them."""
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("min_free_fraction", NEVER_LOW)
        kwargs.setdefault("budget_bytes", HUGE_BUDGET)
        kwargs.setdefault("force", True)
        return maintenance_run.run_maintenance(self.state_root, **kwargs)

    def _state_dir(self, plan_dir: Path | None = None) -> Path:
        return state.plan_state_dir(
            plan_dir if plan_dir is not None else self.plan_dir, self.state_root
        )

    def _run_path(self, run_id: str, plan_dir: Path | None = None) -> Path:
        return self._state_dir(plan_dir) / state.RUNS_DIR_NAME / run_id

    def _pin_run(self, run_id: str) -> None:
        """Open a real assurance issue against `run_id`, pinning its artifacts."""
        finding = assurance_models_findings.Finding(
            category="lifecycle",
            component="agent_session_loop/runner.py",
            invariant_key="retry.budget",
            lane="lifecycle-retry-resume",
            severity="high",
            summary="A retry ran after the budget was exhausted.",
            recommendation="Clamp the retry counter before relaunching.",
            evidence=("events.jsonl",),
        )
        projection.publish_audit(
            projection.AuditPublication(
                run_id=run_id,
                state="findings",
                eligible=True,
                findings=(finding,),
                completed_at=AUDIT_STAMP,
            ),
            state_dir=self.state_root,
            recorded_at=AUDIT_STAMP,
        )
