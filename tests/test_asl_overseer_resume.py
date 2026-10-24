"""Focused tests for the overseer's environmental-recovery resume adapter.

Resuming is the operation that follows every other recovery, and it is the one
with the most ways to be wrong that still look like success. The fixtures are
grouped by those ways.

*No invented answer.* The existing resume service takes reply text because the
pause it was built for is a question. This one takes none, and the test for that
is the spawned child's argv rather than a promise: ``--reply-stdin`` is absent,
nothing is written to the process, and the reply-oriented service still refuses
an empty reply exactly as it did before --- an addition beside it, not a
weakening of it.

*An outdated question.* A reply-shaped pause whose question is still open is
``answer_required``; the same pause with nothing open is recoverable, because
the question went away and there is no longer anybody's words to invent. That
distinction is the whole reason the check reads the mailbox instead of the
classification.

*Missing environment metadata.* A launch is restored from the run's own
``run_config`` or it is not restored at all: absent state, state that predates
the block, a truncated block, a conflicting state directory, and repositories
the action was not authorized for are each ``environment_unrecoverable`` with a
manual command attached. Nothing falls back to this process's defaults, because
a resume under different settings is a replacement reported as a recovery.

*Surviving workers and lost helpers.* A dead helper next to a live lock is
``worker_running`` --- the two are different processes and a lost supervisor is
not proof the worker died. A dead helper next to no lock clears the way. A lock
on another host, a lock with no pid, an unparsable record and an unverifiable
probe are all ``ownership_unknown``, never death.

*Checkpoint then resume.* The whole sequence: a paused run, a settled checkpoint
on its incident, a resume prepared and executed, and a receipt that names the
harness the run's own metadata recorded.

*A concurrent manual resume.* Detected two ways between preparation and
execution --- the situation fingerprint moving, or an owner appearing where the
plan was free --- and in both the launch never happens and nothing is killed.

*Progress, and only progress.* A spawned pid is not a resolution. Execution
reports ``executed`` only after the run's fingerprint moves, ``uncertain`` when
the deadline passes without it, and reconciliation answers the same question
later from the ledger row alone.
"""

from __future__ import annotations

import dataclasses
import unittest
from typing import get_args

from agent_session_loop import (
    overseer_decisions,
    overseer_resume,
    pause_policy,
    run_entry_resume,
    workbench_registry_models,
)
from tests import support_asl_overseer_resume as fixtures
from tests.support import (
    assert_no_runner_import,
)
from tests.support_asl_overseer_resume import ALIAS, ENVIRONMENTAL_PAUSE

ASL_TEST_PORTABLE = True


class ClosedVocabulary(fixtures._ResumeCase):
    """The refusal words are a consumer contract, not a set of strings."""

    def test_refusals_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_resume.REFUSALS),
            set(get_args(overseer_resume.Refusal)),
        )
        self.assertEqual(
            len(overseer_resume.REFUSALS), len(set(overseer_resume.REFUSALS))
        )

    def test_the_authority_words_are_the_checkpoint_boundary_words(self) -> None:
        # One authority check, shared, so the two boundaries cannot drift into
        # saying different things about the same refusal.
        from agent_session_loop import overseer_checkpoint

        self.assertTrue(
            set(overseer_checkpoint.AUTHORITY_REFUSALS) <= set(overseer_resume.REFUSALS)
        )

    def test_escalating_refusals_are_refusals(self) -> None:
        self.assertTrue(
            set(overseer_resume.ESCALATING_REFUSALS) <= set(overseer_resume.REFUSALS)
        )
        self.assertIn("environment_unrecoverable", overseer_resume.ESCALATING_REFUSALS)

    def test_the_registry_holds_the_resume_operation_only(self) -> None:
        registry = overseer_resume.adapters(
            authority=self.authority(), state_dir=self.root
        )
        self.assertTrue(registry.supports("resume"))
        for operation in ("reply", "guidance", "verify", "commit"):
            self.assertFalse(registry.supports(operation), msg=operation)

    def test_resume_operations_is_a_subset_of_the_operational_scope(self) -> None:
        self.assertTrue(
            set(overseer_resume.RESUME_OPERATIONS) < set(overseer_decisions.OPERATIONS)
        )

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_resume)


class LaunchRestoration(fixtures._ResumeCase):
    """The launch is read back from the run's metadata, or it is refused."""

    def test_the_profile_fields_are_the_launchers_own_fields(self) -> None:
        declared = {
            field.name
            for field in dataclasses.fields(workbench_registry_models.LaunchProfile)
        }
        self.assertEqual(declared, set(overseer_resume.PROFILE_FIELDS))

    def test_every_restored_field_survives_the_resume_overlay(self) -> None:
        # ``--resume`` rehydrates by flag dest. A field this module restores or
        # records that the overlay drops would be silently reset by the child.
        restored = set(overseer_resume.PROFILE_FIELDS) | set(
            overseer_resume.LIMIT_FIELDS
        )
        self.assertTrue(restored <= set(run_entry_resume._RESUME_OVERLAY_DESTS))

    def test_the_launch_is_rebuilt_from_the_runs_own_metadata(self) -> None:
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan,
            repositories=(ALIAS,),
            state_dir=self.root,
        )

        self.assertTrue(context.recovered)
        self.assertEqual("codex", context.profile["harness"])
        self.assertEqual("gpt-5-codex", context.profile["harness_model"])
        self.assertEqual("high", context.profile["effort"])
        self.assertEqual(5400, context.profile["timeout"])
        self.assertEqual("per-session", context.profile["commit_mode"])
        self.assertEqual("claude", context.profile["review_harness"])
        self.assertEqual(str(self.repo), context.profile["cwd"])
        self.assertEqual(str(self.root / "workspace"), context.workspace_root)
        self.assertEqual("sha256:registry", context.registry_digest)
        self.assertEqual((ALIAS,), context.repositories)

    def test_the_limits_are_read_for_the_record_and_never_launched(self) -> None:
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual(18, context.limits["max_sessions"])
        self.assertEqual(4, context.limits["max_review_fix_cycles_ceiling"])
        self.assertEqual(2, context.limits["max_repair_group_attempts"])
        # Only the launcher's own fields become a launch. The ceilings and the
        # attempt budgets have no flag here and reach the child only through
        # ``--resume`` reading the run's persisted config.
        for name in ("max_sessions", "max_repair_group_attempts"):
            self.assertNotIn(name, context.profile)
        for name in ("max_sessions", "max_review_fix_cycles_ceiling"):
            self.assertNotIn(name, overseer_resume.PROFILE_FIELDS)

    def test_the_adapter_exposes_no_limit_override_at_all(self) -> None:
        # The strongest form of "ASL's limits stay authoritative": not a
        # parameter this adapter promises not to pass, but no parameter.
        knobs = {
            field.name for field in dataclasses.fields(overseer_resume.ResumeAdapter)
        }
        self.assertTrue(knobs.isdisjoint(set(overseer_resume.LIMIT_FIELDS)))

    def test_a_plan_with_no_state_cannot_be_resumed(self) -> None:
        empty = self.new_plan("plan-empty")
        context = overseer_resume.recover_launch_context(
            plan_dir=empty, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("no readable runner state", context.detail)
        self.assertIn("asl run", context.manual_command)

    def test_state_that_predates_run_config_is_unrecoverable(self) -> None:
        self.write_run_config(omit=True)
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("predates run_config", context.detail)

    def test_a_truncated_launch_block_names_what_is_missing(self) -> None:
        config = self.run_config()
        config["harness"] = None
        config["workspace_root"] = ""
        self.write_run_config(config=config)

        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("harness", context.detail)
        self.assertIn("workspace_root", context.detail)

    def test_repositories_the_action_was_not_authorized_for_are_refused(self) -> None:
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, repositories=("other",), state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("authorized for", context.detail)

    def test_a_conflicting_state_directory_is_unrecoverable(self) -> None:
        config = self.run_config(state_dir_override=str(self.root / "elsewhere"))
        self.write_run_config(config=config)

        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("elsewhere", context.detail)

    def test_the_manual_command_carries_what_was_recorded_and_not_resume(self) -> None:
        self.write_run_config(omit=True)
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        # ``--resume`` is what failed, so sending the operator back through it
        # would be sending them back into this refusal.
        self.assertNotIn("--resume", context.manual_command)
        self.assertIn(self.plan, context.manual_command)

    def test_the_manual_command_is_complete_when_the_metadata_is(self) -> None:
        config = self.run_config()
        config["repositories"] = []
        self.write_run_config(config=config)

        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("--harness codex", context.manual_command)
        self.assertIn("--harness-model gpt-5-codex", context.manual_command)
        self.assertIn("--effort high", context.manual_command)
        self.assertIn("--commit-per-session", context.manual_command)

    def test_preparation_refuses_and_hands_over_the_manual_command(self) -> None:
        self.write_run_config(omit=True)

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "environment_unrecoverable")
        self.assertIn("resume by hand with:", preparation.detail)
        self.assertIsNone(receipt)
        self.assertEqual([], self.launches)

    def test_the_recovered_launch_is_what_reaches_the_launcher(self) -> None:
        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)
        self.assertEqual(1, len(self.launches))
        context = self.launches[0]
        self.assertEqual(self.plan, context.plan_dir)
        self.assertEqual("codex", context.profile["harness"])
        self.assertIn("codex/gpt-5-codex/high", receipt.detail)


class OutdatedQuestion(fixtures._ResumeCase):
    """A pause that is a question is refused; a question that is gone is not."""

    def waiting(self, *, unanswered: tuple[str, ...]) -> None:
        self.runner_state = self.paused_state("awaiting-reply")
        self.unanswered = unanswered

    def test_an_open_question_is_the_operators_to_answer(self) -> None:
        self.waiting(unanswered=("q-1",))

        refusal = overseer_resume.recovery_refusal(self.facts())

        assert refusal is not None
        self.assertEqual("answer_required", refusal[0])
        self.assertIn("still open", refusal[1])

    def test_a_reply_shaped_pause_whose_question_is_gone_is_recoverable(self) -> None:
        # The case this whole path exists for: the run stopped on a question,
        # the question was resolved while it sat there, and what is left is an
        # outdated pause with nobody waiting on an answer.
        self.waiting(unanswered=())

        self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)

    def test_preparation_refuses_while_the_question_stands(self) -> None:
        self.waiting(unanswered=("q-1", "q-2"))

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "answer_required")
        self.assertIn("2 question(s)", preparation.detail)
        self.assertIsNone(receipt)
        self.assertEqual([], self.launches)

    def test_an_environmental_pause_needs_no_answer_even_with_noise(self) -> None:
        self.runner_state = self.paused_state(ENVIRONMENTAL_PAUSE)
        self.unanswered = ("q-1",)

        self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

    def test_a_recoverable_pause_needs_no_answer(self) -> None:
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.runner_state = self.paused_state(reason)
                self.unanswered = ("q-1",)
                self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

    def test_every_inspect_pause_is_environmentally_recoverable(self) -> None:
        for reason in pause_policy.INSPECT_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.runner_state = self.paused_state(reason)
                self.unanswered = ("q-1",)
                self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

    def test_the_runs_own_pause_policy_block_wins(self) -> None:
        # Classifying the reason again here would answer with today's policy
        # about yesterday's pause. The block the runner wrote beside the pause
        # is what that run was actually stopped under.
        self.runner_state = self.paused_state(ENVIRONMENTAL_PAUSE)
        self.runner_state["pause_policy"] = {
            "policy_version": 1,
            "reason": ENVIRONMENTAL_PAUSE,
            "user_action": pause_policy.USER_ACTION_REPLY,
        }
        self.unanswered = ("q-1",)

        policy = overseer_resume.pause_classification(self.facts())
        self.assertEqual(1, policy["policy_version"])

        refusal = overseer_resume.recovery_refusal(self.facts())
        assert refusal is not None
        self.assertEqual("answer_required", refusal[0])

    def test_a_state_file_without_the_block_falls_back_to_the_policy(self) -> None:
        self.runner_state = self.paused_state("awaiting-reply")
        del self.runner_state["pause_policy"]

        self.assertEqual(
            pause_policy.classify_pause("awaiting-reply"),
            dict(overseer_resume.pause_classification(self.facts())),
        )


if __name__ == "__main__":
    unittest.main()
