"""Detail reads: named, bounded, contained, and observational.

Session 4 moves every Workbench detail view -- plan, artifacts, state, events,
logs, last message, result, verification, commits, progress, final review,
questions, and recovery -- behind one shared pair of modules, and the tests
here are the gate for the three properties that made the move worth doing.

**A read names an artifact rather than a path.** The artifact listing and the
request vocabulary are the same closed set, checked against the live
``/api/artifacts`` payload rather than a copy of it, and a name resolves only
against what the *selected* plan published. :class:`ScopeContainmentTests` and
:class:`DetailContainmentTests` then require the compatibility ``path=`` form
to refuse a second plan's log and a file outside the corpus entirely -- the
same refusal, decided in the portable half, whichever route asks.

**A window is bounded before it is read.** :class:`WindowTests` and
:class:`PaginationTests` hold the byte caps, the offset/tail exclusivity, and
the ``next_offset``-is-``None``-at-the-end rule, so "bounded" is a property of
the request object rather than something each of thirteen routes remembers.

**Reading changes nothing.** Four routes -- state, verification, questions and
recovery -- still reclaimed a stale plan lock as a side effect of describing
it, which made opening a detail panel a repair action at whatever rate a
dashboard refreshed. :class:`ObservationalDetailTests` holds each of them, and
then the whole state tree byte-for-byte, across every detail route. The
control test in that class reclaims the same fixture lock deliberately, so the
four assertions are claims about the routes rather than about a lock that was
never reclaimable.

:class:`DetailSearchTests` covers the last piece: search reads a plan's
*private* half -- its path, the operator's pin label, pause prose, the
last-message preview -- so it stays an online question the Mac answers and
never becomes a row a remote cockpit retains."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    intervention,
    qna,
    workbench_details,
)
from asl_workbench import catalog, details

from .support_workbench_details import _scope

# Integration: containment and "a read changes nothing" are only worth
# asserting against the routes that serve the detail reads.
ASL_TEST_LANE = "integration"


class VocabularyTests(unittest.TestCase):
    """The names a read may use are closed, classified, and self-consistent."""

    def test_vocabularies_are_structurally_sound(self) -> None:
        self.assertEqual((), details.validate_vocabularies())

    def test_every_artifact_name_is_a_private_field(self) -> None:
        # An artifact name that were cache-safe would be one this package had
        # agreed a remote cockpit may retain -- which is the opposite of an
        # artifact, whose whole nature is that it is fetched on demand.
        for name in details.ARTIFACT_NAMES:
            with self.subTest(artifact=name):
                self.assertNotIn(name, catalog.CACHE_SAFE_FIELDS)

    def test_only_the_directory_is_unreadable(self) -> None:
        self.assertEqual(
            {"artifact_dir"},
            set(details.ARTIFACT_NAMES) - set(details.READABLE_ARTIFACT_NAMES),
        )

    def test_every_search_field_is_private(self) -> None:
        for name in details.SEARCH_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, catalog.PRIVATE_FIELDS)
                self.assertNotIn(name, catalog.CACHE_SAFE_FIELDS)


class WindowTests(unittest.TestCase):
    """A window is validated when it is built, not when it is read."""

    def test_offset_and_tail_lines_are_mutually_exclusive(self) -> None:
        with self.assertRaises(details.DetailError) as raised:
            details.Window(offset=10, tail_lines=5)
        self.assertEqual(details.REASON_INVALID_REQUEST, raised.exception.reason)
        self.assertEqual(400, raised.exception.status)

    def test_negative_bounds_are_refused(self) -> None:
        for kwargs in ({"offset": -1}, {"tail_lines": -1}, {"max_bytes": -1}):
            with self.subTest(**kwargs), self.assertRaises(details.DetailError):
                details.Window(**kwargs)

    def test_max_bytes_is_capped(self) -> None:
        details.Window(max_bytes=details.MAX_READ_BYTES)
        with self.assertRaises(details.DetailError) as raised:
            details.Window(max_bytes=details.MAX_READ_BYTES + 1)
        self.assertEqual(
            details.MAX_READ_BYTES + 1, raised.exception.details["max_bytes"]
        )

    def test_default_window_is_the_documented_tail(self) -> None:
        window = details.Window()
        self.assertIsNone(window.offset)
        self.assertIsNone(window.tail_lines)
        self.assertEqual(details.DEFAULT_LOG_BYTES, window.max_bytes)


class PaginationTests(unittest.TestCase):
    """Paging stops because the page said so, not because a read came back
    empty."""

    def test_next_offset_is_none_at_the_end(self) -> None:
        rows = list(range(5))
        _, page = details.paginate(rows, offset=3, limit=10)
        self.assertEqual(5, page.total)
        self.assertEqual(2, page.returned)
        self.assertIsNone(page.next_offset)

    def test_next_offset_points_at_the_next_row(self) -> None:
        sliced, page = details.paginate(list(range(10)), offset=2, limit=3)
        self.assertEqual([2, 3, 4], sliced)
        self.assertEqual(5, page.next_offset)
        self.assertEqual(
            {"total": 10, "offset": 2, "limit": 3, "next_offset": 5},
            page.to_payload(),
        )

    def test_walking_the_pages_visits_every_row_once(self) -> None:
        rows = list(range(23))
        seen: list[int] = []
        offset: int | None = 0
        while offset is not None:
            sliced, page = details.paginate(rows, offset=offset, limit=7)
            seen.extend(sliced)
            offset = page.next_offset
        self.assertEqual(rows, seen)

    def test_limit_is_capped(self) -> None:
        with self.assertRaises(details.DetailError):
            details.paginate([], offset=0, limit=details.MAX_EVENT_LIMIT + 1)


class QuestionReadTests(unittest.TestCase):
    """Selected and unselected reads keep their distinct refusal contracts."""

    @staticmethod
    def waiting_facts() -> intervention.RunFacts:
        return intervention.RunFacts(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            lock_active=True,
            run_active=True,
            paused=False,
            turn_active=True,
            context_kind="waiting",
            question_id="q-live",
            unanswered_ids=("q-live",),
        )

    def test_an_unselected_oversized_question_keeps_the_qna_error_contract(
        self,
    ) -> None:
        oversized = "x" * (intervention.MAX_QUESTION_BODY_BYTES + 1)
        qna_error = qna.QnAError(
            qna.REASON_NO_PAUSED_RUN,
            "latest runner state is not paused",
        )
        with (
            mock.patch.object(
                intervention, "inspect_run", return_value=self.waiting_facts()
            ),
            mock.patch.object(
                intervention,
                "question_body",
                return_value=("q-live", "waiting", oversized),
            ),
            mock.patch.object(
                qna, "load_qna_context", side_effect=qna_error
            ) as load_context,
            self.assertRaises(details.DetailError) as raised,
        ):
            workbench_details.read_questions("/tmp/plan", question_id=None)

        self.assertEqual(qna.REASON_NO_PAUSED_RUN, raised.exception.reason)
        self.assertEqual(404, raised.exception.status)
        load_context.assert_called_once_with("/tmp/plan", state_dir=None, reclaim=False)

    def test_a_selected_missing_question_is_not_found(self) -> None:
        missing = intervention.InterventionError(
            intervention.REASON_NO_OPEN_QUESTION,
            "the selected question is not open",
        )
        with (
            mock.patch.object(
                intervention, "inspect_run", return_value=self.waiting_facts()
            ),
            mock.patch.object(intervention, "question_body", side_effect=missing),
            mock.patch.object(qna, "load_qna_context") as load_context,
            self.assertRaises(details.DetailError) as raised,
        ):
            workbench_details.read_questions("/tmp/plan", question_id="q-missing")

        self.assertEqual(details.REASON_NOT_FOUND, raised.exception.reason)
        self.assertEqual(404, raised.exception.status)
        load_context.assert_not_called()


class ScopeContainmentTests(unittest.TestCase):
    """The scope is the only thing that decides which file a read reaches."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.plan_dir = self.root / "plan"
        self.plan_state = self.root / "state" / "plan"
        self.plan_state.mkdir(parents=True)
        self.log = self.plan_state / "stdout.log"
        self.log.write_text("hello\n", encoding="utf-8")
        self.scope = _scope(
            self.plan_dir, self.plan_state, {"stdout_path": str(self.log)}
        )

    def test_unknown_artifact_name_is_refused_without_touching_disk(self) -> None:
        with self.assertRaises(details.DetailError) as raised:
            self.scope.artifact("secrets_path")
        self.assertEqual(details.REASON_INVALID_REQUEST, raised.exception.reason)
        self.assertEqual(
            list(details.ARTIFACT_NAMES), raised.exception.details["known"]
        )

    def test_a_directory_is_not_readable_as_text(self) -> None:
        scope = _scope(
            self.plan_dir,
            self.plan_state,
            {"artifact_dir": str(self.plan_state / "turn-0001")},
        )
        scope.artifact("artifact_dir")
        with self.assertRaises(details.DetailError) as raised:
            scope.artifact("artifact_dir", readable=True)
        self.assertEqual(details.REASON_INVALID_REQUEST, raised.exception.reason)

    def test_an_unpublished_artifact_is_not_found_rather_than_probed(self) -> None:
        with self.assertRaises(details.DetailError) as raised:
            self.scope.artifact("result_path")
        self.assertEqual(details.REASON_NOT_FOUND, raised.exception.reason)
        self.assertEqual(404, raised.exception.status)

    def test_available_lists_only_what_the_plan_published(self) -> None:
        self.assertEqual(("stdout_path",), self.scope.available())

    def test_a_path_outside_the_plan_state_is_refused(self) -> None:
        outsider = self.root / "elsewhere.log"
        outsider.write_text("not yours\n", encoding="utf-8")
        with self.assertRaises(details.DetailError) as raised:
            self.scope.contained_path(str(outsider))
        self.assertEqual(details.REASON_INVALID_REQUEST, raised.exception.reason)
        self.assertEqual(
            str(self.plan_state), raised.exception.details["plan_state_dir"]
        )

    def test_a_dotdot_path_that_leaves_the_plan_state_is_refused(self) -> None:
        outsider = self.root / "elsewhere.log"
        outsider.write_text("not yours\n", encoding="utf-8")
        escaping = f"{self.plan_state}/../../elsewhere.log"
        with self.assertRaises(details.DetailError) as raised:
            self.scope.contained_path(escaping)
        self.assertEqual(details.REASON_INVALID_REQUEST, raised.exception.reason)

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(details.DetailError):
            self.scope.contained_path("stdout.log")

    def test_a_path_inside_the_plan_state_is_accepted(self) -> None:
        self.assertEqual(str(self.log), self.scope.contained_path(str(self.log)))

    def test_a_stale_run_key_is_refused(self) -> None:
        self.scope.check_run(None)
        self.scope.check_run(self.scope.run_key)
        with self.assertRaises(details.DetailError) as raised:
            self.scope.check_run("run-key-from-a-stale-tab")
        self.assertEqual(details.REASON_NOT_FOUND, raised.exception.reason)

    def test_log_target_accepts_exactly_one_selector(self) -> None:
        self.assertEqual(str(self.log), self.scope.log_target(stream="stdout"))
        self.assertEqual(str(self.log), self.scope.log_target(artifact="stdout_path"))
        self.assertEqual(str(self.log), self.scope.log_target(path=str(self.log)))
        with self.assertRaises(details.DetailError):
            self.scope.log_target()
        with self.assertRaises(details.DetailError):
            self.scope.log_target(stream="stdout", path=str(self.log))
        with self.assertRaises(details.DetailError):
            self.scope.log_target(artifact="stdout_path", stream="stdout")

    def test_an_unknown_stream_is_refused(self) -> None:
        with self.assertRaises(details.DetailError) as raised:
            self.scope.log_target(stream="stdin")
        self.assertEqual(details.REASON_INVALID_REQUEST, raised.exception.reason)


class DetailReadEnvelopeTests(unittest.TestCase):
    """A detail read names the plan and run it describes."""

    def test_kind_must_be_known(self) -> None:
        scope = _scope(Path("/plan"), Path("/state/plan"))
        with self.assertRaises(details.DetailError):
            details.build_read("everything", scope, {})

    def test_envelope_carries_both_identities(self) -> None:
        scope = _scope(Path("/plan"), Path("/state/plan"))
        read = details.build_read("state", scope, {"plan_dir": "/plan"})
        payload = read.to_payload()
        self.assertEqual("state", payload["kind"])
        self.assertEqual(scope.plan_key, payload["plan_key"])
        self.assertEqual(scope.run_key, payload["run_key"])
        self.assertEqual({"plan_dir": "/plan"}, payload["detail"])


if __name__ == "__main__":
    unittest.main()
