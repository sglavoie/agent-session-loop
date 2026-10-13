"""Tests for the run-retention projection in `agent_session_loop.retention`.

Covers the age cutoff boundary and every predicate that forbids selecting a
run: a live run, a locked plan directory, the newest run of a plan, unfinished
runs, and artifacts whose timestamps are malformed or missing. Also pins the
no-write contract of the projection itself, which the ordinary lock read would
otherwise break by reclaiming a lock it proved dead.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop import plans_files, retention, state, state_locks

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
DEAD_PID = 4_242_424

RUN_OLD = "20260601T000000000000Z-11"
RUN_NEW = "20260803T000000000000Z-22"
RUN_ABSENT = "20260602T000000000000Z-33"
AUDIT_STAMP = "2026-06-01T00:00:00+00:00"


def _stamp(days_ago: float, *, seconds: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago, seconds=seconds)).isoformat()


class RetentionTestBase(unittest.TestCase):
    """Builds a throwaway state root holding one plan's run directories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.plan_dir = self._make_plan("plan")
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_root)

    def _make_plan(self, name: str, **state_fields: object) -> Path:
        plan_dir = self.tmp_path / name
        plan_dir.mkdir()
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True)
        payload = {
            "plan_dir": str(plans_files.canonicalize_path(plan_dir)),
            **state_fields,
        }
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
        log_bytes: int = 0,
        write_result: bool = True,
        result_text: str | None = None,
    ) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        run_dir = (
            state.plan_state_dir(target, self.state_root) / state.RUNS_DIR_NAME / run_id
        )
        turn_dir = run_dir / "turn-0001-session-1"
        turn_dir.mkdir(parents=True)
        (turn_dir / "stderr.log").write_bytes(b"x" * log_bytes)
        if result_text is not None:
            (turn_dir / "result.json").write_text(result_text, encoding="utf-8")
        elif write_result:
            payload: dict[str, object] = {"status_result": "ok"}
            if finished_at is not None:
                payload["finished_at"] = finished_at
            (turn_dir / "result.json").write_text(
                json.dumps(payload, sort_keys=True), encoding="utf-8"
            )
        return run_dir

    def _report(self, **kwargs: object) -> retention.PlanRetention:
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("live_runs", frozenset())
        return retention.plan_retention(
            self.plan_dir, state_dir=self.state_root, **kwargs
        )

    def _runs_by_id(
        self, report: retention.PlanRetention
    ) -> dict[str, retention.RunCandidate]:
        return {run.run_id: run for run in report.runs}


class RetentionSelectionTests(RetentionTestBase):
    """Age cutoff, byte accounting, and the projection's read-only contract."""

    def test_old_run_is_selected_and_newest_run_is_kept(self) -> None:
        old = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1), log_bytes=10)

        report = self._report()
        runs = self._runs_by_id(report)

        self.assertTrue(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, "")
        self.assertFalse(runs[RUN_NEW].reclaimable)
        self.assertEqual(runs[RUN_NEW].reason, retention.RETAIN_NEWEST_RUN)
        self.assertEqual(report.reclaimable_runs, (runs[RUN_OLD],))
        self.assertEqual(report.reclaimable_bytes, runs[RUN_OLD].size_bytes)
        self.assertEqual(
            report.total_bytes, report.reclaimable_bytes + report.retained_bytes
        )
        self.assertTrue(old.is_dir(), "projection must not delete anything")

    def test_reclaimable_bytes_count_the_logs_inside_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=4096)
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertGreater(run.size_bytes, 4096)
        self.assertLess(run.size_bytes, 4096 + 2048)

    def test_age_cutoff_boundary_is_inclusive(self) -> None:
        self._write_run("20260705T120000000000Z-1", finished_at=_stamp(30))
        self._write_run("20260705T120001000000Z-2", finished_at=_stamp(30, seconds=-1))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        runs = self._runs_by_id(self._report())

        self.assertTrue(runs["20260705T120000000000Z-1"].reclaimable)
        self.assertFalse(runs["20260705T120001000000Z-2"].reclaimable)
        self.assertEqual(
            runs["20260705T120001000000Z-2"].reason,
            retention.RETAIN_WITHIN_CUTOFF,
        )

    def test_max_age_days_moves_the_cutoff(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        strict = self._runs_by_id(self._report(max_age_days=0.5))
        relaxed = self._runs_by_id(self._report(max_age_days=90.0))

        self.assertTrue(strict[RUN_OLD].reclaimable)
        self.assertFalse(relaxed[RUN_OLD].reclaimable)
        self.assertEqual(relaxed[RUN_OLD].reason, retention.RETAIN_WITHIN_CUTOFF)

    def test_run_finish_time_is_the_latest_turn(self) -> None:
        run_dir = self._write_run(RUN_OLD, finished_at=_stamp(45))
        late_turn = run_dir / "turn-0002-session-2"
        late_turn.mkdir()
        (late_turn / "result.json").write_text(
            json.dumps({"finished_at": _stamp(2)}), encoding="utf-8"
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention.RETAIN_WITHIN_CUTOFF)
        self.assertEqual(run.finished_at, _stamp(2))

    def test_newest_run_is_kept_even_when_every_run_is_ancient(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(120))
        self._write_run(RUN_NEW, finished_at=_stamp(90))

        runs = self._runs_by_id(self._report())

        self.assertTrue(runs[RUN_OLD].reclaimable)
        self.assertFalse(runs[RUN_NEW].reclaimable)
        self.assertEqual(runs[RUN_NEW].reason, retention.RETAIN_NEWEST_RUN)

    def test_plan_without_state_directory_reports_no_runs(self) -> None:
        report = retention.plan_retention(
            self.tmp_path / "never-run",
            state_dir=self.state_root,
            now=NOW,
            live_runs=frozenset(),
        )

        self.assertEqual(report.runs, ())
        self.assertEqual(report.reclaimable_bytes, 0)

    def test_negative_max_age_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._report(max_age_days=-1.0)
        with self.assertRaises(ValueError):
            retention.corpus_retention(self.state_root, max_age_days=-1.0)

    def test_report_serializes_for_the_cli(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        payload = self._report().to_dict()

        self.assertEqual(
            payload["plan_dir"], str(plans_files.canonicalize_path(self.plan_dir))
        )
        self.assertEqual(payload["state_dir"], str(self.plan_state))
        self.assertEqual(payload["run_count"], 2)
        self.assertEqual(payload["reclaimable_run_count"], 1)
        self.assertEqual(payload["max_age_days"], retention.DEFAULT_MAX_AGE_DAYS)
        self.assertEqual(
            payload["cutoff_at"],
            (NOW - timedelta(days=retention.DEFAULT_MAX_AGE_DAYS)).isoformat(),
        )
        self.assertEqual([run["run_id"] for run in payload["runs"]], [RUN_OLD, RUN_NEW])
        json.dumps(payload)


class RetentionSafetyTests(RetentionTestBase):
    """Live runs, locked plan dirs, and runs that cannot be dated."""

    def test_live_run_is_never_selected(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        runs = self._runs_by_id(
            self._report(live_runs=frozenset({(str(self.plan_state), RUN_OLD)}))
        )

        self.assertFalse(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, retention.RETAIN_LIVE_RUN)

    def test_live_run_keys_come_from_the_running_scan(self) -> None:
        plan_dir = self._make_plan("running-plan", run_id=RUN_NEW)
        self._write_lock(plan_dir=plan_dir, pid=os.getpid())
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=plan_dir)
        plan_state = state.plan_state_dir(plan_dir, self.state_root)

        keys = retention.live_run_keys(self.state_root)

        self.assertIn((str(plan_state), RUN_NEW), keys)
        self.assertNotIn((str(self.plan_state), RUN_OLD), keys)

    def test_active_lock_retains_every_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_lock(pid=os.getpid())

        report = self._report()

        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(
            [run.reason for run in report.runs], [retention.RETAIN_PLAN_LOCKED]
        )
        self.assertIn("active", report.runs[0].detail)

    def test_unreadable_lock_retains_every_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        lock_path = self._write_lock(pid=DEAD_PID)
        lock_path.write_text("{ not json", encoding="utf-8")

        report = self._report()

        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(report.runs[0].reason, retention.RETAIN_PLAN_LOCKED)

    def test_stale_lock_retains_every_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45))
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        lock_path = self._write_lock(pid=DEAD_PID, hostname=socket.gethostname())

        report = self._report()

        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(report.runs[0].reason, retention.RETAIN_PLAN_LOCKED)
        self.assertIn("stale", report.runs[0].detail)
        self.assertTrue(lock_path.exists(), "projection must not remove the lock")

    def test_run_without_turn_artifacts_is_unfinished(self) -> None:
        self._write_run(RUN_OLD, finished_at=None, write_result=False)
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention.RETAIN_UNFINISHED)
        self.assertIsNone(run.finished_at)
        self.assertIsNone(run.age_seconds)

    def test_turn_without_finished_at_is_unfinished(self) -> None:
        self._write_run(RUN_OLD, finished_at=None)
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention.RETAIN_UNFINISHED)

    def test_one_undated_turn_retains_the_whole_run(self) -> None:
        run_dir = self._write_run(RUN_OLD, finished_at=_stamp(45))
        undated_turn = run_dir / "turn-0002-session-2"
        undated_turn.mkdir()
        (undated_turn / "result.json").write_text(
            json.dumps({"finished_at": "not a timestamp"}), encoding="utf-8"
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention.RETAIN_UNDATED)

    def test_unreadable_turn_artifact_retains_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=None, result_text="{ not json")
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention.RETAIN_UNDATED)

    def test_non_object_turn_artifact_retains_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=None, result_text="[]")
        self._write_run(RUN_NEW, finished_at=_stamp(1))

        run = self._runs_by_id(self._report())[RUN_OLD]

        self.assertFalse(run.reclaimable)
        self.assertEqual(run.reason, retention.RETAIN_UNDATED)


class CorpusRetentionTests(RetentionTestBase):
    """Aggregation across every plan state directory under a state root."""

    def test_aggregates_every_plan(self) -> None:
        other_plan = self._make_plan("other-plan")
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._write_run(
            RUN_OLD, finished_at=_stamp(60), plan_dir=other_plan, log_bytes=500
        )
        self._write_run(RUN_NEW, finished_at=_stamp(2), plan_dir=other_plan)

        report = retention.corpus_retention(self.state_root, now=NOW)

        self.assertEqual(len(report.plans), 2)
        self.assertEqual(
            [entry.plan_dir for entry in report.plans],
            sorted(entry.plan_dir for entry in report.plans),
        )
        self.assertEqual(report.reclaimable_run_count, 2)
        self.assertEqual(
            report.reclaimable_bytes,
            sum(entry.reclaimable_bytes for entry in report.plans),
        )
        self.assertGreater(report.total_bytes, report.reclaimable_bytes)
        self.assertEqual(report.unresolved_state_dirs, ())
        json.dumps(report.to_dict())

    def test_state_dir_without_a_recoverable_plan_is_not_judged(self) -> None:
        orphan = self.state_root / ("0" * 64)
        orphan.mkdir(parents=True)
        (orphan / state.RUNS_DIR_NAME / RUN_OLD).mkdir(parents=True)

        report = retention.corpus_retention(self.state_root, now=NOW)

        self.assertEqual(
            report.unresolved_state_dirs, (str(plans_files.canonicalize_path(orphan)),)
        )
        self.assertEqual(report.reclaimable_bytes, 0)

    def test_state_dir_claiming_another_plan_is_not_judged(self) -> None:
        impostor = self.state_root / ("f" * 64)
        impostor.mkdir(parents=True)
        (impostor / "state.json").write_text(
            json.dumps({"plan_dir": str(plans_files.canonicalize_path(self.plan_dir))}),
            encoding="utf-8",
        )

        report = retention.corpus_retention(self.state_root, now=NOW)

        self.assertEqual(
            report.unresolved_state_dirs,
            (str(plans_files.canonicalize_path(impostor)),),
        )

    def test_missing_state_root_is_empty(self) -> None:
        report = retention.corpus_retention(self.tmp_path / "absent", now=NOW)

        self.assertEqual(report.plans, ())
        self.assertEqual(report.reclaimable_bytes, 0)


class AssurancePinningTests(RetentionTestBase):
    """Open issues pin their evidence, and an unreadable ledger pins everything.

    The ledger is never swept in either case; what changes is who is allowed to
    be swept while it cannot be read, which is nobody.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ledger_path = ledger.ledger_path(self.state_root)

    def _publish(self, run_id: str, *, invariant_key: str = "retry.budget") -> None:
        finding = models.Finding(
            category="lifecycle",
            component="agent_session_loop/runner.py",
            invariant_key=invariant_key,
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

    def test_open_issue_pins_an_otherwise_reclaimable_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)

        runs = self._runs_by_id(self._report())

        self.assertFalse(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, retention.RETAIN_OPEN_ISSUE)
        self.assertIn("open assurance issue", runs[RUN_OLD].detail)

    def test_closing_the_issue_releases_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_root, recorded_at=AUDIT_STAMP
        )

        runs = self._runs_by_id(self._report())

        self.assertTrue(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, "")

    def test_apply_keeps_the_ledger_and_the_pinned_run(self) -> None:
        pinned_dir = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        sweepable = self._make_plan("sweepable")
        removable = self._write_run(
            RUN_OLD, finished_at=_stamp(45), plan_dir=sweepable, log_bytes=100
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=sweepable)
        self._publish(RUN_OLD)
        before = self.ledger_path.read_bytes()

        report = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertTrue(pinned_dir.is_dir())
        # Two plans share the pinned run ID, so both directories are held.
        self.assertTrue(removable.is_dir())
        self.assertEqual(report.removed_run_count, 0)
        self.assertTrue(report.assurance["ledger_excluded"])
        self.assertEqual(report.assurance["ledger_path"], str(self.ledger_path))
        self.assertEqual(report.assurance["open_issue_count"], 1)
        self.assertEqual(report.assurance["pinned_run_count"], 1)
        self.assertEqual(report.assurance["missing_run_references"], [])
        json.dumps(report.to_dict())

    def test_history_survives_the_run_it_pointed_at(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_root, recorded_at=AUDIT_STAMP
        )

        report = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(report.removed_run_count, 1)
        self.assertTrue(self.ledger_path.exists())
        inbox = projection.read_projection(state_dir=self.state_root)
        self.assertEqual(len(inbox.issues), 1)
        self.assertEqual(inbox.issues[0].run_ids, (RUN_OLD,))

    def test_a_reference_with_no_run_directory_is_reported(self) -> None:
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_ABSENT)

        report = retention.run_gc(state_dir=self.state_root, now=NOW)

        self.assertEqual(report.assurance["pinned_run_count"], 1)
        self.assertEqual(report.assurance["missing_run_references"], [RUN_ABSENT])

    def test_plan_scope_resolves_references_against_the_whole_root(self) -> None:
        other_plan = self._make_plan("other-plan")
        self._write_run(RUN_OLD, finished_at=_stamp(45), plan_dir=other_plan)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)

        report = retention.run_gc(self.plan_dir, state_dir=self.state_root, now=NOW)

        self.assertEqual(report.scope, "plan")
        self.assertEqual(report.assurance["missing_run_references"], [])

    def _break_the_ledger(self) -> None:
        """Make the ledger unreadable without making it look absent.

        A missing ledger is a readable empty inbox; only a ledger that exists
        and cannot be parsed leaves the pin question unanswered.
        """
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.mkdir()

    def _reasons(self, report: retention.GCReport) -> set[str]:
        return {run.reason for entry in report.plans for run in entry.retention.runs}

    def test_unreadable_ledger_blocks_collection_and_says_so(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._break_the_ledger()

        report = retention.run_gc(state_dir=self.state_root, now=NOW)

        self.assertFalse(report.assurance["available"])
        self.assertTrue(report.assurance["error"])
        self.assertTrue(report.assurance["collection_blocked"])
        self.assertEqual(report.assurance["pinned_run_count"], 0)
        self.assertEqual(report.reclaimable_run_count, 0)
        self.assertEqual(self._reasons(report), {retention.RETAIN_PIN_UNKNOWN})
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertTrue(payload["assurance"]["collection_blocked"])
        self.assertEqual(
            payload["retained_by_reason"][retention.RETAIN_PIN_UNKNOWN]["run_count"],
            2,
        )

    def test_dry_run_output_says_collection_is_blocked(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._break_the_ledger()
        args = argparse.Namespace(
            plan_dir=None,
            state_dir=self.state_root,
            max_age_days=retention.DEFAULT_MAX_AGE_DAYS,
            apply=False,
            json=False,
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = retention.gc_from_args(args)
        text = buffer.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertIn("collection is blocked", text)
        self.assertIn(retention.RETAIN_PIN_UNKNOWN, text)
        self.assertNotIn("Re-run with --apply", text)

    def test_plan_scope_apply_removes_nothing_when_the_ledger_is_unreadable(
        self,
    ) -> None:
        old = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        new = self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._break_the_ledger()

        report = retention.run_gc(
            self.plan_dir, state_dir=self.state_root, apply=True, now=NOW
        )

        self.assertEqual(report.scope, "plan")
        self.assertEqual(report.removed_run_count, 0)
        self.assertEqual(report.reclaimed_bytes, 0)
        self.assertEqual(report.failure_count, 0)
        self.assertTrue(old.is_dir())
        self.assertTrue(new.is_dir())
        self.assertEqual(self._reasons(report), {retention.RETAIN_PIN_UNKNOWN})

    def test_corpus_scope_apply_removes_nothing_when_the_ledger_is_unreadable(
        self,
    ) -> None:
        first = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        other_plan = self._make_plan("other-plan")
        second = self._write_run(
            RUN_OLD, finished_at=_stamp(45), plan_dir=other_plan, log_bytes=100
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=other_plan)
        self._break_the_ledger()

        blocked = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(blocked.scope, "corpus")
        self.assertEqual(blocked.removed_run_count, 0)
        self.assertEqual(blocked.failure_count, 0)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertEqual(self._reasons(blocked), {retention.RETAIN_PIN_UNKNOWN})

        # Both runs really were sweepable, so the block above held back a
        # deletion that would otherwise have happened against an empty pin set.
        self.ledger_path.rmdir()
        repaired = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(repaired.removed_run_count, 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())


class DryRunPurityTests(RetentionTestBase):
    """A proven-dead same-host lock survives every scope and every mode.

    That lock is the one the ordinary read path removes, which is what made a
    dry run write and made the two scopes disagree: corpus scope reclaimed it
    in the liveness pre-pass and then judged the plan unlocked, while plan
    scope removed it and reported `plan-locked` from the same read.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self.lock_path = self._write_lock(pid=DEAD_PID, hostname=socket.gethostname())

    def _lock_state(self) -> tuple[bytes, int, int]:
        info = self.lock_path.stat()
        return self.lock_path.read_bytes(), info.st_ino, info.st_size

    def _decisions(
        self, report: retention.PlanRetention
    ) -> list[tuple[str, bool, str]]:
        return [(run.run_id, run.reclaimable, run.reason) for run in report.runs]

    def _corpus_report(self, **kwargs: object) -> retention.PlanRetention:
        corpus = retention.corpus_retention(self.state_root, now=NOW, **kwargs)
        canonical = str(plans_files.canonicalize_path(self.plan_dir))
        return next(entry for entry in corpus.plans if entry.plan_dir == canonical)

    def test_the_ordinary_read_would_have_removed_this_lock(self) -> None:
        # Without this the tests below could pass against a lock nothing
        # reclaims, which is exactly what the pre-repair suite did.
        with redirect_stderr(io.StringIO()):
            lock_info = state_locks.read_lock_info(self.plan_dir, self.state_root)

        self.assertIsNotNone(lock_info)
        self.assertFalse(self.lock_path.exists())

    def test_plan_scope_dry_run_preserves_the_lock(self) -> None:
        before = self._lock_state()

        report = retention.plan_retention(
            self.plan_dir, state_dir=self.state_root, now=NOW
        )

        self.assertEqual(before, self._lock_state())
        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(
            {run.reason for run in report.runs}, {retention.RETAIN_PLAN_LOCKED}
        )

    def test_corpus_scope_dry_run_preserves_the_lock(self) -> None:
        before = self._lock_state()

        report = self._corpus_report()

        self.assertEqual(before, self._lock_state())
        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(
            {run.reason for run in report.runs}, {retention.RETAIN_PLAN_LOCKED}
        )

    def test_the_running_scan_preserves_the_lock(self) -> None:
        before = self._lock_state()

        keys = retention.live_run_keys(self.state_root)

        self.assertEqual(before, self._lock_state())
        self.assertEqual(keys, frozenset())

    def test_both_scopes_reach_the_same_decision(self) -> None:
        plan_scope = self._decisions(
            retention.plan_retention(self.plan_dir, state_dir=self.state_root, now=NOW)
        )
        # Re-establish the lock so the second scope judges the same corpus as
        # the first even when the first removed it. The bytes are identical,
        # so this is a no-op against a projection that writes nothing.
        self._write_lock(pid=DEAD_PID, hostname=socket.gethostname())
        corpus_scope = self._decisions(self._corpus_report())

        self.assertEqual(plan_scope, corpus_scope)
        self.assertEqual(
            {reason for _id, _reclaimable, reason in plan_scope},
            {retention.RETAIN_PLAN_LOCKED},
        )
        self.assertTrue(self.lock_path.exists())

    def test_apply_removes_run_directories_and_never_the_lock(self) -> None:
        before = self._lock_state()
        sweepable = self._make_plan("sweepable")
        old = self._write_run(
            RUN_OLD, finished_at=_stamp(45), plan_dir=sweepable, log_bytes=1000
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=sweepable)

        report = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(before, self._lock_state())
        self.assertEqual(report.removed_run_count, 1)
        self.assertFalse(old.exists())
        self.assertTrue(
            (self.plan_state / state.RUNS_DIR_NAME / RUN_OLD).is_dir(),
            "a locked plan keeps every run under --apply",
        )


if __name__ == "__main__":
    unittest.main()
