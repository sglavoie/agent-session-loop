# ruff: noqa: F403, F405
from .asl_overseer_checkpoint_support import *


class ConcurrentEdits(_CheckpointCase):
    """The owned set moving between preparation and execution refuses, twice over."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

    def test_an_edit_after_staging_is_worktree_drift(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        before = self.head()

        self.write("notes.md", "the worker kept going\n")
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("worktree_drifted", receipt.detail)
        self.assertEqual(self.head(), before)
        self.assertEqual(
            (self.repo / "notes.md").read_text(encoding="utf-8"),
            "the worker kept going\n",
        )

    def test_a_restage_after_staging_is_a_snapshot_mismatch(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        before = self.head()

        self.write("notes.md", "the worker kept going\n")
        self.git("add", "notes.md")
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("snapshot_mismatch", receipt.detail)
        self.assertEqual(self.head(), before)

    def test_a_commit_landing_in_the_window_moves_the_baseline(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)

        self.write("elsewhere.md", "another actor\n")
        self.git("add", "elsewhere.md")
        self.git("commit", "-m", "somebody else committed")
        landed = self.head()

        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("baseline_moved", receipt.detail)
        self.assertEqual(self.head(), landed)


class RevokedAuthority(_CheckpointCase):
    """No ruling, the wrong ruling, or a ruling withdrawn mid-flight commits nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

    def test_no_ruling_declines_before_anything_is_staged(self) -> None:
        declined = self.adapter(None).prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_absent", declined.detail)
        self.assertEqual(self.porcelain(), ["?? notes.md"])

    def test_a_ruling_about_another_operation_is_silent_here(self) -> None:
        adapter = self.adapter(self.ruling(operation="guidance"))
        declined = adapter.prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_silent", declined.detail)

    def test_a_ruling_about_another_plan_is_unscoped(self) -> None:
        other = self.new_plan("plan-other")
        declined = self.adapter(self.ruling(plan_dir=other)).prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_unscoped", declined.detail)

    def test_a_refusing_ruling_declines(self) -> None:
        declined = self.adapter(
            self.ruling(authorized=False, refusal="no_decision")
        ).prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_refused", declined.detail)

    def test_a_decision_replaced_between_prepare_and_execute_refuses(self) -> None:
        adapter = self.adapter(self.ruling(), self.ruling(decision_id="d-2"))
        action = self.action()
        before = self.head()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("authority_superseded", receipt.detail)
        self.assertEqual(self.head(), before)

    def test_a_decision_withdrawn_between_prepare_and_execute_refuses(self) -> None:
        adapter = self.adapter(self.ruling(), None)
        action = self.action()
        before = self.head()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("authority_absent", receipt.detail)
        self.assertEqual(self.head(), before)

    def test_an_empty_message_is_not_a_checkpoint(self) -> None:
        declined = self.adapter().prepare(self.action(message="   "))

        self.assertFalse(declined.ready)
        self.assertIn("message_empty", declined.detail)


class CrashReconciliation(_CheckpointCase):
    """A row a dead process left behind, answered from the row alone."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

    def commit_then_forget(
        self,
    ) -> tuple[overseer_store_models.ActionRecord, overseer_dispatch.Action, str]:
        """Make a real checkpoint, then keep only what the ledger persisted."""
        adapter = self.adapter()
        action = self.action()
        record = self.attempted(action)
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed")
        return (record, action, receipt.locator)

    def test_a_committed_checkpoint_reconciles_as_executed(self) -> None:
        record, _, sha = self.commit_then_forget()

        # A new adapter, as a restarted process would build: it never saw the
        # commit and holds no preparation for it.
        receipt = self.adapter().reconcile(record)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, sha)
        self.assertTrue(record.in_doubt)

    def test_an_uncommitted_checkpoint_reconciles_as_failed(self) -> None:
        record = self.attempted(self.action())

        receipt = self.adapter().reconcile(record)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("commit_absent", receipt.detail)

    def test_an_unreadable_repository_is_uncertain_rather_than_absent(self) -> None:
        record = self.attempted(self.action())
        shutil.rmtree(self.repo)

        receipt = self.adapter().reconcile(record)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("repository_unreadable", receipt.detail)

    def test_an_ambiguous_repository_set_is_uncertain(self) -> None:
        record = self.attempted(self.action())

        receipt = self.adapter(repositories=()).reconcile(record)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("repository_ambiguous", receipt.detail)

    def test_another_plans_checkpoint_is_not_read_as_ours(self) -> None:
        record, _, _ = self.commit_then_forget()
        elsewhere = overseer_store_models.ActionRecord(
            action_id=record.action_id,
            plan_dir=self.new_plan("plan-other"),
            incident_id=record.incident_id,
            operation=record.operation,
            target=record.target,
            status=record.status,
            epoch=record.epoch,
            config_fingerprint=record.config_fingerprint,
            request_digest=record.request_digest,
            progress_token=record.progress_token,
            attempted_at=record.attempted_at,
            settled_at=record.settled_at,
            receipt=record.receipt,
        )

        receipt = self.adapter().reconcile(elsewhere)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("commit_absent", receipt.detail)

    def test_a_row_for_another_operation_is_never_answered_here(self) -> None:
        record = self.attempted(self.action())
        other = overseer_store_models.ActionRecord(
            action_id=record.action_id,
            plan_dir=record.plan_dir,
            incident_id=record.incident_id,
            operation="resume",
            target=record.target,
            status=record.status,
            epoch=record.epoch,
            config_fingerprint=record.config_fingerprint,
            request_digest=record.request_digest,
            progress_token=record.progress_token,
            attempted_at=record.attempted_at,
            settled_at=record.settled_at,
            receipt=record.receipt,
        )

        receipt = self.adapter().reconcile(other)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("payload_mismatch", receipt.detail)

    def test_a_retried_checkpoint_finds_the_first_commit(self) -> None:
        record, action, sha = self.commit_then_forget()

        # The retry re-attributes from scratch: the worktree is clean now, so
        # the second preparation declines rather than committing twice.
        adapter = self.adapter()
        declined = adapter.prepare(action)

        self.assertFalse(declined.ready)
        self.assertIn("nothing_to_commit", declined.detail)
        self.assertEqual(
            overseer_checkpoint.find_checkpoint(
                self.repo,
                overseer_checkpoint.checkpoint_id(
                    plan_dir=record.plan_dir,
                    incident_id=record.incident_id,
                    target=record.target,
                ),
            ),
            sha,
        )


class PorcelainReading(_CheckpointCase):
    """The dirty set is read exactly, because it is what ownership is tested on."""

    def test_a_deleted_path_is_dirty_and_can_be_owned(self) -> None:
        self.record_turn(paths=("README.md",))
        (self.repo / "README.md").unlink()

        attribution = self.attribute()

        self.assertEqual(attribution.owned, ("README.md",))

    def test_a_checkpoint_can_commit_a_deletion(self) -> None:
        self.record_turn(paths=("README.md",))
        (self.repo / "README.md").unlink()
        adapter = self.adapter()
        action = self.action()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(self.porcelain(), [])
        self.assertNotIn("README.md", support.git_stdout(self.repo, "ls-files"))

    def test_an_unreadable_status_is_never_read_as_clean(self) -> None:
        missing = overseer_policy_client.Repository(
            alias=ALIAS, path=str(self.root / "absent")
        )

        attribution = self.attribute(repositories=(missing,))

        self.assertEqual(attribution.refusal, "repository_unreadable")
        self.assertFalse(attribution.safe)


class ReusedPrimitives(_CheckpointCase):
    """The commit surface is the one `asl commit` already publishes."""

    def test_the_snapshot_is_the_commit_modules_own_token(self) -> None:
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")
        adapter = self.adapter()
        action = self.action()

        self.assertTrue(adapter.prepare(action).ready)
        prepared = adapter.prepared[next(iter(adapter.prepared))]

        self.assertEqual(
            prepared.snapshot,
            commit_message.pathspec_snapshot(self.repo, prepared.paths),
        )
        self.assertEqual(len(prepared.snapshot), commit_message.SNAPSHOT_TOKEN_LENGTH)


if __name__ == "__main__":
    unittest.main()


__all__ = tuple(name for name in globals() if not name.startswith("__"))
