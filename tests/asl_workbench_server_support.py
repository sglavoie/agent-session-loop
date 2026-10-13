"""Shared base classes for the Workbench HTTP server tests."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_session_loop import plans_files, state, workbench_server
from tests.support import write_plan


class WorkbenchServerTestCase(unittest.TestCase):
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
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)
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


class WorkbenchArtifactReadTestCase(unittest.TestCase):
    """Fixture for read-only artifact, event, log, progress, and review APIs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = self.tmp_path / "workbench.json"
        self.plan_dir = self.tmp_path / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)
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
