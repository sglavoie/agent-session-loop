"""Workbench details routes regressions."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_session_loop import (
    state,
    state_locks,
    workbench_details,
)
from asl_workbench import details

from .support_workbench_details import (
    DETAIL_ROUTES,
    _DetailFixture,
    _reclaimable_lock,
    _tree_fingerprint,
)

ASL_TEST_LANE = "integration"


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

    def test_a_dotdot_path_out_of_the_plan_state_is_refused(self) -> None:
        plan_dir = self.seed_plan()
        secret = self.workbench_root / "secret.txt"
        secret.write_text("not an artifact\n", encoding="utf-8")
        plan_state = state.plan_state_dir(plan_dir, self.state_dir)
        hops = "/".join([".."] * len(plan_state.relative_to(plan_state.anchor).parts))
        escaping = f"{plan_state}/{hops}{secret}"
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), path=escaping
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertNotIn("not an artifact", json.dumps(body))

    def test_a_symlink_out_of_the_plan_state_is_refused(self) -> None:
        plan_dir = self.seed_plan()
        secret = self.workbench_root / "secret.txt"
        secret.write_text("not an artifact\n", encoding="utf-8")
        link = state.plan_state_dir(plan_dir, self.state_dir) / "innocent.log"
        link.symlink_to(secret)
        server = self.start_workbench_server()
        status, body, _ = self.get(
            server, "/api/logs", plan_dir=str(plan_dir), path=str(link)
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


if __name__ == "__main__":
    unittest.main()
