#!/usr/bin/env python3
"""Tests that drive the autopilot against a fake asl executable."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout

from asl_autopilot_test_support import (  # noqa: F401
    _counter_clock,
    autopilot,
    setUpModule,
    tearDownModule,
)

_FAKE_ASL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    state_path = os.environ["FAKE_ASL_STATE"]

    with open(state_path) as fh:
        state = json.load(fh)

    argv = sys.argv[1:]
    sub = argv[0] if argv else ""

    if sub == "status":
        payload = {
            "ok": True,
            "counts": {"pending": state["pending"], "complete": state["complete"]},
            "next_pending_id": (
                None if state["pending"] == 0 else str(state["complete"] + 1)
            ),
            "lock": {"present": False, "active": False, "stale": False},
            "sessions": [],
            "plan_dir": "/tmp/plan",
        }
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    if sub == "run":
        # Model the real runner: drain every pending session in one process
        # unless `--max-sessions N` caps it, in which case take at most N and
        # report the runner's own `max-sessions` stop.
        max_sessions = None
        for i, tok in enumerate(argv):
            if tok == "--max-sessions" and i + 1 < len(argv):
                max_sessions = int(argv[i + 1])
        took = (
            state["pending"]
            if max_sessions is None
            else min(max_sessions, state["pending"])
        )
        # A queued `stop-after-current` request: the runner drains it after the
        # current session, so it takes exactly one session and then exits ok
        # with `reason: "stopped"`. Draining consumes the request (one-shot),
        # and a stop with nothing left pending is a no-op that reports the
        # ordinary all-complete exit.
        stop_after_current = bool(state.get("stop_after_current"))
        if stop_after_current:
            took = min(took, 1)
            state["stop_after_current"] = False
        # A queued `abort` request kills the turn in flight and reverts its
        # session to pending, so the chunk completes nothing at all and exits
        # `stopped` with the terminal abort action in its control block.
        abort_turn = bool(state.get("abort_turn"))
        if abort_turn:
            took = 0
            state["abort_turn"] = False
        state["pending"] -= took
        state["complete"] += took
        state["chunks_run"] = state.get("chunks_run", 0) + 1
        with open(state_path, "w") as fh:
            json.dump(state, fh)
        if abort_turn:
            payload = {
                "ok": True,
                "paused": False,
                "reason": "stopped",
                "control": {"last_action": "abort-turn-honored"},
            }
        elif state["pending"] == 0:
            payload = {"ok": True, "paused": False, "reason": "all-complete"}
        elif stop_after_current:
            payload = {"ok": True, "paused": False, "reason": "stopped"}
        else:
            payload = {"ok": True, "paused": False, "reason": "max-sessions"}
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    sys.stderr.write("unknown subcommand\\n")
    sys.exit(2)
    """
)


class SuperviseRunFakeAslTest(unittest.TestCase):
    """Exercise the real subprocess plumbing against a fake `asl` executable."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asl = os.path.join(self.tmp, "asl")
        with open(self.asl, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ASL)
        os.chmod(self.asl, os.stat(self.asl).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.state_path = os.path.join(self.tmp, "state.json")

    def _write_state(self, **state) -> None:
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def _read_state(self) -> dict:
        with open(self.state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _parsed(self, extra: str = "") -> dict:
        command = (
            f"FAKE_ASL_STATE={self.state_path} {self.asl} run /tmp/plan {extra}".strip()
        )
        parsed = autopilot.parse_command(command)
        self.assertTrue(parsed["ok"], parsed)
        return parsed

    def _supervise(self, *, command_extra: str = "", **kwargs):
        return autopilot.supervise_run(
            self._parsed(command_extra),
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def test_starts_and_completes_single_chunk(self) -> None:
        self._write_state(pending=1, complete=0, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(self._read_state()["chunks_run"], 1)

    def test_already_complete_never_runs(self) -> None:
        self._write_state(pending=0, complete=2, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(self._read_state()["chunks_run"], 0)

    def test_uncapped_drains_all_pending_in_one_chunk(self) -> None:
        # Without `--max-sessions`, the runner completes every pending session in
        # a single process — the supervisor does not chunk through them.
        self._write_state(pending=3, complete=0, chunks_run=0)
        result = self._supervise()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(self._read_state()["chunks_run"], 1)
        self.assertEqual(self._read_state()["complete"], 3)

    def test_max_sessions_is_a_global_budget(self) -> None:
        # End-to-end through parse -> argv -> child: `--max-sessions 2` on a
        # 5-session plan completes exactly 2 and stops cleanly with the remaining
        # sessions still pending — a single chunk, no relaunching.
        self._write_state(pending=5, complete=0, chunks_run=0)
        result = self._supervise(command_extra="--max-sessions 2")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "max-sessions")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["pending"], 3)
        state = self._read_state()
        self.assertEqual(state["complete"], 2)
        self.assertEqual(state["pending"], 3)
        self.assertEqual(state["chunks_run"], 1)

    def test_budget_stop_is_a_resumable_checkpoint(self) -> None:
        # A budget stop leaves a clean checkpoint: re-running the same command
        # continues from the remaining sessions rather than a dead end.
        self._write_state(pending=5, complete=0, chunks_run=0)
        first = self._supervise(command_extra="--max-sessions 2")
        self.assertEqual(first["reason"], "max-sessions")
        self.assertEqual(self._read_state()["complete"], 2)

        second = self._supervise(command_extra="--max-sessions 2")
        self.assertEqual(second["reason"], "max-sessions")
        self.assertEqual(second["completed"], 2)
        self.assertEqual(self._read_state()["complete"], 4)
        self.assertEqual(self._read_state()["pending"], 1)

    def test_honored_stop_after_current_is_a_resumable_checkpoint(self) -> None:
        # End-to-end: a queued `asl control stop-after-current` makes the child
        # finish one session and exit ok with `reason: "stopped"`. The supervisor
        # must stop there — a single chunk, no relaunch — and re-running the same
        # command continues from the remaining sessions.
        self._write_state(pending=4, complete=0, chunks_run=0, stop_after_current=True)
        first = self._supervise()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["reason"], "stopped")
        self.assertEqual(first["chunks"], 1)
        self.assertEqual(first["completed"], 1)
        self.assertEqual(first["pending"], 3)
        state = self._read_state()
        self.assertEqual(state["complete"], 1)
        self.assertEqual(state["chunks_run"], 1)

        second = self._supervise()
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["reason"], "complete")
        self.assertEqual(self._read_state()["pending"], 0)

    def test_honored_abort_is_a_resumable_checkpoint(self) -> None:
        # End-to-end: a queued `asl control abort` makes the child end its turn
        # without completing anything and exit ok with `reason: "stopped"`. Zero
        # progress is the expected shape of an abort, so the supervisor must stop
        # there rather than relaunching, and re-running the same command picks
        # every session back up.
        self._write_state(pending=4, complete=0, chunks_run=0, abort_turn=True)
        first = self._supervise()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["reason"], "stopped")
        self.assertEqual(first["stop_kind"], autopilot.STOP_KIND_ABORT)
        self.assertEqual(first["chunks"], 1)
        self.assertEqual(first["completed"], 0)
        self.assertEqual(first["pending"], 4)
        self.assertEqual(self._read_state()["chunks_run"], 1)

        second = self._supervise()
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["reason"], "complete")
        self.assertEqual(self._read_state()["pending"], 0)

    def test_heartbeats_stay_off_stdout(self) -> None:
        self._write_state(pending=2, complete=0, chunks_run=0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = autopilot.supervise_run(
                self._parsed(),
                clock=_counter_clock(),
                sleep=lambda *_: None,
            )
        self.assertTrue(result["ok"], result)
        # stdout is reserved for the final JSON payload; heartbeats go to stderr.
        self.assertEqual(out.getvalue(), "")
        self.assertIn("started `asl run`", err.getvalue())


_FAKE_ASL_REPLY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import os
    import sys

    capture = os.environ["FAKE_ASL_CAPTURE"]
    argv = sys.argv[1:]
    sub = argv[0] if argv else ""

    if sub == "status":
        done = os.path.exists(capture)
        payload = {
            "ok": True,
            "counts": {"pending": 0 if done else 1, "complete": 1 if done else 0},
            "next_pending_id": None if done else "4",
            "lock": {"present": False, "active": False, "stale": False},
            "sessions": [],
            "plan_dir": "/tmp/plan",
        }
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    if sub == "run":
        reply = sys.stdin.read() if "--reply-stdin" in argv else ""
        with open(capture, "w") as fh:
            json.dump({"argv": argv, "stdin": reply}, fh)
        sys.stdout.write(
            json.dumps({"ok": True, "paused": False, "reason": "all-complete"})
        )
        sys.exit(0)

    sys.stderr.write("unknown subcommand\\n")
    sys.exit(2)
    """
)


class ReplyResumeFakeAslTest(unittest.TestCase):
    """End-to-end resume: reply streams on stdin, never into argv."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-reply-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asl = os.path.join(self.tmp, "asl")
        with open(self.asl, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ASL_REPLY)
        os.chmod(self.asl, os.stat(self.asl).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.capture_path = os.path.join(self.tmp, "capture.json")

    def _read_capture(self) -> dict:
        with open(self.capture_path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_reply_reaches_stdin_and_never_leaks_into_argv(self) -> None:
        reply = "reuse the cached parser output"
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(
            [
                "--command",
                f"FAKE_ASL_CAPTURE={self.capture_path} {self.asl} run /tmp/plan",
                "--reply-stdin",
            ]
        )
        err = io.StringIO()
        with redirect_stderr(err):
            result = autopilot.run_autopilot(args, stdin=io.StringIO(reply))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")

        capture = self._read_capture()
        # The reply is delivered on stdin verbatim...
        self.assertEqual(capture["stdin"], reply)
        # ...and `--reply-stdin` is the flag that carried it...
        self.assertIn("--reply-stdin", capture["argv"])
        # ...but the reply text itself must never appear in argv.
        self.assertNotIn(reply, capture["argv"])
        self.assertFalse(any(reply in token for token in capture["argv"]))


_FAKE_ASL_QUESTIONS = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import sys

    argv = sys.argv[1:]
    sub = argv[0] if argv else ""

    if sub == "questions":
        payload = {
            "ok": True,
            "command": "questions",
            "session_id": "4",
            "next_pending_id": "4",
            "pause_reason": "input-error",
            "rendered_prompt": "Resolve the paused question and reply.",
            "last_message": "Should I cache the parser output?",
            "artifact_paths": {"last_message_path": "/tmp/last.txt"},
        }
        sys.stdout.write(json.dumps(payload))
        sys.exit(0)

    sys.stderr.write("unknown subcommand\\n")
    sys.exit(2)
    """
)


class InvokeQuestionsFakeAslTest(unittest.TestCase):
    """`asl questions --json` transport is parsed into the question payload."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-q-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.asl = os.path.join(self.tmp, "asl")
        with open(self.asl, "w", encoding="utf-8") as fh:
            fh.write(_FAKE_ASL_QUESTIONS)
        os.chmod(self.asl, os.stat(self.asl).st_mode | stat.S_IEXEC | stat.S_IXGRP)

    def test_questions_payload_is_returned(self) -> None:
        parsed = autopilot.parse_command(f"{self.asl} run /tmp/plan")
        self.assertTrue(parsed["ok"], parsed)
        payload = autopilot._invoke_questions(parsed)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["session_id"], "4")
        self.assertEqual(payload["last_message"], "Should I cache the parser output?")


if __name__ == "__main__":
    unittest.main()
