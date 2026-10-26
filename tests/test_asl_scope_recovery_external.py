# ruff: noqa: F403, F405
"""External recovery validation tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_scope_recovery_test_support import *


class ExternalRecoveryTests(ExternalRecoveryTestCase):
    def test_preview_and_accept_preserve_pi_and_original_turn(self):
        before = self.capture()
        session = self.session.read_bytes()
        history = self.event_path.read_bytes()
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertEqual([], preview["remaining_blockers"])
        self.assertEqual(before, self.capture())
        self.assertEqual(history, self.event_path.read_bytes())
        code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(0, code, result)
        self.assertEqual(session, self.session.read_bytes())
        self.assertEqual(before["pi"], self.capture()["pi"])
        self.assertEqual("", git_stdout(self.repos["notes"], "status", "--porcelain"))
        self.assertTrue(self.linked.exists())
        self.assertEqual("1", plans_snapshot.inspect_plan(self.plan).next_pending.id)
        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "1"
            )
        )
        self.assertEqual(
            self.guard,
            json.loads(history.decode().splitlines()[-1])["payload"]["details"][
                "workspace_write_guard"
            ],
        )
        self.pause()
        self.assertIsNotNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "1"
            )
        )

    def test_blank_and_torn_event_lines_do_not_abort_the_preview(self):
        # A crash mid-append leaves a torn tail; it names no incident.
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write('\n\n{"name": "run_st')
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertEqual([], preview["remaining_blockers"])
        self.assertEqual("rejected", preview["original_run_id"])

    def test_torn_and_blank_lines_before_the_failure_do_not_abort(self):
        # Lines between the run start and its failure, not only after it, are
        # what the original-binding check reads.
        lines = self.event_path.read_bytes().splitlines(keepends=True)
        self.event_path.write_bytes(
            lines[0] + b'{"name": "run_st\n' + b"\n" + b"".join(lines[1:])
        )
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertEqual([], preview["remaining_blockers"])
        self.assertEqual("rejected", preview["original_run_id"])

    def test_missing_plan_directory_reports_plan_not_found(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(
                [
                    "acknowledge-external-changes",
                    str(self.plan.parent / "missing-plan"),
                    "--session",
                    "1",
                    "--repository",
                    "pi",
                    "--state-dir",
                    str(self.state_dir),
                ]
            )
        self.assertEqual(1, code)
        self.assertEqual(1, len(output.getvalue().splitlines()))
        result = json.loads(output.getvalue())
        self.assertEqual("acknowledge-external-changes", result["command"])
        self.assertEqual("plan-not-found", result["reason"])
        self.assertIs(False, result["accepted"])
        self.assertIs(False, result["paused"])

    def test_stale_and_drift_refused(self):
        _, preview = self.invoke()
        (self.repos["pi"] / "independent.txt").write_text("new evidence\n")
        self.assertEqual(1, self.invoke(*self.accept_args(preview))[0])
        _, preview = self.invoke()
        (self.repos["notes"] / "README.md").write_text("content drift\n")
        _, fresh = self.invoke()
        self.assertTrue(fresh["remaining_blockers"])
        self.assertEqual(1, self.invoke(*self.accept_args(fresh))[0])

    def test_unrelated_staging_refused(self):
        (self.repos["notes"] / "foreign.txt").write_text("foreign\n")
        git_stdout(self.repos["notes"], "add", "foreign.txt")
        _, preview = self.invoke()
        self.assertTrue(preview["remaining_blockers"])
        self.assertEqual(1, self.invoke(*self.accept_args(preview))[0])
        self.assertIn(
            "A  foreign.txt", git_stdout(self.repos["notes"], "status", "--porcelain")
        )

    def test_reason_checkpoint_authorization_and_active_lock_required(self):
        _, preview = self.invoke()
        self.assertEqual(1, self.invoke("--accept", preview["evidence_digest"])[0])
        self.assertEqual(
            1,
            self.invoke("--accept", preview["evidence_digest"], "--reason", "approved")[
                0
            ],
        )
        state_locks.acquire_lock(self.plan, self.state_dir)
        try:
            self.assertEqual(1, self.invoke(*self.accept_args(preview))[0])
        finally:
            state_locks.release_lock(self.plan, self.state_dir)

    def test_writable_alias_and_changed_scope_binding_fail_closed(self):
        self.assertEqual(1, self.invoke("--repository", "notes")[0])
        self.session.write_text(
            self.session.read_text().replace(
                "**Reads repositories**: notes", "**Reads repositories**: notes, pi"
            )
        )
        self.assertEqual(1, self.invoke()[0])

    def test_allowed_external_alias_may_be_a_dirty_read(self):
        self.session.write_text(
            self.session.read_text().replace(
                "**Reads repositories**: notes", "**Reads repositories**: notes, pi"
            )
        )
        # The rejected turn read pi too.
        self.guard["read_aliases"] = ["notes", "pi"]
        self.pause()
        _, preview = self.invoke()
        self.assertIn(
            "needed read-only repository pi is dirty", preview["remaining_blockers"]
        )
        state_path = self.event_path.parent / events.STATE_FILE_NAME
        state_path.write_text(
            json.dumps({"run_config": {"allow_external_changes": ["pi"]}})
        )
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertEqual([], preview["remaining_blockers"])
        self.assertEqual(["pi"], preview["run_policy"]["allow_external_changes"])
        code, result = self.invoke(*self.accept_args(preview))
        self.assertEqual(0, code, result)
        self.assertEqual(
            "independent dirt\n", (self.repos["pi"] / "independent.txt").read_text()
        )

    def test_an_unreadable_run_state_is_refused_rather_than_crashing(self):
        # Bytes that are not UTF-8 used to escape as UnicodeDecodeError.
        state_path = self.event_path.parent / events.STATE_FILE_NAME
        for raw in (b'{"run_config": "\xff\xfe"}', b"{not json", b"[]"):
            state_path.write_bytes(raw)
            code, preview = self.invoke()
            self.assertEqual(1, code, preview)
            self.assertIn("recorded run state is unreadable", json.dumps(preview))

    def test_a_non_utf8_event_line_is_skipped_like_a_torn_one(self):
        # A stray byte used to fail the whole history with UnicodeDecodeError.
        with self.event_path.open("ab") as handle:
            handle.write(b'\n{"name": "run_started", "note": "\xff"}\n')
        code, preview = self.invoke()
        self.assertEqual(0, code, preview)
        self.assertEqual([], preview["remaining_blockers"])
        self.assertEqual("rejected", preview["original_run_id"])

    def test_an_unreadable_recovery_journal_is_refused_rather_than_crashing(self):
        journal = self.event_path.parent / "external-change-recovery.json"
        for raw in (b'{"proposal": "\xff"}', b"{not json", b"[]"):
            journal.write_bytes(raw)
            code, preview = self.invoke()
            self.assertEqual(1, code, preview)
            self.assertIn("recovery journal is unreadable", json.dumps(preview))
