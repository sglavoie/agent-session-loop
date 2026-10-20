# mypy: ignore-errors
"""Focused test_asl_conversations test group 1."""

# ruff: noqa: F403, F405
from ._test_asl_conversations_support import *


class BindingTests(ConversationTestBase):
    def test_key_is_session_and_harness_only(self) -> None:
        binding = self.binding()
        self.assertEqual(("1", "claude"), binding.key)
        # A different workspace, scope, or access profile must not produce a
        # different key: those are checked on lookup so a change is reported,
        # not used to open a second conversation for the same session.
        for other in (
            self.binding(digest=DIGEST_B),
            self.binding(access_profile="audit-read-only"),
            self.binding(scope_aliases=("scripts",)),
        ):
            self.assertEqual(binding.key, other.key)

    def test_scope_aliases_are_order_insensitive(self) -> None:
        first = self.binding(scope_aliases=("beta", "alpha"))
        second = self.binding(scope_aliases=("alpha", "beta"))
        self.assertEqual(("alpha", "beta"), first.scope_aliases)
        self.assertEqual((), first.mismatches(second))

    def test_repeated_scope_alias_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError) as caught:
            self.binding(scope_aliases=("alpha", "alpha"))
        self.assertEqual(conversations.ERROR_INVALID_REFERENCE, caught.exception.reason)

    def test_mismatches_names_every_differing_field(self) -> None:
        moved = self.tmp_path / "moved"
        mismatches = self.binding().mismatches(
            self.binding(
                plan_dir=moved,
                digest=DIGEST_B,
                access_profile="audit-read-only",
                scope_aliases=("scripts",),
            )
        )
        self.assertEqual(
            ("plan_dir", "registry_digest", "access_profile", "scope_aliases"),
            mismatches,
        )

    def test_unknown_harness_and_access_profile_are_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            self.binding(harness="gemini")
        with self.assertRaises(conversations.ConversationError):
            self.binding(access_profile="root")

    def test_relative_plan_dir_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.ConversationBinding(
                plan_dir="plans/alpha",
                session_id="1",
                harness="claude",
                workspace=self.workspace(),
                access_profile="worker",
            )
        self.assertIn("absolute", str(caught.exception))


class NativeIdentifierTests(ConversationTestBase):
    def test_valid_identifiers_round_trip(self) -> None:
        for value in ("abc", "conv-1", "01H.session:2", "a@b", " padded "):
            self.assertEqual(value.strip(), conversations.validate_native_id(value))

    def test_shell_and_newline_bearing_identifiers_are_refused(self) -> None:
        for value in ("", "-leading", "a b", "a;rm -rf /", "a\nb", "a/b", "a$(x)"):
            with self.assertRaises(conversations.ConversationError):
                conversations.validate_native_id(value)

    def test_over_long_identifier_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.validate_native_id(
                "a" * (conversations.MAX_NATIVE_ID_CHARS + 1)
            )

    def test_non_string_identifier_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.validate_native_id(17)


class SerializationTests(ConversationTestBase):
    def test_reference_round_trips_through_its_payload(self) -> None:
        reference = conversations.ConversationReference(
            binding=self.binding(scope_aliases=("alpha", "beta")),
            native_id="conv-9",
            prior_attempts=(
                conversations.AttemptReference(
                    run_id="run-1", turn=3, outcome="interrupted"
                ),
            ),
        )
        restored = conversations.parse_conversation_reference(reference.to_payload())
        self.assertEqual(reference, restored)
        self.assertEqual("claude:conv-9", restored.locator)

    def test_payload_declares_the_schema_version(self) -> None:
        payload = self.reference().to_payload()
        self.assertEqual(conversations.CONVERSATION_SCHEMA_VERSION, payload["version"])

    def test_unsupported_version_is_refused_on_parse(self) -> None:
        payload = self.reference().to_payload()
        payload["version"] = conversations.CONVERSATION_SCHEMA_VERSION + 1
        with self.assertRaises(conversations.ConversationError):
            conversations.parse_conversation_reference(payload)

    def test_prior_attempts_are_bounded_oldest_first(self) -> None:
        reference = self.reference()
        for index in range(conversations.MAX_PRIOR_ATTEMPTS + 5):
            reference = reference.with_attempt(
                conversations.AttemptReference(
                    run_id=f"run-{index}", turn=index, outcome="interrupted"
                )
            )
        self.assertEqual(
            conversations.MAX_PRIOR_ATTEMPTS, len(reference.prior_attempts)
        )
        # The newest interruptions are what a resumed worker needs, so the
        # oldest is dropped rather than the append being refused.
        self.assertEqual("run-5", reference.prior_attempts[0].run_id)

    def test_negative_turn_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.AttemptReference(run_id="run-1", turn=-1, outcome="failed")

    def test_boolean_turn_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.AttemptReference(run_id="run-1", turn=True, outcome="failed")


class ContinuationIntentTests(ConversationTestBase):
    def test_intent_has_no_serialization(self) -> None:
        # The fresh-conversation override must never be persisted or rehydrated,
        # so the value that carries it deliberately cannot be written down.
        self.assertFalse(hasattr(conversations.ContinuationIntent, "to_payload"))

    def test_fresh_requires_explicit_authorization(self) -> None:
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.fresh_intent(authorized=False)
        self.assertEqual(
            conversations.ERROR_FRESH_NOT_AUTHORIZED, caught.exception.reason
        )

    def test_authorized_fresh_intent_names_no_conversation(self) -> None:
        intent = conversations.fresh_intent(authorized=True)
        self.assertFalse(intent.resumes)
        self.assertIsNone(intent.reference)
        self.assertIsNone(intent.describe()["conversation"])

    def test_resume_requires_a_reference(self) -> None:
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.ContinuationIntent(mode="resume")
        self.assertEqual(
            conversations.ERROR_UNKNOWN_CONVERSATION, caught.exception.reason
        )

    def test_resume_must_not_carry_a_fresh_authorization(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.ContinuationIntent(
                mode="resume", reference=self.reference(), fresh_authorized=True
            )

    def test_fresh_must_not_name_a_conversation(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.ContinuationIntent(
                mode="fresh", reference=self.reference(), fresh_authorized=True
            )

    def test_describe_reports_interrupted_operations(self) -> None:
        intent = conversations.resume_intent(
            self.reference(), interrupted_operations=("turn-3", "verify.gate-1")
        )
        self.assertEqual(
            {
                "mode": "resume",
                "conversation": "claude:conv-1",
                "interrupted_operations": ["turn-3", "verify.gate-1"],
                "fresh_authorized": False,
            },
            intent.describe(),
        )

    def test_operation_identifiers_reject_free_text(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.resume_intent(
                self.reference(),
                interrupted_operations=("the reviewer said the diff was wrong",),
            )

    def test_interrupted_operations_are_bounded(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.resume_intent(
                self.reference(),
                interrupted_operations=tuple(
                    f"turn-{index}"
                    for index in range(conversations.MAX_INTERRUPTED_OPERATIONS + 1)
                ),
            )


class CapabilityReportTests(ConversationTestBase):
    def test_report_names_every_harness_in_order(self) -> None:
        report = conversations.capability_report()
        self.assertEqual(
            list(conversations.HARNESS_NAMES),
            [capability.harness for capability in report.capabilities],
        )

    def test_each_state_follows_the_adapter_that_produces_it(self) -> None:
        """The report is derived, so it may never disagree with the adapters."""
        report = conversations.capability_report()
        for capability in report.capabilities:
            wired = harnesses.continues_conversations(capability.harness)
            self.assertEqual("available" if wired else "staged", capability.state)
            self.assertTrue(capability.detail)
        self.assertEqual(
            tuple(
                name
                for name in conversations.HARNESS_NAMES
                if harnesses.continues_conversations(name)
            ),
            report.available_harnesses,
        )

    def test_the_wired_harnesses_are_named_rather_than_derived(self) -> None:
        """Stated flatly rather than derived: the derivation above proves the
        report follows the adapters, and this pins which adapters those are, so
        flipping a flag by accident fails here instead of silently offering an
        operator a recovery no transport performs."""
        report = conversations.capability_report()
        self.assertEqual(("codex", "claude", "copilot"), report.available_harnesses)

    def test_report_round_trips(self) -> None:
        report = conversations.capability_report()
        self.assertEqual(
            report, conversations.parse_capability_report(report.to_payload())
        )

    def test_missing_harness_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.CapabilityReport(
                capabilities=(
                    conversations.HarnessCapability(harness="codex", state="staged"),
                )
            )
        self.assertEqual(conversations.ERROR_INVALID_RECORD, caught.exception.reason)

    def test_duplicate_harness_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.CapabilityReport(
                capabilities=tuple(
                    conversations.HarnessCapability(harness="codex", state="staged")
                    for _ in conversations.HARNESS_NAMES
                )
            )

    def test_unknown_capability_state_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError):
            conversations.HarnessCapability(harness="codex", state="probably")


class PersistenceTests(ConversationTestBase):
    def test_ledger_lives_beside_state_not_inside_it(self) -> None:
        reference = self.live_conversation()
        self.assertEqual(
            self.plan_state / conversations.CONVERSATION_LEDGER_FILE_NAME,
            self.ledger_path,
        )
        # The next run replaces state.json wholesale. The reference has to
        # survive exactly that, so it is stored in its own file.
        (self.plan_state / "state.json").write_text("{}\n", encoding="utf-8")
        selection = conversations.select_conversation(
            self.plan_state, reference.binding
        )
        self.assertTrue(selection.resumable)
        self.assertEqual(reference.native_id, selection.reference.native_id)

    def test_each_lifecycle_step_appends_one_line(self) -> None:
        reference = self.reference()
        opened = conversations.open_conversation(self.plan_state, reference)
        confirmed = conversations.confirm_conversation(self.plan_state, reference)
        retired = conversations.retire_conversation(
            self.plan_state, reference, reason="operator-discarded"
        )
        self.assertEqual(
            [1, 2, 3], [opened.sequence, confirmed.sequence, retired.sequence]
        )
        self.assertEqual(3, len(self.ledger_lines()))

    def test_records_are_never_rewritten_in_place(self) -> None:
        reference = self.live_conversation()
        before = self.ledger_lines()
        conversations.retire_conversation(
            self.plan_state, reference, reason="superseded"
        )
        self.assertEqual(before, self.ledger_lines()[: len(before)])

    def test_append_creates_the_state_directory(self) -> None:
        self.assertFalse(self.plan_state.exists())
        conversations.open_conversation(self.plan_state, self.reference())
        self.assertTrue(self.ledger_path.is_file())

    def test_ledger_is_not_world_readable(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference())
        self.assertEqual(0o600, self.ledger_path.stat().st_mode & 0o777)

    def test_interrupted_append_is_terminated_not_healed(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference())
        # A crash mid-append leaves a partial line with no newline.
        with self.ledger_path.open("ab") as handle:
            handle.write(b'{"version": 1, "kind": "conversat')
        conversations.open_conversation(
            self.plan_state, self.reference(native_id="conv-2")
        )
        ledger = conversations.read_conversations(self.plan_state)
        self.assertEqual(2, len(ledger.records))
        # The truncated bytes stay in history as a rejected line rather than
        # being cut back, so the crash remains visible.
        self.assertEqual(
            [(2, conversations.REJECT_INVALID_JSON)],
            [(line.sequence, line.reason) for line in ledger.rejected],
        )

    def test_oversized_record_is_refused(self) -> None:
        reference = self.reference()
        for index in range(conversations.MAX_PRIOR_ATTEMPTS):
            reference = reference.with_attempt(
                conversations.AttemptReference(
                    run_id="r" * 200, turn=index, outcome="interrupted"
                )
            )
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.open_conversation(self.plan_state, reference)
        self.assertEqual(conversations.ERROR_RECORD_TOO_LARGE, caught.exception.reason)
        self.assertFalse(self.ledger_path.exists())

    def test_full_ledger_refuses_further_appends(self) -> None:
        self.plan_state.mkdir(parents=True)
        self.ledger_path.write_bytes(b"x" * conversations.MAX_CONVERSATION_LEDGER_BYTES)
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.open_conversation(self.plan_state, self.reference())
        self.assertEqual(conversations.ERROR_LEDGER_FULL, caught.exception.reason)

    def test_stored_record_carries_no_free_text(self) -> None:
        self.live_conversation()
        for line in self.ledger_lines():
            payload = json.loads(line)
            self.assertEqual(
                {"version", "kind", "recorded_at", "reference"}, set(payload)
            )
