"""One path for every registry and archive mutation, and reads that only read.

Session 8's claim has two halves, and they pull in opposite directions. The
first is *consolidation*: pin, unpin, retention, archive, bulk archive, restore,
purge and unlock had grown a copy per surface, and they agreed only because
someone kept them agreeing. The second is *restraint*: one of those paths used
to delete, unasked, as a side effect of a read. Consolidating a deleting read
would have made it a deleting read on every surface at once, so the two halves
had to land together.

**The rules are portable and there is one set of them.**
:class:`PortableArchiveRulesTests` holds :mod:`asl_workbench.archive`: the
traversal rule for an archive id, the two confirmation contracts, the retention
classification, and the refusal-scope table a surface with no status codes
translates through. It imports nothing from ASL, so the Pi half stays loadable.

**Retention marks; it does not remove.**
:class:`RetentionIsAThresholdTests` is the session's stated done-condition held
directly: a review of an archive full of long-expired entries returns them all,
flagged, with every byte still on disk -- and does so however many times it is
run. The deleted maintenance pass is asserted gone rather than merely unwired,
because an unwired deleter is one call site away from returning.
"""

from __future__ import annotations

import json
import unittest

from agent_session_loop import (
    workbench_archive,
    workbench_cli,
    workbench_discovery,
    workbench_registry,
)
from asl_workbench import archive as portable_archive

from .support import (
    assert_no_runner_import,
)
from .workbench_archive_support import NOW, ArchiveFixture, _iso


class PortableArchiveRulesTests(unittest.TestCase):
    """The rules both halves share, proven without ASL in the room."""

    def test_the_portable_rules_do_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, portable_archive)

    def test_an_archive_id_may_not_leave_the_archive_root(self) -> None:
        # The rule has one definition; `workbench_archive._is_valid_archive_id` calls
        # it. A second copy is how a traversal one surface rejects becomes one
        # another accepts.
        for good in ("20260917T120000Z-abcdef123456", "entry-1"):
            self.assertTrue(portable_archive.valid_archive_id(good), good)
        for bad in ("", "   ", ".", "..", "../escape", "a/b", "a\\b", "a\0b", None):
            self.assertFalse(portable_archive.valid_archive_id(bad), repr(bad))

    def test_the_provider_and_the_rules_reject_the_same_ids(self) -> None:
        for candidate in ("..", "../escape", "a/b", "", "entry-1"):
            self.assertEqual(
                portable_archive.valid_archive_id(candidate),
                workbench_archive._is_valid_archive_id(candidate),
                candidate,
            )

    def test_a_retention_window_must_be_a_positive_whole_number_of_days(
        self,
    ) -> None:
        self.assertEqual(7, portable_archive.require_retention_days(7))
        for bad in (0, -1, True, False, 1.5, "7", None):
            with self.assertRaises(portable_archive.ArchiveRequestError) as ctx:
                portable_archive.require_retention_days(bad)
            self.assertEqual(
                portable_archive.REASON_INVALID_REQUEST, ctx.exception.reason
            )

    def test_a_purge_count_must_equal_the_selection(self) -> None:
        self.assertEqual(2, portable_archive.require_count_confirmation(["a", "b"], 2))
        with self.assertRaises(portable_archive.ArchiveRequestError) as ctx:
            portable_archive.require_count_confirmation(["a", "b"], 3)
        self.assertEqual(
            portable_archive.REASON_CONFIRMATION_MISMATCH, ctx.exception.reason
        )
        self.assertEqual({"confirm_count": 3, "archive_ids": 2}, ctx.exception.details)

    def test_an_archive_confirmation_compares_canonical_paths(self) -> None:
        # Comparing raw strings would let a trailing slash or a `.` segment
        # pass for a confirmation the operator never typed.
        portable_archive.require_path_confirmation("/plans/alpha", "/plans/alpha")
        with self.assertRaises(portable_archive.ArchiveRequestError) as ctx:
            portable_archive.require_path_confirmation("/plans/alpha", "/plans/beta")
        self.assertEqual(
            portable_archive.REASON_CONFIRMATION_MISMATCH, ctx.exception.reason
        )

    def test_retention_classifies_and_carries_no_removal_count(self) -> None:
        review = portable_archive.cleanup_review(
            [
                {"archive_id": "old", "archived_at": _iso(40)},
                {"archive_id": "new", "archived_at": _iso(2)},
                {"archive_id": "exact", "archived_at": _iso(30)},
                {"archive_id": "broken", "archived_at": "not-a-timestamp"},
            ],
            now=NOW,
            retention_days=30,
        )

        self.assertEqual(["old", "exact"], list(review.candidate_ids))
        self.assertEqual(1, review.undated_count)
        self.assertEqual(portable_archive.CLEANUP_RETAINED, review.verdict("new").state)
        self.assertEqual(
            portable_archive.CLEANUP_UNDATED, review.verdict("broken").state
        )
        # The absence is the point: there is no field a caller could read as
        # "the pass ran", because there is no pass.
        payload = review.to_payload()
        self.assertNotIn("removed_count", payload)
        self.assertNotIn("failed_count", payload)

    def test_only_purging_is_destructive_and_it_needs_confirming(self) -> None:
        self.assertEqual(
            {portable_archive.OPERATION_PURGE},
            set(portable_archive.DESTRUCTIVE_OPERATIONS),
        )
        for operation in portable_archive.OPERATIONS:
            if portable_archive.is_destructive(operation):
                self.assertTrue(
                    portable_archive.requires_confirmation(operation), operation
                )

    def test_every_reason_has_a_scope_and_an_unknown_one_is_not_the_clients_fault(
        self,
    ) -> None:
        for reason, scope in portable_archive.SCOPE_FOR_REASON.items():
            self.assertIn(scope, portable_archive.REFUSAL_SCOPES, reason)
            self.assertIn(scope, portable_archive.STATUS_FOR_SCOPE, reason)
        # Telling a client to rewrite a body that was fine is the worse error,
        # so an unrecognised refusal is "unavailable", not "bad request".
        self.assertEqual(
            portable_archive.SCOPE_UNAVAILABLE,
            portable_archive.scope_for_reason("something-new"),
        )


class RetentionIsAThresholdTests(ArchiveFixture):
    """Browsing never deletes. The session's done-condition, held directly."""

    def test_the_deleting_maintenance_pass_is_gone_not_merely_unwired(self) -> None:
        # Asserted on the module rather than on a call site: a pass that still
        # exists is one import away from being called again, and the next
        # surface to grow an archive view is the one that would call it.
        for module in (workbench_registry, workbench_discovery, workbench_cli):
            self.assertFalse(hasattr(module, "maintain_archive"))

    def test_reviewing_a_wholly_expired_archive_removes_nothing(self) -> None:
        ids = [self.archive_one(self.make_plan(name)) for name in ("a", "b", "c")]
        for archive_id in ids:
            self.backdate(archive_id, days=400)
        before = self.archive_tree()

        view = self.review()

        self.assertEqual(sorted(ids), sorted(view.review.candidate_ids))
        self.assertEqual(len(ids), view.review.candidate_count)
        self.assertEqual(before, self.archive_tree())

    def test_reviewing_repeatedly_is_still_a_read(self) -> None:
        # Once could be luck. The Pi half polls, so the property that matters
        # is that the hundredth read is the same as the first.
        archive_id = self.archive_one()
        self.backdate(archive_id, days=400)
        before = self.archive_tree()

        for _ in range(5):
            self.review()

        self.assertEqual(before, self.archive_tree())

    def test_each_entry_reports_which_side_of_the_window_it_is_on(self) -> None:
        old = self.archive_one(self.make_plan("old"))
        fresh = self.archive_one(self.make_plan("fresh"))
        self.backdate(old, days=90)
        self.backdate(fresh, days=1)

        payloads = {
            entry["archive_id"]: entry for entry in self.review().entry_payloads()
        }

        self.assertEqual("eligible", payloads[old]["cleanup_state"])
        self.assertTrue(payloads[old]["cleanup_candidate"])
        self.assertEqual("retained", payloads[fresh]["cleanup_state"])
        self.assertFalse(payloads[fresh]["cleanup_candidate"])

    def test_shortening_retention_reclassifies_and_is_reversible(self) -> None:
        # Under the old pass this was a one-way door: shortening the window
        # emptied everything it had just expired, and widening it back restored
        # nothing. The entry is still there to widen back to.
        archive_id = self.archive_one()
        self.backdate(archive_id, days=10)
        self.assertEqual([], list(self.review().review.candidate_ids))

        workbench_archive.set_retention(1, registry_path=self.registry_path)
        self.assertEqual([archive_id], list(self.review().review.candidate_ids))

        workbench_archive.set_retention(365, registry_path=self.registry_path)
        self.assertEqual([], list(self.review().review.candidate_ids))
        self.assertTrue(
            workbench_archive.archive_entry_dir(archive_id, self.state_dir).exists()
        )

    def test_the_retention_window_reported_is_the_live_one(self) -> None:
        self.archive_one()
        workbench_archive.set_retention(3, registry_path=self.registry_path)

        self.assertEqual(3, self.review().retention_days)

    def test_an_unreadable_archive_date_is_never_a_cleanup_candidate(self) -> None:
        # "I cannot tell how old this is" must not resolve to "old enough".
        archive_id = self.archive_one()
        meta_path = (
            workbench_archive.archive_entry_dir(archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["archived_at"] = "whenever"
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        review = self.review().review

        self.assertEqual([], list(review.candidate_ids))
        self.assertEqual(1, review.undated_count)
