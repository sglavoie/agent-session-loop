"""Workflow metrics aggregation and `asl metrics` CLI tests."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from agent_session_loop import cli, metrics, plans


def _iso(now: datetime, *, hours_ago: float) -> str:
    return (now - timedelta(hours=hours_ago)).isoformat()


def _make_state_dir(state_root: Path, plan_dir: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    state_dir = state_root / plans.path_sha256(plan_dir)
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


class MetricsCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()
        self.now = datetime.now(timezone.utc)

    def _collect(self, since_days: int = 7) -> metrics.MetricsReport:
        return metrics.collect_metrics(
            state_dir=self.state_root, since_days=since_days, now=self.now
        )

    def test_empty_state_root_reports_zero_counts(self) -> None:
        report = self._collect()
        self.assertEqual(0, report.state_dirs_scanned)
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual(0, report.agent_mux_delivery["total"])
        self.assertIsNone(report.first_pass["first_pass_rate"])
        self.assertIsNone(report.verification_repair["repair_success_rate"])

    def test_missing_state_root_reports_zero_counts(self) -> None:
        report = metrics.collect_metrics(
            state_dir=self.state_root / "absent", since_days=7, now=self.now
        )
        self.assertEqual(0, report.state_dirs_scanned)

    def test_rejects_non_positive_since_days(self) -> None:
        with self.assertRaises(ValueError):
            self._collect(since_days=0)

    def test_first_pass_completion_counts_first_turn_success(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-a")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=5),
            finished_at=_iso(self.now, hours_ago=4.9),
        )
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=2,
            session_id="2",
            started_at=_iso(self.now, hours_ago=4),
            finished_at=_iso(self.now, hours_ago=3.9),
            status_result="verify-failed",
        )
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=3,
            session_id="2",
            started_at=_iso(self.now, hours_ago=3),
            finished_at=_iso(self.now, hours_ago=2.9),
        )
        report = self._collect()
        self.assertEqual(2, report.first_pass["sessions_attempted"])
        self.assertEqual(2, report.first_pass["sessions_completed"])
        self.assertEqual(1, report.first_pass["sessions_completed_first_pass"])
        self.assertEqual(0.5, report.first_pass["first_pass_rate"])
        self.assertEqual(3, report.turns["total"])
        self.assertEqual(1, report.turns["unsuccessful"])
        self.assertEqual(60.0, report.turns["unsuccessful_runtime_seconds"])

    def test_review_turns_do_not_count_toward_first_pass(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-b")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="3",
            session_kind="review",
            started_at=_iso(self.now, hours_ago=2),
            finished_at=_iso(self.now, hours_ago=1.9),
        )
        report = self._collect()
        self.assertEqual(0, report.first_pass["sessions_attempted"])
        self.assertEqual(1, report.turns["total"])

    def test_turns_outside_window_are_excluded(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-c")
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=24 * 30),
            finished_at=_iso(self.now, hours_ago=24 * 30 - 0.1),
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=24 * 30),
            name="run_paused",
            payload={"reason": "verify-failed", "details": {}},
        )
        report = self._collect()
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])
        self.assertEqual(0, report.state_dirs_active)

    def test_pause_classification_counts_by_reason_and_retryability(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-d")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=3),
            name="run_paused",
            payload={
                "reason": "quota-exhausted",
                "details": {
                    "failure_classification": {
                        "reason": "quota-exhausted",
                        "retryable": False,
                    }
                },
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name="run_paused",
            payload={
                "reason": "transport-failed",
                "details": {
                    "failure_classification": {
                        "reason": "transport-failed",
                        "retryable": True,
                    }
                },
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=1),
            name="run_paused",
            payload={"reason": "blocked", "details": {}},
        )
        report = self._collect()
        self.assertEqual(3, report.pauses["total"])
        self.assertEqual(
            {"blocked": 1, "quota-exhausted": 1, "transport-failed": 1},
            report.pauses["by_reason"],
        )
        self.assertEqual(1, report.pauses["classified_never_retry"])
        self.assertEqual(1, report.pauses["classified_retryable"])

    def test_agent_mux_failures_are_visible_without_changing_lifecycle_metrics(
        self,
    ) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-delivery")
        for hours_ago, status, acknowledged, classification in (
            (3, "accepted", True, "none"),
            (2, "retryable_exhausted", False, "retryable_outcome"),
            (1, "legacy_unacknowledged", False, "legacy_binary"),
        ):
            _append_event(
                state_dir,
                created_at=_iso(self.now, hours_ago=hours_ago),
                name="agent_mux_delivery",
                payload={
                    "run_id": "run-1",
                    "final_status": status,
                    "acknowledged": acknowledged,
                    "error_classification": classification,
                },
            )

        report = self._collect()
        delivery = report.agent_mux_delivery
        self.assertEqual(3, delivery["total"])
        self.assertEqual(1, delivery["acknowledged"])
        self.assertEqual(1, delivery["failed"])
        self.assertEqual(1, delivery["legacy_unacknowledged"])
        self.assertEqual(
            {
                "accepted": 1,
                "legacy_unacknowledged": 1,
                "retryable_exhausted": 1,
            },
            delivery["by_status"],
        )
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])

    def test_verification_repair_success_tracks_later_completion(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-e")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=6),
            name="run_paused",
            payload={
                "reason": "verify-failed",
                "details": {"session": "2", "verification_repair_exhausted": False},
            },
        )
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=2,
            session_id="2",
            started_at=_iso(self.now, hours_ago=5),
            finished_at=_iso(self.now, hours_ago=4.9),
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=4),
            name="run_paused",
            payload={
                "reason": "verify-failed",
                "details": {"session": "3", "verification_repair_exhausted": True},
            },
        )
        report = self._collect()
        repair = report.verification_repair
        self.assertEqual(2, repair["verify_failed_pauses"])
        self.assertEqual(1, repair["repair_exhausted_pauses"])
        self.assertEqual(2, repair["sessions_with_verify_failures"])
        self.assertEqual(1, repair["sessions_repaired"])
        self.assertEqual(0.5, repair["repair_success_rate"])

    def test_review_cycles_aggregate_dispositions_and_findings(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-f")
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=4),
            name="review_cycle",
            payload={
                "disposition": "repairs-materialized",
                "introduced_findings": ["finding:aa", "finding:bb"],
                "closed_findings": [],
                "repeated_findings": [],
                "repair_session_ids": ["3.1"],
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name="review_cycle",
            payload={
                "disposition": "all-clear",
                "introduced_findings": [],
                "closed_findings": ["finding:aa", "finding:bb"],
                "repeated_findings": [],
                "repair_session_ids": [],
            },
        )
        report = self._collect()
        cycles = report.review_cycles
        self.assertEqual(2, cycles["cycles_recorded"])
        self.assertEqual(
            {"all-clear": 1, "repairs-materialized": 1}, cycles["by_disposition"]
        )
        self.assertEqual(1, cycles["repairs_materialized"])
        self.assertEqual(2, cycles["findings_introduced"])
        self.assertEqual(2, cycles["findings_closed"])
        self.assertEqual(0, cycles["findings_repeated"])

    def test_compaction_projection_covers_only_active_dirs(self) -> None:
        active_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-g")
        fat_checks = [
            {
                "name": "check-1",
                "kind": "command",
                "passed": False,
                "stdout": "x" * (64 * 1024),
                "stderr": "y" * (64 * 1024),
                "stdout_sha256": "",
                "stderr_sha256": "",
            }
        ]
        _write_turn_result(
            active_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=3),
            finished_at=_iso(self.now, hours_ago=2.9),
            extra={"verification": {"checks": fat_checks, "failures": fat_checks}},
        )
        idle_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-h")
        _write_turn_result(
            idle_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=24 * 60),
            finished_at=_iso(self.now, hours_ago=24 * 60 - 0.1),
        )
        report = self._collect()
        self.assertEqual(2, report.state_dirs_scanned)
        self.assertEqual(1, report.state_dirs_active)
        compaction = report.compaction
        self.assertEqual(1, compaction["dirs_projected"])
        self.assertEqual([], compaction["dirs_skipped"])
        self.assertGreater(compaction["projected_bytes_reclaimed"], 0)
        # Projected reclaim is net of the gzip bytes the failed-check
        # artifacts would add, and the artifact figure is surfaced.
        self.assertGreater(compaction["artifact_bytes_added"], 0)
        self.assertEqual(
            compaction["projected_bytes_reclaimed"],
            compaction["bytes_before"]
            - compaction["bytes_after"]
            - compaction["artifact_bytes_added"],
        )
        self.assertGreater(report.artifacts["total_bytes"], 0)
        self.assertGreater(report.artifacts["grown_bytes"], 0)

    def test_active_dir_without_plan_dir_is_skipped_for_compaction(self) -> None:
        state_dir = self.state_root / "orphaned"
        state_dir.mkdir()
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=1),
            finished_at=_iso(self.now, hours_ago=0.9),
        )
        report = self._collect()
        self.assertEqual(["orphaned"], report.compaction["dirs_skipped"])
        self.assertEqual(0, report.compaction["dirs_projected"])

    def test_malformed_event_lines_and_results_are_tolerated(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-i")
        (state_dir / "events.jsonl").write_text(
            "not-json\n" + json.dumps([1, 2]) + "\n", encoding="utf-8"
        )
        turn_dir = state_dir / "runs" / "run-1" / "turn-0001-session-1"
        turn_dir.mkdir(parents=True)
        (turn_dir / "result.json").write_text("{broken", encoding="utf-8")
        report = self._collect()
        self.assertEqual(0, report.turns["total"])
        self.assertEqual(0, report.pauses["total"])


class MetricsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()

    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_metrics_json_defaults_to_seven_days(self) -> None:
        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root), "--json"]
        )
        self.assertEqual(0, exit_code)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("metrics", payload["command"])
        self.assertEqual(metrics.DEFAULT_SINCE_DAYS, payload["since_days"])
        self.assertEqual(7, metrics.DEFAULT_SINCE_DAYS)
        for section in (
            "turns",
            "first_pass",
            "pauses",
            "verification_repair",
            "review_cycles",
            "artifacts",
            "compaction",
        ):
            self.assertIn(section, payload)

    def test_metrics_text_output_reports_sections(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-a")
        now = datetime.now(timezone.utc)
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(now, hours_ago=2),
            finished_at=_iso(now, hours_ago=1.9),
        )
        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root)]
        )
        self.assertEqual(0, exit_code)
        self.assertIn("First-pass completion", stdout)
        self.assertIn("Verification repair", stdout)
        self.assertIn("Review cycles", stdout)
        self.assertIn("Compaction", stdout)

    def test_metrics_rejects_invalid_since_days(self) -> None:
        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root), "--since-days", "0", "--json"]
        )
        self.assertEqual(1, exit_code)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("since-days", payload["error"])


if __name__ == "__main__":
    unittest.main()
