"""Workbench server tests: result, verification, commit, progress, and review reads.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import json
import unittest
import urllib.parse

from agent_session_loop import (
    plans_metadata,
    plans_progress,
    plans_registry,
    workbench_summary_helpers,
)
from tests.asl_workbench_server_support import (
    WorkbenchArtifactReadTestCase,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _http_request,
    write_multi_repo_plan,
)

ASL_TEST_LANE = "integration"


class WorkbenchServerRunResultTests(WorkbenchArtifactReadTestCase):
    """Result, verification, commit, progress, and final-review read endpoints."""

    def test_last_message_endpoint_returns_bounded_text(self) -> None:
        last_message = self.plan_state_dir / "last-message.txt"
        last_message.write_text("hello world\n" * 200, encoding="utf-8")
        self._write_state(
            {
                "active_turn": self._make_turn(last_message_path=str(last_message)),
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "max_bytes": "24",
            }
        )
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/last-message?{query}"
        )
        self.assertEqual(200, status)
        self.assertTrue(body["truncated"])
        self.assertEqual(24, len(body["text"]))

    def test_last_message_endpoint_404_when_unset(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/last-message?{query}"
        )
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_result_endpoint_returns_parsed_json(self) -> None:
        result_path = self.plan_state_dir / "result.json"
        result_path.write_text(
            json.dumps({"status_result": "complete", "argv": ["asl", "run"]}),
            encoding="utf-8",
        )
        self._write_state(
            {
                "active_turn": self._make_turn(result_path=str(result_path)),
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/result?{query}")
        self.assertEqual(200, status)
        self.assertEqual("complete", body["result"]["status_result"])
        self.assertEqual(["asl", "run"], body["result"]["argv"])

    def test_result_endpoint_does_not_expose_reply_text(self) -> None:
        """Persisted argv must already be redacted; the API just passes through."""
        redacted_argv = [
            "asl",
            "run",
            "--reply",
            "<asl reply prompt redacted: 12 chars>",
        ]
        result_path = self.plan_state_dir / "result.json"
        result_path.write_text(
            json.dumps({"argv": redacted_argv, "prompt_kind": "reply"}),
            encoding="utf-8",
        )
        self._write_state(
            {
                "active_turn": self._make_turn(result_path=str(result_path)),
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/result?{query}")
        self.assertEqual(200, status)
        self.assertEqual(redacted_argv, body["result"]["argv"])

    def test_result_endpoint_404_when_missing(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/result?{query}")
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_verification_endpoint_returns_active_verification(self) -> None:
        verification_payload = {
            "session_id": "1",
            "turn_index": 1,
            "check_count": 3,
            "current_check_index": 1,
            "checks": [{"name": "tests"}],
        }
        self._write_state(
            {
                "run_id": "run-v",
                "harness": "codex",
                "active_turn": None,
                "active_verification": verification_payload,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/verification?{query}"
        )
        self.assertEqual(200, status)
        self.assertEqual(verification_payload, body["active_verification"])
        self.assertEqual("run-v", body["run_id"])

    def test_verification_endpoint_returns_none_when_idle(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/verification?{query}"
        )
        self.assertEqual(200, status)
        self.assertIsNone(body["active_verification"])

    def test_commits_endpoint_pairs_started_and_finished_events(self) -> None:
        records = [
            {
                "name": "commit_started",
                "created_at": "2026-02-01T00:00:00+00:00",
                "payload": {
                    "run_id": "run-c",
                    "commit_index": 0,
                    "scope": "session",
                    "session_id": "1",
                    "stdout_path": "/tmp/stdout.log",
                    "stderr_path": "/tmp/stderr.log",
                    "result_path": "/tmp/result.json",
                },
            },
            {
                "name": "commit_finished",
                "created_at": "2026-02-01T00:00:05+00:00",
                "payload": {
                    "run_id": "run-c",
                    "commit_index": 0,
                    "scope": "session",
                    "session_id": "1",
                    "status_result": "success",
                    "commit_sha": "abc123",
                    "duration_seconds": 5.0,
                },
            },
            {
                "name": "commit_started",
                "created_at": "2026-02-01T00:01:00+00:00",
                "payload": {
                    "run_id": "run-c",
                    "commit_index": 1,
                    "scope": "end",
                },
            },
        ]
        self._write_events(records)
        self._write_state(
            {
                "run_id": "run-c",
                "active_turn": None,
                "active_verification": None,
            }
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/commits?{query}")
        self.assertEqual(200, status)
        commits = body["commits"]
        self.assertEqual(2, len(commits))
        self.assertEqual(0, commits[0]["commit_index"])
        self.assertEqual("success", commits[0]["status_result"])
        self.assertEqual("abc123", commits[0]["commit_sha"])
        self.assertEqual(1, commits[1]["commit_index"])
        self.assertIsNone(commits[1]["status_result"])
        self.assertIsNone(commits[1]["finished_at"])

    def test_progress_endpoint_returns_entries(self) -> None:
        progress_path = self.canonical_plan_dir / plans_progress.PROGRESS_FILE_NAME
        progress_path.write_text(
            "\n".join(
                [
                    "# Progress Journal",
                    "<!-- agent-session-loop progress-journal v1 -->",
                    "",
                    "## Session 1 — complete (2026-01-01T00:00:00+00:00)",
                    "- Files touched: a.py, b.py",
                    "- Decisions: pick option A",
                    "- Discovered facts:",
                    "  - fact one",
                    "- Open questions: none",
                    "- Blockers: none",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/progress?{query}"
        )
        self.assertEqual(200, status)
        self.assertTrue(body["exists"])
        self.assertEqual(1, len(body["entries"]))
        entry = body["entries"][0]
        self.assertEqual("1", entry["session_id"])
        self.assertEqual(["a.py", "b.py"], entry["files_modified"])
        self.assertEqual("pick option A", entry["key_decisions"])
        self.assertEqual(["fact one"], entry["discovered_facts"])

    def test_progress_endpoint_returns_empty_when_missing(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/progress?{query}"
        )
        self.assertEqual(200, status)
        self.assertFalse(body["exists"])
        self.assertEqual([], body["entries"])

    def test_progress_endpoint_500_on_malformed_journal(self) -> None:
        progress_path = self.canonical_plan_dir / plans_progress.PROGRESS_FILE_NAME
        progress_path.write_text(
            "# Progress Journal\nno schema header\n", encoding="utf-8"
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/progress?{query}"
        )
        self.assertEqual(500, status)
        self.assertEqual("malformed-plan", body["reason"])

    def test_final_review_endpoint_returns_bounded_text(self) -> None:
        review_path = self.canonical_plan_dir / "final-review.md"
        review_path.write_text("# Findings\n" + ("x" * 1000), encoding="utf-8")
        server = self._start_server()
        query = urllib.parse.urlencode(
            {
                "plan_dir": str(self.canonical_plan_dir),
                "max_bytes": "32",
            }
        )
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/final-review?{query}"
        )
        self.assertEqual(200, status)
        self.assertTrue(body["truncated"])
        self.assertEqual(32, len(body["text"]))

    def test_final_review_endpoint_returns_structured_repository_findings(self) -> None:
        (self.canonical_plan_dir / "final-review.md").write_text(
            "# Findings\n", encoding="utf-8"
        )
        artifact = {
            "version": 3,
            "outcome": "findings",
            "findings": [
                {
                    "id": "F1",
                    "repo": "SVC",
                    "title": "Repository-specific finding",
                }
            ],
        }
        (self.canonical_plan_dir / "final-review.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/final-review?{query}"
        )

        self.assertEqual(200, status)
        self.assertEqual(artifact, body["structured_review"])
        self.assertEqual("SVC", body["structured_review"]["findings"][0]["repo"])
        self.assertTrue(body["artifact_path"].endswith("final-review.json"))

    def test_registered_final_review_retains_cwd_compatibility_key(self) -> None:
        registered = self.tmp_path / "plans" / "registered-review"
        write_multi_repo_plan(
            registered, ("complete",), specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )
        registry = plans_registry.read_repository_registry(registered)
        assert registry is not None
        workspace_root = self.tmp_path / "workspace"
        plans_metadata.save_workspace_plan_metadata(
            registered, workspace_root, registry.digest
        )
        (registered / workbench_summary_helpers.FINAL_REVIEW_FILE_NAME).write_text(
            "# Findings\n", encoding="utf-8"
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(registered)})

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/final-review?{query}"
        )

        self.assertEqual(200, status)
        self.assertEqual(str(workspace_root.resolve()), body["cwd"])
        for key in (
            "cwd",
            "workspace_root",
            "registry_digest",
            "repositories",
            "repository_count",
        ):
            self.assertIn(key, body)

    def test_final_review_endpoint_404_when_missing(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/final-review?{query}"
        )
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_read_endpoints_do_not_require_token(self) -> None:
        server = self._start_server()
        # Hit several read endpoints without an Authorization header.
        for endpoint in (
            "/api/state",
            "/api/events",
            "/api/progress",
            "/api/verification",
        ):
            query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
            status, _, _ = _http_request(f"{self._base_url(server)}{endpoint}?{query}")
            self.assertIn(status, (200, 404), f"endpoint={endpoint}")

    def test_read_endpoints_require_plan_dir(self) -> None:
        server = self._start_server()
        for endpoint in (
            "/api/state",
            "/api/events",
            "/api/logs",
            "/api/last-message",
            "/api/result",
            "/api/verification",
            "/api/commits",
            "/api/progress",
            "/api/final-review",
        ):
            status, body, _ = _http_request(f"{self._base_url(server)}{endpoint}")
            self.assertEqual(400, status, f"endpoint={endpoint}")
            self.assertEqual("invalid-request", body["reason"], f"endpoint={endpoint}")


if __name__ == "__main__":
    unittest.main()
