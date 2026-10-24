# mypy: ignore-errors
"""Binding identity and the continue/open/stop plan for a turn's conversation."""

# ruff: noqa: F403, F405
from ._test_asl_conversation_resume_support import *


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
            conversation_capabilities, "continues_conversations", return_value=False
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
