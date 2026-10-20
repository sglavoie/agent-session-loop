"""Local API and bridge deep-link characterization for the Workbench foundations.

Captures today's local API behavior over isolated fixtures, including the two
behaviors the program intends to change. Those two are pinned *and* registered
in :mod:`asl_workbench.changes`, so a later session that breaks them can tell
an expected failure from a regression. The tokenized bridge deep link is pinned
as a contract here too.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from agent_session_loop import (
    bridge_protocol,
    bridge_workers,
    plans_files,
    workbench_archive,
    workbench_registry,
)
from asl_workbench import changes
from tests.support import _http_request, _http_request_raw, write_plan
from tests.workbench_support import WorkbenchAPIFixture, recording_server_factory


class LocalAPICharacterizationTests(WorkbenchAPIFixture, unittest.TestCase):
    """Today's local API behavior, captured before anything is extracted."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))
        self.plan_dir = self.plans_root / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)

    def test_health_answers_without_a_token(self) -> None:
        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/health")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("workbench", body["command"])

    def test_the_queue_answers_with_the_triage_fields_the_front_end_reads(
        self,
    ) -> None:
        registry = workbench_registry.upsert_pin(
            workbench_registry.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="Alpha plan",
        )
        workbench_registry.save_registry(registry, self.registry_path)
        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/queue")
        self.assertEqual(200, status)
        payload = body["plans"][0]
        for field in ("urgency_rank", "urgency_group", "activity_label", "plan_dir"):
            with self.subTest(field=field):
                self.assertIn(field, payload)

    def test_a_mutation_without_the_bearer_token_is_refused(self) -> None:
        server = self.start_workbench_server(token="fixed-token")
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["reason"])
        # And the refusal is a refusal: nothing was written.
        self.assertEqual((), workbench_registry.load_registry(self.registry_path).pins)

    def test_the_same_mutation_with_the_token_is_accepted(self) -> None:
        server = self.start_workbench_server(token="fixed-token")
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self.auth_headers(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(
            1, len(workbench_registry.load_registry(self.registry_path).pins)
        )

    def test_an_unknown_path_is_404_and_a_wrong_method_is_405(self) -> None:
        # The vocabulary matters to the front end: 404 means "no such route",
        # 405 carries the methods that route does answer.
        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/nonexistent")
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/queue",
            method="POST",
            body={},
            headers=self.auth_headers(server),
        )
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", body["reason"])
        self.assertIn("GET", body["allow"])

    def test_the_shell_is_served_as_html_without_a_token(self) -> None:
        # Raw rather than JSON: the shell is the one 200 that is not a payload.
        server = self.start_workbench_server(token="fixed-token")
        status, body, headers = _http_request_raw(f"{self.base_url(server)}/")
        self.assertEqual(200, status)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<html", body.lower())

    def test_reading_the_archive_reports_an_expired_entry_and_keeps_it(
        self,
    ) -> None:
        # This test used to assert the opposite, as characterization of the
        # behavior the program intended to change: the read was what deleted.
        # Session 8 landed the change, so the scenario is unchanged and the
        # outcome is inverted -- which is exactly what the register said would
        # happen, and why a session watching it flip could tell it from a
        # regression.
        record = changes.intentional_change(changes.KEY_OBSERVATIONAL_READS)
        self.assertIn("cleanup_state", record.current_behavior)
        self.assertIn("no longer exists", record.current_behavior)
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        meta_path = (
            workbench_archive.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")

        server = self.start_workbench_server()
        status, body, _ = _http_request(f"{self.base_url(server)}/api/archive")
        self.assertEqual(200, status)
        self.assertEqual([result.archive_id], body["archive_cleanup"]["candidate_ids"])
        self.assertEqual(
            [result.archive_id], [entry["archive_id"] for entry in body["entries"]]
        )
        self.assertEqual("eligible", body["entries"][0]["cleanup_state"])
        self.assertTrue(
            workbench_archive.archive_entry_dir(
                result.archive_id, self.state_dir
            ).exists()
        )

    def test_permanently_emptying_the_archive_requires_a_matching_count(
        self,
    ) -> None:
        # The confirmation contract the other intentional change keeps.
        record = changes.intentional_change(changes.KEY_EXPLICIT_ARCHIVE_CLEANUP)
        self.assertIn("confirm_count", record.current_behavior)
        result = workbench_archive.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self.start_workbench_server()
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/archive/delete",
            method="POST",
            body={"archive_ids": [result.archive_id], "confirm_count": 2},
            headers=self.auth_headers(server),
        )
        self.assertEqual(400, status)
        self.assertEqual(
            (result.archive_id,),
            tuple(
                entry.archive_id
                for entry in workbench_archive.list_archive_entries(
                    self.state_dir
                ).entries
            ),
            "a mismatched confirmation must leave the archive untouched",
        )
        self.assertTrue(body["error"])

    def test_archiving_one_plan_requires_the_plan_dir_be_confirmed(self) -> None:
        server = self.start_workbench_server()
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir / "wrong"),
            },
            headers=self.auth_headers(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_the_historic_delete_route_archives_instead_of_deleting(self) -> None:
        # The compatibility adapter the matrix records: same route, same
        # confirmation, and the plan is recoverable afterwards.
        server = self.start_workbench_server()
        status, body, _ = _http_request(
            f"{self.base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self.auth_headers(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertFalse(self.canonical_plan_dir.exists())
        entries = workbench_archive.list_archive_entries(self.state_dir).entries
        self.assertEqual(1, len(entries))


class BridgeDeepLinkTests(unittest.TestCase):
    """The tokenized deep link the bridge hands out, pinned as a contract."""

    def test_the_token_rides_in_the_query_and_the_plan_in_the_fragment(
        self,
    ) -> None:
        # Split deliberately: the fragment never reaches the server, so the
        # token cannot live there; the front end routes on the fragment, so the
        # plan selection cannot live in the query.
        factory, built = recording_server_factory()
        lifecycle = bridge_workers.WorkbenchLifecycle(server_factory=factory)
        self.addCleanup(lifecycle.stop)
        url = lifecycle.url("/plans/alpha", bridge_protocol.WORKBENCH_FOCUS_REPLY)

        parsed = urllib.parse.urlparse(url)
        self.assertEqual("http", parsed.scheme)
        self.assertEqual("127.0.0.1", parsed.hostname)
        self.assertEqual("/", parsed.path)
        self.assertEqual(
            {"token": [built[0].token]}, urllib.parse.parse_qs(parsed.query)
        )
        route, _, fragment_query = parsed.fragment.partition("?")
        self.assertEqual("plan", route)
        self.assertEqual(
            {"dir": ["/plans/alpha"], "focus": [bridge_protocol.WORKBENCH_FOCUS_REPLY]},
            urllib.parse.parse_qs(fragment_query),
        )

    def test_exactly_one_server_is_started_however_many_links_are_asked_for(
        self,
    ) -> None:
        factory, built = recording_server_factory()
        lifecycle = bridge_workers.WorkbenchLifecycle(server_factory=factory)
        self.addCleanup(lifecycle.stop)
        self.assertEqual([], built, "the server must start lazily")
        lifecycle.url("/plans/alpha", bridge_protocol.WORKBENCH_FOCUS_ACTIVE)
        lifecycle.url("/plans/beta", bridge_protocol.WORKBENCH_FOCUS_EVENTS)
        self.assertEqual(1, len(built))
        self.assertEqual(0, built[0].kwargs["port"], "must bind ephemerally")
        self.assertEqual("127.0.0.1", built[0].kwargs["host"])

    def test_the_bridge_owned_server_stops_with_the_bridge(self) -> None:
        factory, built = recording_server_factory()
        lifecycle = bridge_workers.WorkbenchLifecycle(server_factory=factory)
        lifecycle.url("/plans/alpha", bridge_protocol.WORKBENCH_FOCUS_ACTIVE)
        lifecycle.stop()
        self.assertEqual(1, built[0].shutdown_calls)

    def test_the_focus_values_the_matrix_relies_on_exist(self) -> None:
        for focus in (
            bridge_protocol.WORKBENCH_FOCUS_REPLY,
            bridge_protocol.WORKBENCH_FOCUS_VERIFICATION,
            bridge_protocol.WORKBENCH_FOCUS_ACTIVE,
            bridge_protocol.WORKBENCH_FOCUS_EVENTS,
        ):
            with self.subTest(focus=focus):
                self.assertTrue(focus)


if __name__ == "__main__":
    unittest.main()
