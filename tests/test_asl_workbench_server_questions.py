"""Workbench server tests: the questions endpoint.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from agent_session_loop import events, plans_files, state, workbench_server
from tests.support import (
    _http_request,
    write_plan,
)


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
        canonical = plans_files.canonicalize_path(plan_dir)
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
        canonical = plans_files.canonicalize_path(plan_dir)
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
