"""Explicit cleanup: purge, archive, restore and unlock confirmation contracts.

**Destroying takes saying so.** :class:`ExplicitCleanupTests` holds the
confirmation contracts that did *not* change, which is the half a reader of
"retention stopped deleting" could easily mis-read as "deletion got easier". A
purge without a matching count removes nothing; an archive without its path
retyped archives nothing; a plan a run still holds is refused outright.
"""

from __future__ import annotations

import os

from agent_session_loop import (
    workbench_archive,
)
from asl_workbench import archive as portable_archive

from .support import (
    _write_lock_file,
    write_plan,
)
from .workbench_archive_support import DEAD_PID, ArchiveFixture

ASL_TEST_LANE = "integration"


class ExplicitCleanupTests(ArchiveFixture):
    """The confirmations that did not change, held so they cannot drift."""

    def test_purging_without_a_matching_count_removes_nothing(self) -> None:
        # Pinned by `changes.KEY_EXPLICIT_ARCHIVE_CLEANUP`. Retention stopping
        # deleting is not deletion getting easier: the count is checked before
        # a single entry is touched, so a mismatch is a no-op on disk.
        ids = [self.archive_one(self.make_plan(name)) for name in ("a", "b")]
        before = self.archive_tree()

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.purge_entries(ids, 1, state_dir=self.state_dir)

        self.assertEqual("confirmation-mismatch", ctx.exception.reason)
        self.assertEqual(400, ctx.exception.status)
        self.assertEqual(before, self.archive_tree())

    def test_purging_with_a_matching_count_empties_the_selection(self) -> None:
        kept = self.archive_one(self.make_plan("kept"))
        doomed = self.archive_one(self.make_plan("doomed"))

        result = workbench_archive.purge_entries([doomed], 1, state_dir=self.state_dir)

        self.assertEqual(1, result.removed_count)
        self.assertEqual(0, result.failed_count)
        self.assertFalse(
            workbench_archive.archive_entry_dir(doomed, self.state_dir).exists()
        )
        self.assertTrue(
            workbench_archive.archive_entry_dir(kept, self.state_dir).exists()
        )

    def test_one_bad_id_in_a_purge_does_not_spare_the_rest(self) -> None:
        # Past the confirmation gate each id is its own outcome: an id that
        # names nothing is a row saying so, not a reason to abandon the others.
        good = self.archive_one()

        result = workbench_archive.purge_entries(
            [good, "../escape"], 2, state_dir=self.state_dir
        )

        by_id = {outcome.archive_id: outcome for outcome in result.outcomes}
        self.assertTrue(by_id[good].removed)
        self.assertIsNotNone(by_id["../escape"].error)
        self.assertEqual(1, result.removed_count)
        self.assertEqual(1, result.failed_count)

    def test_archiving_without_the_path_retyped_archives_nothing(self) -> None:
        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.archive_one(
                str(self.canonical),
                str(self.canonical / "not-the-same"),
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                include_default_dirs=False,
            )

        self.assertEqual("confirmation-mismatch", ctx.exception.reason)
        self.assertTrue(self.canonical.exists())
        self.assertEqual({}, self.archive_tree())

    def test_a_plan_a_run_still_holds_is_refused(self) -> None:
        # The safety gate is ASL's answer, asked once. Two surfaces asking it
        # separately is how one of them ends up archiving a live run.
        _write_lock_file(self.canonical, self.state_dir, pid=os.getpid())

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.archive_one(
                str(self.canonical),
                str(self.canonical),
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                include_default_dirs=False,
            )

        self.assertEqual("active-lock", ctx.exception.reason)
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual(portable_archive.SCOPE_PLAN, ctx.exception.scope)
        self.assertTrue(self.canonical.exists())

    def test_a_bulk_archive_reports_one_row_per_plan(self) -> None:
        second = self.make_plan("beta")
        blocked = self.make_plan("blocked")
        _write_lock_file(blocked, self.state_dir, pid=os.getpid())

        bulk = workbench_archive.archive_many(
            [str(self.canonical), str(blocked), str(second)],
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )

        self.assertEqual(3, len(bulk.outcomes))
        self.assertEqual(2, bulk.archived_count)
        self.assertEqual(1, bulk.failed_count)
        by_target = {outcome.target: outcome for outcome in bulk.outcomes}
        self.assertFalse(by_target[str(blocked)].ok)
        self.assertEqual("active-lock", by_target[str(blocked)].reason)
        # The blocked plan is still there; the two beside it are gone.
        self.assertTrue(blocked.exists())
        self.assertFalse(self.canonical.exists())
        self.assertFalse(second.exists())

    def test_a_restore_refuses_rather_than_overwriting_a_live_plan(self) -> None:
        archive_id = self.archive_one()
        write_plan(self.canonical, ("pending",))

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.restore_entry(
                archive_id,
                state_dir=self.state_dir,
                registry_path=self.registry_path,
            )

        self.assertEqual("restore-conflict", ctx.exception.reason)
        self.assertEqual(409, ctx.exception.status)
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )

    def test_a_restore_puts_the_plan_back_where_it_came_from(self) -> None:
        archive_id = self.archive_one()
        self.assertFalse(self.canonical.exists())

        result = workbench_archive.restore_entry(
            archive_id, state_dir=self.state_dir, registry_path=self.registry_path
        )

        self.assertTrue(result.plan_restored)
        self.assertTrue(self.canonical.exists())
        self.assertEqual({}, self.archive_tree())

    def test_restoring_something_that_is_not_there_is_a_miss_not_a_failure(
        self,
    ) -> None:
        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.restore_entry(
                "20000101T000000Z-deadbeefcafe",
                state_dir=self.state_dir,
                registry_path=self.registry_path,
            )

        self.assertEqual("not-found", ctx.exception.reason)
        self.assertEqual(404, ctx.exception.status)

    def test_unlock_is_the_one_place_reclaim_is_still_deliberate(self) -> None:
        # Every read stopped reclaiming in sessions 2 and 4. This is what is
        # left: an operator saying the lock's owner is gone.
        _write_lock_file(self.canonical, self.state_dir, pid=DEAD_PID)

        result = workbench_archive.unlock(str(self.canonical), state_dir=self.state_dir)

        self.assertTrue(result.removed)
        self.assertTrue(result.was_stale)
        self.assertFalse(self.lock_path().exists())

    def test_unlock_refuses_a_lock_whose_owner_is_alive(self) -> None:
        _write_lock_file(self.canonical, self.state_dir, pid=os.getpid())

        with self.assertRaises(workbench_archive.ArchiveRefused) as ctx:
            workbench_archive.unlock(str(self.canonical), state_dir=self.state_dir)

        self.assertEqual("active-lock", ctx.exception.reason)
        self.assertEqual(409, ctx.exception.status)
        self.assertTrue(self.lock_path().exists())
