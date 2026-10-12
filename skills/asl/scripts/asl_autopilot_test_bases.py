"""Shared test base classes for the split asl autopilot test modules."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime

from asl_autopilot_test_support import _counter_clock, autopilot


class SuperviseRunInjectedBase(unittest.TestCase):
    def _supervise(
        self, status_invoker, run_invoker, *, parsed=None, clock=None, **kwargs
    ):
        return autopilot.supervise_run(
            parsed
            or {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=clock or _counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def _seed_run_config(self, run_config):
        """Write a persisted ``state.json`` and return its path.

        Mirrors the on-disk shape the runner persists so a ``--resume``
        supervised command can resolve the effective review harness/cap from it.
        """
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_path = os.path.join(tmp, "state.json")
        with open(state_path, "w", encoding="utf-8") as handle:
            json.dump({"run_config": run_config}, handle)
        return state_path

    def _resume_parsed(self, state_path):
        """Parsed model for a resume-only command (no review harness/cap)."""
        return {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--resume", "--json"],
            "review_harness": None,
            "max_review_fix_cycles": autopilot.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            "max_review_fix_cycles_explicit": False,
            "resume": True,
        }

    def _budget_parsed(self, max_sessions):
        return {
            "plan_dir": "/tmp/plan",
            "argv": [
                "asl",
                "run",
                "/tmp/plan",
                "--max-sessions",
                str(max_sessions),
                "--json",
            ],
            "max_sessions": max_sessions,
        }

    def _launch_before(self, moment: str) -> None:
        """Pin the launch instant so scripted runs start after this chunk did."""
        original = autopilot._utc_now
        autopilot._utc_now = lambda: datetime.fromisoformat(moment)
        self.addCleanup(setattr, autopilot, "_utc_now", original)


class MailboxWatcherBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-mailbox-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan_dir = os.path.join(self.tmp, "plan")
        self.state_root = os.path.join(self.tmp, "state")
        self.state_path = os.path.join(self.state_root, "plan-hash")
        os.makedirs(self.plan_dir)
        self.mailbox_path = os.path.join(self.state_path, "mailbox.jsonl")
        self.lease_path = os.path.join(self.state_path, "mailbox-lease.json")

    def _status(self, *, last_seq=1):
        return {
            "ok": True,
            "plan_dir": self.plan_dir,
            "state_dir": self.state_path,
            "mailbox": {
                "path": self.mailbox_path,
                "lease_path": self.lease_path,
                "last_seq": last_seq,
            },
        }

    def _question(self, *, seq=2, text="Need a decision", run_id=None):
        return autopilot._mailbox.MailboxMessage(
            seq=seq,
            id="00000000-0000-0000-0000-000000000011",
            kind=autopilot._mailbox.KIND_QUESTION,
            created_at="2026-07-24T19:00:00+00:00",
            text=text,
            run_id=run_id,
        )

    def _notice(
        self,
        *,
        seq=2,
        text="Informational update",
        run_id="run-current",
    ):
        return autopilot._mailbox.MailboxMessage(
            seq=seq,
            id=f"00000000-0000-0000-0000-{seq:012d}",
            kind=autopilot._mailbox.KIND_NOTICE,
            created_at="2026-07-24T19:00:00+00:00",
            text=text,
            run_id=run_id,
            session_id="6",
            turn_index=1,
        )


class SupervisionEndBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-supervision-end-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan_dir, self.state_dir, self.command = self._plan("plan")

    def _plan(self, name: str) -> tuple:
        plan_dir = os.path.join(self.tmp, name)
        os.makedirs(plan_dir, exist_ok=True)
        state_dir = os.path.join(self.tmp, f"{name}-state")
        return plan_dir, state_dir, f"asl run {plan_dir} --state-dir {state_dir}"

    def _patch(self, owner, name: str, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _supervision_ends(self, plan_dir: str, state_dir: str) -> list:
        view = autopilot._mailbox.read_mailbox(plan_dir, state_dir=state_dir)
        self.assertEqual((), view.rejected)
        return [
            message
            for message in view.messages
            if message.kind == autopilot._mailbox.KIND_SUPERVISION_END
        ]

    def _run_main(self, command: str, supervision, *extra_argv: str) -> tuple:
        self._patch(autopilot, "run_autopilot", supervision)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(["--command", command, "--json", *extra_argv])
        return exit_code, buffer.getvalue()

    def _complete_result(self, command: str) -> dict:
        return {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "command": autopilot.parse_command(command),
        }

    def _counting_signal(self, *, call: int, fire):
        """Patch ``signal.signal`` to fire ``fire`` on its ``call``-th use.

        ``_termination_handlers`` uses it four times: two installs, then two
        restores in reverse order (SIGINT, then SIGTERM). Numbering the calls
        addresses each seam between them.
        """
        real_signal = signal.signal
        seen = {"count": 0}

        def counting_signal(number, handler):
            seen["count"] += 1
            if seen["count"] == call:
                return fire(lambda: real_signal(number, handler))
            return real_signal(number, handler)

        self.addCleanup(self._restore_signal_state, real_signal)
        self._patch(autopilot.signal, "signal", counting_signal)
        return seen

    def _restore_signal_state(self, real_signal) -> None:
        # A seam test can leave a handler installed or a signal blocked; put
        # the interpreter back before the next test runs.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, autopilot.TERMINATION_SIGNALS)
        for number in autopilot.TERMINATION_SIGNALS:
            current = signal.getsignal(number)
            if getattr(current, "__qualname__", "").startswith("_termination_handlers"):
                real_signal(number, signal.SIG_DFL)

    def _assert_terminal_contract(self, records: list, stdout: str) -> None:
        """Assert the contract a woken supervisor decides from.

        One terminal record per helper process, always. A non-``error``
        outcome additionally promises the complete result on stdout; ``error``
        promises only a self-describing record, because the helper can die
        before, during, or just after that write.
        """
        self.assertEqual(1, len(records))
        outcome = records[0].supervision_outcome
        self.assertIn(outcome, autopilot._mailbox.SUPERVISION_OUTCOMES)
        if outcome == autopilot._mailbox.SUPERVISION_OUTCOME_ERROR:
            self.assertRegex(records[0].text, r"^supervision ended: error - \S+$")
            return
        self.assertTrue(stdout.endswith("\n"))
        self.assertIsInstance(json.loads(stdout), dict)


class RunAutopilotReviewBase(unittest.TestCase):
    def _args(self, command="asl run /tmp/plan --review-harness codex"):
        parser = autopilot.build_arg_parser()
        return parser.parse_args(["--command", command])

    def _complete_supervisor(self, parsed):
        return {
            "ok": True,
            "stage": autopilot.RUN_STAGE,
            "reason": "complete",
            "chunks": 1,
        }
