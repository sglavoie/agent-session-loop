"""Workbench server tests: state, events, and log read endpoints.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import urllib.parse

from agent_session_loop import (
    artifacts,
    plans_files,
    plans_metadata,
    workbench,
    workbench_server,
)
from tests.asl_workbench_server_support import (
    WorkbenchArtifactReadTestCase,
)
from tests.support import (
    _http_request,
    write_multi_repo_plan,
    write_plan_with_ids,
)


class WorkbenchServerRunStateTests(WorkbenchArtifactReadTestCase):
    """State, events, and log read endpoints."""

    def test_state_endpoint_returns_runner_payload(self) -> None:
        self._write_state(
            {
                "run_id": "run-x",
                "harness": "codex",
                "ok": True,
                "active_turn": self._make_turn(),
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/state?{query}")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("run-x", body["state"]["run_id"])
        self.assertEqual("codex", body["state"]["harness"])
        self.assertTrue(body["state"]["present"])
        self.assertEqual(["1", "2"], [s["id"] for s in body["sessions"]])
        self.assertEqual(
            ["pending", "pending"], [s["status"] for s in body["sessions"]]
        )
        self.assertEqual(
            {"pending": 2, "complete": 0, "empty": 0, "unknown": 0},
            body["counts"],
        )
        self.assertEqual("1", body["next_pending_id"])

    def test_state_endpoint_handles_missing_state(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/state?{query}")
        self.assertEqual(200, status)
        self.assertFalse(body["state"]["present"])
        self.assertEqual(["1", "2"], [s["id"] for s in body["sessions"]])
        self.assertEqual(
            {"pending": 2, "complete": 0, "empty": 0, "unknown": 0},
            body["counts"],
        )
        self.assertEqual("1", body["next_pending_id"])

    def test_state_endpoint_emits_legacy_workspace_keys(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})

        status, body, _ = _http_request(f"{self._base_url(server)}/api/state?{query}")

        self.assertEqual(200, status)
        self.assertIsNone(body["cwd"])
        self.assertIsNone(body["workspace_root"])
        self.assertIsNone(body["registry_digest"])
        self.assertEqual([], body["repositories"])
        self.assertEqual(0, body["repository_count"])

    def test_state_and_final_review_survive_corrupt_workspace_metadata(
        self,
    ) -> None:
        registered = self.tmp_path / "plans" / "corrupt-workspace-metadata"
        write_multi_repo_plan(registered, ("pending",))
        (registered / plans_metadata.PLAN_METADATA_FILE_NAME).write_text(
            "{not-json", encoding="utf-8"
        )
        (registered / workbench.FINAL_REVIEW_FILE_NAME).write_text(
            "# Findings\n", encoding="utf-8"
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(registered)})

        for endpoint in ("state", "final-review"):
            with self.subTest(endpoint=endpoint):
                status, body, _ = _http_request(
                    f"{self._base_url(server)}/api/{endpoint}?{query}"
                )
                self.assertEqual(200, status)
                self.assertTrue(body["ok"])
                self.assertEqual({}, body["workspace"])
                self.assertIsNone(body["workspace_root"])
                self.assertIsNone(body["registry_digest"])
                self.assertEqual([], body["repositories"])
                self.assertEqual(0, body["repository_count"])

    def test_state_endpoint_returns_dotted_sessions_and_counts(self) -> None:
        dotted_plan_dir = self.tmp_path / "plans" / "dotted"
        write_plan_with_ids(
            dotted_plan_dir,
            (
                ("1", "complete"),
                ("2", "complete"),
                ("2.1", "pending"),
                ("2.2", "pending"),
                ("3", "pending"),
            ),
        )
        canonical_dotted = plans_files.canonicalize_path(dotted_plan_dir)
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(canonical_dotted)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/state?{query}")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(
            ["1", "2", "2.1", "2.2", "3"],
            [s["id"] for s in body["sessions"]],
        )
        self.assertEqual(
            ["complete", "complete", "pending", "pending", "pending"],
            [s["status"] for s in body["sessions"]],
        )
        self.assertEqual(
            {"pending": 3, "complete": 2, "empty": 0, "unknown": 0},
            body["counts"],
        )
        self.assertEqual("2.1", body["next_pending_id"])

    def test_state_endpoint_reports_malformed_plan_error(self) -> None:
        (self.canonical_plan_dir / "manifest.md").unlink()
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/state?{query}")
        self.assertEqual(500, status)
        self.assertFalse(body["ok"])
        self.assertEqual("malformed-plan", body["reason"])
        self.assertEqual(str(self.canonical_plan_dir), body["details"]["plan_dir"])

    def test_events_endpoint_paginates_and_filters_by_run(self) -> None:
        records = [
            {
                "name": "run_started",
                "created_at": "2026-01-01T00:00:00+00:00",
                "payload": {"run_id": "run-a"},
            },
            {
                "name": "turn_started",
                "created_at": "2026-01-01T00:00:01+00:00",
                "payload": {"run_id": "run-a", "session_id": "1"},
            },
            {
                "name": "turn_finished",
                "created_at": "2026-01-01T00:00:02+00:00",
                "payload": {"run_id": "run-a", "session_id": "1"},
            },
            {
                "name": "run_started",
                "created_at": "2026-01-01T00:00:03+00:00",
                "payload": {"run_id": "run-b"},
            },
        ]
        self._write_events(records)
        self._write_state(
            {"run_id": "run-b", "active_turn": None, "active_verification": None}
        )

        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/events?{query}")
        self.assertEqual(200, status)
        self.assertEqual("run-b", body["run_id"])
        self.assertEqual(1, body["total"])

        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "run_id": "run-a",
                "limit": "2",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/events?{query}")
        self.assertEqual(200, status)
        self.assertEqual(3, body["total"])
        self.assertEqual(2, len(body["events"]))
        self.assertEqual(2, body["next_offset"])

        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "run_id": "run-a",
                "limit": "2",
                "offset": "2",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/events?{query}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(body["events"]))
        self.assertIsNone(body["next_offset"])

    def test_events_endpoint_rejects_negative_offset(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode(
            {"plan_dir": str(self.canonical_plan_dir), "offset": "-1"}
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/events?{query}")
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_events_endpoint_rejects_limit_over_max(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "limit": str(workbench_server.MAX_EVENT_LIMIT + 1),
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/events?{query}")
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_logs_endpoint_returns_tail_with_bound(self) -> None:
        stdout_path = self.plan_state_dir / "stdout.log"
        stdout_path.write_text(
            "line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8"
        )
        self._write_state(
            {
                "active_turn": self._make_turn(stdout_path=str(stdout_path)),
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "stream": "stdout",
                "tail_lines": "2",
                "max_bytes": "1024",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(200, status)
        self.assertEqual("line 4\nline 5", body["text"])
        self.assertTrue(body["exists"])
        self.assertEqual(str(stdout_path), body["path"])

    def test_logs_endpoint_returns_offset_window(self) -> None:
        stdout_path = self.plan_state_dir / "stdout.log"
        stdout_path.write_text("0123456789abcdef", encoding="utf-8")
        self._write_state(
            {
                "active_turn": self._make_turn(stdout_path=str(stdout_path)),
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "stream": "stdout",
                "offset": "4",
                "max_bytes": "4",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(200, status)
        self.assertEqual("4567", body["text"])
        self.assertEqual(4, body["offset"])
        self.assertEqual(8, body["next_offset"])
        self.assertTrue(body["truncated_head"])
        self.assertTrue(body["truncated_tail"])

    def test_logs_endpoint_reads_gzip_artifact_through_window(self) -> None:
        nested = self.plan_state_dir / "runs" / "turn-001" / "verification"
        nested.mkdir(parents=True)
        gz_path = nested / "c1-stdout.gz"
        artifacts.atomic_write_gzip(gz_path, b"line 1\nline 2\nline 3\nline 4\n")
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "path": str(gz_path),
                "tail_lines": "2",
                "max_bytes": "1024",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(200, status)
        self.assertTrue(body["exists"])
        self.assertEqual("line 3\nline 4", body["text"])
        self.assertEqual(str(gz_path), body["path"])

    def test_logs_endpoint_gzip_offset_window(self) -> None:
        nested = self.plan_state_dir / "runs" / "turn-001" / "verification"
        nested.mkdir(parents=True)
        gz_path = nested / "c1-stderr.gz"
        artifacts.atomic_write_gzip(gz_path, b"0123456789abcdef")
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "path": str(gz_path),
                "offset": "4",
                "max_bytes": "4",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(200, status)
        self.assertEqual("4567", body["text"])
        self.assertEqual(4, body["offset"])
        self.assertEqual(8, body["next_offset"])
        self.assertEqual(16, body["size"])
        self.assertTrue(body["truncated_head"])
        self.assertTrue(body["truncated_tail"])

    def test_logs_endpoint_404_when_no_active_stream(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode(
            {"plan_dir": str(self.canonical_plan_dir), "stream": "stdout"}
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_logs_endpoint_rejects_outside_plan_state(self) -> None:
        outside = self.tmp_path / "outside.log"
        outside.write_text("hi\n", encoding="utf-8")
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "path": str(outside),
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_logs_endpoint_accepts_explicit_path_inside_plan_state(
        self,
    ) -> None:
        nested = self.plan_state_dir / "runs" / "turn-001"
        nested.mkdir(parents=True)
        log_path = nested / "stdout.log"
        log_path.write_text("inside\n", encoding="utf-8")
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "path": str(log_path),
                "max_bytes": "1024",
            }
        )
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(200, status)
        self.assertEqual("inside\n", body["text"])

    def test_logs_endpoint_requires_stream_or_path(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/logs?{query}")
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
