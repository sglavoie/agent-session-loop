#!/usr/bin/env python3
"""Fake Codex executable used by agent-session-loop integration tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

#: The protocol's own channel, kept before any turn redirects ``sys.stdout``.
_REAL_STDOUT = sys.stdout

#: Prompts delivered over ACP's ``session/prompt``. The one-shot form states
#: the prompt on argv, so the readers below look in both places rather than
#: making every branch care which transport ran the turn.
_ACP_PROMPTS: list[str] = []

STATUS_PENDING = "- **Status**: pending"
STATUS_COMPLETE = "- **Status**: complete"
HANDOFF_HEADING = "## Handoff"
SESSION_FILE_RE = re.compile(r"^session-([1-9][0-9]*(?:\.[1-9][0-9]*)*)\.md$")
VALIDATION_PACKET_PREFIX = "# ASL preparation validation packet"
COMMIT_PROMPT_PREFIX = "You are a commit message generator."
COMMIT_MESSAGE = (
    "feat: add the fake codex commit branch\n"
    "\n"
    "Answer an `asl commit` generation turn with a stable conventional-commits "
    "message so handler tests assert on content rather than on a live model.\n"
)


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


def _prompt_values() -> list[str]:
    return [*sys.argv[1:], *_ACP_PROMPTS]


def _prompt_contains_reply_context() -> bool:
    return any("## ASL reply context" in value for value in _prompt_values())


def _prompt_contains_resolution_context() -> bool:
    return any("## ASL resolution context" in value for value in _prompt_values())


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
    session_label = _session_label(path)
    handoff_files_json = os.environ.get("FAKE_CODEX_HANDOFF_FILES_JSON")
    handoff_files: list[str] = []
    if handoff_files_json:
        payload = json.loads(handoff_files_json)
        if not isinstance(payload, dict):
            raise RuntimeError("FAKE_CODEX_HANDOFF_FILES_JSON must contain an object")
        raw_files = payload.get(session_label, [])
        if not isinstance(raw_files, list) or not all(
            isinstance(value, str) for value in raw_files
        ):
            raise RuntimeError("fake Codex handoff file entries must be string lists")
        handoff_files = raw_files
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        for status_index in range(index + 1, len(lines)):
            if lines[status_index].startswith("## "):
                break
            if lines[status_index] == STATUS_PENDING:
                lines[status_index] = STATUS_COMPLETE
                for handoff_index in range(status_index + 1, len(lines)):
                    if lines[handoff_index].startswith("## "):
                        break
                    if lines[handoff_index] == "- **Files modified**: (none yet)":
                        rendered_files = ", ".join(handoff_files) or "(none)"
                        lines[handoff_index] = f"- **Files modified**: {rendered_files}"
                    elif lines[handoff_index] == "- **Key decisions**: (none yet)":
                        lines[handoff_index] = (
                            "- **Key decisions**: Fake harness applied "
                            "deterministic fixture work."
                        )
                suffix = "\n" if text.endswith("\n") else ""
                path.write_text("\n".join(lines) + suffix, encoding="utf-8")
                return
        return


def _apply_workspace_mutation() -> None:
    """Apply an opt-in repository mutation for runner guard integration tests."""
    mutations_json = os.environ.get("FAKE_CODEX_MUTATIONS_JSON")
    if mutations_json:
        mutations = json.loads(mutations_json)
        if not isinstance(mutations, list):
            raise RuntimeError("FAKE_CODEX_MUTATIONS_JSON must contain a list")
        for mutation in mutations:
            if not isinstance(mutation, dict) or not isinstance(
                mutation.get("path"), str
            ):
                raise RuntimeError("fake Codex mutation entries need a path")
            _apply_workspace_mutation_record(mutation)
        return
    target_value = os.environ.get("FAKE_CODEX_MUTATION_PATH")
    if not target_value:
        return
    _apply_workspace_mutation_record(
        {
            "path": target_value,
            "action": os.environ.get("FAKE_CODEX_MUTATION_ACTION", "write"),
            "text": os.environ.get("FAKE_CODEX_MUTATION_TEXT", "mutated\n"),
            "destination": os.environ.get("FAKE_CODEX_MUTATION_DESTINATION"),
        }
    )


def _apply_workspace_mutation_record(mutation: dict[str, object]) -> None:
    target = Path(str(mutation["path"]))
    action = str(mutation.get("action", "write"))
    if action == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            str(mutation.get("text", "mutated\n")),
            encoding="utf-8",
        )
        return
    if action == "append":
        # Every turn leaves a distinct change, which is what a per-session
        # commit mode needs: an overwrite with identical bytes would leave the
        # second session's repository clean and its commit with nothing to do.
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(str(mutation.get("text", "appended\n")))
        return
    if action == "delete":
        target.unlink()
        return
    if action == "rename":
        destination = mutation.get("destination")
        if not destination:
            raise RuntimeError("rename mutation needs a destination")
        target.rename(Path(str(destination)))
        return
    if action == "stage":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            str(mutation.get("text", "staged mutation\n")),
            encoding="utf-8",
        )
        repository = subprocess.run(
            ["git", "-C", str(target.parent), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", repository, "add", str(target)],
            capture_output=True,
            check=True,
        )
        return
    if action == "git-init":
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        return
    if action == "commit-head":
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "commit",
                "--allow-empty",
                "-m",
                "harness commit",
            ],
            capture_output=True,
            check=True,
        )
        return
    raise RuntimeError(f"unsupported workspace mutation action: {action}")


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


def _tamper_with_named_session(plan_dir: Path) -> None:
    """Append a note into a named packet, whatever the scripted mode does next.

    Applied before the mode branches so a turn that never returns normally —
    a timeout, a structured pause — still leaves the tampering behind.
    """
    target_name = os.environ.get("FAKE_CODEX_TAMPER_SESSION")
    if not target_name:
        return
    _append_future_note(
        plan_dir / target_name,
        source_session_id=_session_label(_session_files(plan_dir)[0]),
    )
