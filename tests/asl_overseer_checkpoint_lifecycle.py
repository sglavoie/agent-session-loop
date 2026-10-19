# ruff: noqa: F403, F405
from .asl_overseer_checkpoint_support import *


class VerificationState(_CheckpointCase):
    """A checkpoint reports the checks the run recorded, and never more."""

    def test_no_recorded_verification_is_unrecorded(self) -> None:
        self.record_turn(paths=("notes.md",))

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "unrecorded")
        self.assertFalse(recorded.satisfied)

    def test_a_failing_verification_is_reported_as_failed(self) -> None:
        self.record_turn(
            paths=("notes.md",),
            verification={
                "session": "1",
                "passed": False,
                "reason": "check-1 failed",
                "failures": ["check-1"],
            },
        )

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "failed")
        self.assertEqual(recorded.failures, ("check-1",))
        self.assertFalse(recorded.satisfied)

    def test_an_unresolved_verification_is_not_a_pass(self) -> None:
        self.record_turn(
            paths=("notes.md",),
            verification={"session": "1", "passed": None, "reason": "interrupted"},
        )

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "unresolved")
        self.assertFalse(recorded.satisfied)

    def test_the_newest_recorded_verification_wins(self) -> None:
        self.record_turn(
            index=1,
            paths=("notes.md",),
            verification={"session": "1", "passed": True},
        )
        self.record_turn(
            index=2,
            paths=("notes.md",),
            verification={"session": "1", "passed": False},
        )

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "failed")

    def test_a_turn_that_verified_nothing_does_not_erase_the_last_one(self) -> None:
        self.record_turn(
            index=1,
            paths=("notes.md",),
            verification={"session": "1", "passed": True},
        )
        self.record_turn(index=2, paths=("notes.md",))

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "passed")


class SafeCheckpoint(_CheckpointCase):
    """The whole path, when every change belongs to the run."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(
            paths=("notes.md",),
            verification={"session": "1", "passed": False, "failures": ["check-1"]},
        )
        self.write("notes.md", "work in progress\n")

    def test_a_prepared_checkpoint_commits_the_owned_set(self) -> None:
        adapter = self.adapter()
        action = self.action()
        before = self.head()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, self.head())
        self.assertNotEqual(self.head(), before)
        self.assertEqual(self.porcelain(), [])
        self.assertIn("notes.md", support.git_stdout(self.repo, "show", "--name-only"))

    def test_the_commit_carries_the_derived_checkpoint_trailer(self) -> None:
        adapter = self.adapter()
        action = self.action()

        adapter.prepare(action)
        adapter.execute(action)

        identity = overseer_checkpoint.checkpoint_id(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            target=action.target,
        )
        body = self.commit_body()
        self.assertIn(MESSAGE, body)
        self.assertIn(f"{overseer_checkpoint.CHECKPOINT_TRAILER}: {identity}", body)

    def test_the_receipt_reports_the_verification_the_run_recorded(self) -> None:
        adapter = self.adapter()
        action = self.action()

        adapter.prepare(action)
        receipt = adapter.execute(action)

        self.assertIn("verification=failed", receipt.detail)
        self.assertNotIn("verification=passed", receipt.detail)

    def test_somebody_elses_staged_entry_is_left_staged(self) -> None:
        # Staging is where an unrelated actor's work is most easily destroyed:
        # a checkpoint that reset the index to make room for itself would erase
        # it silently. This one refuses instead, and the entry is still there.
        self.write("somebody-else.md", "not this run\n")
        self.git("add", "somebody-else.md")

        declined = self.adapter().prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("changes_unattributed", declined.detail)
        self.assertIn(
            "A  somebody-else.md",
            support.git_stdout(self.repo, "status", "--porcelain"),
        )

    def test_work_outside_the_repository_index_is_untouched(self) -> None:
        # An ignored scratch file is unrelated work that attribution never sees
        # and the commit must never carry away.
        self.write(".gitignore", "scratch/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore scratch")
        self.write("scratch/notes.txt", "somebody else's scratch\n")
        self.record_turn(index=2, paths=("notes.md",))

        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertNotIn(
            "scratch", support.git_stdout(self.repo, "show", "--name-only")
        )
        self.assertEqual(
            (self.repo / "scratch" / "notes.txt").read_text(encoding="utf-8"),
            "somebody else's scratch\n",
        )


class AlreadyClean(_CheckpointCase):
    """Nothing to checkpoint is refused, and refused before anything is staged."""

    def test_a_clean_repository_declines_preparation(self) -> None:
        self.record_turn(paths=("notes.md",))
        adapter = self.adapter()
        before = self.head()

        declined = adapter.prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("nothing_to_commit", declined.detail)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.porcelain(), [])

    def test_execution_without_preparation_commits_nothing(self) -> None:
        self.record_turn(paths=("notes.md",))
        adapter = self.adapter()
        before = self.head()

        receipt = adapter.execute(self.action())

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("not_prepared", receipt.detail)
        self.assertEqual(self.head(), before)


class AttributionRefusal(_CheckpointCase):
    """A change this run cannot be shown to own stops the whole checkpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")
        self.write("somebody-else.md", "not this run\n")

    def test_preparation_declines_and_leaves_the_worktree_alone(self) -> None:
        adapter = self.adapter()
        before = self.head()

        declined = adapter.prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("changes_unattributed", declined.detail)
        self.assertEqual(self.head(), before)
        self.assertEqual(
            sorted(self.porcelain()), ["?? notes.md", "?? somebody-else.md"]
        )

    def test_a_declined_preparation_leaves_nothing_to_execute(self) -> None:
        adapter = self.adapter()
        action = self.action()

        adapter.prepare(action)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("not_prepared", receipt.detail)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
