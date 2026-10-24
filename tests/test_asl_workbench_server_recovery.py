"""Workbench server tests: recovery detail endpoint.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import urllib.parse

from agent_session_loop import (
    plans_files,
    plans_metadata,
    plans_registry,
    workbench_registry,
)
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
    _write_raw_state_payload,
    _write_state_payload,
    write_multi_repo_plan,
)

ASL_TEST_LANE = "integration"


class WorkbenchServerRecoveryTests(WorkbenchServerTestCase):
    """Recovery detail endpoint."""

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
        registry = plans_registry.read_repository_registry(registered)
        assert registry is not None
        workspace_root = self.tmp_path / "workspace"
        plans_metadata.save_workspace_plan_metadata(
            registered, workspace_root, registry.digest
        )
        canonical = plans_files.canonicalize_path(registered)
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
        registry = workbench_registry.load_registry(self.registry_path)
        registry = workbench_registry.upsert_pin(
            registry,
            self.canonical_plan_dir,
            label="Alpha plan",
        )
        workbench_registry.save_registry(registry, self.registry_path)

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
