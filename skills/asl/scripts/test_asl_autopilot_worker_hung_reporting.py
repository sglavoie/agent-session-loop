#!/usr/bin/env python3
"""Tests for worker-hung reporting and supervision liveness records."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from asl_autopilot_test_payloads import _hang_block
from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    setUpModule,
    supervision_liveness,
    tearDownModule,
)


class RunAutopilotWorkerHungTest(unittest.TestCase):
    """`run_autopilot` passes a `worker_hung` stop through untouched."""

    def _args(self, command="asl run /tmp/plan --review-harness codex"):
        parser = autopilot.build_arg_parser()
        return parser.parse_args(["--command", command])

    def test_worker_hung_skips_questions_and_review(self) -> None:
        hang = {
            "class": autopilot.HANG_CLASS_ZERO_CPU,
            "idle_seconds": 8412.0,
            "next_steps": list(autopilot.HANG_NEXT_STEPS),
        }

        def hung_supervisor(parsed):
            return {
                "ok": False,
                "stage": autopilot.RUN_STAGE,
                "reason": "worker_hung",
                "chunks": 1,
                "hang": hang,
            }

        def fail_questioner(parsed):  # pragma: no cover - must not run
            raise AssertionError("asl questions must not be fetched for worker_hung")

        def fail_review_reader(parsed):  # pragma: no cover - must not run
            raise AssertionError("no review verdict is read for worker_hung")

        result = autopilot.run_autopilot(
            self._args(),
            supervisor=hung_supervisor,
            questioner=fail_questioner,
            review_reader=fail_review_reader,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"], hang)
        self.assertNotIn("questions", result)
        self.assertNotIn("questions_error", result)
        self.assertNotIn("review", result)


class HumanSummaryWorkerHungTest(unittest.TestCase):
    """The one-line summary names the class, idle time, and hung leaf."""

    def test_summary_names_class_idle_and_leaf_command(self) -> None:
        tree = [
            {
                "pid": 4243,
                "ppid": 4242,
                "state": "S",
                "cpu_seconds": 12.0,
                "command": "node /usr/local/bin/harness",
            },
            {
                "pid": 4244,
                "ppid": 4243,
                "state": "S",
                "cpu_seconds": 0.2,
                "command": "/bin/zsh -c git diff | diff-so-fancy",
            },
            {
                "pid": 4245,
                "ppid": 4244,
                "state": "S+",
                "cpu_seconds": 0.0,
                "command": "perl /usr/local/bin/diff-so-fancy",
            },
        ]
        result = {
            "ok": False,
            "reason": "worker_hung",
            "hang": {
                "class": autopilot.HANG_CLASS_ZERO_CPU,
                "idle_seconds": 8412.0,
                "hang_suspected": _hang_block(
                    autopilot.HANG_CLASS_ZERO_CPU, idle=8412.0, process_tree=tree
                ),
            },
        }
        summary = autopilot._human_summary(result)
        self.assertIn("worker_hung", summary)
        self.assertIn(autopilot.HANG_CLASS_ZERO_CPU, summary)
        self.assertIn("idle 8412s", summary)
        self.assertIn("perl /usr/local/bin/diff-so-fancy", summary)
        self.assertIn("inspect before killing", summary)
        self.assertIn("re-run the same command to re-attach", summary)

    def test_summary_survives_a_missing_hang_payload(self) -> None:
        summary = autopilot._human_summary({"ok": False, "reason": "worker_hung"})
        self.assertIn("worker_hung", summary)
        self.assertIn("idle unknown", summary)
        self.assertNotIn("deepest descendant", summary)

    def test_abort_summary_names_the_reverted_session(self) -> None:
        # An aborted run reports zero completed sessions on purpose; the line has
        # to say the session went back to pending or the counts read as a stall.
        summary = autopilot._human_summary(
            {
                "ok": True,
                "reason": "stopped",
                "stop_kind": autopilot.STOP_KIND_ABORT,
                "completed": 2,
                "pending": 3,
            }
        )
        self.assertTrue(summary.startswith("ok: stopped - "), summary)
        self.assertIn("supervisor abort ended the in-flight turn", summary)
        self.assertIn("returned its session to pending", summary)
        self.assertIn("completed 2 session(s), 3 still pending", summary)
        self.assertIn("re-run the same command to continue", summary)

    def test_boundary_stop_summary_is_unchanged_by_the_abort_branch(self) -> None:
        for stop_kind in (autopilot.STOP_KIND_AFTER_CURRENT, None):
            with self.subTest(stop_kind=stop_kind):
                result = {
                    "ok": True,
                    "reason": "stopped",
                    "completed": 2,
                    "pending": 3,
                }
                if stop_kind is not None:
                    result["stop_kind"] = stop_kind
                summary = autopilot._human_summary(result)
                self.assertIn("graceful stop after the current session", summary)
                self.assertNotIn("abort", summary)


class SupervisionLivenessRecordTest(unittest.TestCase):
    """The helper leaves probe-able evidence before it launches anything."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-liveness-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan_dir = os.path.join(self.tmp, "plan")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.plan_dir)
        self.supervision_id = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    def _patch(self, owner, name: str, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _args(self, *extra: str):
        parser = autopilot.build_arg_parser()
        command = f"asl run {self.plan_dir} --state-dir {self.state_dir}"
        return parser.parse_args(
            [
                "--command",
                " ".join([command, *extra]),
                "--supervision-id",
                self.supervision_id,
            ]
        )

    def _read_record(self):
        return supervision_liveness.read_record(
            self.plan_dir, self.supervision_id, state_dir=self.state_dir
        )

    def _complete(self, parsed):
        return {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "chunks": 1,
        }

    def test_record_lands_before_the_run_is_invoked(self) -> None:
        seen = {}

        def fake_supervisor(parsed):
            seen["record"] = self._read_record()
            return self._complete(parsed)

        result = autopilot.run_autopilot(self._args(), supervisor=fake_supervisor)

        record = seen["record"]
        self.assertIsNotNone(record, "no liveness record existed when the run started")
        self.assertEqual(os.getpid(), record.pid)
        self.assertEqual(self.supervision_id, record.supervision_id)
        self.assertEqual(tuple(autopilot.sys.argv), record.argv)
        self.assertEqual(self.plan_dir, record.plan_dir)
        self.assertTrue(record.command["ok"])
        self.assertEqual(
            {
                "written": True,
                "supervision_id": self.supervision_id,
                "path": str(
                    supervision_liveness.record_path_for_plan(
                        self.plan_dir, self.supervision_id, self.state_dir
                    )
                ),
            },
            result["supervision_liveness"],
        )

    def test_no_mailbox_run_still_leaves_a_record(self) -> None:
        seen = {}

        def fake_supervisor(parsed):
            seen["no_mailbox"] = parsed["no_mailbox"]
            return self._complete(parsed)

        autopilot.run_autopilot(self._args("--no-mailbox"), supervisor=fake_supervisor)

        self.assertTrue(seen["no_mailbox"])
        self.assertIsNotNone(self._read_record())

    def test_failed_write_is_reported_without_aborting_supervision(self) -> None:
        supervised = []

        def failing_write(*args, **kwargs):
            raise OSError("read-only state dir")

        def fake_supervisor(parsed):
            supervised.append(parsed)
            return self._complete(parsed)

        self._patch(autopilot._supervision_liveness, "write_record", failing_write)
        result = autopilot.run_autopilot(self._args(), supervisor=fake_supervisor)

        self.assertEqual(1, len(supervised))
        self.assertTrue(result["ok"])
        self.assertFalse(result["supervision_liveness"]["written"])
        self.assertIn("read-only state dir", result["supervision_liveness"]["error"])
        self.assertIsNone(self._read_record())

    def test_generated_launch_identity_reaches_the_record(self) -> None:
        real_run_autopilot = autopilot.run_autopilot
        seen = {}

        def supervision(args):
            seen["supervision_id"] = args.supervision_id
            return real_run_autopilot(args, supervisor=self._complete)

        self._patch(autopilot, "run_autopilot", supervision)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            autopilot.main(
                [
                    "--command",
                    f"asl run {self.plan_dir} --state-dir {self.state_dir}",
                    "--json",
                ]
            )

        payload = json.loads(buffer.getvalue())
        # One launch identity: the record, the result, and the terminal
        # `supervision-end` record all name the id `main()` generated.
        self.assertEqual(payload["supervision_id"], seen["supervision_id"])
        self.assertEqual(
            payload["supervision_id"],
            payload["supervision_liveness"]["supervision_id"],
        )
        self.assertIsNotNone(
            supervision_liveness.read_record(
                self.plan_dir,
                payload["supervision_id"],
                state_dir=self.state_dir,
            )
        )


if __name__ == "__main__":
    unittest.main()
