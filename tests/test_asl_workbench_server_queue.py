"""Workbench server tests: queue and plan summary endpoints.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import os
import urllib.parse

from agent_session_loop import plans_files, plans_metadata, state, workbench
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    write_multi_repo_plan,
    write_plan,
)


class WorkbenchServerQueueTests(WorkbenchServerTestCase):
    """Queue and plan summary endpoints."""

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
        (broken / plans_metadata.PLAN_METADATA_FILE_NAME).write_text(
            "{not-json", encoding="utf-8"
        )
        canonical_broken = plans_files.canonicalize_path(broken)
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
        complete_plan = plans_files.canonicalize_path(complete_dir)
        registry = workbench.upsert_pin(registry, complete_plan, label="Done plan")

        failed_dir = self.tmp_path / "plans" / "failed"
        write_plan(failed_dir, ("pending", "pending"))
        failed_plan = plans_files.canonicalize_path(failed_dir)
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
        plan_root = plans_files.canonicalize_path(self.plan_dir.parent)
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
