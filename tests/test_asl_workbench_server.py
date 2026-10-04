"""Workbench HTTP server and API tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    artifacts,
    control_cli,
    control_queue,
    events,
    plans,
    state,
    workbench,
    workbench_server,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _discovered,
    _http_request,
    _write_lock_file,
    _write_raw_state_payload,
    _write_state_payload,
    write_multi_repo_plan,
    write_plan,
    write_plan_with_ids,
)


class WorkbenchServerTests(unittest.TestCase):
    """End-to-end coverage for the loopback HTTP control plane."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / "workbench.json"
        self.plan_dir = self.tmp_path / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical_plan_dir = plans.canonicalize_path(self.plan_dir)
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def tearDown(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
            finally:
                srv.server_close()

    def _start_server(
        self,
        *,
        token: str | None = None,
        plan_roots: tuple[str, ...] = (),
    ) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=plan_roots,
            include_default_dirs=False,
            token=token,
        )
        thread = threading.Thread(
            target=server.serve_forever, name="workbench-test", daemon=True
        )
        thread.start()
        self.servers.append(server)

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def _base_url(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        return f"http://{server.bound_host}:{server.bound_port}"

    def _auth(self, server: workbench_server.WorkbenchHTTPServer) -> dict:
        return {"Authorization": f"Bearer {server.token}"}

    def test_server_binds_to_ephemeral_port(self) -> None:
        server = self._start_server(token="fixed-token-1")
        self.assertEqual("127.0.0.1", server.bound_host)
        self.assertNotEqual(0, server.bound_port)
        self.assertEqual("fixed-token-1", server.token)

    def test_health_endpoint_does_not_require_token(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/health")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("workbench", body["command"])
        self.assertEqual("ok", body["reason"])

    def test_token_generated_when_not_provided(self) -> None:
        server = self._start_server()
        # Default tokens should be url-safe and non-empty.
        self.assertTrue(server.token)
        self.assertGreaterEqual(len(server.token), 32)

    def test_queue_exposes_triage_payload_fields(self) -> None:
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(
            registry,
            self.canonical_plan_dir,
            label="Alpha plan",
        )
        workbench.save_registry(registry, self.registry_path)

        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        plan_payload = body["plans"][0]
        for key in (
            "urgency_rank",
            "urgency_group",
            "activity_label",
            "concise_reason",
            "last_activity_at",
            "last_activity_source",
            "completed_at",
            "searchable_text",
        ):
            self.assertIn(key, plan_payload)
        self.assertIn(plan_payload["urgency_group"], workbench.URGENCY_GROUPS)
        self.assertIsInstance(plan_payload["urgency_rank"], int)
        self.assertIn("alpha plan", plan_payload["searchable_text"])

    def test_queue_isolates_corrupt_workspace_metadata_per_plan(self) -> None:
        broken = self.tmp_path / "plans" / "broken-metadata"
        write_multi_repo_plan(broken, ("pending",))
        (broken / plans.PLAN_METADATA_FILE_NAME).write_text(
            "{not-json", encoding="utf-8"
        )
        canonical_broken = plans.canonicalize_path(broken)
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(registry, self.canonical_plan_dir)
        registry = workbench.upsert_pin(registry, canonical_broken)
        workbench.save_registry(registry, self.registry_path)

        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        plans_by_dir = {row["plan_dir"]: row for row in body["plans"]}
        self.assertEqual(2, len(plans_by_dir))
        self.assertEqual(
            "ready", plans_by_dir[str(self.canonical_plan_dir)]["primary_state"]
        )
        broken_payload = plans_by_dir[str(canonical_broken)]
        self.assertEqual("invalid", broken_payload["primary_state"])
        self.assertIn("workspace metadata unreadable", broken_payload["detail"])

    def test_plan_endpoint_exposes_triage_payload_fields(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/plan?{query}")

        self.assertEqual(200, status)
        plan_payload = body["plan"]
        for key in (
            "urgency_rank",
            "urgency_group",
            "activity_label",
            "concise_reason",
            "last_activity_at",
            "last_activity_source",
            "completed_at",
            "searchable_text",
        ):
            self.assertIn(key, plan_payload)
        self.assertIn(plan_payload["urgency_group"], workbench.URGENCY_GROUPS)

    def test_plan_endpoint_names_the_plan_by_its_manifest_title(self) -> None:
        # The queue row calls a plan by its manifest's `# H1`; the detail read
        # once had no title, so opening a plan retitled it with its directory.
        (self.canonical_plan_dir / "manifest.md").write_text(
            "# Ship the widget\n", encoding="utf-8"
        )
        registry = workbench.upsert_pin(
            workbench.load_registry(self.registry_path), self.canonical_plan_dir
        )
        workbench.save_registry(registry, self.registry_path)
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        _, queue, _ = _http_request(f"{self._base_url(server)}/api/queue")
        status, body, _ = _http_request(f"{self._base_url(server)}/api/plan?{query}")

        self.assertEqual(200, status)
        self.assertEqual("Ship the widget", body["plan"]["title"])
        row = next(
            r for r in queue["plans"] if r["plan_dir"] == str(self.canonical_plan_dir)
        )
        self.assertEqual(row["title"], body["plan"]["title"])

    def test_queue_and_plan_expose_compact_recovery_payload(self) -> None:
        _write_state_payload(
            self.canonical_plan_dir,
            self.state_dir,
            {
                "ok": False,
                "paused": True,
                "reason": "not-loop-ready",
                "pause_reason": "not-loop-ready",
                "pause_message": "preflight failed",
                "pause_details": {"readiness_state": "reviewed"},
                "run_id": "run-recovery-summary",
            },
        )
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(
            registry,
            self.canonical_plan_dir,
            label="Alpha plan",
        )
        workbench.save_registry(registry, self.registry_path)

        server = self._start_server()
        status, queue_body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        queue_recovery = queue_body["plans"][0]["recovery"]
        for key in (
            "kind",
            "title",
            "summary",
            "severity",
            "recoverable",
            "primary_action",
            "artifact_count",
            "command_count",
            "warning_count",
        ):
            self.assertIn(key, queue_recovery)
        self.assertEqual("not-loop-ready", queue_recovery["kind"])
        self.assertEqual("launch", queue_recovery["primary_action"]["target"])
        self.assertGreater(queue_recovery["command_count"], 0)

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, plan_body, _ = _http_request(
            f"{self._base_url(server)}/api/plan?{query}"
        )
        self.assertEqual(200, status)
        plan_recovery = plan_body["plan"]["recovery"]
        self.assertEqual(queue_recovery, plan_recovery)

    def test_queue_payload_preserves_legacy_fields_alongside_triage(
        self,
    ) -> None:
        # Session 8 regression: adding the additive triage fields in
        # session 1 must not have dropped any pre-existing payload key
        # the frontend / external consumers rely on. Assert the full
        # legacy contract still ships in both /api/queue and /api/plan
        # so a future refactor cannot silently regress backwards
        # compatibility.
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(
            registry,
            self.canonical_plan_dir,
            label="Alpha plan",
            profile=workbench.LaunchProfile(harness="codex"),
        )
        workbench.save_registry(registry, self.registry_path)

        legacy_keys = (
            "plan_dir",
            "label",
            "profile",
            "sources",
            "exists",
            "primary_state",
            "recommended_action",
            "next_action_hint",
            "detail",
            "next_pending_session_id",
            "next_pending_session_path",
            "counts",
            "state_path",
            "events_path",
            "progress_path",
            "final_review_path",
            "artifact_dir",
            "stdout_path",
            "stderr_path",
            "last_message_path",
            "result_path",
            "run_id",
            "harness",
            "pause_reason",
            "pause_message",
            "last_message_preview",
            "warnings",
        )
        triage_keys = (
            "urgency_rank",
            "urgency_group",
            "activity_label",
            "concise_reason",
            "last_activity_at",
            "last_activity_source",
            "completed_at",
            "searchable_text",
        )
        recovery_keys = ("recovery",)

        server = self._start_server()
        status, queue_body, _ = _http_request(f"{self._base_url(server)}/api/queue")
        self.assertEqual(200, status)
        plans_payload = queue_body["plans"]
        self.assertEqual(1, len(plans_payload))
        queue_plan = plans_payload[0]
        for key in legacy_keys + triage_keys + recovery_keys:
            self.assertIn(
                key,
                queue_plan,
                msg=f"queue payload missing key: {key}",
            )
        self.assertEqual("Alpha plan", queue_plan["label"])
        self.assertEqual("codex", queue_plan["profile"]["harness"])
        self.assertEqual(str(self.canonical_plan_dir), queue_plan["plan_dir"])
        self.assertEqual("none", queue_plan["recovery"]["kind"])

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, plan_body, _ = _http_request(
            f"{self._base_url(server)}/api/plan?{query}"
        )
        self.assertEqual(200, status)
        plan_payload = plan_body["plan"]
        for key in legacy_keys + triage_keys + recovery_keys:
            self.assertIn(
                key,
                plan_payload,
                msg=f"plan payload missing key: {key}",
            )
        self.assertEqual("none", plan_payload["recovery"]["kind"])

    def test_queue_payload_urgency_rank_sorts_across_primary_states(
        self,
    ) -> None:
        # Session 8 integration regression: build a queue with plans in
        # multiple primary states and assert urgency_rank,
        # urgency_group, and activity_label match the runtime
        # constants. The frontend sorts ascending by urgency_rank
        # client-side, so a server-side regression here would break the
        # default triage order in the browser too.
        ready_plan = self.canonical_plan_dir
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(registry, ready_plan, label="Ready plan")

        complete_dir = self.tmp_path / "plans" / "done"
        write_plan(complete_dir, ("complete", "complete"))
        complete_plan = plans.canonicalize_path(complete_dir)
        registry = workbench.upsert_pin(registry, complete_plan, label="Done plan")

        failed_dir = self.tmp_path / "plans" / "failed"
        write_plan(failed_dir, ("pending", "pending"))
        failed_plan = plans.canonicalize_path(failed_dir)
        _write_state_payload(
            failed_plan,
            self.state_dir,
            {
                "ok": False,
                "paused": False,
                "run_id": "run-failed",
                "harness": "claude",
                "run_finished_at": "2026-06-19T18:00:00+00:00",
                "active_turn": None,
                "active_verification": None,
            },
        )
        registry = workbench.upsert_pin(registry, failed_plan, label="Failed plan")

        workbench.save_registry(registry, self.registry_path)

        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")
        self.assertEqual(200, status)
        plans_by_dir = {payload["plan_dir"]: payload for payload in body["plans"]}
        self.assertEqual(
            {str(ready_plan), str(complete_plan), str(failed_plan)},
            set(plans_by_dir),
        )

        for plan_path, expected_state in (
            (ready_plan, "ready"),
            (complete_plan, "complete"),
            (failed_plan, "failed"),
        ):
            payload = plans_by_dir[str(plan_path)]
            self.assertEqual(
                expected_state,
                payload["primary_state"],
                msg=f"{plan_path} expected primary_state={expected_state}",
            )
            self.assertEqual(
                workbench.URGENCY_RANK_BY_STATE[expected_state],
                payload["urgency_rank"],
                msg=f"{plan_path} urgency_rank drift",
            )
            self.assertEqual(
                workbench.URGENCY_GROUP_BY_STATE[expected_state],
                payload["urgency_group"],
                msg=f"{plan_path} urgency_group drift",
            )
            self.assertEqual(
                workbench.ACTIVITY_LABEL_BY_STATE[expected_state],
                payload["activity_label"],
                msg=f"{plan_path} activity_label drift",
            )

        sorted_by_rank = [
            payload["primary_state"]
            for payload in sorted(
                body["plans"], key=lambda payload: payload["urgency_rank"]
            )
        ]
        # failed (20) < ready (80) < complete (90) is the canonical
        # triage ordering surfaced by the queue UI.
        self.assertEqual(["failed", "ready", "complete"], sorted_by_rank)

    def test_queue_returns_summaries_with_registry_pins(self) -> None:
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(
            registry,
            self.canonical_plan_dir,
            label="Alpha plan",
            profile=workbench.LaunchProfile(harness="codex"),
        )
        workbench.save_registry(registry, self.registry_path)

        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        plan_payloads = body["plans"]
        self.assertEqual(1, len(plan_payloads))
        plan_payload = plan_payloads[0]
        self.assertEqual(str(self.canonical_plan_dir), plan_payload["plan_dir"])
        self.assertEqual("Alpha plan", plan_payload["label"])
        self.assertIn(plan_payload["primary_state"], workbench.PRIMARY_STATES)
        self.assertEqual(str(state.state_root(self.state_dir)), body["state_root"])
        self.assertTrue(body["state_root_exists"])
        self.assertEqual([], body["registry_warnings"])

    def test_queue_classifies_active_locked_turn_as_running(self) -> None:
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(registry, self.canonical_plan_dir)
        workbench.save_registry(registry, self.registry_path)
        _write_state_payload(
            self.canonical_plan_dir,
            self.state_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "run-active",
                "harness": "claude",
                "run_finished_at": None,
                "active_turn": {"session_id": "1", "turn_index": 2},
                "active_verification": None,
            },
        )
        _write_lock_file(
            self.canonical_plan_dir,
            self.state_dir,
            pid=os.getpid(),
        )

        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(1, len(body["plans"]))
        plan_payload = body["plans"][0]
        self.assertEqual("running", plan_payload["primary_state"])
        self.assertEqual("watch", plan_payload["recommended_action"])
        self.assertIn("session 1", plan_payload["next_action_hint"])

    def test_queue_surfaces_plans_from_registry_plan_roots(self) -> None:
        plan_root = plans.canonicalize_path(self.plan_dir.parent)
        registry = workbench.WorkbenchRegistry(
            plan_roots=(str(plan_root),),
        )
        workbench.save_registry(registry, self.registry_path)

        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        plan_payloads = body["plans"]
        self.assertEqual(1, len(plan_payloads))
        plan_payload = plan_payloads[0]
        self.assertEqual(str(self.canonical_plan_dir), plan_payload["plan_dir"])
        self.assertEqual([], body["registry_warnings"])
        self.assertEqual([], body["discovery_warnings"])

    def test_plan_endpoint_returns_single_summary(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(f"{self._base_url(server)}/api/plan?{query}")

        self.assertEqual(200, status)
        plan_payload = body["plan"]
        self.assertEqual(str(self.canonical_plan_dir), plan_payload["plan_dir"])
        self.assertIn("primary_state", plan_payload)
        self.assertIn(plan_payload["recommended_action"], workbench.RECOMMENDED_ACTIONS)

    def test_plan_endpoint_rejects_missing_plan_dir(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/plan")
        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])

    def test_recovery_endpoint_returns_detail_payload_without_token(self) -> None:
        _write_state_payload(
            self.canonical_plan_dir,
            self.state_dir,
            {
                "ok": False,
                "paused": True,
                "reason": "not-loop-ready",
                "pause_reason": "not-loop-ready",
                "pause_message": "preflight failed",
                "pause_details": {"readiness_state": "reviewed"},
                "run_id": "run-recovery",
            },
        )
        server = self._start_server(token="read-token-not-required")
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/recovery?{query}"
        )

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(str(self.canonical_plan_dir), body["plan_dir"])
        self.assertEqual("not-loop-ready", body["recovery"]["kind"])
        self.assertEqual("not-loop-ready", body["recovery"]["compact"]["kind"])
        self.assertEqual("run-recovery", body["run_id"])
        self.assertTrue(
            any(
                command["command"].endswith("--mark-loop-ready")
                for command in body["recovery"]["commands"]
            )
        )
        self.assertTrue(
            any(artifact["kind"] == "state" for artifact in body["artifacts"])
        )

    def test_registered_recovery_retains_cwd_compatibility_key(self) -> None:
        registered = self.tmp_path / "plans" / "registered-recovery"
        write_multi_repo_plan(registered, ("pending",))
        registry = plans.read_repository_registry(registered)
        assert registry is not None
        workspace_root = self.tmp_path / "workspace"
        plans.save_workspace_plan_metadata(registered, workspace_root, registry.digest)
        canonical = plans.canonicalize_path(registered)
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(canonical)})

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/recovery?{query}"
        )

        self.assertEqual(200, status)
        recovery = body["recovery"]
        self.assertEqual(str(workspace_root.resolve()), recovery["cwd"])
        for key in (
            "cwd",
            "workspace_root",
            "registry_digest",
            "repositories",
            "repository_count",
        ):
            self.assertIn(key, recovery)

    def test_recovery_endpoint_includes_warning_details_additively(self) -> None:
        _write_raw_state_payload(self.canonical_plan_dir, self.state_dir, "[]")
        server = self._start_server(token="warning-token-not-required")
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/recovery?{query}"
        )

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        recovery = body["recovery"]
        self.assertEqual(
            ["state.json did not contain a JSON object"], recovery["warnings"]
        )
        self.assertEqual(
            [
                {
                    "code": "state-invalid-json-shape",
                    "message": "state.json did not contain a JSON object",
                    "severity": "warning",
                }
            ],
            recovery["warning_details"],
        )
        self.assertEqual(1, recovery["compact"]["warning_count"])

    def test_warning_details_remain_full_recovery_only(self) -> None:
        _write_raw_state_payload(self.canonical_plan_dir, self.state_dir, "[]")
        registry = workbench.load_registry(self.registry_path)
        registry = workbench.upsert_pin(
            registry,
            self.canonical_plan_dir,
            label="Alpha plan",
        )
        workbench.save_registry(registry, self.registry_path)

        server = self._start_server(token="warning-boundary-token")
        status, queue_body, _ = _http_request(f"{self._base_url(server)}/api/queue")

        self.assertEqual(200, status)
        queue_recovery = queue_body["plans"][0]["recovery"]
        self.assertEqual(1, queue_recovery["warning_count"])
        self.assertNotIn("warnings", queue_recovery)
        self.assertNotIn("warning_details", queue_recovery)

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, plan_body, _ = _http_request(
            f"{self._base_url(server)}/api/plan?{query}"
        )
        self.assertEqual(200, status)
        plan_recovery = plan_body["plan"]["recovery"]
        self.assertEqual(queue_recovery, plan_recovery)
        self.assertNotIn("warnings", plan_recovery)
        self.assertNotIn("warning_details", plan_recovery)

        status, recovery_body, _ = _http_request(
            f"{self._base_url(server)}/api/recovery?{query}"
        )
        self.assertEqual(200, status)
        full_recovery = recovery_body["recovery"]
        self.assertEqual(
            ["state.json did not contain a JSON object"],
            full_recovery["warnings"],
        )
        self.assertEqual(
            "state-invalid-json-shape",
            full_recovery["warning_details"][0]["code"],
        )
        self.assertEqual(1, full_recovery["compact"]["warning_count"])
        self.assertNotIn("warnings", full_recovery["compact"])
        self.assertNotIn("warning_details", full_recovery["compact"])

    def test_recovery_endpoint_rejects_malformed_plan(self) -> None:
        malformed = self.tmp_path / "plans" / "malformed"
        malformed.mkdir(parents=True)
        (malformed / "context.md").write_text("# Context\n", encoding="utf-8")
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(malformed)})

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/recovery?{query}"
        )

        self.assertEqual(500, status)
        self.assertFalse(body["ok"])
        self.assertEqual("malformed-plan", body["reason"])

    def test_recovery_endpoint_rejects_invalid_requests(self) -> None:
        server = self._start_server()

        status, body, _ = _http_request(f"{self._base_url(server)}/api/recovery")

        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])

        query = urllib.parse.urlencode(
            {"plan_dir": str(self.tmp_path / "plans" / "missing")}
        )
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/recovery?{query}"
        )

        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-plan", body["reason"])

    def test_registry_get_returns_empty_when_unset(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
        self.assertEqual(200, status)
        self.assertEqual([], body["registry"]["pins"])
        self.assertEqual([], body["registry"]["plan_roots"])

    def test_artifacts_endpoint_describes_known_paths(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/artifacts?{query}"
        )
        self.assertEqual(200, status)
        artifacts = body["artifacts"]
        self.assertIn("state_path", artifacts)
        self.assertIn("events_path", artifacts)
        # No runner state exists yet so these are not present.
        self.assertFalse(artifacts["state_path"]["exists"])

    def test_mutating_endpoint_rejects_missing_token(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
        )
        self.assertEqual(401, status)
        self.assertFalse(body["ok"])
        self.assertEqual("unauthorized", body["reason"])

    def test_mutating_endpoint_rejects_wrong_token(self) -> None:
        server = self._start_server(token="correct-token")
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["reason"])

    def test_registry_pin_persists_to_disk(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "label": "Alpha plan",
                "profile": {"harness": "codex", "timeout": 600},
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual(1, len(reloaded.pins))
        self.assertEqual("Alpha plan", reloaded.pins[0].label)
        self.assertEqual("codex", reloaded.pins[0].profile.harness)
        self.assertEqual(600, reloaded.pins[0].profile.timeout)

    def test_registry_pin_rejects_negative_review_fix_cap_before_persisting(
        self,
    ) -> None:
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"max_review_fix_cycles": -1},
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("max_review_fix_cycles", body["error"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual((), reloaded.pins)

    def test_registry_pin_names_non_integer_review_fix_cap_in_error(self) -> None:
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"max_review_fix_cycles": "two"},
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])
        self.assertEqual(
            "profile max_review_fix_cycles must be an integer when set",
            body["error"],
        )

    def test_registry_unpin_removes_pin(self) -> None:
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="seed",
        )
        workbench.save_registry(seeded, self.registry_path)
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/unpin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual([], body["registry"]["pins"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual((), reloaded.pins)

    def test_plan_delete_alias_archives_plan_state_and_pin(self) -> None:
        # ``/api/plan/delete`` is now a backward-compatible archive alias: it
        # keeps the confirmation contract but moves the plan into the archive
        # instead of permanently removing it.
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="seed",
        )
        workbench.save_registry(seeded, self.registry_path)
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(self.canonical_plan_dir)}),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan_archived"])
        self.assertTrue(body["state_archived"])
        self.assertTrue(body["pin_archived"])
        self.assertEqual(str(self.canonical_plan_dir), body["plan_dir"])
        # The live plan and state are gone from their original locations...
        self.assertFalse(self.canonical_plan_dir.exists())
        self.assertFalse(plan_state.exists())
        self.assertEqual((), workbench.load_registry(self.registry_path).pins)
        # ...but the plan is preserved under a fresh archive entry.
        listing = workbench.list_archive_entries(self.state_dir)
        self.assertEqual(1, len(listing.entries))
        self.assertEqual(body["archive_id"], listing.entries[0].archive_id)
        self.assertEqual(
            str(self.canonical_plan_dir),
            listing.entries[0].original_plan_dir,
        )

    def test_plan_delete_requires_matching_confirmation(self) -> None:
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.tmp_path / "plans" / "other"),
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_plan_delete_rejects_active_lock(self) -> None:
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            self.canonical_plan_dir,
            label="seed",
        )
        workbench.save_registry(seeded, self.registry_path)
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-07-03T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )

        self.assertEqual(409, status)
        self.assertEqual("active-lock", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())
        self.assertTrue(lock_path.exists())
        self.assertEqual(1, len(workbench.load_registry(self.registry_path).pins))

    def test_plan_delete_rejects_arbitrary_non_plan_directory(self) -> None:
        target = self.tmp_path / "not-a-plan"
        target.mkdir()
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(target),
                "confirm_plan_dir": str(target),
            },
            headers=self._auth(server),
        )

        self.assertEqual(400, status)
        self.assertEqual("invalid-plan", body["reason"])
        self.assertTrue(target.exists())

    def test_plan_delete_alias_archives_missing_discovered_plan_state(self) -> None:
        missing_plan = plans.canonicalize_path(self.tmp_path / "plans" / "missing")
        seeded = workbench.upsert_pin(
            workbench.load_registry(self.registry_path),
            missing_plan,
            label="stale",
        )
        workbench.save_registry(seeded, self.registry_path)
        plan_state = state.plan_state_dir(missing_plan, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(missing_plan)}),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/delete",
            method="POST",
            body={
                "plan_dir": str(missing_plan),
                "confirm_plan_dir": str(missing_plan),
            },
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        # A stale discovered pin archives only the available state and pin.
        self.assertFalse(body["plan_archived"])
        self.assertTrue(body["state_archived"])
        self.assertTrue(body["pin_archived"])
        self.assertFalse(plan_state.exists())
        self.assertEqual((), workbench.load_registry(self.registry_path).pins)
        listing = workbench.list_archive_entries(self.state_dir)
        self.assertEqual(1, len(listing.entries))
        self.assertTrue(listing.entries[0].state_archived)
        self.assertFalse(listing.entries[0].plan_archived)

    def test_plan_archive_endpoint_archives_with_confirmation(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan_archived"])
        self.assertIn("archive_id", body)
        self.assertFalse(self.canonical_plan_dir.exists())
        listing = workbench.list_archive_entries(self.state_dir)
        self.assertEqual([body["archive_id"]], [e.archive_id for e in listing.entries])

    def test_plan_archive_rejects_confirmation_mismatch(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.tmp_path / "plans" / "other"),
            },
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_plan_archive_rejects_active_lock_with_409(self) -> None:
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-07-03T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plan/archive",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "confirm_plan_dir": str(self.canonical_plan_dir),
            },
            headers=self._auth(server),
        )
        self.assertEqual(409, status)
        self.assertEqual("active-lock", body["reason"])
        self.assertTrue(self.canonical_plan_dir.exists())

    def test_plans_archive_bulk_returns_per_entry_results(self) -> None:
        second_plan = self.tmp_path / "plans" / "beta"
        write_plan(second_plan, ("pending",))
        canonical_second = plans.canonicalize_path(second_plan)
        bogus = self.tmp_path / "not-a-plan"
        bogus.mkdir()
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plans/archive",
            method="POST",
            body={
                "plan_dirs": [
                    str(self.canonical_plan_dir),
                    str(canonical_second),
                    str(bogus),
                ]
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertEqual(2, body["archived_count"])
        self.assertEqual(1, body["failed_count"])
        by_dir = {row["plan_dir"]: row for row in body["results"]}
        self.assertTrue(by_dir[str(self.canonical_plan_dir)]["archived"])
        self.assertTrue(by_dir[str(canonical_second)]["archived"])
        self.assertFalse(by_dir[str(bogus)]["archived"])
        self.assertEqual("invalid-plan", by_dir[str(bogus)]["reason"])
        # One blocked entry never prevents the others from being archived.
        self.assertFalse(self.canonical_plan_dir.exists())
        self.assertFalse(canonical_second.exists())
        self.assertTrue(bogus.exists())

    def test_plans_archive_requires_non_empty_list(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/plans/archive",
            method="POST",
            body={"plan_dirs": []},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_archive_list_returns_entries_and_retention(self) -> None:
        result = workbench.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/archive")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(30, body["archive_retention_days"])
        self.assertEqual(
            [result.archive_id], [e["archive_id"] for e in body["entries"]]
        )
        # A fresh entry is inside its window, and the listing says so without
        # having had to touch it.
        self.assertEqual(0, body["archive_cleanup"]["candidate_count"])
        self.assertEqual("retained", body["entries"][0]["cleanup_state"])
        self.assertFalse(body["entries"][0]["cleanup_candidate"])
        # The old maintenance block is gone, not zeroed. A caller that still
        # reads `archive_maintenance` gets nothing rather than "0 purged",
        # which would read as "the pass ran and found nothing".
        self.assertNotIn("archive_maintenance", body)

    def test_archive_list_leaves_expired_entries_in_place(self) -> None:
        # Reading the archive used to be the act that emptied it: this same
        # scenario returned `[]` and a removal count. It now returns the entry,
        # flagged, and leaves it on disk for an operator to decide about. See
        # `changes.KEY_EXPLICIT_ARCHIVE_CLEANUP`.
        result = workbench.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        # Backdate the archived_at so the entry is well past the 30-day window.
        meta_path = (
            workbench.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        server = self._start_server()

        status, body, _ = _http_request(f"{self._base_url(server)}/api/archive")

        self.assertEqual(200, status)
        self.assertEqual(
            [result.archive_id], [e["archive_id"] for e in body["entries"]]
        )
        self.assertEqual("eligible", body["entries"][0]["cleanup_state"])
        self.assertTrue(body["entries"][0]["cleanup_candidate"])
        self.assertEqual([result.archive_id], body["archive_cleanup"]["candidate_ids"])
        self.assertTrue(
            workbench.archive_entry_dir(result.archive_id, self.state_dir).exists()
        )

    def test_repeated_archive_reads_leave_the_archive_tree_unchanged(self) -> None:
        # The property, not one instance of it: reading twice must produce the
        # same tree it started with, expired entry included.
        result = workbench.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        meta_path = (
            workbench.archive_entry_dir(result.archive_id, self.state_dir)
            / workbench.ARCHIVE_METADATA_FILE_NAME
        )
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
        raw["archived_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(raw), encoding="utf-8")
        archive_root = workbench.archive_root(self.state_dir)

        def tree() -> list[str]:
            return sorted(
                str(path.relative_to(archive_root)) for path in archive_root.rglob("*")
            )

        before = tree()
        server = self._start_server()
        for _ in range(3):
            status, _, _ = _http_request(f"{self._base_url(server)}/api/archive")
            self.assertEqual(200, status)

        self.assertEqual(before, tree())

    def test_archive_restore_moves_plan_back(self) -> None:
        result = workbench.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        self.assertFalse(self.canonical_plan_dir.exists())
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/restore",
            method="POST",
            body={"archive_id": result.archive_id},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan_restored"])
        self.assertTrue(self.canonical_plan_dir.exists())
        self.assertEqual((), workbench.list_archive_entries(self.state_dir).entries)

    def test_archive_restore_missing_entry_returns_404(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/restore",
            method="POST",
            body={"archive_id": "20000101T000000Z-deadbeefcafe"},
            headers=self._auth(server),
        )
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_archive_delete_purges_with_matching_confirmation(self) -> None:
        result = workbench.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/delete",
            method="POST",
            body={"archive_ids": [result.archive_id], "confirm_count": 1},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual(1, body["removed_count"])
        self.assertEqual(0, body["failed_count"])
        self.assertEqual((), workbench.list_archive_entries(self.state_dir).entries)

    def test_archive_delete_rejects_count_mismatch(self) -> None:
        result = workbench.archive_plan(
            self.canonical_plan_dir,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/delete",
            method="POST",
            body={"archive_ids": [result.archive_id], "confirm_count": 2},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("confirmation-mismatch", body["reason"])
        # Nothing is purged when confirmation does not match.
        self.assertEqual(1, len(workbench.list_archive_entries(self.state_dir).entries))

    def test_archive_settings_updates_retention_days(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/settings",
            method="POST",
            body={"archive_retention_days": 7},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertEqual(7, body["archive_retention_days"])
        reloaded = workbench.load_registry(self.registry_path)
        self.assertEqual(7, reloaded.archive_retention_days)

    def test_archive_settings_rejects_non_positive(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/archive/settings",
            method="POST",
            body={"archive_retention_days": 0},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_queue_exposes_archivable_and_catalog_fields(self) -> None:
        server = self._start_server(plan_roots=(str(self.tmp_path / "plans"),))
        status, body, _ = _http_request(f"{self._base_url(server)}/api/queue")
        self.assertEqual(200, status)
        self.assertIn("archive_retention_days", body)
        # `archive_maintenance` is gone on purpose: the queue answers from one
        # non-reclaiming catalog scan and runs no retention, so it has no
        # maintenance result to report. No read has one any more -- the pass
        # itself no longer exists. See `changes.KEY_OBSERVATIONAL_READS`.
        self.assertNotIn("archive_maintenance", body)
        self.assertIn("catalog_generation", body)
        self.assertTrue(body["catalog_complete"])
        self.assertEqual(len(body["plans"]), body["catalog_total"])
        self.assertEqual([], body["catalog_warnings"])
        plan_payload = body["plans"][0]
        self.assertIn("archivable", plan_payload)
        self.assertIn("archive_blocker", plan_payload)
        # A ready plan with no active lock is archivable.
        self.assertTrue(plan_payload["archivable"])
        self.assertIsNone(plan_payload["archive_blocker"])

    def test_queue_oldest_sort_orders_missing_activity_first(self) -> None:
        # Second plan carries a finished runner state so it has activity; the
        # first plan has none and must sort oldest-first.
        second_plan = self.tmp_path / "plans" / "beta"
        write_plan(second_plan, ("pending",))
        canonical_second = plans.canonicalize_path(second_plan)
        second_state = state.plan_state_dir(canonical_second, self.state_dir)
        second_state.mkdir(parents=True, exist_ok=True)
        (second_state / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "plan_dir": str(canonical_second),
                    "run_finished_at": "2026-07-03T12:00:00+00:00",
                    "active_turn": {"last_activity_at": "2026-07-03T12:00:00+00:00"},
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server(plan_roots=(str(self.tmp_path / "plans"),))
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/queue?sort=oldest"
        )
        self.assertEqual(200, status)
        ordered = [p["plan_dir"] for p in body["plans"]]
        self.assertEqual(str(self.canonical_plan_dir), ordered[0])

    def test_oldest_sort_uses_completed_at_for_complete_plans(self) -> None:
        # Build a real complete summary, then force completed_at and
        # last_activity_at to diverge so the comparator must be reading
        # completed_at (not last_activity_at) for complete plans. If the
        # old last_activity_at path were still in force the ordering would
        # invert and this assertion would fail.
        done_dir = self.tmp_path / "plans" / "done"
        write_plan(done_dir, ("complete",))
        canonical = plans.canonicalize_path(done_dir)
        base = workbench.build_workbench_summary(
            _discovered(canonical), state_dir=self.state_dir
        )
        self.assertEqual("complete", base.primary_state)

        older = dataclasses.replace(
            base,
            completed_at="2026-01-01T00:00:00Z",
            last_activity_at="2026-12-31T00:00:00Z",
        )
        newer = dataclasses.replace(
            base,
            completed_at="2026-06-01T00:00:00Z",
            last_activity_at="2026-01-01T00:00:00Z",
        )

        ordered = workbench_server._sort_summaries_oldest_first([newer, older])

        self.assertEqual(
            ["2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z"],
            [summary.completed_at for summary in ordered],
        )

    def test_oldest_sort_missing_completed_at_sorts_complete_plan_first(
        self,
    ) -> None:
        # A complete plan with no completion timestamp must sort as the
        # oldest, ahead of an active plan that does have activity.
        done_dir = self.tmp_path / "plans" / "done-bare"
        write_plan(done_dir, ("complete",))
        complete = workbench.build_workbench_summary(
            _discovered(plans.canonicalize_path(done_dir)),
            state_dir=self.state_dir,
        )
        self.assertEqual("complete", complete.primary_state)
        self.assertIsNone(complete.completed_at)

        active = dataclasses.replace(
            workbench.build_workbench_summary(
                _discovered(self.canonical_plan_dir), state_dir=self.state_dir
            ),
            last_activity_at="2026-06-01T00:00:00Z",
        )

        ordered = workbench_server._sort_summaries_oldest_first([active, complete])

        self.assertEqual("complete", ordered[0].primary_state)

    def test_queue_rejects_unknown_sort_mode(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/queue?sort=bogus"
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])

    def test_launch_endpoint_uses_workbench_helper(self) -> None:
        server = self._start_server()
        sentinel = workbench.LaunchResult(
            pid=4242,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=False,
            reply_length=None,
            reply_origin=None,
            state_dir=self.state_dir,
        )
        with mock.patch.object(
            workbench, "launch_plan", return_value=sentinel
        ) as patched:
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/launch",
                method="POST",
                body={"plan_dir": str(self.canonical_plan_dir)},
                headers=self._auth(server),
            )
        self.assertEqual(200, status)
        self.assertEqual(4242, body["launch"]["pid"])
        patched.assert_called_once()
        kwargs = patched.call_args.kwargs
        self.assertEqual(self.state_dir, kwargs["state_dir"])
        self.assertIsNone(kwargs["profile"])

    def test_launch_endpoint_maps_invalid_plan_error(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/launch",
            method="POST",
            body={"plan_dir": str(self.tmp_path / "does-not-exist")},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual(workbench.LAUNCH_REASON_INVALID_PLAN, body["reason"])

    def test_reply_endpoint_invokes_resume_helper(self) -> None:
        server = self._start_server()
        sentinel = workbench.LaunchResult(
            pid=99,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=True,
            reply_length=5,
            reply_origin="stdin",
            state_dir=self.state_dir,
        )
        with mock.patch.object(
            workbench, "resume_plan_with_reply", return_value=sentinel
        ) as patched:
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/reply",
                method="POST",
                body={
                    "plan_dir": str(self.canonical_plan_dir),
                    "reply_text": "go-ok",
                },
                headers=self._auth(server),
            )
        self.assertEqual(200, status)
        self.assertEqual(99, body["launch"]["pid"])
        self.assertTrue(body["launch"]["reply_provided"])
        kwargs = patched.call_args.kwargs
        self.assertEqual("go-ok", kwargs["reply_text"])

    def test_reply_endpoint_requires_reply_text(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/reply",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("reply_text", body["error"])

    def test_control_stop_after_current_enqueues_command(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/stop-after-current",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertTrue(body["ok"])
        queued = body["queued_command"]
        self.assertEqual(
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            queued["command"],
        )
        queue_path = control_queue.queue_path_for_plan(
            self.canonical_plan_dir, self.state_dir
        )
        self.assertTrue(queue_path.is_file())
        lines = queue_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        record = json.loads(lines[0])
        self.assertEqual(queued["id"], record["id"])

    def test_control_abort_enqueues_command_with_cli_payload_shape(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertTrue(body["ok"])
        self.assertFalse(body["run_active"])
        self.assertFalse(body["turn_active"])
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS, body["grace_seconds"]
        )
        self.assertIsNone(body["abort_reason"])
        self.assertEqual(control_cli.NO_ACTIVE_RUN_NOTE, body["note"])
        queued = body["queued_command"]
        self.assertEqual(control_queue.COMMAND_REQUEST_ABORT_TURN, queued["command"])
        queue_path = control_queue.queue_path_for_plan(
            self.canonical_plan_dir, self.state_dir
        )
        record = json.loads(queue_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(queued["id"], record["id"])
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS, record["grace_seconds"]
        )

    def test_control_abort_accepts_grace_and_reason(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "grace_seconds": 30,
                "reason": "supervisor stop",
            },
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertEqual(30, body["grace_seconds"])
        self.assertEqual("supervisor stop", body["abort_reason"])
        self.assertEqual(30, body["queued_command"]["grace_seconds"])
        self.assertEqual("supervisor stop", body["queued_command"]["reason"])

    def test_control_abort_reports_active_turn(self) -> None:
        _write_lock_file(self.canonical_plan_dir, self.state_dir, pid=os.getpid())
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "plan_dir": str(self.canonical_plan_dir),
                    "active_turn": {"session": "1", "turn_index": 1},
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(202, status)
        self.assertTrue(body["run_active"])
        self.assertTrue(body["turn_active"])
        self.assertEqual(control_cli.ABORT_WILL_APPLY_NOTE, body["note"])

    def test_control_abort_degrades_note_without_active_turn(self) -> None:
        _write_lock_file(self.canonical_plan_dir, self.state_dir, pid=os.getpid())
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(202, status)
        self.assertTrue(body["run_active"])
        self.assertFalse(body["turn_active"])
        self.assertEqual(control_cli.ABORT_DEGRADES_NOTE, body["note"])

    def test_control_abort_sees_run_behind_symlinked_plan_path(self) -> None:
        # The lock records the canonical plan directory, so an uncanonical
        # request path must not make an active run look absent.
        link_root = self.tmp_path / "link"
        link_root.symlink_to(self.tmp_path / "plans", target_is_directory=True)
        linked_plan_dir = link_root / "alpha"
        _write_lock_file(self.canonical_plan_dir, self.state_dir, pid=os.getpid())
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(linked_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(202, status)
        self.assertTrue(body["run_active"])
        self.assertEqual(control_cli.ABORT_DEGRADES_NOTE, body["note"])

    def test_control_abort_rejects_grace_the_cli_rejects(self) -> None:
        # Same bounds and the same stable reasons the CLI reports, because
        # both surfaces validate through control_queue.append_command.
        server = self._start_server()
        for grace in (
            0,
            control_queue.MAX_ABORT_GRACE_SECONDS + 1,
            "120",
            True,
        ):
            with self.subTest(grace=grace):
                status, body, _ = _http_request(
                    f"{self._base_url(server)}/api/control/abort",
                    method="POST",
                    body={
                        "plan_dir": str(self.canonical_plan_dir),
                        "grace_seconds": grace,
                    },
                    headers=self._auth(server),
                )
                self.assertEqual(400, status)
                self.assertEqual(control_queue.REJECT_INVALID_GRACE, body["reason"])
        queue_path = control_queue.queue_path_for_plan(
            self.canonical_plan_dir, self.state_dir
        )
        self.assertFalse(queue_path.exists())

    def test_control_abort_rejects_empty_reason(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir), "reason": ""},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual(control_queue.REJECT_INVALID_REASON, body["reason"])

    def test_control_abort_requires_plan_dir(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("plan_dir", body["error"])

    def test_control_select_harness_validates_name(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/select-harness",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "harness": "bogus",
            },
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-harness", body["reason"])

    def test_control_select_harness_accepts_supported_name(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/select-harness",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "harness": "codex",
            },
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertEqual(
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            body["queued_command"]["command"],
        )
        self.assertEqual("codex", body["queued_command"]["harness"])

    def test_unlock_rejects_active_lock_with_409(self) -> None:
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-06-24T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(409, status)
        self.assertEqual("active-lock", body["reason"])
        self.assertTrue(lock_path.exists())

    def test_unlock_force_removes_active_lock(self) -> None:
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-06-24T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "force": True,
            },
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        self.assertTrue(body["removed"])
        self.assertFalse(lock_path.exists())

    def test_unlock_idempotent_when_no_lock(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertFalse(body["had_lock"])
        self.assertFalse(body["removed"])

    def test_post_with_malformed_json_returns_400(self) -> None:
        server = self._start_server()
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self._base_url(server)}/api/registry/pin",
            data=b"{not valid json",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {server.token}",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(400, ctx.exception.code)
        body = json.loads(ctx.exception.read())
        self.assertEqual("invalid-request", body["reason"])

    def test_post_with_missing_required_field_returns_400(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/stop-after-current",
            method="POST",
            body={},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("plan_dir", body["error"])

    def test_unknown_route_returns_404(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_wrong_method_returns_405_with_allow(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request(
            f"{self._base_url(server)}/api/queue",
            method="POST",
            body={},
            headers=self._auth(server),
        )
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", body["reason"])
        self.assertIn("GET", body["allow"])

    def test_response_has_no_store_cache_control(self) -> None:
        server = self._start_server()
        _, _, headers = _http_request(f"{self._base_url(server)}/api/health")
        self.assertEqual("no-store", headers.get("Cache-Control"))

    def test_registry_pin_round_trip_preserves_every_profile_field(
        self,
    ) -> None:
        # The frontend profile editor reads-modifies-writes pin payloads.
        # Confirm every dataclass field survives a POST + GET round trip
        # so the detail view does not silently drop operator settings.
        server = self._start_server()
        full_profile_body = {
            "cwd": "/tmp/cwd",
            "harness": "codex",
            "harness_model": "o3-mini",
            "effort": "medium",
            "timeout": 1234,
            "commit_mode": "per-session",
            "review_harness": "claude",
            "review_harness_model": "claude-sonnet",
            "max_review_fix_cycles": 3,
            "unsafe_bypass": True,
            "allow_not_loop_ready": True,
            "note": "scratch profile",
        }
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "label": "Round Trip",
                "profile": full_profile_body,
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])

        status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
        self.assertEqual(200, status)
        pins = body["registry"]["pins"]
        self.assertEqual(1, len(pins))
        pin = pins[0]
        self.assertEqual("Round Trip", pin["label"])
        profile = pin["profile"]
        for key, expected in full_profile_body.items():
            self.assertEqual(
                expected,
                profile.get(key),
                msg=f"profile field {key!r} did not round-trip",
            )
        self.assertNotIn("model", profile)

    def test_registry_pin_refuses_an_unpermitted_commit_mode(self) -> None:
        # `commit_mode` is a two-value closed set, so an unrecognized value is
        # a malformed request rather than a preference the launch can resolve.
        # Refusing it here is what keeps a pin from storing a mode no launch
        # would ever honour.
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"commit_mode": "sometimes"},
            },
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("commit_mode", body["error"])

    def test_registry_pin_accepts_an_absent_commit_mode(self) -> None:
        # Absence is not a wrong value: a profile that never names a mode must
        # still round-trip as `null` rather than being refused with it.
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "profile": {"commit_mode": None, "note": "no preference"},
            },
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
        self.assertEqual(200, status)
        self.assertIsNone(body["registry"]["pins"][0]["profile"]["commit_mode"])

    def test_registry_pin_round_trip_preserves_nullable_boolean_states(
        self,
    ) -> None:
        # Nullable launch booleans must survive a POST + GET round trip in
        # each of their three states (null / false / true). A blank launch
        # profile that records explicit ``None`` must not silently coerce
        # to ``False`` because the runner would then emit
        # ``--no-unsafe-bypass`` and override the harness default.
        server = self._start_server()
        cases = (
            ("explicit-null", None, None),
            ("explicit-false", False, False),
            ("explicit-true", True, True),
        )
        for label, unsafe_value, allow_value in cases:
            profile_body = {
                "unsafe_bypass": unsafe_value,
                "allow_not_loop_ready": allow_value,
            }
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/registry/pin",
                method="POST",
                body={
                    "plan_dir": str(self.canonical_plan_dir),
                    "label": label,
                    "profile": profile_body,
                },
                headers=self._auth(server),
            )
            self.assertEqual(200, status, msg=f"case={label}")
            self.assertTrue(body["ok"], msg=f"case={label}")

            status, body, _ = _http_request(f"{self._base_url(server)}/api/registry")
            self.assertEqual(200, status, msg=f"case={label}")
            pins = body["registry"]["pins"]
            self.assertEqual(1, len(pins), msg=f"case={label}")
            profile = pins[0]["profile"]
            # ``profile.get`` would conflate a missing key with ``None``;
            # assert the key is present so a future regression that drops
            # the field entirely cannot pass silently.
            self.assertIn("unsafe_bypass", profile, msg=f"case={label}")
            self.assertIn("allow_not_loop_ready", profile, msg=f"case={label}")
            self.assertIs(
                unsafe_value,
                profile["unsafe_bypass"],
                msg=f"case={label} unsafe_bypass",
            )
            self.assertIs(
                allow_value,
                profile["allow_not_loop_ready"],
                msg=f"case={label} allow_not_loop_ready",
            )

    def test_reply_response_never_echoes_reply_text(self) -> None:
        # The reply endpoint must never persist or echo the operator's
        # answer text; only the length + origin marker are surfaced.
        server = self._start_server()
        sensitive_reply = "TOKEN-XYZ-must-not-leak"
        sentinel = workbench.LaunchResult(
            pid=77,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=True,
            reply_length=len(sensitive_reply),
            reply_origin="stdin",
            state_dir=self.state_dir,
        )
        with mock.patch.object(
            workbench, "resume_plan_with_reply", return_value=sentinel
        ):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/reply",
                method="POST",
                body={
                    "plan_dir": str(self.canonical_plan_dir),
                    "reply_text": sensitive_reply,
                },
                headers=self._auth(server),
            )

        self.assertEqual(200, status)
        encoded = json.dumps(body)
        self.assertNotIn(
            sensitive_reply,
            encoded,
            msg="reply text leaked into the response body",
        )
        self.assertTrue(body["launch"]["reply_provided"])
        self.assertEqual(len(sensitive_reply), body["launch"]["reply_length"])
        self.assertEqual("stdin", body["launch"]["reply_origin"])


class WorkbenchQuestionsAPITests(unittest.TestCase):
    """Cover ``GET /api/questions`` paused-run resolver context."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / "workbench.json"
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def tearDown(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
            finally:
                srv.server_close()

    def _start_server(self) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="workbench-questions-test",
            daemon=True,
        )
        thread.start()
        self.servers.append(server)

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def _base_url(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        return f"http://{server.bound_host}:{server.bound_port}"

    def _seed_paused_plan(
        self,
        *,
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
        paused: bool = True,
        last_message_text: str = "Should we use 1a or 1b?\n",
    ) -> Path:
        plan_dir = self.tmp_path / "plans" / "qna"
        if plan_dir.exists():
            shutil.rmtree(plan_dir)
        write_plan(plan_dir, statuses)
        canonical = plans.canonicalize_path(plan_dir)
        plan_state = state.plan_state_dir(canonical, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        last_message_path = plan_state / "last-message.txt"
        last_message_path.write_text(last_message_text, encoding="utf-8")
        turn: dict[str, object] = {
            "run_id": "run-1",
            "turn_index": 1,
            "session_id": paused_session_id,
            "session": int(paused_session_id),
            "session_number": int(paused_session_id),
            "harness": "fake",
            "model": "test-model",
            "model_source": "default",
            "effort": "medium",
            "effort_source": "default",
            "artifact_dir": str(plan_state / "turn-0001"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": not paused,
            "paused": paused,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": int(paused_session_id)},
            "details": {"session": int(paused_session_id)},
            "run_id": "run-1",
            "run_dir": str(plan_state / "runs" / "run-1"),
            "harness": "fake",
            "model": "test-model",
            "effort": "medium",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return canonical

    def test_questions_endpoint_returns_rendered_prompt_and_last_message(
        self,
    ) -> None:
        canonical = self._seed_paused_plan()
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(canonical)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/questions?{query}"
        )
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("workbench", body["command"])
        self.assertEqual("ok", body["reason"])
        self.assertIn("rendered_prompt", body)
        self.assertIn("last_message", body)
        self.assertIn("Should we use 1a or 1b?", body["last_message"])
        self.assertEqual("1", body["session_id"])
        self.assertEqual("runner-pause", body["pause_reason"])

    def test_questions_endpoint_returns_404_when_no_paused_run(self) -> None:
        plan_dir = self.tmp_path / "plans" / "no-paused"
        write_plan(plan_dir, ("pending", "pending"))
        canonical = plans.canonicalize_path(plan_dir)
        # No state file at all → load_qna_context raises no-paused-run.
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(canonical)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/questions?{query}"
        )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])
        self.assertEqual("no-paused-run", body["reason"])

    def test_questions_endpoint_returns_400_on_reply_context_mismatch(
        self,
    ) -> None:
        # Session 1 already completed, session 2 pending, but the paused
        # state references session 1: a context mismatch.
        canonical = self._seed_paused_plan(
            statuses=("complete", "pending"),
            paused_session_id="1",
        )
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(canonical)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/questions?{query}"
        )
        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("reply-context-mismatch", body["reason"])

    def test_questions_endpoint_requires_plan_dir(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/questions")
        self.assertEqual(400, status)
        self.assertFalse(body["ok"])
        self.assertEqual("invalid-request", body["reason"])


class WorkbenchArtifactReadAPITests(unittest.TestCase):
    """Cover read-only artifact, event, log, progress, and review APIs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / "workbench.json"
        self.plan_dir = self.tmp_path / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical_plan_dir = plans.canonicalize_path(self.plan_dir)
        self.plan_state_dir = state.plan_state_dir(
            self.canonical_plan_dir, self.state_dir
        )
        self.plan_state_dir.mkdir(parents=True, exist_ok=True)
        self.servers: list[workbench_server.WorkbenchHTTPServer] = []

    def tearDown(self) -> None:
        for srv in self.servers:
            try:
                srv.shutdown()
            finally:
                srv.server_close()

    def _start_server(self) -> workbench_server.WorkbenchHTTPServer:
        server = workbench_server.create_server(
            host="127.0.0.1",
            port=0,
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            plan_roots=(),
            include_default_dirs=False,
        )
        thread = threading.Thread(
            target=server.serve_forever, name="workbench-test", daemon=True
        )
        thread.start()
        self.servers.append(server)

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def _base_url(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        return f"http://{server.bound_host}:{server.bound_port}"

    def _write_state(self, payload: dict) -> Path:
        state_path = self.plan_state_dir / "state.json"
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return state_path

    def _write_events(self, records: list[dict]) -> Path:
        events_path = self.plan_state_dir / "events.jsonl"
        lines = [json.dumps(record, sort_keys=True) for record in records]
        events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return events_path

    def _make_turn(
        self,
        *,
        run_id: str = "run-1",
        turn_index: int = 1,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
        last_message_path: str | None = None,
        result_path: str | None = None,
    ) -> dict:
        return {
            "run_id": run_id,
            "turn_index": turn_index,
            "session_id": "1",
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "last_message_path": last_message_path,
            "result_path": result_path,
        }

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
        (registered / plans.PLAN_METADATA_FILE_NAME).write_text(
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
        canonical_dotted = plans.canonicalize_path(dotted_plan_dir)
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
        progress_path = self.canonical_plan_dir / plans.PROGRESS_FILE_NAME
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
        progress_path = self.canonical_plan_dir / plans.PROGRESS_FILE_NAME
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
        registry = plans.read_repository_registry(registered)
        assert registry is not None
        workspace_root = self.tmp_path / "workspace"
        plans.save_workspace_plan_metadata(registered, workspace_root, registry.digest)
        (registered / workbench.FINAL_REVIEW_FILE_NAME).write_text(
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
