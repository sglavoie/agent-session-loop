#!/usr/bin/env python3
"""Fake Codex executable used by agent-session-loop integration tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time


STATUS_PENDING = "- **Status**: pending"
STATUS_COMPLETE = "- **Status**: complete"


def _arg_value(flag: str) -> str | None:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def _session_number(path: Path) -> int:
    return int(path.stem.split("-", 1)[1])


def _session_files(plan_dir: Path) -> list[Path]:
    return sorted(plan_dir.glob("session-*.md"), key=_session_number)


def _pending_sessions(plan_dir: Path) -> list[Path]:
    return [
        path
        for path in _session_files(plan_dir)
        if STATUS_PENDING in path.read_text(encoding="utf-8")
    ]


def _mark_complete(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(STATUS_PENDING, STATUS_COMPLETE, 1), encoding="utf-8")


def _write_last_message(text: str) -> None:
    last_message = _arg_value("-o")
    if last_message is None:
        return
    path = Path(last_message)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _step_sleep() -> None:
    time.sleep(float(os.environ.get("FAKE_CODEX_STEP_SLEEP", "0.1")))


def main() -> int:
    plan_dir_arg = _arg_value("--add-dir")
    if plan_dir_arg is None:
        print("missing --add-dir", file=sys.stderr)
        return 2

    mode = os.environ.get("FAKE_CODEX_MODE", "success")
    plan_dir = Path(plan_dir_arg)

    _write_last_message(f"fake codex mode: {mode}\n")

    if mode == "timeout":
        time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "5")))
        return 0

    if mode == "failure":
        print("fake codex failure", file=sys.stderr)
        _write_last_message("fake codex failure\n")
        return 7

    pending = _pending_sessions(plan_dir)
    if mode == "no-progress":
        _write_last_message("Runner status: complete\n")
        print("left plan unchanged")
        return 0

    if mode == "multiple-progress":
        for path in pending[:2]:
            _mark_complete(path)
        _write_last_message("Runner status: complete\n")
        print("completed multiple sessions")
        return 0

    if mode == "unexpected-progress":
        if len(pending) > 1:
            _mark_complete(pending[1])
        _write_last_message("Runner status: complete\n")
        print("completed unexpected session")
        return 0

    if mode == "runner-pause":
        _write_last_message("RUNNER_PAUSE: operator requested pause\n")
        print("runner requested pause")
        return 0

    if mode == "malformed-after-turn":
        (plan_dir / "context.md").unlink(missing_ok=True)
        _write_last_message("Runner status: complete\n")
        print("mutated plan after turn")
        return 0

    if mode == "success":
        if pending:
            _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print("completed one session")
        return 0

    if mode == "streaming-success":
        if not pending:
            _write_last_message("Runner status: complete\n")
            print("no pending sessions", flush=True)
            return 0

        session_number = _session_number(pending[0])
        print(f"session {session_number} stdout before completion", flush=True)
        print(
            f"session {session_number} stderr before completion",
            file=sys.stderr,
            flush=True,
        )
        _step_sleep()
        _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print(f"session {session_number} stdout after completion", flush=True)
        print(
            f"session {session_number} stderr after completion",
            file=sys.stderr,
            flush=True,
        )
        return 0

    print(f"unsupported fake codex mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
