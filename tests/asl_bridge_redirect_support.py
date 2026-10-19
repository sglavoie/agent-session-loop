"""Urgent corrections over the bridge: who may submit one, and what survives.

The three redirect methods sit on top of subsystems that already have their own
tests -- admission decides what may be submitted, the journal decides what is
durable, and ``redirect_cli`` decides what an operator is told. What is provable
only here is the boundary those three meet at:

- A correction is a mutation, so it is gated on the handshake *and* the action
  token, and a client that fails either gate must leave the journal untouched.
  A refusal that still wrote is worse than no feature: the operator is told the
  correction did not land and the worker gets it anyway.
- The two companion methods are reads, so they answer a client whose contract
  set no longer matches. The client most in need of "where did my correction
  get to" is the one that just lost its own record of it.
- Every redirect frame is body-free. The bridge never calls the one function
  that returns an operator's words, so the proof here is the frame: digest and
  byte count, never the text, in the receipt, the status, the recovery report,
  and the operation event alike.
- The answer survives the bridge that gave it. A second ``BridgeSession`` over
  the same corpus -- a restart, from the client's side -- reports the same
  correction from the journal rather than from the receipt cache the first one
  held.

Every served frame is validated against the canonical corpus schema at its
declared root, so this module also fails when a handler grows a field the
contract does not carry.
"""

from __future__ import annotations

# ruff: noqa: F401
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import agent_mux_contract as contract
from agent_session_loop import (
    agent_mux_projection,
    bridge_projection,
    bridge_protocol,
    bridge_session,
    events,
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
)

from .support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "20260619T140000Z-1"
SESSION_ID = "9"
FEEDBACK = "stop and re-read the session contract"
# Long enough that a substring search for it is meaningful, and distinctive
# enough that finding it anywhere in a frame is unambiguous.
SECOND_FEEDBACK = "the acceptance criteria changed; re-read them before continuing"


def problems(document: Any, root: str) -> list[str]:
    """Every way one served frame disagrees with the canonical schema."""
    schema = contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)
    return sorted(
        violation.reason
        for violation in contract.validate_document(schema, document, root=root)
    )


class _Clock:
    """A hand-advanced monotonic clock.

    The projection is cached for an interval the session derives from it, so
    "the corpus moved under a handshake" is only expressible with a clock a
    test can push past that interval.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RedirectBridgeTestCase(unittest.TestCase):
    """A plans root holding one plan, plus a bridge session over it.

    The snapshot is a real walk of that corpus rather than an invented dict, so
    the action token a test mutates with is the one a client would have been
    shown, and the handshake digest is the one the bridge actually holds.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plans_root = root / "plans"
        self.plans_root.mkdir()
        self.plan_dir = self.plans_root / "plan-alpha"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.snapshots: list[dict[str, Any]] = []
        self.clock = _Clock()

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

    # -- bridge -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return bridge_projection.build_runs_snapshot(
            state_dir=self.state_dir,
            plan_roots=(self.plans_root,),
            include_default_dirs=False,
        )

    def session(self) -> bridge_session.BridgeSession:
        """A bridge session whose projection is a real walk of this corpus.

        Each call builds a *new* session over the same corpus, which is what
        makes "reconnect to another bridge process" expressible: the second
        session shares the journal and shares nothing else.
        """
        self.snapshots.append(self.snapshot())
        return bridge_session.BridgeSession(
            snapshot_builder=lambda: self.snapshots[-1],
            state_dir=self.state_dir,
            clock=self.clock,
        )

    def handshaken(self) -> bridge_session.BridgeSession:
        session = self.session()
        response = self.request(
            session,
            bridge_protocol.METHOD_HANDSHAKE,
            {
                "contract_set_id": agent_mux_projection.CONTRACT_SET_ID,
                "snapshot_digest": self.snapshots[-1]["digest"],
            },
        )
        assert response["result"]["mutations_enabled"], response
        return session

    def request(
        self,
        session: bridge_session.BridgeSession,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: str = "req-1",
    ) -> dict[str, Any]:
        """Round-trip one request through the framing the wire actually uses."""
        frame: dict[str, Any] = {
            "type": "request",
            "protocol": bridge_protocol.PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params
        response = session.handle_frame(
            bridge_protocol.encode_frame(frame).rstrip(b"\n")
        )
        assert response is not None
        return response

    def action_token(self) -> str:
        facts = intervention.inspect_run(self.plan_dir, state_dir=self.state_dir)
        return intervention.action_token(facts)

    def redirect_params(self, **overrides: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": "amx-op-9c31aa",
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        params.update(overrides)
        return params

    def submit(
        self,
        session: bridge_session.BridgeSession | None = None,
        *,
        request_id: str = "req-2",
        **overrides: Any,
    ) -> dict[str, Any]:
        active = self.handshaken() if session is None else session
        return self.request(
            active,
            intervention.ACTION_REDIRECT,
            self.redirect_params(**overrides),
            request_id=request_id,
        )

    def status(
        self,
        session: bridge_session.BridgeSession | None = None,
        *,
        request_id: str = "req-3",
        **params: Any,
    ) -> dict[str, Any]:
        active = self.session() if session is None else session
        return self.request(
            active,
            bridge_protocol.METHOD_REDIRECT_STATUS,
            {"plan_dir": str(self.plan_dir), **params},
            request_id=request_id,
        )

    def recovery(
        self,
        session: bridge_session.BridgeSession | None = None,
        *,
        request_id: str = "req-4",
    ) -> dict[str, Any]:
        active = self.session() if session is None else session
        return self.request(
            active,
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
            {"plan_dir": str(self.plan_dir)},
            request_id=request_id,
        )


__all__ = [
    "json",
    "os",
    "socket",
    "tempfile",
    "threading",
    "time",
    "unittest",
    "Path",
    "Any",
    "contract",
    "agent_mux_projection",
    "bridge_projection",
    "bridge_protocol",
    "bridge_session",
    "events",
    "intervention",
    "redirect_admission",
    "redirect_cli",
    "redirect_recovery",
    "redirect_store_carry",
    "redirect_store_delivery",
    "redirect_store_journal",
    "redirect_store_models",
    "redirect_store_requests",
    "redirect_store_validation",
    "redirect_store_vocab",
    "_write_lock_file",
    "_write_state_payload",
    "live_redirect_endpoint",
    "write_plan",
    "RUN_ID",
    "SESSION_ID",
    "FEEDBACK",
    "SECOND_FEEDBACK",
    "problems",
    "_Clock",
    "RedirectBridgeTestCase",
]
