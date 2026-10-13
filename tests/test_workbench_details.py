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
never becomes a row a remote cockpit retains.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    intervention,
    plans,
    qna,
    state,
    state_locks,
    workbench_details,
)
from asl_workbench import catalog, details
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _http_request,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture

#: A pid this host can prove is not running, which is what makes the fixture
#: lock reclaimable and therefore makes "the read did not reclaim it" a claim
#: about the read.
DEAD_PID = _WORKBENCH_DEAD_PID

#: Every detail route, with the query it needs beyond ``plan_dir``. Driving the
#: observational check off one list is deliberate: a route added to the server
#: without being added here is a route nobody proved is observational, and the
#: route-coverage test below fails on exactly that.
DETAIL_ROUTES: tuple[tuple[str, dict[str, str]], ...] = (
    ("/api/archive", {}),
    ("/api/plan", {}),
    ("/api/artifacts", {}),
    ("/api/state", {}),
    ("/api/events", {}),
    ("/api/logs", {"stream": "stdout"}),
    ("/api/last-message", {}),
    ("/api/result", {}),
    ("/api/verification", {}),
    ("/api/commits", {}),
    ("/api/progress", {}),
    ("/api/final-review", {}),
    ("/api/questions", {}),
    ("/api/recovery", {}),
)


def _reclaimable_lock(plan_dir: Path, state_root: Path) -> Path:
    """A lock ``state`` would reclaim on sight, for a read to leave alone.

    Both halves matter. The pid is one this host can prove is not running, and
    the hostname is this host's -- ``state._read_lock_path`` reclaims only when
    the lock names the process table that refutes it, so a lock without a
    hostname is never reclaimed by anything and "the read left it alone" would
    be true of a read that reclaimed everything it could.
    :meth:`ObservationalDetailTests.test_the_fixture_lock_is_actually_reclaimable`
    is the standing check that this stays true.
    """
    return _write_lock_file(
        plan_dir,
        state_root,
        pid=DEAD_PID,
        hostname=socket.gethostname(),
    )


def _tree_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under ``root`` by relative path, with its size and bytes.

    Content rather than mtime, for the same reason the catalog tests use
    content: a read that rewrote a file with identical bytes is still a write,
    while a touched mtime is not the failure this program cares about and is
    flaky to assert on a fast machine.
    """
    fingerprint: dict[str, tuple[int, bytes]] = {}
    if not root.is_dir():
        return fingerprint
    for path in sorted(root.rglob("*")):
        if path.is_file():
            payload = path.read_bytes()
            fingerprint[str(path.relative_to(root))] = (len(payload), payload)
    return fingerprint


def _scope(
    plan_dir: Path,
    plan_state: Path,
    artifacts: dict[str, str | None] | None = None,
) -> details.DetailScope:
    """A scope with no artifact published unless the test publishes one."""
    table: dict[str, str | None] = dict.fromkeys(details.ARTIFACT_NAMES)
    table.update(artifacts or {})
    key = catalog.plan_key(str(plan_dir))
    return details.DetailScope(
        plan_key=key,
        plan_dir=str(plan_dir),
        plan_state_dir=str(plan_state),
        artifacts=table,
        run_key=catalog.run_key(key, "run-1"),
        run_id="run-1",
    )


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


class _DetailFixture(WorkbenchAPIFixture):
    """A seeded, paused plan with logs, artifacts, and a reclaimable lock."""

    assertEqual: Any
    assertTrue: Any

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))

    def seed_plan(self, name: str = "qna", *, paused: bool = True) -> Path:
        """One plan with the artifacts every detail route needs to answer.

        Modelled on the paused-run fixture the server tests already use: a
        completed turn that recorded a session id and a readable last message
        is what makes ``GET /api/questions`` answerable at all, and the same
        turn supplies the stdout/stderr/result artifacts the log, result, and
        artifact routes resolve.
        """
        plan_dir = self.plans_root / name
        if plan_dir.exists():
            shutil.rmtree(plan_dir)
        write_plan(plan_dir, ("pending", "pending"))
        canonical = plans.canonicalize_path(plan_dir)
        plan_state = state.plan_state_dir(canonical, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)

        artifact_dir = plan_state / "turn-0001"
        artifact_dir.mkdir(exist_ok=True)
        last_message = plan_state / "last-message.txt"
        last_message.write_text("Should we use 1a or 1b?\n", encoding="utf-8")
        stdout_path = plan_state / "stdout.log"
        stdout_path.write_text("".join(f"line {i}\n" for i in range(50)), "utf-8")
        stderr_path = plan_state / "stderr.log"
        stderr_path.write_text("warning: nothing\n", encoding="utf-8")
        result_path = plan_state / "result.json"
        result_path.write_text(json.dumps({"ok": True}), encoding="utf-8")
        (canonical / "final-review.md").write_text("# Review\n\nAll good.\n", "utf-8")
        (canonical / "final-review.json").write_text(
            json.dumps({"verdict": "pass"}), encoding="utf-8"
        )
        (plan_state / events.EVENTS_FILE_NAME).write_text(
            "".join(
                json.dumps(
                    {
                        "name": "turn_started",
                        "created_at": f"2026-06-19T14:0{i}:00+00:00",
                        "run_id": "run-1",
                        "payload": {"run_id": "run-1", "turn_index": i},
                    }
                )
                + "\n"
                for i in range(5)
            ),
            encoding="utf-8",
        )

        turn: dict[str, object] = {
            "run_id": "run-1",
            "turn_index": 1,
            "session_id": "1",
            "session": 1,
            "session_number": 1,
            "harness": "fake",
            "model": "test-model",
            "model_source": "default",
            "effort": "medium",
            "effort_source": "default",
            "artifact_dir": str(artifact_dir),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "last_message_path": str(last_message),
            "result_path": str(result_path),
        }
        _write_state_payload(
            canonical,
            self.state_dir,
            {
                "plan_dir": str(canonical),
                "ok": not paused,
                "paused": paused,
                "reason": "runner-pause",
                "pause_reason": "runner-pause",
                "pause_message": "agent asked a question",
                "pause_details": {"session": 1},
                "details": {"session": 1},
                "run_id": "run-1",
                "run_dir": str(plan_state / "runs" / "run-1"),
                "harness": "fake",
                "model": "test-model",
                "effort": "medium",
                "active_verification": None,
                "turns": [turn],
            },
        )
        return canonical

    def url(self, server: Any, route: str, **query: str) -> str:
        return f"{self.base_url(server)}{route}?{urllib.parse.urlencode(query)}"

    def get(self, server: Any, route: str, **query: str) -> tuple[int, dict, dict]:
        return _http_request(self.url(server, route, **query))


class DetailRouteTests(_DetailFixture, unittest.TestCase):
    """Every detail view still answers with the information it answered with."""

    def test_every_detail_route_answers(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server(plan_roots=(str(self.plans_root),))
        for route, extra in DETAIL_ROUTES:
            with self.subTest(route=route):
                status, body, _ = self.get(
                    server, route, plan_dir=str(plan_dir), **extra
                )
                self.assertEqual(200, status, body)
                self.assertTrue(body["ok"])
                self.assertEqual("workbench", body["command"])

    def test_route_list_covers_every_detail_route_the_server_serves(self) -> None:
        # The observational and containment checks below iterate DETAIL_ROUTES.
        # A detail route added to the server and not to that list would be a
        # route nobody proved anything about, so the list is checked against
        # the kinds the portable half declares.
        covered = {route.removeprefix("/api/") for route, _ in DETAIL_ROUTES}
        self.assertEqual(set(details.DETAIL_KINDS), covered)

    def test_artifacts_listing_is_the_request_vocabulary(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(server, "/api/artifacts", plan_dir=str(plan_dir))
        self.assertEqual(200, status)
        # Exactly the names a read may ask for: the descriptor a caller was
        # handed is the request it can make next.
        self.assertEqual(set(details.ARTIFACT_NAMES), set(body["artifacts"]))
        for name, descriptor in body["artifacts"].items():
            with self.subTest(artifact=name):
                self.assertEqual({"path", "exists", "kind", "size"}, set(descriptor))

    def test_a_named_artifact_reads_what_the_stream_alias_reads(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        _, by_stream, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), stream="stdout"
        )
        _, by_name, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), artifact="stdout_path"
        )
        self.assertEqual(by_stream["text"], by_name["text"])
        self.assertEqual(by_stream["path"], by_name["path"])

    def test_log_window_honours_tail_offset_and_byte_bounds(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        _, tailed, _ = self.get(
            server,
            "/api/logs",
            plan_dir=str(plan_dir),
            stream="stdout",
            tail_lines="3",
        )
        self.assertEqual(["line 47", "line 48", "line 49"], tailed["text"].splitlines())

        _, windowed, _ = self.get(
            server,
            "/api/logs",
            plan_dir=str(plan_dir),
            stream="stdout",
            offset="0",
            max_bytes="7",
        )
        self.assertEqual(0, windowed["offset"])
        self.assertEqual("line 0\n", windowed["text"])
        self.assertTrue(windowed["truncated_tail"])
        self.assertEqual(7, windowed["next_offset"])

    def test_log_window_refuses_offset_and_tail_together(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server,
            "/api/logs",
            plan_dir=str(plan_dir),
            stream="stdout",
            offset="0",
            tail_lines="3",
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_log_window_refuses_an_oversized_read(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server,
            "/api/logs",
            plan_dir=str(plan_dir),
            stream="stdout",
            max_bytes=str(details.MAX_READ_BYTES + 1),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_events_pagination_reports_the_page_it_cut(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        _, first, _ = self.get(
            server, "/api/events", plan_dir=str(plan_dir), offset="0", limit="2"
        )
        self.assertEqual(5, first["total"])
        self.assertEqual(2, len(first["events"]))
        self.assertEqual(2, first["next_offset"])
        _, last, _ = self.get(
            server, "/api/events", plan_dir=str(plan_dir), offset="4", limit="2"
        )
        self.assertIsNone(last["next_offset"])

    def test_final_review_carries_both_documents(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(server, "/api/final-review", plan_dir=str(plan_dir))
        self.assertEqual(200, status)
        self.assertIn("All good.", body["text"])
        self.assertEqual({"verdict": "pass"}, body["structured_review"])
        self.assertIsNone(body["structured_error"])

    def test_state_read_keeps_the_session_table(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(server, "/api/state", plan_dir=str(plan_dir))
        self.assertEqual(200, status)
        self.assertEqual(2, len(body["sessions"]))
        self.assertEqual("1", body["next_pending_id"])
        self.assertIn("counts", body)

    def test_questions_read_keeps_the_resolver_prompt(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(server, "/api/questions", plan_dir=str(plan_dir))
        self.assertEqual(200, status)
        self.assertIn("Should we use 1a or 1b?", body["last_message"])
        self.assertEqual("1", body["session_id"])


class DetailContainmentTests(_DetailFixture, unittest.TestCase):
    """No detail route can be talked into reading an unrelated file."""

    def test_a_path_outside_the_corpus_is_refused(self) -> None:
        plan_dir = self.seed_plan()
        secret = self.workbench_root / "secret.txt"
        secret.write_text("not an artifact\n", encoding="utf-8")
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), path=str(secret)
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertNotIn("not an artifact", json.dumps(body))

    def test_another_plans_log_is_refused(self) -> None:
        first = self.seed_plan("alpha")
        second = self.seed_plan("beta")
        other_log = state.plan_state_dir(second, self.state_dir) / "stdout.log"
        self.assertTrue(other_log.is_file())
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server, "/api/logs", plan_dir=str(first), path=str(other_log)
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_an_unknown_artifact_name_is_refused(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), artifact="etc_passwd"
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_the_artifact_directory_cannot_be_read_as_a_log(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), artifact="artifact_dir"
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_a_log_read_needs_a_selector(self) -> None:
        plan_dir = self.seed_plan()
        server = self.start_workbench_server()
        status, body, _ = self.get(server, "/api/logs", plan_dir=str(plan_dir))
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])


class ObservationalDetailTests(_DetailFixture, unittest.TestCase):
    """Inspecting a plan does not repair it."""

    def _locked_plan(self) -> tuple[Path, Path, bytes]:
        plan_dir = self.seed_plan()
        lock_path = _reclaimable_lock(plan_dir, self.state_dir)
        return plan_dir, lock_path, lock_path.read_bytes()

    def _assert_route_left_the_lock(self, route: str, **extra: str) -> None:
        plan_dir, lock_path, before = self._locked_plan()
        server = self.start_workbench_server(plan_roots=(str(self.plans_root),))
        status, body, _ = self.get(server, route, plan_dir=str(plan_dir), **extra)
        self.assertEqual(200, status, body)
        self.assertTrue(
            lock_path.is_file(),
            f"{route} reclaimed a lock it was only asked to describe",
        )
        self.assertEqual(before, lock_path.read_bytes())

    def test_the_fixture_lock_is_actually_reclaimable(self) -> None:
        # The control. Without it the four assertions below would pass just as
        # happily against a lock nothing would ever have reclaimed.
        plan_dir, lock_path, _ = self._locked_plan()
        info = state_locks._read_lock_path(lock_path, plan_dir, reclaim=True)
        self.assertIsNotNone(info)
        self.assertFalse(
            lock_path.is_file(),
            "the fixture lock was not reclaimable, so the tests below prove nothing",
        )

    def test_state_read_leaves_a_reclaimable_lock_alone(self) -> None:
        self._assert_route_left_the_lock("/api/state")

    def test_verification_read_leaves_a_reclaimable_lock_alone(self) -> None:
        self._assert_route_left_the_lock("/api/verification")

    def test_recovery_read_leaves_a_reclaimable_lock_alone(self) -> None:
        self._assert_route_left_the_lock("/api/recovery")

    def test_questions_read_leaves_a_reclaimable_lock_alone(self) -> None:
        self._assert_route_left_the_lock("/api/questions")

    def test_every_detail_read_leaves_the_whole_state_tree_unchanged(self) -> None:
        plan_dir, _lock_path, _before = self._locked_plan()
        server = self.start_workbench_server(plan_roots=(str(self.plans_root),))
        state_tree = _tree_fingerprint(self.state_dir)
        plan_tree = _tree_fingerprint(self.plans_root)

        for _ in range(2):
            for route, extra in DETAIL_ROUTES:
                status, body, _ = self.get(
                    server, route, plan_dir=str(plan_dir), **extra
                )
                self.assertEqual(200, status, f"{route}: {body}")

        self.assertEqual(state_tree, _tree_fingerprint(self.state_dir))
        self.assertEqual(plan_tree, _tree_fingerprint(self.plans_root))

    def test_the_provider_reads_a_lock_without_removing_it(self) -> None:
        plan_dir, lock_path, before = self._locked_plan()
        reader = workbench_details.open_plan(plan_dir, state_dir=self.state_dir)
        info = workbench_details.read_lock(reader)
        self.assertIsNotNone(info)
        self.assertTrue(lock_path.is_file())
        self.assertEqual(before, lock_path.read_bytes())


class DetailSearchTests(unittest.TestCase):
    """Search reads the private half, online, and retains nothing."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()

    def _detail(self, **fields: object) -> catalog.PlanDetail:
        return catalog.PlanDetail(plan_key="pk", fields=dict(fields))

    def test_pause_prose_is_searchable(self) -> None:
        detail = self._detail(pause_message="Awaiting confirmation on the v1 column")
        self.assertTrue(details.matches_search(detail, "v1 column"))
        self.assertFalse(details.matches_search(detail, "v2 column"))

    def test_the_operator_label_is_searchable_and_stays_private(self) -> None:
        detail = self._detail(label="Alex's migration spike")
        self.assertTrue(details.matches_search(detail, "migration spike"))
        self.assertEqual("Alex's migration spike", details.private_label(detail))
        # The label is private precisely so it does not travel on a cached row.
        self.assertIn("label", catalog.PRIVATE_FIELDS)
        self.assertNotIn("label", catalog.CACHE_SAFE_FIELDS)

    def test_an_empty_query_is_not_a_filter(self) -> None:
        detail = self._detail(plan_dir="/plans/alpha")
        self.assertTrue(details.matches_search(detail, ""))
        self.assertTrue(details.matches_search(detail, "   "))
        self.assertFalse(details.matches_search(detail, None))

    def test_the_summary_haystack_is_preferred_when_present(self) -> None:
        detail = self._detail(searchable_text="alpha beta", pause_message="gamma delta")
        self.assertEqual("alpha beta", details.detail_search_text(detail))
        self.assertFalse(details.matches_search(detail, "gamma"))

    def test_a_hit_carries_only_the_key_label_and_haystack(self) -> None:
        detail = self._detail(
            plan_dir="/plans/alpha", label="Spike", pause_message="waiting"
        )
        self.assertEqual(
            {"plan_key", "label", "search_text"}, set(details.search_payload(detail))
        )

    def test_searching_reads_the_corpus_and_changes_nothing(self) -> None:
        plan_dir = _make_workbench_plan(self.plans_root)
        _write_state_payload(
            plan_dir,
            self.state_dir,
            {
                "plan_dir": str(plan_dir),
                "ok": False,
                "paused": True,
                "reason": "agent-question",
                "pause_reason": "agent-question",
                "pause_message": "Awaiting confirmation on the v1 column",
                "run_id": "run-1",
                "harness": "claude",
            },
        )
        lock_path = _reclaimable_lock(plan_dir, self.state_dir)
        before = lock_path.read_bytes()
        fingerprint = _tree_fingerprint(self.state_dir)

        # The plan's path -- private, and never on a cache-safe row -- is the
        # thing an operator actually types, and it is what makes this search a
        # question only the Mac can answer.
        hits = workbench_details.search_plans(
            plan_dir.name,
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )

        self.assertEqual(1, len(hits))
        self.assertIn(str(plan_dir).lower(), hits[0]["search_text"])
        self.assertEqual(catalog.plan_key(str(plan_dir)), hits[0]["plan_key"])
        self.assertTrue(lock_path.is_file())
        self.assertEqual(before, lock_path.read_bytes())
        self.assertEqual(fingerprint, _tree_fingerprint(self.state_dir))

    def test_a_query_that_matches_nothing_returns_nothing(self) -> None:
        _make_workbench_plan(self.plans_root)
        hits = workbench_details.search_plans(
            "no plan says this",
            state_dir=self.state_dir,
            plan_roots=(str(self.plans_root),),
            include_default_dirs=False,
        )
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
