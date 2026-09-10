"""Conversation reference, continuation intent, and durable ledger tests.

The behaviour under test is continuity across an interruption: a native harness
conversation has to survive the replacement of ``state.json`` by the next run,
and an invalid or unproven reference has to refuse rather than reach for some
other conversation. The cases below are organised around the ways that can go
wrong -- a crash before work is submitted, a plan with no conversation state at
all, a truncated append, a session that finished, and a plan directory that
moved.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import conversations, harnesses, plans, workspaces

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class ConversationTestBase(unittest.TestCase):
    """Shared fixture: one plan directory and its plan-owned state directory."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans.canonicalize_path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.plan_dir.mkdir()
        self.plan_state = self.tmp_path / "state" / "plan-hash"
        self.ledger_path = conversations.conversation_ledger_path(self.plan_state)

    def workspace(self, digest: str = DIGEST_A) -> workspaces.WorkspaceBinding:
        return workspaces.WorkspaceBinding(
            workspace_root=str(self.tmp_path), registry_digest=digest
        )

    def binding(
        self,
        *,
        session_id: str = "1",
        harness: str = "claude",
        plan_dir: Path | None = None,
        digest: str = DIGEST_A,
        access_profile: str = "worker",
        scope_aliases: tuple[str, ...] = (),
    ) -> conversations.ConversationBinding:
        return conversations.ConversationBinding(
            plan_dir=str(self.plan_dir if plan_dir is None else plan_dir),
            session_id=session_id,
            harness=harness,
            workspace=self.workspace(digest),
            access_profile=access_profile,
            scope_aliases=scope_aliases,
        )

    def reference(
        self, *, native_id: str = "conv-1", **kwargs: Any
    ) -> conversations.ConversationReference:
        return conversations.ConversationReference(
            binding=self.binding(**kwargs), native_id=native_id
        )

    def live_conversation(
        self, *, native_id: str = "conv-1", **kwargs: Any
    ) -> conversations.ConversationReference:
        """Open *and* confirm a conversation, the only state that resumes."""
        reference = self.reference(native_id=native_id, **kwargs)
        conversations.open_conversation(self.plan_state, reference)
        conversations.confirm_conversation(self.plan_state, reference)
        return reference

    def ledger_lines(self) -> list[str]:
        return self.ledger_path.read_text(encoding="utf-8").splitlines()


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

    def test_codex_is_the_harness_recovery_is_wired_for(self) -> None:
        """Stated flatly rather than derived: the derivation above proves the
        report follows the adapters, and this pins which adapter that is, so
        flipping a flag by accident fails here instead of silently offering an
        operator a recovery no transport performs."""
        report = conversations.capability_report()
        self.assertEqual(("codex",), report.available_harnesses)

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


class SelectionTests(ConversationTestBase):
    def test_absent_legacy_metadata_reads_as_empty(self) -> None:
        # A plan whose state predates this feature has no ledger at all. That is
        # a recovery decision for the caller, never a crash.
        ledger = conversations.read_conversations(self.plan_state)
        self.assertEqual((), ledger.records)
        self.assertTrue(ledger.trustworthy)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(conversations.SELECTION_ABSENT, selection.state)
        self.assertFalse(selection.resumable)
        self.assertIsNone(selection.reference)

    def test_confirmed_conversation_is_resumable(self) -> None:
        reference = self.live_conversation()
        selection = conversations.select_conversation(
            self.plan_state, reference.binding
        )
        self.assertEqual(conversations.SELECTION_RESUMABLE, selection.state)
        self.assertEqual(reference, selection.reference)

    def test_failure_before_work_starts_is_unconfirmed(self) -> None:
        reference = self.reference()
        conversations.open_conversation(self.plan_state, reference)
        selection = conversations.select_conversation(
            self.plan_state, reference.binding
        )
        # The native id was persisted before work was submitted, so the
        # conversation may hold nothing. Resuming into it is refused.
        self.assertEqual(conversations.SELECTION_UNCONFIRMED, selection.state)
        self.assertFalse(selection.resumable)

    def test_confirming_an_unopened_conversation_is_refused(self) -> None:
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.confirm_conversation(self.plan_state, self.reference())
        self.assertEqual(
            conversations.ERROR_UNKNOWN_CONVERSATION, caught.exception.reason
        )

    def test_confirming_a_different_native_id_is_refused(self) -> None:
        conversations.open_conversation(self.plan_state, self.reference())
        with self.assertRaises(conversations.ConversationError):
            conversations.confirm_conversation(
                self.plan_state, self.reference(native_id="conv-typo")
            )

    def test_another_session_never_borrows_this_conversation(self) -> None:
        self.live_conversation()
        selection = conversations.select_conversation(
            self.plan_state, self.binding(session_id="2")
        )
        self.assertEqual(conversations.SELECTION_ABSENT, selection.state)
        self.assertIsNone(selection.reference)

    def test_another_harness_never_borrows_this_conversation(self) -> None:
        self.live_conversation()
        selection = conversations.select_conversation(
            self.plan_state, self.binding(harness="codex")
        )
        self.assertEqual(conversations.SELECTION_ABSENT, selection.state)

    def test_changed_binding_is_incompatible_not_a_new_conversation(self) -> None:
        self.live_conversation()
        selection = conversations.select_conversation(
            self.plan_state, self.binding(digest=DIGEST_B)
        )
        # The acceptance case: an invalid reference must not select another
        # conversation. It names the mismatch and refuses.
        self.assertEqual(conversations.SELECTION_INCOMPATIBLE, selection.state)
        self.assertEqual(("registry_digest",), selection.mismatched_fields)
        self.assertFalse(selection.resumable)

    def test_changed_access_profile_is_incompatible(self) -> None:
        self.live_conversation()
        selection = conversations.select_conversation(
            self.plan_state, self.binding(access_profile="audit-read-only")
        )
        self.assertEqual(conversations.SELECTION_INCOMPATIBLE, selection.state)
        self.assertEqual(("access_profile",), selection.mismatched_fields)

    def test_changed_scope_is_incompatible(self) -> None:
        self.live_conversation(scope_aliases=("alpha",))
        selection = conversations.select_conversation(
            self.plan_state, self.binding(scope_aliases=("alpha", "beta"))
        )
        self.assertEqual(conversations.SELECTION_INCOMPATIBLE, selection.state)
        self.assertEqual(("scope_aliases",), selection.mismatched_fields)

    def test_latest_record_wins_within_one_key(self) -> None:
        self.live_conversation(native_id="conv-1")
        second = self.reference(native_id="conv-2")
        conversations.open_conversation(self.plan_state, second)
        conversations.confirm_conversation(self.plan_state, second)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual("conv-2", selection.reference.native_id)

    def test_no_latest_conversation_fallback_exists(self) -> None:
        # There is deliberately no API that answers "the newest conversation";
        # every lookup is keyed and then checked.
        for name in dir(conversations):
            self.assertNotIn("latest", name.lower())


class RetirementTests(ConversationTestBase):
    def test_completed_session_conversation_is_retired(self) -> None:
        self.live_conversation()
        retired = conversations.retire_completed_session(
            self.plan_state, session_id="1"
        )
        self.assertEqual(1, len(retired))
        self.assertEqual("session-completed", retired[0].retirement_reason)
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(conversations.SELECTION_RETIRED, selection.state)
        self.assertFalse(selection.resumable)

    def test_retirement_appends_rather_than_deleting(self) -> None:
        self.live_conversation()
        conversations.retire_completed_session(self.plan_state, session_id="1")
        ledger = conversations.read_conversations(self.plan_state)
        self.assertEqual(
            [
                conversations.KIND_CONVERSATION_OPENED,
                conversations.KIND_CONVERSATION_CONFIRMED,
                conversations.KIND_CONVERSATION_RETIRED,
            ],
            [record.kind for record in ledger.records],
        )

    def test_completion_retires_every_harness_for_the_session(self) -> None:
        self.live_conversation(native_id="conv-claude")
        self.live_conversation(harness="codex", native_id="conv-codex")
        retired = conversations.retire_completed_session(
            self.plan_state, session_id="1"
        )
        self.assertEqual(
            {"codex", "claude"},
            {record.reference.binding.harness for record in retired},
        )

    def test_naming_a_harness_retires_only_that_one(self) -> None:
        self.live_conversation(native_id="conv-claude")
        self.live_conversation(harness="codex", native_id="conv-codex")
        conversations.retire_completed_session(
            self.plan_state, session_id="1", harness="codex"
        )
        self.assertEqual(
            conversations.SELECTION_RETIRED,
            conversations.select_conversation(
                self.plan_state, self.binding(harness="codex")
            ).state,
        )
        self.assertTrue(
            conversations.select_conversation(
                self.plan_state, self.binding(harness="claude")
            ).resumable
        )

    def test_retiring_twice_is_a_no_op(self) -> None:
        self.live_conversation()
        conversations.retire_completed_session(self.plan_state, session_id="1")
        self.assertEqual(
            (), conversations.retire_completed_session(self.plan_state, session_id="1")
        )

    def test_retiring_an_untouched_session_is_a_no_op(self) -> None:
        self.assertEqual(
            (), conversations.retire_completed_session(self.plan_state, session_id="7")
        )

    def test_retirement_refuses_a_partially_readable_ledger(self) -> None:
        self.live_conversation()
        with self.ledger_path.open("ab") as handle:
            handle.write(b"{not json}\n")
        with self.assertRaises(conversations.ConversationError) as caught:
            conversations.retire_completed_session(self.plan_state, session_id="1")
        self.assertEqual(conversations.ERROR_INVALID_RECORD, caught.exception.reason)

    def test_unknown_retirement_reason_is_refused(self) -> None:
        reference = self.live_conversation()
        with self.assertRaises(conversations.ConversationError):
            conversations.retire_conversation(
                self.plan_state, reference, reason="because"
            )


class CorruptionTests(ConversationTestBase):
    def _corrupt_with(self, payload: bytes) -> None:
        self.live_conversation()
        with self.ledger_path.open("ab") as handle:
            handle.write(payload + b"\n")

    def test_every_rejection_reason_is_reachable(self) -> None:
        good = self.reference().to_payload()
        cases = {
            conversations.REJECT_EMPTY_LINE: b"   ",
            conversations.REJECT_INVALID_JSON: b"{not json}",
            conversations.REJECT_NOT_OBJECT: b"[1, 2, 3]",
            conversations.REJECT_UNSUPPORTED_VERSION: json.dumps(
                {"version": 99, "kind": "conversation_opened", "reference": good}
            ).encode("utf-8"),
            conversations.REJECT_UNKNOWN_KIND: json.dumps(
                {"version": 1, "kind": "conversation_forked", "reference": good}
            ).encode("utf-8"),
            conversations.REJECT_INVALID_RECORD: json.dumps(
                {"version": 1, "kind": "conversation_opened", "reference": {}}
            ).encode("utf-8"),
            conversations.REJECT_RECORD_TOO_LARGE: b"x"
            * (conversations.MAX_CONVERSATION_RECORD_BYTES + 1),
        }
        for reason, payload in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                self._corrupt_with(payload)
                ledger = conversations.read_conversations(self.plan_state)
                self.assertEqual([reason], [line.reason for line in ledger.rejected])

    def test_rejected_line_keeps_its_positional_sequence(self) -> None:
        self._corrupt_with(b"{not json}")
        ledger = conversations.read_conversations(self.plan_state)
        self.assertEqual(3, ledger.rejected[0].sequence)
        self.assertEqual([1, 2], [record.sequence for record in ledger.records])

    def test_rejected_preview_is_bounded(self) -> None:
        self._corrupt_with(b'"' + b"z" * 4000 + b'"')
        ledger = conversations.read_conversations(self.plan_state)
        self.assertLessEqual(
            len(ledger.rejected[0].raw), conversations.MAX_REJECTED_RAW_CHARS
        )

    def test_any_unreadable_line_blocks_every_selection(self) -> None:
        self._corrupt_with(b"{not json}")
        # The bad line cannot be attributed, so it may be the retirement of the
        # very conversation being asked about. Nothing may be selected.
        for binding in (self.binding(), self.binding(session_id="4")):
            selection = conversations.select_conversation(self.plan_state, binding)
            self.assertEqual(conversations.SELECTION_CORRUPT, selection.state)
            self.assertFalse(selection.resumable)

    def test_retirement_reason_on_a_non_retirement_record_is_rejected(self) -> None:
        self._corrupt_with(
            json.dumps(
                {
                    "version": 1,
                    "kind": "conversation_confirmed",
                    "recorded_at": "2026-09-10T00:00:00+00:00",
                    "reference": self.reference().to_payload(),
                    "retirement_reason": "session-completed",
                }
            ).encode("utf-8")
        )
        ledger = conversations.read_conversations(self.plan_state)
        self.assertEqual(
            [conversations.REJECT_INVALID_RECORD],
            [line.reason for line in ledger.rejected],
        )

    def test_tampered_native_id_is_rejected_rather_than_resumed(self) -> None:
        payload = self.reference().to_payload()
        payload["native_id"] = "conv 1; rm -rf /"
        self._corrupt_with(
            json.dumps(
                {
                    "version": 1,
                    "kind": "conversation_confirmed",
                    "recorded_at": "2026-09-10T00:00:00+00:00",
                    "reference": payload,
                }
            ).encode("utf-8")
        )
        selection = conversations.select_conversation(self.plan_state, self.binding())
        self.assertEqual(conversations.SELECTION_CORRUPT, selection.state)

    def test_unreadable_ledger_is_not_read_as_absent(self) -> None:
        self.live_conversation()
        self.ledger_path.chmod(0o000)
        self.addCleanup(self.ledger_path.chmod, 0o600)
        if os.access(self.ledger_path, os.R_OK):
            self.skipTest("running as a user that ignores file permissions")
        ledger = conversations.read_conversations(self.plan_state)
        self.assertEqual(
            [conversations.REJECT_UNREADABLE_LEDGER],
            [line.reason for line in ledger.rejected],
        )
        self.assertEqual(
            conversations.SELECTION_CORRUPT,
            conversations.select_conversation(self.plan_state, self.binding()).state,
        )


class RelocationTests(ConversationTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.moved_plan = self.tmp_path / "moved"

    def test_dry_run_reports_without_writing(self) -> None:
        self.live_conversation()
        before = self.ledger_path.read_bytes()
        report = conversations.relocate_records(
            self.plan_state, from_plan_dir=self.plan_dir, to_plan_dir=self.moved_plan
        )
        self.assertEqual(
            (2, 2, 0, False),
            (report.records, report.rewritten, report.rejected, report.applied),
        )
        self.assertEqual(before, self.ledger_path.read_bytes())

    def test_apply_repoints_every_record(self) -> None:
        self.live_conversation()
        report = conversations.relocate_records(
            self.plan_state,
            from_plan_dir=self.plan_dir,
            to_plan_dir=self.moved_plan,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertEqual(
            (str(self.moved_plan),), conversations.recorded_plan_dirs(self.plan_state)
        )

    def test_relocated_conversation_still_resumes(self) -> None:
        self.live_conversation()
        conversations.relocate_records(
            self.plan_state,
            from_plan_dir=self.plan_dir,
            to_plan_dir=self.moved_plan,
            apply=True,
        )
        self.assertTrue(
            conversations.select_conversation(
                self.plan_state, self.binding(plan_dir=self.moved_plan)
            ).resumable
        )
        # And the stale binding no longer matches, rather than matching loosely.
        self.assertEqual(
            conversations.SELECTION_INCOMPATIBLE,
            conversations.select_conversation(self.plan_state, self.binding()).state,
        )

    def test_line_order_and_count_are_preserved(self) -> None:
        self.live_conversation()
        conversations.retire_completed_session(self.plan_state, session_id="1")
        before = [json.loads(line)["kind"] for line in self.ledger_lines()]
        conversations.relocate_records(
            self.plan_state,
            from_plan_dir=self.plan_dir,
            to_plan_dir=self.moved_plan,
            apply=True,
        )
        after = [json.loads(line)["kind"] for line in self.ledger_lines()]
        self.assertEqual(before, after)

    def test_unreadable_lines_are_copied_back_unchanged(self) -> None:
        self.live_conversation()
        with self.ledger_path.open("ab") as handle:
            handle.write(b"{not json}\n")
        report = conversations.relocate_records(
            self.plan_state,
            from_plan_dir=self.plan_dir,
            to_plan_dir=self.moved_plan,
            apply=True,
        )
        self.assertEqual(1, report.rejected)
        self.assertIn("{not json}", self.ledger_lines())

    def test_an_interrupted_append_stays_unterminated(self) -> None:
        self.live_conversation()
        with self.ledger_path.open("ab") as handle:
            handle.write(b'{"partial"')
        conversations.relocate_records(
            self.plan_state,
            from_plan_dir=self.plan_dir,
            to_plan_dir=self.moved_plan,
            apply=True,
        )
        self.assertTrue(self.ledger_path.read_bytes().endswith(b'{"partial"'))

    def test_records_of_another_plan_are_left_alone(self) -> None:
        other = self.tmp_path / "other"
        other.mkdir()
        self.live_conversation()
        report = conversations.relocate_records(
            self.plan_state, from_plan_dir=other, to_plan_dir=self.moved_plan
        )
        self.assertEqual(0, report.rewritten)
        self.assertFalse(report.applied)

    def test_absent_ledger_relocates_to_nothing(self) -> None:
        report = conversations.relocate_records(
            self.plan_state,
            from_plan_dir=self.plan_dir,
            to_plan_dir=self.moved_plan,
            apply=True,
        )
        self.assertEqual(
            (0, 0, False), (report.records, report.rewritten, report.applied)
        )

    def test_recorded_plan_dirs_is_empty_without_a_ledger(self) -> None:
        self.assertEqual((), conversations.recorded_plan_dirs(self.plan_state))


if __name__ == "__main__":
    unittest.main()
