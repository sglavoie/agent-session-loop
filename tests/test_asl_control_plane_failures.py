"""A worker's failed ASL control command reaches its turn as degradation.

ASI-169: a worker's final ``asl inbox read`` died on a ``NameError`` and the
session was accepted with ``degraded: false``, because nothing the runner reads
knew the read had failed.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    cli_trace,
    control_plane_failures,
    plans_snapshot,
    supervisor_audit_evidence_collector,
    supervisor_audit_evidence_models,
    supervisor_audit_evidence_turns,
)
from tests.support_plans import write_plan

ASL_TEST_PORTABLE = True


class WorkerControlCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.turn_dir = self.tmp / "run" / "turn-0002-session-7"
        self.turn_dir.mkdir(parents=True)
        self.log = self.turn_dir / control_plane_failures.FILE_NAME
        self.plan_dir = self.tmp / "plan"
        write_plan(self.plan_dir, ("complete", "in-progress"))
        self.state_dir = self.tmp / "state"
        patcher = mock.patch.dict(
            os.environ,
            {
                control_plane_failures.ENV_VAR: str(self.log),
                cli_trace.TRACE_ENV_VAR: "0",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv: str) -> int:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def inbox_read(self, plan: Path | None = None) -> int:
        # The shape the worker ran: a plan directory with a trailing slash.
        target = f"{self.plan_dir if plan is None else plan}/"
        return self.main("inbox", "read", target, "--state-dir", str(self.state_dir))

    def test_a_crashing_inbox_read_is_recorded_with_its_site(self) -> None:
        # The ASI-169 traceback: `inbox read` -> `resolve_plan_context` ->
        # `inspect_plan`, which raised NameError from a half-edited checkout.
        with (
            mock.patch.object(
                plans_snapshot,
                "inspect_plan",
                side_effect=NameError("name 'context_path' is not defined"),
            ),
            self.assertRaises(NameError),
        ):
            self.inbox_read()

        records = control_plane_failures.read_records(self.log)
        self.assertEqual(1, len(records), records)
        entry = records[0]
        self.assertEqual("inbox read", entry["command"])
        self.assertEqual(cli_trace.OUTCOME_EXCEPTION, entry["outcome"])
        self.assertIsNone(entry["exit_code"])
        self.assertEqual("NameError", entry["exception_type"])
        self.assertTrue(
            entry["exception_site"].startswith("cli_common.resolve_plan_context:"),
            entry["exception_site"],
        )
        # Content-free like the trace: no message, no plan path.
        text = self.log.read_text(encoding="utf-8")
        self.assertNotIn("context_path", text)
        self.assertNotIn(str(self.plan_dir), text)

        degradation = control_plane_failures.turn_degradation(self.turn_dir)
        assert degradation is not None
        self.assertEqual(control_plane_failures.MARKER, degradation["marker"])
        self.assertEqual(1, degradation["failure_count"])
        self.assertEqual(["inbox read"], degradation["commands"])

    def test_a_successful_inbox_read_records_nothing(self) -> None:
        self.assertEqual(0, self.inbox_read())

        self.assertFalse(self.log.exists())
        self.assertIsNone(control_plane_failures.turn_degradation(self.turn_dir))

    def test_an_inbox_read_that_exits_nonzero_is_a_failed_read(self) -> None:
        self.assertEqual(1, self.inbox_read(self.tmp / "missing-plan"))

        (entry,) = control_plane_failures.read_records(self.log)
        self.assertEqual(cli_trace.OUTCOME_FAILED, entry["outcome"])
        self.assertEqual(1, entry["exit_code"])
        self.assertIsNone(entry["exception_type"])

    def test_a_refused_acknowledgement_is_not_a_control_plane_failure(self) -> None:
        code = self.main(
            "inbox",
            "acknowledge",
            str(self.plan_dir),
            "--attempt",
            "run:1:1",
            "--state-dir",
            str(self.state_dir),
        )

        self.assertNotEqual(0, code)
        self.assertFalse(self.log.exists())

    def test_a_crash_outside_the_worker_commands_is_not_recorded(self) -> None:
        def exploding(_args: object) -> int:
            raise NameError("unrelated")

        with (
            mock.patch.object(cli, "resolve_handler", return_value=exploding),
            self.assertRaises(NameError),
        ):
            self.main("tail", str(self.plan_dir))

        self.assertFalse(self.log.exists())

    def test_without_the_runner_variable_nothing_is_written(self) -> None:
        os.environ.pop(control_plane_failures.ENV_VAR)

        self.assertEqual(1, self.inbox_read(self.tmp / "missing-plan"))

        self.assertEqual([], list(self.turn_dir.iterdir()))

    def test_an_unwritable_log_never_changes_the_command_outcome(self) -> None:
        os.environ[control_plane_failures.ENV_VAR] = str(
            self.tmp / "gone" / control_plane_failures.FILE_NAME
        )

        self.assertEqual(1, self.inbox_read(self.tmp / "missing-plan"))

        self.assertFalse((self.tmp / "gone").exists())

    def test_degradation_keeps_an_exact_count_and_bounded_records(self) -> None:
        total = control_plane_failures.RECORD_LIMIT + 3
        lines = [
            json.dumps({"command": "inbox read", "outcome": "failed", "seq": n})
            for n in range(total)
        ]
        self.log.write_text("\n".join([*lines, "not json", "[]"]) + "\n")

        degradation = control_plane_failures.turn_degradation(self.turn_dir)

        assert degradation is not None
        self.assertEqual(total, degradation["failure_count"])
        self.assertEqual(
            control_plane_failures.RECORD_LIMIT, len(degradation["failures"])
        )
        self.assertEqual(total - 1, degradation["failures"][-1]["seq"])


class AuditDegradedTests(unittest.TestCase):
    def turn(self, result: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            directory = root / "run" / "turn-0001-session-1"
            directory.mkdir(parents=True)
            (directory / "result.json").write_text(json.dumps(result))
            roots = supervisor_audit_evidence_models.EvidenceRoots.build(
                run_dir=root / "run", state_dir=root / "state", plan_dir=root
            )
            collector = supervisor_audit_evidence_collector._Collector(roots)
            return supervisor_audit_evidence_turns._turn_section(collector, directory)

    def test_a_failed_worker_control_command_marks_the_turn_degraded(self) -> None:
        section = self.turn(
            {
                "status_result": "complete",
                "returncode": 0,
                "control_plane_degradation": {
                    "marker": control_plane_failures.MARKER,
                    "failure_count": 1,
                },
            }
        )

        self.assertTrue(section["degraded"])

    def test_a_clean_turn_is_not_degraded(self) -> None:
        section = self.turn({"status_result": "complete", "returncode": 0})

        self.assertFalse(section["degraded"])


if __name__ == "__main__":
    unittest.main()
