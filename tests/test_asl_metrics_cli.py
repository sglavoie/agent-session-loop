"""`asl metrics` CLI tests."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    cli,
    cli_trace,
    metrics,
)
from tests.asl_metrics_support import (
    _iso,
    _make_state_dir,
    _publish_audit,
    _trace_record,
    _write_turn_result,
)

ASL_TEST_PORTABLE = True


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
            "assurance",
        ):
            self.assertIn(section, payload)

    def test_metrics_text_output_reports_sections(self) -> None:
        state_dir = _make_state_dir(self.state_root, self.tmp_path / "plan-a")
        now = datetime.now(UTC)
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
        self.assertIn("unique materializations", stdout)
        self.assertIn("reopen attempts", stdout)
        self.assertIn("Compaction", stdout)
        self.assertIn("Supervisor audit:", stdout)
        self.assertIn("recurrence after resolution", stdout)

    def test_metrics_text_reports_what_the_audit_found(self) -> None:
        now = datetime.now(UTC)
        _publish_audit(self.state_root, recorded_at=_iso(now, hours_ago=2))

        exit_code, stdout, _ = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root)]
        )

        self.assertEqual(0, exit_code)
        self.assertIn("states all-clear 0, findings 1", stdout)
        self.assertIn("1 sighting(s), 1 new, 0 recurring, 1 open now", stdout)

    def test_metrics_rejects_invalid_since_days(self) -> None:
        exit_code, stdout, _ = self._run_cli(
            [
                "metrics",
                "--state-dir",
                str(self.state_root),
                "--since-days",
                "0",
                "--json",
            ]
        )
        self.assertEqual(1, exit_code)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("since-days", payload["error"])


class CliInvocationsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir()

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = cli.main(argv)
        return exit_code, stdout.getvalue()

    def _seed_failure(self) -> None:
        now = datetime.now(UTC)
        start = _trace_record(
            event=cli_trace.EVENT_START,
            invocation_id="seeded",
            timestamp=_iso(now, hours_ago=1),
            command_path=["run"],
            origin=cli_trace.ORIGIN_WORKBENCH,
        )
        end = dict(start)
        end.update(
            {
                "event": cli_trace.EVENT_END,
                "timestamp": _iso(now, hours_ago=0.9),
                "exit_code": 1,
                "outcome": cli_trace.OUTCOME_FAILED,
                "duration_ms": 1500.0,
                "exception_type": None,
            }
        )
        path = self.state_root / cli_trace.trace_file_name(now.date())
        path.write_text(
            "\n".join(json.dumps(record, sort_keys=True) for record in (start, end))
            + "\n",
            encoding="utf-8",
        )

    def test_json_carries_cli_invocations_beside_the_existing_sections(self) -> None:
        self._seed_failure()

        exit_code, stdout = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root), "--json"]
        )

        self.assertEqual(0, exit_code)
        payload = json.loads(stdout)
        for section in (
            "turns",
            "first_pass",
            "pauses",
            "verification_repair",
            "review_cycles",
            "agent_mux_delivery",
            "artifacts",
            "compaction",
            "assurance",
            "cli_invocations",
        ):
            self.assertIn(section, payload)
        invocations = payload["cli_invocations"]
        self.assertEqual(payload["state_root"], invocations["trace_root"])
        self.assertEqual(cli_trace.RETENTION_DAYS, invocations["retention_days"])
        self.assertEqual(1, invocations["nonzero_exits"])
        self.assertEqual({cli_trace.ORIGIN_WORKBENCH: 1}, invocations["by_origin"])
        self.assertEqual(
            ["seeded"],
            [entry["invocation_id"] for entry in invocations["recent_failures"]],
        )

    def test_text_output_adds_a_cli_invocations_section(self) -> None:
        self._seed_failure()

        exit_code, stdout = self._run_cli(
            ["metrics", "--state-dir", str(self.state_root)]
        )

        self.assertEqual(0, exit_code)
        self.assertIn("CLI invocations:", stdout)
        self.assertIn(f"retention {cli_trace.RETENTION_DAYS} day(s)", stdout)
        self.assertIn("unmatched starts 0", stdout)
        self.assertIn("nonzero exits 1", stdout)
        self.assertIn("by origin: workbench 1", stdout)
        self.assertIn("recent failures (1):", stdout)
        # Existing sections stay put: the block is additive, not a replacement.
        self.assertIn("First-pass completion", stdout)
        self.assertIn("Supervisor audit:", stdout)

    def test_metrics_excludes_its_own_traced_invocation(self) -> None:
        self._seed_failure()

        with mock.patch.dict(os.environ, {cli_trace.TRACE_ENV_VAR: "1"}):
            exit_code, stdout = self._run_cli(
                ["metrics", "--state-dir", str(self.state_root), "--json"]
            )

        self.assertEqual(0, exit_code)
        invocations = json.loads(stdout)["cli_invocations"]
        self.assertEqual(0, invocations["unmatched_starts"])
        self.assertEqual({"run": 1}, invocations["by_command"])
        written = cli_trace.read_corpus(self.state_root).records
        self.assertIn(
            ["metrics"],
            [record["command_path"] for record in written],
        )


if __name__ == "__main__":
    unittest.main()
