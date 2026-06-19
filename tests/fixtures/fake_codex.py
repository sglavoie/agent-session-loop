#!/usr/bin/env python3
"""Fake Codex executable used by agent-session-loop integration tests."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import sys
import time


STATUS_PENDING = "- **Status**: pending"
STATUS_COMPLETE = "- **Status**: complete"
HANDOFF_HEADING = "## Handoff"
SESSION_FILE_RE = re.compile(r"^session-([1-9][0-9]*(?:\.[1-9][0-9]*)*)\.md$")


def _arg_value(flag: str) -> str | None:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def _session_id(path: Path) -> str | None:
    match = SESSION_FILE_RE.fullmatch(path.name)
    if match is None:
        return None
    return match.group(1)


def _session_sort_key(path: Path) -> tuple[int, ...]:
    session_id = _session_id(path)
    if session_id is None:
        raise ValueError(f"not a session file: {path.name}")
    return tuple(int(component) for component in session_id.split("."))


def _session_files(plan_dir: Path) -> list[Path]:
    return sorted(
        (path for path in plan_dir.glob("session-*.md") if _session_id(path)),
        key=_session_sort_key,
    )


def _pending_sessions(plan_dir: Path) -> list[Path]:
    return [
        path
        for path in _session_files(plan_dir)
        if _handoff_status_line(path) == STATUS_PENDING
    ]


def _prompt_tokens() -> list[str]:
    for value in reversed(sys.argv[1:]):
        if "session-run" not in value:
            continue
        try:
            return shlex.split(value)
        except ValueError:
            return value.split()
    return []


def _selected_session(plan_dir: Path) -> Path | None:
    tokens = _prompt_tokens()
    for index, token in enumerate(tokens):
        if token != "--session":
            continue
        if index + 1 >= len(tokens):
            return None
        session_id = tokens[index + 1]
        if SESSION_FILE_RE.fullmatch(f"session-{session_id}.md") is None:
            return None
        return plan_dir / f"session-{session_id}.md"

    for token in tokens:
        path = Path(token)
        if _session_id(path) is None:
            continue
        return path if path.is_absolute() else plan_dir / path.name
    return None


def _session_label(path: Path) -> str:
    session_id = _session_id(path)
    if session_id is None:
        raise ValueError(f"not a session file: {path.name}")
    return session_id


def _handoff_status_line(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        for line in lines[index + 1 :]:
            if line.startswith("## "):
                return None
            if line.startswith("- **Status**:"):
                return line
        return None
    return None


def _mark_complete(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        for status_index in range(index + 1, len(lines)):
            if lines[status_index].startswith("## "):
                break
            if lines[status_index] == STATUS_PENDING:
                lines[status_index] = STATUS_COMPLETE
                suffix = "\n" if text.endswith("\n") else ""
                path.write_text("\n".join(lines) + suffix, encoding="utf-8")
                return
        return


def _append_future_note(path: Path, *, source_session_id: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        note = [
            f"## Notes from session-{source_session_id}",
            "",
            "- Generated future-session note from fake harness.",
            "",
        ]
        suffix = "\n" if text.endswith("\n") else ""
        path.write_text(
            "\n".join(lines[:index] + note + lines[index:]) + suffix,
            encoding="utf-8",
        )
        return


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

    if mode == "runner-pause-unsafe":
        _write_last_message(
            "RUNNER_PAUSE: unsafe - plan readiness is not current; "
            "run `asl preflight --mark-loop-ready` before continuing\n"
        )
        print("runner requested unsafe pause")
        return 0

    if mode == "malformed-after-turn":
        (plan_dir / "context.md").unlink(missing_ok=True)
        _write_last_message("Runner status: complete\n")
        print("mutated plan after turn")
        return 0

    if mode == "success":
        selected = _selected_session(plan_dir)
        if (
            selected is not None
            and selected.is_file()
            and _handoff_status_line(selected) == STATUS_PENDING
        ):
            _mark_complete(selected)
        elif pending:
            _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print("completed one session")
        return 0

    if mode == "success-future-note":
        selected = _selected_session(plan_dir)
        if (
            selected is not None
            and selected.is_file()
            and _handoff_status_line(selected) == STATUS_PENDING
        ):
            target = selected
        elif pending:
            target = pending[0]
        else:
            target = None
        if target is not None:
            source_session_id = _session_label(target)
            _mark_complete(target)
            remaining = [
                path
                for path in _pending_sessions(plan_dir)
                if _session_sort_key(path) > _session_sort_key(target)
            ]
            if remaining:
                _append_future_note(
                    remaining[0],
                    source_session_id=source_session_id,
                )
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print("completed one session and added a future note")
        return 0

    if mode == "streaming-success":
        if not pending:
            _write_last_message("Runner status: complete\n")
            print("no pending sessions", flush=True)
            return 0

        session_label = _session_label(pending[0])
        print(f"session {session_label} stdout before completion", flush=True)
        print(
            f"session {session_label} stderr before completion",
            file=sys.stderr,
            flush=True,
        )
        _step_sleep()
        _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print(f"session {session_label} stdout after completion", flush=True)
        print(
            f"session {session_label} stderr after completion",
            file=sys.stderr,
            flush=True,
        )
        return 0

    print(f"unsupported fake codex mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
