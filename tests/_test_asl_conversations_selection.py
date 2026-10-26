# mypy: ignore-errors
"""Conversation ledger selection, retirement, corruption handling, and relocation."""

# ruff: noqa: F403, F405
from unittest import mock

from agent_session_loop import artifacts

from ._test_asl_conversations_support import *


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

    def test_nul_bearing_paths_are_rejected_rather_than_crashing(self) -> None:
        # os.path.realpath raises ValueError on an embedded NUL, which is not a
        # ConversationError; selection must report corruption, not a traceback.
        for field in ("plan_dir", "workspace_root"):
            with self.subTest(field=field):
                payload = self.reference().to_payload()
                payload[field] = f"{payload[field]}\x00tail"
                self.ledger_path.unlink(missing_ok=True)
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
                ledger = conversations.read_conversations(self.plan_state)
                self.assertEqual(
                    [conversations.REJECT_INVALID_RECORD],
                    [line.reason for line in ledger.rejected],
                )
                selection = conversations.select_conversation(
                    self.plan_state, self.binding()
                )
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

    def test_a_failed_rewrite_is_a_ledger_write_error(self) -> None:
        # State migration names a ConversationError after its rename; a raw
        # OSError escaped it as a traceback with the ledger left stale.
        self.live_conversation()
        before = self.ledger_path.read_bytes()
        with (
            mock.patch.object(
                artifacts,
                "atomic_write_bytes",
                side_effect=OSError("No space left on device"),
            ),
            self.assertRaises(conversations.ConversationError) as raised,
        ):
            conversations.relocate_records(
                self.plan_state,
                from_plan_dir=self.plan_dir,
                to_plan_dir=self.moved_plan,
                apply=True,
            )
        self.assertEqual(conversations.ERROR_LEDGER_WRITE, raised.exception.reason)
        self.assertEqual(before, self.ledger_path.read_bytes())

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
