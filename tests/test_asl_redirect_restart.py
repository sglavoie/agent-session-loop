"""Restart gates exercise real relaunches, not just recovery report contents."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    durability,
    redirect_restart,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_requests,
    state,
    state_locks,
)
from tests import asl_redirect_continuation_support as t
from tests import support
from tests import test_asl_lost_submission_response as lost_tests
from tests.asl_redirect_run_support import _RedirectedRun

ASL_TEST_LANE = "integration"


class _LivePredecessor(_RedirectedRun):
    def _controller_factory(self):
        build = super()._controller_factory()

        def wrapped(**kwargs):
            controller = build(**kwargs)

            def confirm(**kw):
                live = self.orphan.poll() is None
                return {
                    "pgid": kw["pgid"],
                    "complete": not live,
                    "incomplete_reason": "remaining-processes" if live else None,
                }

            controller._tools = dataclasses.replace(controller._tools, confirm=confirm)
            self.controllers.append(controller)
            return controller

        return wrapped


def _lost_case():
    class LostThenRecovered(lost_tests.LostSubmissionResponseRunTests):
        def _fake_worker(self, name, **kwargs):
            if len(self.harness_calls) < 2:
                return super()._fake_worker(name, **kwargs)
            self.prompts.append(str(kwargs.get("prompt") or ""))
            return _RedirectedRun._fake_worker(self, name, **kwargs)

    return LostThenRecovered()


class RestartTests(unittest.TestCase):
    def test_a_live_predecessor_blocks_relaunch_until_proven_stopped(self):
        case = _LivePredecessor()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.controllers = []
        orphan = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(120)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        case.orphan = orphan
        try:
            with mock.patch.object(t, "WORKER_PID", orphan.pid):
                first = case.run_loop_once(expect_exit_code=1)
                self.assertEqual("redirect-successor-blocked", first["reason"])
                self.assertIsNone(orphan.poll())
                second = case.run_loop_once(
                    run_id="live-2", prepare_plan=False, expect_exit_code=1
                )
                self.assertEqual(1, len(case.harness_calls))
                self.assertEqual("pending", case._session_status())
                self.assertTrue(second["paused"])
                orphan.terminate()
                orphan.wait(timeout=5)
                third = case.run_loop_once(run_id="stopped-3", prepare_plan=False)
                self.assertEqual(2, len(case.harness_calls))
                self.assertFalse(third["paused"])
                self.assertEqual("complete", case._session_status())
        finally:
            if orphan.poll() is None:
                orphan.kill()
            orphan.wait(timeout=5)
            for controller in case.controllers:
                controller.release()
                controller.join(2)
                self.assertFalse(
                    any(thread.is_alive() for thread in controller._threads)
                )

    def test_uncertain_submission_requires_an_explicit_decision_before_relaunch(self):
        case = _lost_case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.run_loop_once(expect_exit_code=1)
        payload = case.run_loop_once(
            run_id="unresolved-2", prepare_plan=False, expect_exit_code=1
        )
        self.assertEqual("redirect-submission-unresolved", payload["reason"])
        self.assertEqual(2, len(case.harness_calls))
        self.assertEqual("pending", case._session_status())
        self.assertTrue(case._request().launch_ambiguous)
        redirect_restart.resolve_submission(
            case.plan_dir,
            "op-1",
            outcome="submitted",
            reason="Operator confirmed native receipt",
            state_dir=case.state_dir,
        )
        case.run_loop_once(run_id="resolved-3", prepare_plan=False)
        self.assertEqual(3, len(case.harness_calls))
        self.assertFalse(case._request().acknowledged)
        self.assertNotIn(t.FEEDBACK, case.prompts[-1])

    def test_unsent_resolution_releases_feedback_for_recovery(self):
        case = _lost_case()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.run_loop_once(expect_exit_code=1)
        redirect_restart.resolve_submission(
            case.plan_dir,
            "op-1",
            outcome="not-submitted",
            reason="Operator inspected native history",
            state_dir=case.state_dir,
        )
        self.assertFalse(case._request().launch_ambiguous)
        self.assertTrue(case._request().released)
        case.run_loop_once(run_id="unsent-2", prepare_plan=False)
        self.assertEqual(3, len(case.harness_calls))
        self.assertTrue(case._request().delivered)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def test_missing_foreign_disabled_and_malformed_evidence_never_prove_stop(self):
        self.assertIsNotNone(redirect_restart.predecessor_blocker(self.directory))
        for evidence in (
            {},
            {"collected": False},
            {"collected": True, "disabled": "opt-out"},
        ):
            redirect_restart.write_evidence(
                self.directory, {"pid": 999999, "pgid": 999999}, evidence
            )
            self.assertIsNotNone(redirect_restart.predecessor_blocker(self.directory))
        durability.write_json_file(
            self.directory / "evidence.json",
            {
                "host": socket.gethostname() + "-foreign",
                "worker": {"pid": 999999, "pgid": 999999},
                "evidence": {"root_pid": 999999, "collected": True, "entries": []},
            },
        )
        self.assertIsNotNone(redirect_restart.predecessor_blocker(self.directory))

    def test_unknown_descendant_identity_blocks_even_after_the_group_stops(self):
        redirect_restart.write_evidence(
            self.directory,
            {"pid": 999999, "pgid": 999999},
            {
                "root_pid": 999999,
                "collected": True,
                "entries": [{"pid": os.getpid(), "birth": None}],
            },
        )
        self.assertIsNotNone(redirect_restart.predecessor_blocker(self.directory))

    def test_sweep_opt_out_blocks_an_otherwise_stopped_predecessor(self):
        redirect_restart.write_evidence(
            self.directory,
            {"pid": 999999, "pgid": 999999},
            {
                "root_pid": 999999,
                "collected": True,
                "entries": [],
            },
        )
        with mock.patch.dict(os.environ, {"ASL_PROCESS_SWEEP_DISABLED": "1"}):
            self.assertIsNotNone(redirect_restart.predecessor_blocker(self.directory))
        with mock.patch.dict(os.environ, {"ASL_PROCESS_SWEEP_DISABLED": "0"}):
            self.assertIsNone(redirect_restart.predecessor_blocker(self.directory))

    def test_reused_descendant_pid_does_not_count_as_a_survivor(self):
        redirect_restart.write_evidence(
            self.directory,
            {"pid": 999999, "pgid": 999999},
            {
                "root_pid": 999999,
                "collected": True,
                "entries": [
                    {
                        "pid": os.getpid(),
                        "birth": "Mon Jan 1 00:00:00 1900",
                        "command": "private feedback",
                    }
                ],
            },
        )
        self.assertNotIn(
            "private feedback", (self.directory / "evidence.json").read_text()
        )
        self.assertIsNone(redirect_restart.predecessor_blocker(self.directory))
        # A cleared identity cannot be revived by later reuse of its numbers.
        with mock.patch.object(
            redirect_restart.process_tree,
            "confirm_termination",
            side_effect=AssertionError("must not re-probe a cleared identity"),
        ):
            self.assertIsNone(redirect_restart.predecessor_blocker(self.directory))

    def test_predecessor_key_uses_turn_identity_not_optional_harness_metadata(self):
        self.assertEqual(
            redirect_restart.evidence_dir(self.directory, t.target()),
            redirect_restart.evidence_dir(self.directory, t.target(harness=None)),
        )

    def test_resolution_cli_requires_outcome_and_reason(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["control", "resolve-redirect", "/tmp/plan", "--operation-id", "op"]
            )


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.plan = Path(temp.name) / "plan"
        self.state_dir = Path(temp.name) / "state"
        support.write_plan(self.plan, ("pending",))
        redirect_store_requests.submit_request(
            self.plan,
            target=t.target(),
            feedback=t.FEEDBACK,
            operation_id="op",
            state_dir=self.state_dir,
        )
        redirect_store_requests.accept_request(
            self.plan, "op", target=t.target(), state_dir=self.state_dir
        )
        redirect_store_carry.carry_request(
            self.plan, "op", continuation=t.target(2), state_dir=self.state_dir
        )
        redirect_store_carry.launch_request(
            self.plan, "op", continuation=t.target(2), state_dir=self.state_dir
        )
        redirect_restart.mark_stopped(
            redirect_restart.evidence_dir(self.plan, t.target(), self.state_dir)
        )

    def resolve(self, outcome):
        return redirect_restart.resolve_submission(
            self.plan,
            "op",
            outcome=outcome,
            reason="explicit test decision",
            state_dir=self.state_dir,
        )

    def test_live_runner_excludes_resolution(self):
        state_locks.acquire_lock(self.plan, self.state_dir)
        try:
            with self.assertRaises(state.StateError):
                self.resolve("submitted")
        finally:
            state_locks.release_lock(self.plan, self.state_dir)

    def test_a_decision_is_idempotent_but_cannot_be_contradicted(self):
        for outcome in ("submitted",):
            first = self.resolve(outcome)
            self.assertEqual(first, self.resolve(outcome))
            with self.assertRaises(ValueError):
                self.resolve("not-submitted")

    def test_release_retry_after_a_lost_response_is_idempotent(self):
        first = self.resolve("not-submitted")
        self.assertEqual(first, self.resolve("not-submitted"))
        with self.assertRaises(ValueError):
            self.resolve("submitted")

    def test_decision_survives_a_crash_before_release(self):
        with (
            mock.patch.object(
                redirect_store_delivery,
                "release_request",
                side_effect=OSError("interrupted"),
            ),
            self.assertRaises(OSError),
        ):
            self.resolve("not-submitted")
        self.assertIsNone(
            redirect_restart.restart_blocker(
                self.plan, pending_session_ids=("1",), state_dir=self.state_dir
            )
        )
        request = redirect_store_journal.read_journal(
            self.plan, state_dir=self.state_dir
        ).request("op")
        self.assertTrue(request.released)

    def test_a_previous_decision_does_not_resolve_a_new_launch_window(self):
        self.resolve("not-submitted")
        redirect_store_carry.carry_request(
            self.plan, "op", continuation=t.target(3), state_dir=self.state_dir
        )
        redirect_store_carry.launch_request(
            self.plan, "op", continuation=t.target(3), state_dir=self.state_dir
        )
        blocked = redirect_restart.restart_blocker(
            self.plan, pending_session_ids=("1",), state_dir=self.state_dir
        )
        self.assertEqual("redirect-submission-unresolved", blocked["reason"])

    def test_recorded_delivery_cannot_be_replayed_as_unsent(self):
        redirect_store_delivery.deliver_request(
            self.plan, "op", continuation=t.target(2), state_dir=self.state_dir
        )
        with self.assertRaises(ValueError):
            self.resolve("not-submitted")

    def test_cli_records_the_explicit_decision_and_status_exposes_it(self):
        import contextlib
        import io

        from agent_session_loop import redirect_cli

        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "control",
                "resolve-redirect",
                str(self.plan),
                "--operation-id",
                "op",
                "--outcome",
                "submitted",
                "--reason",
                "verified",
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, redirect_cli.control_resolve_redirect_from_args(args))
        self.assertEqual(
            "submitted", json.loads(output.getvalue())["resolution"]["outcome"]
        )
        args = parser.parse_args(
            [
                "control",
                "redirect-status",
                str(self.plan),
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, redirect_cli.control_redirect_status_from_args(args))
        operation = json.loads(output.getvalue())["operations"][0]
        self.assertEqual("submitted", operation["operator_resolution"]["outcome"])
        self.assertNotEqual("acknowledged", operation["stage"])
