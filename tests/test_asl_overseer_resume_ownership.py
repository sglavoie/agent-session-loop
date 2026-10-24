"""Tests for test asl overseer resume ownership."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    intervention,
    overseer_resume,
    supervision_liveness,
    workbench_launch,
)
from tests import support_asl_overseer_resume as fixtures
from tests.support import (
    _FakePopen,
    write_plan,
)
from tests.support_asl_overseer_resume import HOST

ASL_TEST_PORTABLE = True


class SurvivingWorkers(fixtures._ResumeCase):
    """A lost supervisor is not proof the worker died."""

    def census(self, plan_dir: str | None = None) -> overseer_resume.Census:
        return overseer_resume.census(
            plan_dir=plan_dir or self.plan,
            state_dir=self.root,
            build_inventory=self.build_inventory,
            probe=self.probe,
        )

    def test_a_live_lock_beside_a_dead_helper_is_a_running_worker(self) -> None:
        self.live_lock(pid=5150)
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        observed = self.census()

        self.assertFalse(observed.clear)
        self.assertEqual("worker_running", observed.refusal)
        self.assertIn("5150", observed.detail)
        self.assertIn("lost supervisor is not proof", observed.detail)
        # And the helper really was probed dead, so this is not the probe
        # failing to run.
        self.assertEqual(
            (supervision_liveness.DEAD,),
            tuple(helper.verdict for helper in observed.helpers),
        )

    def test_a_live_helper_stops_a_second_supervisor(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.ALIVE, pid=9001)

        observed = self.census()

        self.assertEqual("supervisor_live", observed.refusal)
        self.assertIn(self.helper_id("sup-1"), observed.detail)
        self.assertIn("9001", observed.detail)

    def test_a_lock_on_another_host_is_unknown_not_dead(self) -> None:
        self.own(
            lock_present=True,
            pid=5150,
            hostname="elsewhere",
            foreign_host=True,
            alive=None,
        )

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn("elsewhere", observed.detail)

    def test_a_lock_with_no_pid_is_unknown(self) -> None:
        self.own(
            lock_present=True,
            pid=None,
            hostname=HOST,
            alive=None,
        )

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn("names no pid", observed.detail)

    def test_an_unverifiable_helper_fails_closed(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.UNVERIFIED)

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn(self.helper_id("sup-1"), observed.detail)

    def test_an_unreadable_helper_record_fails_closed(self) -> None:
        self.corrupt_helper("sup-broken")

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertEqual((self.helper_id("sup-broken"),), observed.unreadable)

    def test_a_plan_discovery_cannot_read_is_unknown(self) -> None:
        self.record_observed = False

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn("nothing is known", observed.detail)

    def test_preparation_refuses_a_plan_somebody_is_running(self) -> None:
        self.live_lock()

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "worker_running")
        self.assertIsNone(receipt)
        self.assertEqual([], self.launches)

    def test_a_running_worker_is_checked_after_the_launch_is_recovered(self) -> None:
        # Ordering matters for the operator: a busy plan whose metadata is also
        # gone should say the thing a human has to act on.
        self.live_lock()
        self.write_run_config(omit=True)

        preparation, _ = self.dispatch()

        self.assertRefused(preparation.detail, "environment_unrecoverable")


class LostHelpers(fixtures._ResumeCase):
    """A helper that is provably gone clears out of the way, and only then."""

    def census(self) -> overseer_resume.Census:
        return overseer_resume.census(
            plan_dir=self.plan,
            state_dir=self.root,
            build_inventory=self.build_inventory,
            probe=self.probe,
        )

    def test_a_dead_helper_beside_no_lock_clears_the_plan(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        observed = self.census()

        self.assertTrue(observed.clear)
        self.assertIsNone(observed.refusal)
        self.assertEqual(
            (self.helper_id("sup-1"),),
            tuple(helper.supervision_id for helper in observed.helpers),
        )

    def test_a_dead_helper_beside_a_dead_lock_clears_the_plan(self) -> None:
        self.dead_lock()
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        self.assertTrue(self.census().clear)

    def test_no_helper_record_at_all_proves_nothing_either_way(self) -> None:
        observed = self.census()

        self.assertTrue(observed.clear)
        self.assertEqual((), observed.helpers)
        self.assertEqual((), observed.unreadable)

    def test_a_lost_helper_lets_the_resume_through(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)

    def test_one_live_helper_among_dead_ones_still_stops_the_resume(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD, pid=9001)
        self.write_helper("sup-2", verdict=supervision_liveness.ALIVE, pid=9002)

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "supervisor_live")
        self.assertIn(self.helper_id("sup-2"), preparation.detail)
        self.assertIsNone(receipt)


class CheckpointThenResume(fixtures._ResumeCase):
    """The sequence this operation exists to complete."""

    def test_a_checkpointed_run_resumes_under_its_own_launch(self) -> None:
        # What a checkpoint leaves behind: a settled commit on this incident.
        commit = self.ledger.attempt_action(
            grant=self.grant(),
            plan_dir=self.plan,
            operation="commit",
            target="checkpoint",
            incident_id=self.incident_id,
        )
        self.ledger.confirm_action(
            action_id=commit.action_id,
            outcome="executed",
            locator="0" * 40,
        )

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)
        self.assertEqual("pid 4242", receipt.locator)
        self.assertIn("the run has moved since", receipt.detail)
        self.assertEqual(1, len(self.launches))
        self.assertEqual("codex", self.launches[0].profile["harness"])

    def test_a_run_that_is_not_paused_has_nothing_to_resume(self) -> None:
        self.runner_state["paused"] = False

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "not_paused")
        self.assertIsNone(receipt)

    def test_a_resume_does_not_choose_which_session_to_stop_on(self) -> None:
        preparation, receipt = self.dispatch(action=self.action(session_id="9"))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "session_mismatch")
        self.assertIn("'15'", preparation.detail)
        self.assertIsNone(receipt)

    def test_naming_the_session_the_run_stopped_on_is_allowed(self) -> None:
        preparation, receipt = self.dispatch(action=self.action(session_id="15"))

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)

    def test_a_run_that_cannot_be_read_is_never_read_as_gone(self) -> None:
        self.readable = False

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "run_unreadable")
        self.assertIsNone(receipt)


class NoInventedReply(fixtures._ResumeCase):
    """The absence of a reply is a property of the spawned process."""

    def launchable_plan(self) -> Path:
        directory = self.root / "launchable"
        write_plan(directory, ("pending", "pending"))
        from agent_session_loop import plans_files

        return plans_files.canonicalize_path(directory)

    def spawn(self, **kwargs: Any) -> tuple[Any, list[_FakePopen]]:
        captured: list[_FakePopen] = []

        def factory(argv: Any, **popen_kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **popen_kwargs)
            captured.append(process)
            return process

        result = intervention.resume_recovered_run(
            self.launchable_plan(), popen_factory=factory, **kwargs
        )
        return result, captured

    def test_the_recovery_resume_opens_no_channel_for_a_reply(self) -> None:
        result, captured = self.spawn()

        self.assertEqual(1, len(captured))
        spawned = captured[0]
        self.assertIn("--resume", spawned.argv)
        self.assertNotIn("--reply-stdin", spawned.argv)
        self.assertNotIn("--reply", spawned.argv)
        self.assertFalse(result.reply_provided)
        self.assertIsNone(result.reply_origin)
        self.assertIsNone(result.reply_length)
        # Nothing was written to the child, because there was nothing to write
        # and no pipe to write it down.
        self.assertFalse(spawned.stdin_text)

    def test_the_restored_profile_becomes_flags_on_the_child(self) -> None:
        _, captured = self.spawn(
            profile_fields={
                "harness": "codex",
                "harness_model": "gpt-5-codex",
                "effort": "high",
                "commit_mode": "per-session",
            }
        )

        argv = captured[0].argv
        self.assertIn("--harness", argv)
        self.assertIn("gpt-5-codex", argv)
        self.assertIn("high", argv)
        self.assertIn("--commit-per-session", argv)

    def test_a_field_the_launcher_does_not_have_is_refused(self) -> None:
        with self.assertRaises(intervention.InterventionError) as caught:
            self.spawn(profile_fields={"max_sessions": 99})

        self.assertEqual(
            intervention.REASON_UNKNOWN_PROFILE_FIELD, caught.exception.reason
        )
        self.assertIn("max_sessions", str(caught.exception))

    def test_the_reply_oriented_service_is_untouched(self) -> None:
        # This path is an addition beside the manual interface, so the manual
        # interface must still refuse a reply that says nothing.
        captured: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            captured.append(process)
            return process

        with self.assertRaises(workbench_launch.LaunchError):
            workbench_launch.launch_plan(
                self.launchable_plan(),
                reply_text="   \n ",
                popen_factory=factory,
                resume=True,
            )
        self.assertEqual([], captured)

    def test_a_reply_still_travels_by_stdin_and_never_by_argv(self) -> None:
        captured: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            captured.append(process)
            return process

        reply = "the answer is option B"
        result = workbench_launch.launch_plan(
            self.launchable_plan(),
            reply_text=reply,
            popen_factory=factory,
            resume=True,
        )

        self.assertTrue(result.reply_provided)
        self.assertIn("--reply-stdin", captured[0].argv)
        self.assertEqual(reply, captured[0].stdin_text)


if __name__ == "__main__":
    unittest.main()
