# mypy: ignore-errors
# ruff: noqa
"""Urgent corrections and recovery reads over the Workbench HTTP surface.

The subsystems underneath are already tested elsewhere -- admission decides what
may be submitted, the journal decides what is durable, ``redirect_cli`` decides
what an operator is told, and ``intervention`` decides who may act at all. What
is provable only here is that the HTTP surface reaches them on the same terms
the CLI and the bridge do:

- A correction is a mutation, so it is gated on the bearer token *and* the
  action token, and a request that fails either gate must leave the journal
  untouched. A refusal that still wrote is worse than no feature: the operator
  is told the correction did not land and the worker gets it anyway.
- A refusal that persisted nothing is never answered ``202``. The status code
  and the receipt have to agree about whether anything exists to ask after.
- A browser may retry. Replay is served out of the journal by ``operation_id``
  before the action token is even looked at, because the correction that landed
  is exactly what made the token the client still holds stale.
- The spawning endpoints get the same protection against a reconnecting browser
  that the redirect endpoint gets from the journal, except that nothing durable
  records a launch, so the guard is this process's own bookkeeping plus the
  plan's ownership.
- Every redirect frame is body-free. The handlers never call the one function
  that returns an operator's words, so the proof here is the frame: digest and
  byte count, never the text, in the receipt, the status, and the recovery
  report alike.
- The answer survives the server that gave it. A second server over the same
  corpus -- a restart, from the browser's side -- reports the same correction
  from the journal rather than from anything the first one held.

The two recovery reads are read-only by construction, and the tests say so
rather than implying it: both pauses they cover hand a decision to a person, and
one of the two has no authorization that exists at all.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    intervention,
    plans_files,
    redirect_admission,
    redirect_cli,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
    state,
    workbench_launch,
    workbench_registry_models,
    workbench_server,
)

from .support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "20260619T140000Z-1"
SESSION_ID = "9"
OPERATION_ID = "wb-op-4f1c22"
# Long enough that a substring search for it is meaningful, and distinctive
# enough that finding it anywhere in a response is unambiguous.
FEEDBACK = "stop and re-read the session contract before touching the journal"
# What a second correction looks like: the operator kept typing after the
# response was lost, so the draft no longer matches the id the browser holds.
EDITED_FEEDBACK = "stop, re-read the session contract, and then revert the journal"


class WorkbenchRedirectTestCase(unittest.TestCase):
    """A plan on disk, plus loopback servers over it on demand.

    Servers are started per test rather than per class because two of the facts
    under test are about a server's own memory: the launch guard is in-process,
    and the redirect answer must not be.
    """

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
        # Built from the canonicalizer rather than from `resolve`: on macOS the
        # two disagree about /var, and every handler answers with the canonical
        # form.
        self.canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)
        self.wakeup: threading.Event | None = None
        self._server: workbench_server.WorkbenchHTTPServer | None = None

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
        """Serve this plan's urgent FIFO for the test, and name it in the lock."""
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

    def accept_when_pending(
        self, target: redirect_store_models.RedirectTarget | None = None
    ) -> threading.Thread:
        """Play the runner: record acceptance while the submit wait is running.

        Acceptance is read from the journal rather than inferred from the
        delivered wakeup, so a test that wants an accepted correction has to
        write the real acceptance record with the real writer, during the wait.
        """
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

    def action_token(self) -> str:
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        return intervention.action_token(facts)

    # -- servers ----------------------------------------------------------

    def start_server(self) -> workbench_server.WorkbenchHTTPServer:
        """Bind one more loopback server over this corpus, shut down on cleanup."""
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
            target=server.serve_forever, name="workbench-redirect-test", daemon=True
        )
        thread.start()

        def _shutdown() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_shutdown)
        return server

    def server(self) -> workbench_server.WorkbenchHTTPServer:
        """The default server for this test, started on first use."""
        if self._server is None:
            self._server = self.start_server()
        return self._server

    def post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        server: workbench_server.WorkbenchHTTPServer | None = None,
        token: bool = True,
    ) -> tuple[int, dict, dict]:
        active = self.server() if server is None else server
        headers = {"Authorization": f"Bearer {active.token}"} if token else None
        return _http_request(
            f"http://{active.bound_host}:{active.bound_port}{path}",
            method="POST",
            body=body,
            headers=headers,
            # Comfortably above every acceptance wait these tests ask for, so a
            # failure reads as the server's answer rather than as the client
            # giving up on it.
            timeout=30.0,
        )

    def get(
        self,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
        server: workbench_server.WorkbenchHTTPServer | None = None,
        method: str = "GET",
    ) -> tuple[int, dict, dict]:
        active = self.server() if server is None else server
        url = f"http://{active.bound_host}:{active.bound_port}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(list(params))}"
        return _http_request(url, method=method, timeout=30.0)

    # -- requests ---------------------------------------------------------

    def redirect_body(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": OPERATION_ID,
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        body.update(overrides)
        return body

    def submit(
        self,
        *,
        server: workbench_server.WorkbenchHTTPServer | None = None,
        token: bool = True,
        **overrides: Any,
    ) -> tuple[int, dict, dict]:
        return self.post(
            "/api/control/redirect",
            self.redirect_body(**overrides),
            server=server,
            token=token,
        )

    def status(
        self,
        *,
        server: workbench_server.WorkbenchHTTPServer | None = None,
        params: tuple[tuple[str, str], ...] = (),
    ) -> tuple[int, dict, dict]:
        return self.get(
            "/api/redirect-status",
            params=(("plan_dir", str(self.plan_dir)), *params),
            server=server,
        )

    def recovery(
        self, *, server: workbench_server.WorkbenchHTTPServer | None = None
    ) -> tuple[int, dict, dict]:
        return self.get(
            "/api/conversation-recovery",
            params=(("plan_dir", str(self.plan_dir)),),
            server=server,
        )


__all__ = [name for name in globals() if not name.startswith("__")]
