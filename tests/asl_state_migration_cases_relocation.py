from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_state_migration_support import *


class ConversationRelocationTests(StateMigrationTestBase):
    """The conversation ledger moves with the state directory it lives in.

    Conversation references are plan-owned operational state stored beside
    ``state.json`` rather than inside it, so relocating a stranded state
    directory has to repoint them too -- and has to refuse a ledger that belongs
    to some other plan.
    """

    def _seed_conversation(
        self, *, plan_dir: Path | None = None, session_id: str = "1"
    ) -> conversations.ConversationReference:
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        reference = conversations.ConversationReference(
            binding=conversations.ConversationBinding(
                plan_dir=str(self.old_plan if plan_dir is None else plan_dir),
                session_id=session_id,
                harness="claude",
                workspace=workspaces.WorkspaceBinding(
                    workspace_root=str(plans_files.canonicalize_path(self.tmp_path)),
                    registry_digest="a" * 64,
                ),
                access_profile="worker",
            ),
            native_id="conv-1",
        )
        conversations.open_conversation(self.old_state_dir, reference)
        conversations.confirm_conversation(self.old_state_dir, reference)
        return reference

    def _binding_for(self, plan_dir: Path) -> conversations.ConversationBinding:
        return conversations.ConversationBinding(
            plan_dir=str(plan_dir),
            session_id="1",
            harness="claude",
            workspace=workspaces.WorkspaceBinding(
                workspace_root=str(plans_files.canonicalize_path(self.tmp_path)),
                registry_digest="a" * 64,
            ),
            access_profile="worker",
        )

    def test_dry_run_reports_the_relocation_without_writing(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        ledger_path = conversations.conversation_ledger_path(self.old_state_dir)
        before = ledger_path.read_bytes()
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertEqual(2, report.conversation_records)
        self.assertEqual(2, report.conversation_records_rewritten)
        self.assertIsNotNone(report.conversation_note)
        self.assertEqual(before, ledger_path.read_bytes())

    def test_apply_repoints_the_ledger_and_keeps_it_resumable(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertEqual(2, report.conversation_records_rewritten)
        self.assertFalse(
            conversations.conversation_ledger_path(self.old_state_dir).exists()
        )
        self.assertEqual(
            (str(self.new_plan),),
            conversations.recorded_plan_dirs(self.new_state_dir),
        )
        selection = conversations.select_conversation(
            self.new_state_dir, self._binding_for(self.new_plan)
        )
        self.assertTrue(selection.resumable)
        self.assertEqual("conv-1", selection.reference.native_id)

    def test_migrated_event_records_the_conversation_counts(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        records = read_jsonl(self.new_state_dir / events.EVENTS_FILE_NAME)
        migrated = [
            record
            for record in records
            if record.get("name") == state.STATE_MIGRATED_EVENT
        ]
        self.assertEqual(1, len(migrated))
        self.assertEqual(2, migrated[0]["payload"]["conversation_records"])
        self.assertEqual(2, migrated[0]["payload"]["conversation_records_rewritten"])

    def test_a_foreign_ledger_refuses_the_migration(self) -> None:
        self._write_orphan_state()
        stranger = plans_files.canonicalize_path(self.tmp_path / "plans" / "stranger")
        self._seed_conversation(plan_dir=stranger)
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("conversation-plan-mismatch", caught.exception.reason)
        # Refused before anything moved, exactly like the other identity gates.
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_unreadable_lines_are_reported_not_refused(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        ledger_path = conversations.conversation_ledger_path(self.old_state_dir)
        with ledger_path.open("ab") as handle:
            handle.write(b"{not json}\n")
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        # One truncated append must not strand the whole state directory.
        self.assertTrue(report.applied)
        self.assertIn("unreadable", report.conversation_note)
        self.assertIn(
            "{not json}",
            conversations.conversation_ledger_path(self.new_state_dir).read_text(
                encoding="utf-8"
            ),
        )

    def test_an_unreadable_ledger_file_refuses_before_the_rename(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        ledger_path = conversations.conversation_ledger_path(self.old_state_dir)
        ledger_path.chmod(0o000)
        self.addCleanup(ledger_path.chmod, 0o600)
        if os.access(ledger_path, os.R_OK):
            self.skipTest("running as a user that ignores file permissions")
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("conversation-ledger-error", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_a_plan_with_no_ledger_migrates_unchanged(self) -> None:
        self._write_orphan_state()
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertEqual(0, report.conversation_records)
        self.assertIsNone(report.conversation_note)

    def test_report_dict_carries_the_conversation_fields(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        payload = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        ).to_dict()
        self.assertEqual(2, payload["conversation_records"])
        self.assertEqual(2, payload["conversation_records_rewritten"])
        self.assertIn("conversation_note", payload)


class RedirectJournalMigrationTests(StateMigrationTestBase):
    """The redirect journal travels with the state directory, unedited.

    A redirect request names a run, a session and a turn -- never a path -- so
    relocation has nothing to rewrite inside it. What migration owes an operator
    is therefore a count rather than an edit: an unfinished request is a
    correction still owed an answer, and by the time anyone is migrating, the
    run that owed it is gone.
    """

    def _seed_redirect(
        self, *, session_id: str = "1", accept: bool = False
    ) -> redirect_store_models.RedirectRequest:
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        target = redirect_store_models.RedirectTarget(
            run_id="run-gone",
            session_id=session_id,
            turn_index=1,
            harness="claude",
        )
        request = redirect_store_requests.submit_request(
            self.old_plan,
            target=target,
            feedback="the second column is the one that matters",
            state_dir=self.state_dir,
        )
        if accept:
            request = redirect_store_requests.accept_request(
                self.old_plan,
                request.operation_id,
                target=target,
                state_dir=self.state_dir,
            )
        return request

    def _old_journal(self) -> Path:
        return self.old_state_dir / redirect_store_vocab.REDIRECT_JOURNAL_FILE_NAME

    def _new_journal(self) -> Path:
        return self.new_state_dir / redirect_store_vocab.REDIRECT_JOURNAL_FILE_NAME

    def test_dry_run_counts_the_journal_without_writing(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        before = self._old_journal().read_bytes()
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertEqual(1, report.redirect_requests)
        self.assertEqual(1, report.redirect_requests_unfinished)
        self.assertIn("would move", report.redirect_note)
        self.assertEqual(before, self._old_journal().read_bytes())

    def test_apply_moves_the_journal_byte_for_byte(self) -> None:
        self._write_orphan_state()
        request = self._seed_redirect(accept=True)
        before = self._old_journal().read_bytes()
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertFalse(self._old_journal().exists())
        self.assertEqual(before, self._new_journal().read_bytes())
        # And it still replays to the same request at the new location, which
        # is what makes the correction survivable across the move.
        view = redirect_store_journal.read_journal(
            self.new_plan, state_dir=self.state_dir
        )
        stored = view.request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.accepted)

    def test_a_settled_request_is_counted_but_not_called_unfinished(self) -> None:
        self._write_orphan_state()
        request = self._seed_redirect()
        redirect_store_requests.reject_request(
            self.old_plan,
            request.operation_id,
            reason=redirect_store_vocab.REASON_RUN_ENDED,
            state_dir=self.state_dir,
        )
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertEqual(1, report.redirect_requests)
        self.assertEqual(0, report.redirect_requests_unfinished)
        self.assertNotIn("unfinished", report.redirect_note)

    def test_migrated_event_records_the_redirect_counts(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        records = read_jsonl(self.new_state_dir / events.EVENTS_FILE_NAME)
        migrated = [
            record
            for record in records
            if record.get("name") == state.STATE_MIGRATED_EVENT
        ]
        self.assertEqual(1, len(migrated))
        self.assertEqual(1, migrated[0]["payload"]["redirect_requests"])
        self.assertEqual(1, migrated[0]["payload"]["redirect_requests_unfinished"])

    def test_the_event_never_carries_the_operator_words(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        rendered = (self.new_state_dir / events.EVENTS_FILE_NAME).read_text(
            encoding="utf-8"
        )
        self.assertNotIn("the second column", rendered)

    def test_a_torn_tail_is_reported_and_preserved(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        with self._old_journal().open("a", encoding="utf-8") as handle:
            handle.write('{"version":1,"kind":"acce')
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        # A crash mid-append must not strand the state directory, and the bytes
        # go across untouched so the next run sees the same torn tail.
        self.assertTrue(report.applied)
        self.assertIn("torn tail", report.redirect_note)
        self.assertTrue(
            self._new_journal()
            .read_text(encoding="utf-8")
            .endswith('{"version":1,"kind":"acce')
        )

    def test_an_unreadable_journal_refuses_before_the_rename(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        journal = self._old_journal()
        journal.chmod(0o000)
        self.addCleanup(journal.chmod, 0o600)
        if os.access(journal, os.R_OK):
            self.skipTest("running as a user that ignores file permissions")
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("redirect-journal-error", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_a_plan_with_no_journal_migrates_unchanged(self) -> None:
        self._write_orphan_state()
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertEqual(0, report.redirect_requests)
        self.assertIsNone(report.redirect_note)

    def test_report_dict_carries_the_redirect_fields(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        payload = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        ).to_dict()
        self.assertEqual(1, payload["redirect_requests"])
        self.assertEqual(1, payload["redirect_requests_unfinished"])
        self.assertIn("redirect_note", payload)

    def test_the_printed_report_mentions_the_redirects(self) -> None:
        self._write_orphan_state()
        self._seed_redirect()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = cli.main(
                [
                    "migrate-state",
                    str(self.new_plan),
                    "--from",
                    str(self.old_plan),
                    "--state-dir",
                    str(self.state_dir),
                ]
            )
        self.assertEqual(0, exit_code)
        self.assertIn("Redirects:", buffer.getvalue())
