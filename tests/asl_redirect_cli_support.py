"""The operator surface: what an operator is told, and what they do next.

The subsystem underneath is already proven by its own tests. What is provable
only here is the part an operator actually sees -- that a correction which was
persisted comes back with an id even when the news is bad, that a refusal names
the refusal the journal would name, that "not acknowledged yet" is never dressed
up as a failure worth resubmitting over, and that an unresumable conversation is
answered with a relaunch rather than with a request to answer a question nobody
asked.

Exit codes are asserted everywhere because they are the interface for the
callers that read nothing else: 2 means the request is settled and retrying
later is safe, 3 means stop and reconcile by id.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import cli as asl_cli
from agent_session_loop import (
    clock,
    events,
    redirect_ack,
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
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    live_redirect_endpoint,
    write_plan,
)

RUN_ID = "run-20260910-000000"
SESSION_ID = "session-9"
FEEDBACK = "stop and re-read the session contract"


class RedirectCliTestCase(unittest.TestCase):
    """A plan whose run state, lock, and journal are set per test."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.parser = asl_cli.build_parser(prog="asl")

    # -- fixtures ---------------------------------------------------------

    def write_state(
        self,
        *,
        run_id: str | None = RUN_ID,
        turn_index: int = 1,
        paused: bool = False,
        pause_reason: str | None = None,
        active_turn: dict | None = None,
    ) -> None:
        if active_turn is None and run_id is not None:
            active_turn = {
                "run_id": run_id,
                "session_id": SESSION_ID,
                "turn_index": turn_index,
                "session_kind": "implementation",
                "harness": "claude",
            }
        payload: dict = {
            "version": 3,
            "plan_dir": str(self.plan_dir),
            "paused": paused,
        }
        if pause_reason is not None:
            payload["reason"] = pause_reason
        if run_id is not None:
            payload["run_id"] = run_id
        if active_turn is not None:
            payload["active_turn"] = active_turn
        _write_state_payload(self.plan_dir, self.state_dir, payload)

    def write_lock(self, **kwargs) -> None:
        params = {"pid": os.getpid(), "hostname": socket.gethostname()}
        params.update(kwargs)
        _write_lock_file(self.plan_dir, self.state_dir, **params)

    def make_live_run(self, **state_kwargs) -> Path:
        """A run that is live, has an active turn, and serves urgent redirects."""
        self.write_state(**state_kwargs)
        endpoint, self.wakeup = self.enterContext(
            live_redirect_endpoint(self.plan_dir, self.state_dir)
        )
        self.write_lock(redirect_endpoint=endpoint)
        return endpoint

    def make_unservable_run(self, **state_kwargs) -> None:
        """A live run whose lock names a redirect endpoint nothing is serving."""
        self.write_state(**state_kwargs)
        self.write_lock(redirect_endpoint=self.state_dir / "nothing-here.fifo")

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

    # -- invocation -------------------------------------------------------

    def run_cli(
        self, argv: list[str], stdin: str | None = None
    ) -> tuple[int, str, str]:
        args = self.parser.parse_args(
            [*argv, "--state-dir", str(self.state_dir)],
        )
        handler = asl_cli.resolve_handler(args)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            if stdin is None:
                code = handler(args)
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = handler(args)
        return code, out.getvalue(), err.getvalue()

    def run_json(self, argv: list[str], stdin: str | None = None) -> tuple[int, dict]:
        code, out, err = self.run_cli([*argv, "--json"], stdin=stdin)
        self.assertEqual("", err, err)
        return code, json.loads(out)

    def facts(self, payload: dict) -> dict:
        """The fields a command reported, whichever envelope carried them.

        A success payload spreads them at the top level; the shared error
        envelope nests them under ``details``. That split belongs to the
        envelope, not to the command, so the tests read through it.
        """
        return payload["details"] if payload.get("ok") is False else payload

    def redirect(self, *extra: str, stdin: str | None = None) -> tuple[int, dict]:
        return self.run_json(
            ["control", "redirect", str(self.plan_dir), *extra], stdin=stdin
        )

    def status(self, *extra: str) -> tuple[int, dict]:
        return self.run_json(["control", "redirect-status", str(self.plan_dir), *extra])

    def recover(self, *extra: str) -> tuple[int, dict]:
        return self.run_json(
            ["control", "recover-conversation", str(self.plan_dir), *extra]
        )

    def accept_when_pending(
        self, target: redirect_store_models.RedirectTarget
    ) -> threading.Thread:
        """Play the runner: accept the first correction that appears.

        The submit wait polls the journal, so acceptance has to arrive *during*
        the wait rather than before it -- the request does not exist until the
        command runs. A bounded background poller is the honest way to stage
        that: the real acceptance record, written by the real writer, while the
        real wait is running.
        """

        def accept() -> None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                pending = redirect_store_journal.read_journal(
                    self.plan_dir, state_dir=self.state_dir
                ).pending()
                if pending:
                    redirect_store_requests.accept_request(
                        self.plan_dir,
                        pending[0].operation_id,
                        target=target,
                        state_dir=self.state_dir,
                    )
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)
        return thread


__all__ = ["io", "json", "os", "socket", "tempfile", "threading", "time", "unittest", "redirect_stderr", "redirect_stdout", "Path", "mock", "asl_cli", "clock", "events", "redirect_ack", "redirect_admission", "redirect_cli", "redirect_recovery", "redirect_store_carry", "redirect_store_delivery", "redirect_store_journal", "redirect_store_models", "redirect_store_requests", "redirect_store_validation", "redirect_store_vocab", "_write_lock_file", "_write_state_payload", "live_redirect_endpoint", "write_plan", "RUN_ID", "SESSION_ID", "FEEDBACK", "RedirectCliTestCase"]  # fmt: skip
