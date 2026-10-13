"""Servers and fixtures shared by the Workbench front-end test modules."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from agent_session_loop import plans_files, state, workbench_server
from tests.support import (
    _http_request_raw,
    write_plan,
)


class ShellSourceMixin:
    """Read back the whole front end the server hands a browser.

    The shell is native ES modules: the entry is ``/static/app.js`` and the
    behavior is in ``/static/modules/``. Fetching them over HTTP rather than
    reading them off disk keeps these assertions about what a browser
    receives, and concatenating them keeps each assertion about the front end
    as a whole rather than about which file a function ended up in.
    """

    def _shell_source(self, server: workbench_server.WorkbenchHTTPServer) -> str:
        base = self._base_url(server)  # type: ignore[attr-defined]
        assert_equal = self.assertEqual  # type: ignore[attr-defined]
        status, body, _ = _http_request_raw(base + "/static/app.js")
        assert_equal(200, status)
        sources = [body.decode("utf-8")]
        module_dir = workbench_server.STATIC_DIR / "modules"
        self.assertTrue(module_dir.is_dir(), str(module_dir))  # type: ignore[attr-defined]
        for module in sorted(module_dir.glob("*.js")):
            status, body, _ = _http_request_raw(f"{base}/static/modules/{module.name}")
            assert_equal(200, status, module.name)
            sources.append(body.decode("utf-8"))
        return "\n".join(sources)


class WorkbenchStaticCase(ShellSourceMixin, unittest.TestCase):
    """Server fixture for tests of the frontend served at ``/`` and ``/static/*``."""

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
            target=server.serve_forever, name="workbench-static-test", daemon=True
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


class WorkbenchHardeningCase(ShellSourceMixin, unittest.TestCase):
    """Server and plan-state fixture for the Workbench hardening tests."""

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
            target=server.serve_forever,
            name="workbench-hardening-test",
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

    def _write_state_with_turn(self, *, stdout_path: Path) -> None:
        state_path = self.plan_state_dir / "state.json"
        payload = {
            "run_id": "run-h",
            "harness": "codex",
            "active_turn": {
                "run_id": "run-h",
                "turn_index": 1,
                "session_id": "1",
                "stdout_path": str(stdout_path),
                "stderr_path": None,
                "last_message_path": None,
                "result_path": None,
            },
            "active_verification": None,
        }
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    # -- Cleanup mode + Archive view (session 4) ---------------------------
