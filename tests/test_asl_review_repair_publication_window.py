"""Repair publication window durability tests."""

import json
import unittest
from unittest import mock

from agent_session_loop import (
    authoring_transaction,
    plans_errors,
    review_repair,
    review_repair_publication,
    review_repair_validation,
)
from tests.review_repair_support import (
    PublicationWindowFixtures,
    _SimulatedCrash,
)

ASL_TEST_LANE = "integration"


class ReviewRepairPublicationWindowTests(PublicationWindowFixtures, unittest.TestCase):
    """Materialize repair children through the crash-safe publication window."""

    def test_the_happy_path_publishes_children_and_retires_the_window(self) -> None:
        result = self._materialize()

        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertFalse(result.rolled_back)
        self.assertIsNone(result.rollback)
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())
        self.assertIn(
            "**Readiness**: loop-ready",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_a_blocked_loop_ready_gate_withdraws_the_children_it_authored(
        self,
    ) -> None:
        before = self._plan_bytes()

        with mock.patch.object(
            review_repair_validation,
            "_make_children_executable",
            return_value=self._blocked_gate(non_bypassable=[]),
        ):
            result = self._materialize()

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual((), result.children)
        self.assertTrue(result.rolled_back)
        assert result.rollback is not None
        self.assertEqual([], result.rollback["errors"])
        self.assertIn("session-2.1.md", result.rollback["removed"])
        self.assertIn("withdrawn after authoring", result.error or "")
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())

    def test_allow_not_loop_ready_still_retains_bypassable_children(self) -> None:
        with mock.patch.object(
            review_repair_validation,
            "_make_children_executable",
            return_value=self._blocked_gate(non_bypassable=[]),
        ):
            result = self._materialize(allow_not_loop_ready=True)

        self.assertEqual(
            review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY, result.reason
        )
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertFalse(result.loop_ready)
        self.assertFalse(result.rolled_back)
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())

    def test_a_non_bypassable_blocker_withdraws_children_even_when_allowed(
        self,
    ) -> None:
        before = self._plan_bytes()

        with mock.patch.object(
            review_repair_validation,
            "_make_children_executable",
            return_value=self._blocked_gate(non_bypassable=["repository-footprint"]),
        ):
            result = self._materialize(allow_not_loop_ready=True)

        self.assertEqual((), result.children)
        self.assertTrue(result.rolled_back)
        self.assertEqual(before, self._plan_bytes())

    def test_a_failed_commit_leaves_the_plan_and_retires_the_window(self) -> None:
        before = self._plan_bytes()

        with mock.patch.object(
            authoring_transaction.PlanTransaction,
            "commit",
            side_effect=plans_errors.PlanError("injected commit failure"),
        ):
            result = self._materialize()

        self.assertEqual(review_repair.REVIEW_REPAIR_REASON_MALFORMED, result.reason)
        self.assertFalse(result.ok)
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_an_interrupt_between_authoring_and_the_gate_rolls_back(self) -> None:
        before = self._plan_bytes()

        with (
            mock.patch.object(
                authoring_transaction,
                "refresh_readiness_metadata",
                side_effect=KeyboardInterrupt(),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._materialize()

        self.assertEqual(before, self._plan_bytes())
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertFalse(self._window_dir().exists())

    def test_an_interrupt_inside_the_gate_rolls_back(self) -> None:
        before = self._plan_bytes()

        with (
            mock.patch.object(
                review_repair_validation,
                "_make_children_executable",
                side_effect=KeyboardInterrupt(),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._materialize()

        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_a_crash_after_authoring_is_rolled_back_by_the_next_locked_caller(
        self,
    ) -> None:
        before = self._crash_mid_publication("authoring")
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("rolled-back", record["reason"])
        self.assertEqual([], record["errors"])
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_a_crash_inside_the_gate_is_rolled_back_by_the_next_locked_caller(
        self,
    ) -> None:
        before = self._crash_mid_publication("validating")

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("validating", record["phase"])
        self.assertEqual(before, self._plan_bytes())

    def test_a_crash_in_the_publishing_phase_is_rolled_back(
        self,
    ) -> None:
        before = self._plan_bytes()
        advance_phase = review_repair_publication._advance_publication_phase

        def crash_after_publishing_marker(
            window: review_repair_publication._PublicationWindow, phase: str
        ) -> None:
            advance_phase(window, phase)
            if phase == review_repair.PUBLICATION_PHASE_PUBLISHING:
                raise _SimulatedCrash()

        with (
            mock.patch.object(
                review_repair_publication,
                "_advance_publication_phase",
                side_effect=crash_after_publishing_marker,
            ),
            self.assertRaises(_SimulatedCrash),
        ):
            self._materialize()

        self.assertTrue(self._window_dir().is_dir())
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertIn(
            "**Readiness**: loop-ready",
            (self.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("publishing", record["phase"])
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_a_crashed_publication_is_recovered_by_the_next_materialization(
        self,
    ) -> None:
        before = self._crash_mid_publication("validating")

        result = self._materialize()

        self.assertTrue(result.loop_ready, msg=str(result.blockers))
        self.assertEqual(("2.1",), tuple(c.session_id.value for c in result.children))
        self.assertNotEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_recovery_is_idempotent(self) -> None:
        before = self._crash_mid_publication("validating")

        first = review_repair.recover_review_repair_publication(self.plan_dir)
        second = review_repair.recover_review_repair_publication(self.plan_dir)

        assert first is not None
        self.assertTrue(first["recovered"])
        self.assertIsNone(second)
        self.assertEqual(before, self._plan_bytes())

    def test_nothing_to_recover_reports_nothing(self) -> None:
        self.assertIsNone(
            review_repair.recover_review_repair_publication(self.plan_dir)
        )

    def test_a_window_without_a_journal_never_opened_and_is_discarded(self) -> None:
        before = self._plan_bytes()
        self._window_dir().mkdir(parents=True)

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertTrue(record["recovered"])
        self.assertEqual("unopened-publication-window", record["reason"])
        self.assertEqual(before, self._plan_bytes())
        self.assertFalse(self._window_dir().exists())

    def test_an_undecodable_journal_reads_as_torn_rather_than_crashing(
        self,
    ) -> None:
        # Bytes that are not UTF-8, and nesting deep enough that the decoder
        # raises RecursionError, are no more a journal than a torn document.
        journal = self._window_dir() / review_repair.REPAIR_PUBLICATION_JOURNAL_NAME
        for raw in (b"{\xff}\n", b"[" * 1_000_000):
            with self.subTest(raw=raw[:4]):
                before = self._plan_bytes()
                self._window_dir().mkdir(parents=True)
                journal.write_bytes(raw)

                record = review_repair.recover_review_repair_publication(self.plan_dir)

                assert record is not None
                self.assertEqual("unopened-publication-window", record["reason"])
                self.assertEqual(before, self._plan_bytes())
                self.assertFalse(self._window_dir().exists())

    def test_an_unsupported_publication_schema_fails_closed(self) -> None:
        window = self._window_dir()
        window.mkdir(parents=True)
        (window / review_repair.REPAIR_PUBLICATION_JOURNAL_NAME).write_text(
            json.dumps({"version": 999, "entries": []}), encoding="utf-8"
        )

        record = review_repair.recover_review_repair_publication(self.plan_dir)

        assert record is not None
        self.assertFalse(record["recovered"])
        self.assertEqual("unsupported-publication-schema", record["reason"])
        self.assertTrue(window.is_dir())

        result = self._materialize()

        self.assertFalse(result.ok)
        self.assertIn("could not be rolled back", result.error or "")
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())
        self.assertTrue(window.is_dir())


if __name__ == "__main__":
    unittest.main()
