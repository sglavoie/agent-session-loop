"""Shared fixtures for test_workbench_controls."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_projection,
    bridge_projection,
    bridge_protocol,
    control_queue,
    intervention,
    plans_files,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    workbench_controls,
    workbench_server,
)
from agent_session_loop.bridge_session import BridgeSession

from .support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "20260619T140000Z-1"
SESSION_ID = "9"
OPERATION_ID = "wbc-op-3d17aa"
FEEDBACK = "stop and re-read the session contract before touching the journal"
EDITED_FEEDBACK = "stop, re-read the session contract, and then revert the journal"
#: A pid this host is not running, so the journal's owner reads as gone and the
#: interrupted statuses become reachable without killing anything real.
DEAD_PID = 4_242_424
#: A path no filesystem can be asked about, which is how a read of the plan
#: itself is made to fail without depending on permissions or on a corpus the
#: readers deliberately tolerate.
UNRESOLVABLE_PLAN_DIR = "plans/\x00/plan-alpha"
#: The pause that hands a person a one-use authorization to start over.
RECOVERY_PAUSE_REASON = "conversation-recovery-required"


class ControlsTestCase(unittest.TestCase):
    """A plan on disk, with a runner as live or as absent as a test needs."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.plans_root = root / "plans"
        self.plans_root.mkdir()
        self.plan_dir = self.plans_root / "plan-alpha"
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.registry_path = root / "workbench.json"
        write_plan(self.plan_dir, ("pending",))
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)
        self.wakeup: threading.Event | None = None
        self._server: workbench_server.WorkbenchHTTPServer | None = None
        self.snapshots: list[dict[str, Any]] = []

    # -- corpus -----------------------------------------------------------

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        turn_index: int | None = 1,
        paused: bool = False,
        pause_reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
            "harness": "claude",
            "run_started_at": "2026-06-19T14:00:00+00:00",
        }
        if pause_reason is not None:
            payload["reason"] = pause_reason
            payload["pause_reason"] = pause_reason
        if run_id is not None:
            payload["run_id"] = run_id
        if run_id is not None and turn_index is not None:
            payload["active_turn"] = {
                "run_id": run_id,
                "session_id": SESSION_ID,
                "turn_index": turn_index,
                "session_kind": "implementation",
                "harness": "claude",
            }
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs: Any) -> None:
        params: dict[str, Any] = {"pid": os.getpid(), "hostname": socket.gethostname()}
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def serve_redirects(self) -> Path:
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def live_run(self, **state_kwargs: Any) -> Path:
        """A run that is live, holds a turn, and answers the urgent channel."""
        self.write_state(**state_kwargs)
        return self.serve_redirects()

    def target(self, *, turn_index: int = 1) -> redirect_store_models.RedirectTarget:
        return redirect_store_models.RedirectTarget(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            turn_index=turn_index,
            harness="claude",
        )

    def journal(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def queued(self) -> control_queue.DrainResult:
        return control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

    def facts(self) -> intervention.RunFacts:
        return intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)

    def action_token(self) -> str:
        return intervention.action_token(self.facts())

    def accept_when_pending(
        self, target: redirect_store_models.RedirectTarget | None = None
    ) -> threading.Thread:
        """Play the runner: record acceptance while the submit wait is running."""
        wanted = self.target() if target is None else target

        def accept() -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                pending = self.journal().pending()
                if pending:
                    redirect_store_requests.accept_request(
                        self.plan_dir,
                        pending[0].operation_id,
                        target=wanted,
                        state_dir=self.state_dir,
                    )
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        return thread

    def submit(self, **overrides: Any) -> workbench_controls.RedirectSubmission:
        """One correction through the shared service, carrying a fresh proof.

        A correction is the one control that is held to the turn it interrupts
        under every authorization, so the token is part of the default request
        rather than something only the strict tests supply.
        """
        params: dict[str, Any] = {
            "operation_id": OPERATION_ID,
            "text": FEEDBACK,
            "state_dir": self.state_dir,
            "wait_seconds_raw": 0.05,
            "action_token": self.action_token(),
        }
        params.update(overrides)
        return workbench_controls.submit_correction(self.plan_dir, **params)

    # -- surfaces ---------------------------------------------------------

    def server(self) -> workbench_server.WorkbenchHTTPServer:
        if self._server is None:
            server = workbench_server.create_server(
                host="127.0.0.1",
                port=0,
                state_dir=self.state_dir,
                registry_path=self.registry_path,
                plan_roots=(),
                include_default_dirs=False,
                token=None,
            )
            thread = threading.Thread(
                target=server.serve_forever, name="workbench-controls-test", daemon=True
            )
            thread.start()

            def _shutdown() -> None:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.addCleanup(_shutdown)
            self._server = server
        return self._server

    def post(self, path: str, body: dict[str, Any]) -> tuple[int, dict, dict]:
        active = self.server()
        return _http_request(
            f"http://{active.bound_host}:{active.bound_port}{path}",
            method="POST",
            body=body,
            headers={"Authorization": f"Bearer {active.token}"},
            timeout=30.0,
        )

    def get(self, path: str) -> tuple[int, dict, dict]:
        active = self.server()
        return _http_request(
            f"http://{active.bound_host}:{active.bound_port}{path}", timeout=30.0
        )

    def bridge_session(self) -> BridgeSession:
        self.snapshots.append(
            bridge_projection.build_runs_snapshot(
                state_dir=self.state_dir,
                plan_roots=(self.plans_root,),
                include_default_dirs=False,
            )
        )
        return BridgeSession(
            snapshot_builder=lambda: self.snapshots[-1], state_dir=self.state_dir
        )

    def handshaken(self) -> BridgeSession:
        session = self.bridge_session()
        response = self.frame(
            session,
            bridge_protocol.METHOD_HANDSHAKE,
            {
                "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                "snapshot_digest": self.snapshots[-1]["digest"],
            },
        )
        assert response["result"]["mutations_enabled"], response
        return session

    def frame(
        self,
        session: BridgeSession,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str = "req-1",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "request",
            "protocol": bridge_protocol.PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        response = session.handle_frame(
            bridge_protocol.encode_frame(payload).rstrip(b"\n")
        )
        assert response is not None
        return response
