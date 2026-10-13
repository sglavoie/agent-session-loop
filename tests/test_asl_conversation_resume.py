"""Turn-level conversation continuity: what a turn resumes, and what it refuses.

``tests/test_asl_conversations.py`` covers the durable ledger in isolation --
what a record is, how it is appended, what a lookup may answer. These tests
cover the layer above it: the policy that turns a pending session plus a harness
into exactly one of continue / open / stop, and the wiring that carries that
decision into the adapter and back out into the ledger.

Two properties are load-bearing enough to be tested from more than one angle.
The first is that the intended conversation is restored -- keyed by session and
harness, so distinct plan sessions and generated repair children never reach
each other's. The second is that ``--fresh-conversation`` authorizes exactly one
fresh start: not the next turn, not the next session, not a supervisor retry,
and not a ``--resume`` that rehydrates flags from disk. The leak surfaces have
one test each, because each covers a replay the others cannot.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    conversation_resume,
    conversations,
    events,
    harnesses,
    pause_policy,
    plans_files,
    preflight_runtime_hints,
    run_entry,
    run_flags,
    turn_execution_executor,
    workbench_recovery_models,
    workspaces,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_autopilot() -> types.ModuleType:
    """Import the supervisor helper, which ships as a script rather than a module."""
    module_path = REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
    spec = importlib.util.spec_from_file_location(
        "asl_autopilot_continuity_containment", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def harness_result(**overrides: object) -> harnesses.HarnessResult:
    """A finished, successful turn, unless a field is overridden."""
    fields: dict[str, object] = {
        "argv": ["codex"],
        "returncode": 0,
        "stdout": "",
        "stderr": "",
    }
    fields.update(overrides)
    return harnesses.HarnessResult(**fields)


class ContinuationTestBase(unittest.TestCase):
    """One plan directory and the plan-owned state directory beside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans_files.canonicalize_path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.plan_dir.mkdir()
        self.cwd = self.tmp_path / "repo"
        self.cwd.mkdir()
        self.plan_state = self.tmp_path / "state" / "plan-hash"
        self.ledger_path = conversations.conversation_ledger_path(self.plan_state)

    def binding(
        self,
        *,
        session_id: str = "7",
        harness: str = "codex",
        cwd: Path | None = None,
        access_profile: str = harnesses.WORKER_ACCESS.name,
        scope_aliases: tuple[str, ...] = (),
    ) -> conversations.ConversationBinding:
        return conversation_resume.turn_binding(
            plan_dir=self.plan_dir,
            session_id=session_id,
            harness=harness,
            cwd=self.cwd if cwd is None else cwd,
            access_profile=access_profile,
            scope_aliases=scope_aliases,
        )

    def reference(
        self, native_id: str, **kwargs: object
    ) -> conversations.ConversationReference:
        return conversations.ConversationReference(
            binding=self.binding(**kwargs),
            native_id=native_id,
        )

    def confirmed(
        self, native_id: str, **kwargs: object
    ) -> conversations.ConversationReference:
        """Record a conversation that a turn has already submitted work on."""
        reference = self.reference(native_id, **kwargs)
        conversations.open_conversation(self.plan_state, reference)
        conversations.confirm_conversation(self.plan_state, reference)
        return reference

    def granted(self) -> conversation_resume.FreshConversationAuthorization:
        return conversation_resume.FreshConversationAuthorization(True)

    def withheld(self) -> conversation_resume.FreshConversationAuthorization:
        return conversation_resume.FreshConversationAuthorization(False)


class BindingIdentityTests(ContinuationTestBase):
    """What a conversation is bound to, and what it is deliberately not."""

    def test_distinct_sessions_never_share_a_conversation(self) -> None:
        self.confirmed("thread-7", session_id="7")
        selection = conversations.select_conversation(
            self.plan_state, self.binding(session_id="8")
        )
        self.assertEqual(selection.state, conversations.SELECTION_ABSENT)

    def test_a_repair_child_does_not_inherit_its_parents_conversation(self) -> None:
        # A generated repair child carries its own session id, so the lookup
        # key differs and no fallback exists to close the gap.
        self.confirmed("thread-7", session_id="7")
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state,
            binding=self.binding(session_id="7.1"),
            authorization=self.withheld(),
        )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_OPENED
        )
        self.assertIsNone(continuation.resume_conversation_id)

    def test_a_different_harness_is_a_different_conversation(self) -> None:
        self.confirmed("thread-7", harness="codex")
        selection = conversations.select_conversation(
            self.plan_state, self.binding(harness="claude")
        )
        self.assertEqual(selection.state, conversations.SELECTION_ABSENT)

    def test_a_moved_workspace_root_makes_the_binding_incompatible(self) -> None:
        self.confirmed("thread-7")
        elsewhere = self.tmp_path / "other-repo"
        elsewhere.mkdir()
        selection = conversations.select_conversation(
            self.plan_state, self.binding(cwd=elsewhere)
        )
        self.assertEqual(selection.state, conversations.SELECTION_INCOMPATIBLE)
        self.assertIn("workspace_root", selection.mismatched_fields)

    def test_a_changed_access_profile_makes_the_binding_incompatible(self) -> None:
        self.confirmed("thread-7")
        selection = conversations.select_conversation(
            self.plan_state, self.binding(access_profile="audit-read-only")
        )
        self.assertEqual(selection.state, conversations.SELECTION_INCOMPATIBLE)
        self.assertIn("access_profile", selection.mismatched_fields)

    def test_a_changed_writable_alias_set_makes_the_binding_incompatible(self) -> None:
        self.confirmed("thread-7", scope_aliases=("app",))
        selection = conversations.select_conversation(
            self.plan_state, self.binding(scope_aliases=("app", "docs"))
        )
        self.assertEqual(selection.state, conversations.SELECTION_INCOMPATIBLE)
        self.assertIn("scope_aliases", selection.mismatched_fields)

    def test_authorized_settings_are_not_part_of_the_binding(self) -> None:
        # Model, effort, timeout and spark routing are changes an operator makes
        # between attempts on purpose; none of them invalidates work already
        # done, so the binding has nowhere to put them.
        parameters = set(inspect.signature(conversation_resume.turn_binding).parameters)
        self.assertEqual(
            parameters
            & {"model", "effort", "timeout", "spark", "harness_model", "unsafe_bypass"},
            set(),
        )


class LegacyBindingTests(ContinuationTestBase):
    """The synthesized digest a plan with no repository registry is bound by."""

    def test_the_digest_is_deterministic_and_registry_shaped(self) -> None:
        first = conversation_resume.legacy_registry_digest(self.cwd)
        second = conversation_resume.legacy_registry_digest(self.cwd)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_a_different_root_gets_a_different_digest(self) -> None:
        other = self.tmp_path / "other-repo"
        other.mkdir()
        self.assertNotEqual(
            conversation_resume.legacy_registry_digest(self.cwd),
            conversation_resume.legacy_registry_digest(other),
        )

    def test_a_resolved_workspace_supplies_its_own_binding(self) -> None:
        declared = workspaces.WorkspaceBinding(
            workspace_root=str(self.cwd), registry_digest=DIGEST_B
        )
        workspace = mock.Mock(spec=["binding"])
        workspace.binding = declared
        self.assertIs(
            conversation_resume.workspace_binding(cwd=self.cwd, workspace=workspace),
            declared,
        )

    def test_a_legacy_plan_falls_back_to_the_synthesized_digest(self) -> None:
        binding = conversation_resume.workspace_binding(cwd=self.cwd, workspace=None)
        self.assertEqual(binding.workspace_root, str(self.cwd))
        self.assertEqual(
            binding.registry_digest,
            conversation_resume.legacy_registry_digest(self.cwd),
        )


class FreshConversationAuthorizationTests(unittest.TestCase):
    """The counter that makes ``--fresh-conversation`` mean once, not always."""

    def test_an_ungranted_authorization_is_never_available(self) -> None:
        authorization = conversation_resume.FreshConversationAuthorization(False)
        self.assertFalse(authorization.available)
        self.assertFalse(authorization.consume())
        self.assertFalse(authorization.consumed)

    def test_a_granted_authorization_is_spent_exactly_once(self) -> None:
        authorization = conversation_resume.FreshConversationAuthorization(True)
        self.assertTrue(authorization.consume())
        self.assertFalse(authorization.consume())
        self.assertFalse(authorization.consume())
        self.assertTrue(authorization.granted)
        self.assertTrue(authorization.consumed)
        self.assertFalse(authorization.available)

    def test_describe_reports_the_counter_without_persisting_it(self) -> None:
        authorization = conversation_resume.FreshConversationAuthorization(True)
        self.assertEqual(
            authorization.describe(),
            {"granted": True, "consumed": False, "available": True},
        )
        authorization.consume()
        self.assertEqual(
            authorization.describe(),
            {"granted": True, "consumed": True, "available": False},
        )
        # No serialization of its own: the value the flag becomes has nowhere to
        # be written down, which is what keeps it out of durable state.
        self.assertFalse(hasattr(authorization, "__dict__"))


class TurnContinuationPlanTests(ContinuationTestBase):
    """The three outcomes, and the absence of a fourth."""

    def plan(
        self,
        *,
        authorization: conversation_resume.FreshConversationAuthorization | None = None,
        **kwargs: object,
    ) -> conversation_resume.TurnContinuation:
        return conversation_resume.plan_turn_continuation(
            self.plan_state,
            binding=self.binding(**kwargs),
            authorization=self.withheld() if authorization is None else authorization,
        )

    def test_a_confirmed_conversation_is_resumed_by_native_identifier(self) -> None:
        self.confirmed("thread-7")
        continuation = self.plan()
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_RESUMED
        )
        self.assertEqual(continuation.resume_conversation_id, "thread-7")
        self.assertTrue(continuation.resumes)
        self.assertFalse(continuation.paused)
        self.assertTrue(continuation.captures)
        self.assertIsNotNone(continuation.intent)
        self.assertTrue(continuation.intent.resumes)

    def test_resuming_needs_no_authorization_and_spends_none(self) -> None:
        self.confirmed("thread-7")
        authorization = self.granted()
        continuation = self.plan(authorization=authorization)
        self.assertTrue(continuation.resumes)
        self.assertFalse(authorization.consumed)

    def test_a_first_turn_opens_a_conversation_without_authorization(self) -> None:
        authorization = self.granted()
        continuation = self.plan(authorization=authorization)
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_OPENED
        )
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertTrue(continuation.captures)
        # Nothing is being discarded, so the operator's one fresh start is
        # still unspent for the turn that genuinely needs it.
        self.assertFalse(authorization.consumed)

    def test_an_unconfirmed_conversation_stops_the_run(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        continuation = self.plan()
        self.assertEqual(
            continuation.disposition,
            conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
        )
        self.assertTrue(continuation.paused)
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertFalse(continuation.captures)

    def test_an_incompatible_binding_stops_the_run_naming_what_moved(self) -> None:
        self.confirmed("thread-7")
        elsewhere = self.tmp_path / "other-repo"
        elsewhere.mkdir()
        continuation = self.plan(cwd=elsewhere)
        self.assertTrue(continuation.paused)
        self.assertIn("workspace_root", continuation.mismatched_fields)

    def test_a_retired_conversation_stops_the_run(self) -> None:
        reference = self.confirmed("thread-7")
        conversations.retire_conversation(
            self.plan_state, reference, reason="session-completed"
        )
        self.assertTrue(self.plan().paused)

    def test_an_unreadable_ledger_stops_the_run_rather_than_starting_over(
        self,
    ) -> None:
        self.confirmed("thread-7")
        with self.ledger_path.open("ab") as handle:
            handle.write(b"{not json\n")
        continuation = self.plan()
        self.assertTrue(continuation.paused)

    def test_every_disposition_a_turn_can_reach_is_in_the_vocabulary(self) -> None:
        # The `conversation_continuation` event spells its disposition from
        # these constants on all three producers -- the decision, the post-turn
        # record, and session completion -- so a value outside the tuple would
        # be a lane nothing downstream could name.
        reached = {self.plan().disposition}
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        reached.add(self.plan().disposition)
        conversations.confirm_conversation(self.plan_state, self.reference("thread-7"))
        reached.add(self.plan().disposition)
        self.assertEqual(
            reached,
            {
                conversation_resume.CONTINUATION_OPENED,
                conversation_resume.CONTINUATION_RECOVERY_REQUIRED,
                conversation_resume.CONTINUATION_RESUMED,
            },
        )
        self.assertTrue(reached <= set(conversation_resume.CONTINUATION_DISPOSITIONS))
        self.assertIn(
            conversation_resume.CONTINUATION_RETIRED,
            conversation_resume.CONTINUATION_DISPOSITIONS,
        )

    def test_every_selection_state_but_two_requires_recovery(self) -> None:
        # Written as the complement so a selection state added later fails
        # closed instead of falling through to an unauthorized fresh start.
        self.assertEqual(
            set(conversation_resume.PROCEEDING_SELECTION_STATES)
            | set(conversation_resume.RECOVERY_REQUIRED_SELECTION_STATES),
            set(conversations.SELECTION_STATES),
        )
        self.assertEqual(
            set(conversation_resume.PROCEEDING_SELECTION_STATES)
            & set(conversation_resume.RECOVERY_REQUIRED_SELECTION_STATES),
            set(),
        )
        self.assertEqual(
            set(conversation_resume.PROCEEDING_SELECTION_STATES),
            {conversations.SELECTION_RESUMABLE, conversations.SELECTION_ABSENT},
        )

    def test_a_harness_without_continuity_runs_the_turn_with_neither_half(self) -> None:
        with mock.patch.object(
            conversations, "continues_conversations", return_value=False
        ):
            continuation = self.plan()
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_UNAVAILABLE
        )
        self.assertFalse(continuation.paused)
        self.assertFalse(continuation.captures)
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertIsNone(continuation.capture(self.plan_state))

    def test_the_recovery_message_names_both_ways_forward(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        message = conversation_resume.recovery_message(self.plan())
        self.assertIn("--fresh-conversation", message)
        self.assertIn("thread-7", message)

    def test_describe_is_a_flat_json_serializable_projection(self) -> None:
        self.confirmed("thread-7")
        payload = self.plan().describe()
        self.assertEqual(payload["disposition"], "resumed")
        self.assertEqual(payload["conversation"], "thread-7")
        self.assertEqual(payload["session_id"], "7")
        self.assertEqual(payload["harness"], "codex")
        json.dumps(payload)


class AuthorizedFreshStartTests(ContinuationTestBase):
    """What ``--fresh-conversation`` buys, and how far it reaches."""

    def unresumable(self, native_id: str = "thread-7") -> None:
        """Leave a conversation that exists but may not be continued."""
        conversations.open_conversation(self.plan_state, self.reference(native_id))

    def test_authorization_turns_a_refusal_into_a_fresh_start(self) -> None:
        self.unresumable()
        authorization = self.granted()
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_FRESH
        )
        self.assertFalse(continuation.paused)
        self.assertTrue(continuation.captures)
        self.assertIsNone(continuation.resume_conversation_id)
        self.assertTrue(authorization.consumed)
        self.assertIsNotNone(continuation.intent)
        self.assertFalse(continuation.intent.resumes)

    def test_the_superseded_conversation_is_retired_with_a_reason(self) -> None:
        self.unresumable()
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.granted()
        )
        self.assertEqual(continuation.superseded, ("codex:thread-7",))
        ledger = conversations.read_conversations(self.plan_state)
        retired = [
            record
            for record in ledger.history(self.binding().key)
            if record.kind == conversations.KIND_CONVERSATION_RETIRED
        ]
        self.assertEqual(
            [record.retirement_reason for record in retired], ["superseded"]
        )

    def test_a_retired_record_is_not_retired_a_second_time(self) -> None:
        reference = self.confirmed("thread-7")
        conversations.retire_conversation(
            self.plan_state, reference, reason="session-completed"
        )
        before = self.ledger_path.read_bytes()
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.granted()
        )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_FRESH
        )
        self.assertEqual(continuation.superseded, ())
        self.assertEqual(self.ledger_path.read_bytes(), before)

    def test_a_failed_retirement_is_reported_not_raised(self) -> None:
        self.unresumable()
        with mock.patch.object(
            conversations,
            "retire_conversation",
            side_effect=OSError("read-only ledger"),
        ):
            continuation = conversation_resume.plan_turn_continuation(
                self.plan_state, binding=self.binding(), authorization=self.granted()
            )
        self.assertEqual(
            continuation.disposition, conversation_resume.CONTINUATION_FRESH
        )
        self.assertEqual(continuation.superseded, ())
        self.assertTrue(any("read-only ledger" in note for note in continuation.notes))

    def test_the_authorization_does_not_survive_into_a_later_turn(self) -> None:
        # A retry inside the same run reads the same object the operator
        # authorized once, and is refused by it.
        self.unresumable()
        authorization = self.granted()
        first = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        self.assertEqual(first.disposition, conversation_resume.CONTINUATION_FRESH)
        self.unresumable("thread-8")
        second = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        self.assertEqual(
            second.disposition, conversation_resume.CONTINUATION_RECOVERY_REQUIRED
        )
        self.assertTrue(second.paused)

    def test_the_authorization_does_not_survive_into_a_later_session(self) -> None:
        self.unresumable()
        authorization = self.granted()
        conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=authorization
        )
        conversations.open_conversation(
            self.plan_state, self.reference("thread-8", session_id="8")
        )
        later = conversation_resume.plan_turn_continuation(
            self.plan_state,
            binding=self.binding(session_id="8"),
            authorization=authorization,
        )
        self.assertTrue(later.paused)


class AuthorizationContainmentTests(unittest.TestCase):
    """The three replay surfaces the one-use flag is kept out of."""

    def flag(self) -> run_flags.RunFlag:
        for flag in run_flags.RUN_FLAG_SPEC:
            if flag.dest == "fresh_conversation":
                return flag
        raise AssertionError("RUN_FLAG_SPEC declares no --fresh-conversation flag")

    def test_the_flag_is_a_declared_store_true_option(self) -> None:
        flag = self.flag()
        self.assertEqual(flag.option_strings, ("--fresh-conversation",))
        self.assertEqual(flag.action, "store_true")
        self.assertIs(flag.cli_default, False)

    def test_resume_does_not_rehydrate_the_authorization(self) -> None:
        self.assertIn("fresh_conversation", run_entry._RESUME_OVERLAY_EXCLUDED_DESTS)
        self.assertNotIn("fresh_conversation", run_entry._resume_overlay_dests())

    def test_the_supervisor_does_not_replay_the_authorization_to_a_child(self) -> None:
        autopilot = _load_autopilot()

        self.assertIn("fresh_conversation", autopilot._CHILD_RUN_ARG_SKIP_DESTS)

    def test_nothing_persists_the_authorization_in_run_config(self) -> None:
        from agent_session_loop import run_payloads

        source = inspect.getsource(run_payloads._run_config_payload)
        self.assertNotIn("fresh_conversation", source)


class RecoveryPauseRegistrationTests(unittest.TestCase):
    """The pause reason, wired everywhere a pause reason has to be wired."""

    reason = conversation_resume.RECOVERY_PAUSE_REASON

    def test_the_reason_is_registered_in_the_event_vocabulary(self) -> None:
        self.assertEqual(self.reason, "conversation-recovery-required")
        self.assertIn(self.reason, events.PAUSE_REASONS)

    def test_the_reason_is_environment_blocked_and_never_auto_retried(self) -> None:
        # A supervisor retry that "recovered" this pause would be a retry that
        # discarded an agent's context without anyone deciding to.
        self.assertIn(self.reason, pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS)
        self.assertNotIn(self.reason, pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_the_workbench_routes_the_reason_to_its_own_recovery_card(self) -> None:
        """The dedicated card, not the generic one.

        This reason was in the generic allowlist when it was registered, because
        nothing yet knew how to render it. The Workbench later grew a
        `conversation-recovery` card that reads the decision, the one-use
        authorization, and the evidence behind both, and the two tables are
        disjoint by construction, so the card is now the only place the reason
        may appear.
        """
        self.assertEqual(
            "conversation-recovery",
            workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON[self.reason],
        )
        self.assertNotIn(
            self.reason, workbench_recovery_models.GENERIC_RECOVERY_PAUSE_REASONS
        )

    def test_a_repair_hint_explains_the_two_ways_forward(self) -> None:
        hint = preflight_runtime_hints.RUNTIME_REPAIR_HINTS[self.reason]
        self.assertEqual(hint.check_id, f"runtime.{self.reason}")
        self.assertIn("--fresh-conversation", hint.message)

    def test_the_continuation_event_is_named_once(self) -> None:
        self.assertEqual(
            events.CONVERSATION_CONTINUATION_EVENT, "conversation_continuation"
        )


class ConversationCaptureTests(ContinuationTestBase):
    """The sink that records a conversation before any work is submitted."""

    def capture(
        self, expected: str | None = None
    ) -> conversation_resume.ConversationCapture:
        return conversation_resume.ConversationCapture(
            self.plan_state, binding=self.binding(), expected_native_id=expected
        )

    def test_a_new_conversation_is_opened_before_the_turn_submits(self) -> None:
        capture = self.capture()
        capture("thread-7")
        self.assertEqual(capture.reference.native_id, "thread-7")
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_UNCONFIRMED)

    def test_a_resumed_conversation_is_not_re_opened(self) -> None:
        self.confirmed("thread-7")
        before = self.ledger_path.read_bytes()
        capture = self.capture("thread-7")
        capture("thread-7")
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(capture.reference.native_id, "thread-7")

    def test_a_substituted_conversation_refuses_the_turn(self) -> None:
        capture = self.capture("thread-7")
        with self.assertRaises(harnesses.ConversationCaptureError):
            capture("thread-9")
        self.assertIn("thread-9", capture.error)
        self.assertIsNone(capture.reference)

    def test_repeating_the_same_identifier_writes_nothing_further(self) -> None:
        capture = self.capture()
        capture("thread-7")
        after_first = self.ledger_path.read_bytes()
        capture("thread-7")
        self.assertEqual(self.ledger_path.read_bytes(), after_first)

    def test_a_continuation_builds_the_capture_matched_to_its_decision(self) -> None:
        self.confirmed("thread-7")
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.withheld()
        )
        capture = continuation.capture(self.plan_state)
        self.assertIsNotNone(capture)
        with self.assertRaises(harnesses.ConversationCaptureError):
            capture("thread-9")

    def test_a_paused_continuation_builds_no_capture(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference("thread-7"))
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.withheld()
        )
        self.assertIsNone(continuation.capture(self.plan_state))


class TurnOutcomeTests(ContinuationTestBase):
    """What the ledger learns after the turn, and what it deliberately does not."""

    def opened_capture(self) -> conversation_resume.ConversationCapture:
        capture = conversation_resume.ConversationCapture(
            self.plan_state, binding=self.binding()
        )
        capture("thread-7")
        return capture

    def record(
        self, capture: conversation_resume.ConversationCapture, **overrides: object
    ) -> conversation_resume.TurnOutcomeRecord:
        return conversation_resume.record_turn_outcome(
            self.plan_state,
            capture=capture,
            result=harness_result(**overrides),
            run_id="run-1",
            turn=3,
        )

    def test_a_finished_turn_makes_its_conversation_resumable(self) -> None:
        outcome = self.record(self.opened_capture())
        self.assertTrue(outcome.confirmed)
        self.assertIsNone(outcome.attempt)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_RESUMABLE)
        self.assertEqual(selection.reference.native_id, "thread-7")

    def test_an_interrupted_turn_is_resumable_and_carries_its_attempt(self) -> None:
        outcome = self.record(self.opened_capture(), interrupted=True)
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.attempt.outcome, "interrupted")
        self.assertEqual(outcome.attempt.run_id, "run-1")
        self.assertEqual(outcome.attempt.turn, 3)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_RESUMABLE)

    def test_a_timed_out_turn_is_an_interrupted_attempt(self) -> None:
        self.assertEqual(
            conversation_resume.attempt_outcome(harness_result(timed_out=True)),
            "interrupted",
        )
        self.assertEqual(
            conversation_resume.attempt_outcome(harness_result(aborted=True)),
            "interrupted",
        )
        self.assertEqual(
            conversation_resume.attempt_outcome(
                harness_result(cancel_stage=harnesses.CANCEL_STAGE_EXECUTION)
            ),
            "interrupted",
        )

    def test_a_transport_failure_is_a_failed_attempt_that_still_resumes(self) -> None:
        outcome = self.record(self.opened_capture(), returncode=2)
        self.assertTrue(outcome.confirmed)
        self.assertEqual(outcome.attempt.outcome, "failed")
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_RESUMABLE)

    def test_a_turn_that_submitted_nothing_leaves_it_unconfirmed(self) -> None:
        for overrides in (
            {"cancel_stage": harnesses.CANCEL_STAGE_UNSTARTED},
            {"cancel_stage": harnesses.CANCEL_STAGE_HANDSHAKE},
            {"conversation_error": "ledger unwritable"},
        ):
            with self.subTest(**overrides):
                self.assertFalse(
                    conversation_resume.turn_submitted_work(harness_result(**overrides))
                )

    def test_an_unsubmitted_turn_does_not_confirm_its_conversation(self) -> None:
        outcome = self.record(
            self.opened_capture(), cancel_stage=harnesses.CANCEL_STAGE_HANDSHAKE
        )
        self.assertFalse(outcome.confirmed)
        self.assertIsNotNone(outcome.error)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_UNCONFIRMED)

    def test_a_turn_that_never_reported_a_conversation_records_nothing(self) -> None:
        capture = conversation_resume.ConversationCapture(
            self.plan_state, binding=self.binding()
        )
        outcome = self.record(capture)
        self.assertFalse(outcome.confirmed)
        self.assertIsNone(outcome.conversation)

    def test_a_failed_confirmation_is_reported_rather_than_raised(self) -> None:
        capture = self.opened_capture()
        with mock.patch.object(
            conversations, "confirm_conversation", side_effect=OSError("disk full")
        ):
            outcome = self.record(capture)
        self.assertFalse(outcome.confirmed)
        self.assertIn("disk full", outcome.error)
        # Fail closed: the next attempt reads unconfirmed and asks the operator.
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(selection.state, conversations.SELECTION_UNCONFIRMED)


class SessionCompletionTests(ContinuationTestBase):
    """A completed session's conversation stops being reachable."""

    def test_completion_retires_every_harness_conversation_for_the_session(
        self,
    ) -> None:
        self.confirmed("thread-7", harness="codex")
        self.confirmed("thread-7c", harness="claude")
        self.confirmed("thread-8", session_id="8")
        retired, error = conversation_resume.retire_session_conversations(
            self.plan_state, session_id="7"
        )
        self.assertIsNone(error)
        self.assertEqual(set(retired), {"codex:thread-7", "claude:thread-7c"})
        self.assertEqual(
            conversations.select_conversation(self.plan_state, self.binding()).state,
            conversations.SELECTION_RETIRED,
        )
        self.assertEqual(
            conversations.select_conversation(
                self.plan_state, self.binding(session_id="8")
            ).state,
            conversations.SELECTION_RESUMABLE,
        )

    def test_a_later_attempt_at_a_finished_session_stops_for_recovery(self) -> None:
        self.confirmed("thread-7")
        conversation_resume.retire_session_conversations(
            self.plan_state, session_id="7"
        )
        continuation = conversation_resume.plan_turn_continuation(
            self.plan_state, binding=self.binding(), authorization=self.withheld()
        )
        self.assertTrue(continuation.paused)

    def test_a_failed_retirement_is_reported_rather_than_raised(self) -> None:
        self.confirmed("thread-7")
        with mock.patch.object(
            conversations,
            "retire_completed_session",
            side_effect=conversations.ConversationError("invalid-record", "boom"),
        ):
            retired, error = conversation_resume.retire_session_conversations(
                self.plan_state, session_id="7"
            )
        self.assertEqual(retired, ())
        self.assertIn("boom", error)


class TurnLaunchWiringTests(unittest.TestCase):
    """The decision reaches the adapter through the turn launch, unaltered."""

    def test_the_launch_carries_both_halves_of_continuity(self) -> None:
        annotations = turn_execution_executor.TurnLaunch.__annotations__
        self.assertIn("resume_conversation_id", annotations)
        self.assertIn("on_conversation", annotations)

    def test_the_executor_forwards_both_halves_to_the_harness(self) -> None:
        source = inspect.getsource(turn_execution_executor.TurnExecutor.launch)
        self.assertIn("resume_conversation_id=launch.resume_conversation_id", source)
        self.assertIn("on_conversation=launch.on_conversation", source)

    def test_run_harness_accepts_the_resume_request(self) -> None:
        parameters = inspect.signature(harnesses.run_harness).parameters
        self.assertIn("resume_conversation_id", parameters)
        self.assertIn("on_conversation", parameters)

    def test_every_adapter_accepts_the_resume_request(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                adapter = harnesses.get_adapter(name)
                self.assertIn(
                    "resume_conversation_id",
                    inspect.signature(adapter.run).parameters,
                )


if __name__ == "__main__":
    unittest.main()
