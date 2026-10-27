"""Test asl pause ux recovery."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    intervention,
    mailbox,
    mailbox_appenders,
    overseer_resume,
    pause_policy,
    runner,
    state,
    turn_execution_constants,
)
from tests import support_asl_pause_ux as fixtures
from tests.support import (
    init_git_repo,
    read_jsonl,
    write_plan,
)

ASL_TEST_LANE = "integration"


class ArtifactSafetyAndPauseUxTests(fixtures.ArtifactSafetyAndPauseUxCase):
    def test_worker_self_pause_inside_abort_grace_window_is_not_a_kill(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            init_git_repo(tmp_dir)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending", "pending"))
            # Creating the mailbox file is what activates the protocol, so seed
            # it with an already-answered record rather than pending guidance.
            seeded = mailbox_appenders.append_guidance(
                plan_dir, "seeded", state_dir=state_dir
            )
            mailbox_appenders.append_acknowledgement(
                plan_dir, seeded.seq, state_dir=state_dir
            )
            observed_guidance: list[str] = []

            def fake_run_harness(name, **kwargs):
                control_queue.append_command(
                    plan_dir,
                    control_queue.COMMAND_REQUEST_ABORT_TURN,
                    state_dir=state_dir,
                    grace_seconds=60,
                    reason="operator needs the machine",
                    command_id="abort-1",
                )
                abort = kwargs["abort"]
                deadline = time.monotonic() + 10.0
                while not observed_guidance and time.monotonic() < deadline:
                    observed_guidance.extend(
                        message.text
                        for message in mailbox.pending_guidance(
                            plan_dir, state_dir=state_dir
                        )
                        if "Supervisor abort requested" in message.text
                    )
                    if not observed_guidance:
                        time.sleep(0.01)
                self.assertTrue(observed_guidance, "abort guidance never arrived")
                self.assertTrue(abort.armed)
                # The worker complies: it winds down, leaves the session
                # pending, and pauses the run itself well inside the window.
                last_message = "RUNNER_PAUSE: supervisor requested abort\n"
                last_message_path = kwargs.get("last_message_path")
                if last_message_path is not None:
                    Path(last_message_path).write_text(last_message, encoding="utf-8")
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, "--prompt", str(kwargs.get("prompt", ""))],
                    returncode=0,
                    stdout=last_message,
                    stderr="",
                    last_message=last_message,
                )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_dir),
                harness="codex",
                review_harness=None,
                state_dir=str(state_dir),
                json=True,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
                reply=None,
            )

            with (
                mock.patch.object(
                    turn_execution_constants, "LIVE_CONTROL_POLL_INTERVAL_SECONDS", 0.02
                ),
                mock.patch.object(
                    harnesses, "run_harness", side_effect=fake_run_harness
                ),
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)

            self.assertEqual(1, code, out.getvalue())
            payload = json.loads(out.getvalue())
            # The existing runner-pause path owns the outcome: no kill, no
            # revert note, and no abort-specific pause reason.
            self.assertTrue(payload["paused"])
            self.assertEqual("runner-pause", payload["pause_reason"])
            self.assertEqual(1, len(payload["turns"]))
            self.assertEqual(0, payload["turns"][0]["returncode"])
            self.assertFalse(payload["turns"][0].get("aborted", False))
            self.assertEqual(1, payload["final_snapshot"]["next_pending"])
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertNotIn("turn aborted by supervisor request", session_text)

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            actions = [
                record["payload"]["action"]
                for record in read_jsonl(plan_state / events.EVENTS_FILE_NAME)
                if record["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertEqual(
                [
                    events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                    events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                    events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
                ],
                actions,
            )
            self.assertFalse(payload["control"]["abort_turn_requested"])
            self.assertIn("operator needs the machine", observed_guidance[0])


class PauseRecoveryPathsTests(unittest.TestCase):
    """Which pauses an operator answers, and which ones they repair instead.

    ``pause_policy`` decides what a stopped run needs from a human, and
    ``overseer_resume`` is the only caller that acts on the difference without
    a human present. Reading both here keeps the two honest about each other:
    a reason added to one of the inspect tuples must not quietly become a
    resume that invents an answer, and a reply-shaped pause must not quietly
    become one an overseer can restart around.
    """

    def _facts(
        self, reason: str, *, unanswered: tuple[str, ...] = ()
    ) -> intervention.RunFacts:
        return intervention.build_run_facts(
            Path("/plans/plan-a"),
            runner_state={
                "present": True,
                "run_id": "run-1",
                "paused": True,
                "reason": reason,
                "pause_policy": pause_policy.classify_pause(reason),
            },
            lock_active=False,
            unanswered_ids=unanswered,
        )

    def test_every_inspect_pause_is_recoverable_without_an_answer(self) -> None:
        # These are the pauses a human fixes the world for -- a quota, an auth
        # failure, a dirty worktree. Once the world is fixed there is nothing
        # for anybody to say, so a resume that carries no reply is the right
        # instrument and refusing it would strand the run.
        for reason in pause_policy.INSPECT_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT,
                    pause_policy.classify_pause(reason)["user_action"],
                )
                self.assertIsNone(
                    overseer_resume.recovery_refusal(
                        self._facts(reason, unanswered=("q-1",))
                    )
                )

    def test_every_recoverable_pause_is_recoverable_without_an_answer(self) -> None:
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.assertIsNone(overseer_resume.recovery_refusal(self._facts(reason)))

    def test_a_reply_shaped_pause_with_an_open_question_stays_the_operators(
        self,
    ) -> None:
        facts = self._facts("awaiting-reply", unanswered=("q-1",))

        self.assertEqual(
            pause_policy.USER_ACTION_REPLY,
            pause_policy.classify_pause("awaiting-reply")["user_action"],
        )
        refusal = overseer_resume.recovery_refusal(facts)
        assert refusal is not None
        self.assertEqual("answer_required", refusal[0])

    def test_a_reply_shaped_pause_whose_question_closed_is_recoverable(self) -> None:
        # The question was answered or superseded while the run sat paused.
        # Nothing is waiting on words now, so recovery does not have to make
        # any up.
        self.assertIsNone(
            overseer_resume.recovery_refusal(self._facts("awaiting-reply"))
        )

    def test_an_exhausted_review_verdict_repair_needs_inspection(self) -> None:
        # The review-verdict repair loop pauses under the same `verify-failed`
        # reason as the verification repair loop. A relaunch only burns a
        # review turn that is exhausted on arrival, so either budget flag has
        # to take the pause off the recoverable path.
        for key in pause_policy.REPAIR_EXHAUSTED_DETAIL_KEYS:
            with self.subTest(key=key):
                policy = pause_policy.classify_pause("verify-failed", {key: True})
                self.assertFalse(policy["recoverable"])
                self.assertTrue(policy["requires_user"])
                self.assertEqual(0, policy["max_retries"])
                self.assertEqual(
                    pause_policy.USER_ACTION_INSPECT, policy["user_action"]
                )

        # A declined retry records the flag as false: no budget was spent.
        declined = pause_policy.classify_pause(
            "verify-failed", {"review_verdict_repair_exhausted": False}
        )
        self.assertTrue(declined["recoverable"])


if __name__ == "__main__":
    unittest.main()
