"""Commit-message and prepare-validator branches of the fake fixture."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from fake_codex_common import (
    COMMIT_MESSAGE,
    COMMIT_PROMPT_PREFIX,
    VALIDATION_PACKET_PREFIX,
    _prompt_values,
)
from fake_codex_review import _write_last_message


def _validation_prompt() -> str | None:
    return next(
        (
            value
            for value in _prompt_values()
            if value.startswith(VALIDATION_PACKET_PREFIX)
        ),
        None,
    )


def _commit_prompt() -> str | None:
    return next(
        (value for value in _prompt_values() if value.startswith(COMMIT_PROMPT_PREFIX)),
        None,
    )


def _append_commit_log(mode: str) -> None:
    """Append one line per commit-message turn to the opt-in JSONL log.

    `asl commit` runs the generator with the repository as its cwd, so `cwd`
    is what identifies which repository of a workspace this turn commits.
    """
    log_path = os.environ.get("FAKE_CODEX_COMMIT_LOG")
    if log_path is None:
        return
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"argv": list(sys.argv), "cwd": str(Path.cwd()), "mode": mode},
                sort_keys=True,
            )
        )
        handle.write("\n")


def _commit_failure_budget_spent() -> bool:
    """Report whether a bounded failure streak has already been served.

    `FAKE_CODEX_COMMIT_FAIL_LIMIT` turns an always-failing commit harness into
    one that fails its first N turns and then succeeds, which is how a test
    exercises a transient failure that heals when the run retries it. The
    commit log doubles as the counter — it already records the mode of every
    turn — so this knob needs `FAKE_CODEX_COMMIT_LOG` and is inert without it.
    """
    limit = os.environ.get("FAKE_CODEX_COMMIT_FAIL_LIMIT")
    log_path = os.environ.get("FAKE_CODEX_COMMIT_LOG")
    if limit is None or log_path is None:
        return False
    path = Path(log_path)
    if not path.exists():
        return False
    served = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if json.loads(line).get("mode") == "failure":
            served += 1
    return served >= int(limit)


def _hang_until_killed() -> int:
    """Stall a commit turn the way a wedged harness does, helper and all.

    A real stall is not one silent process: the harness has already fanned out
    into children that inherit its process group and hold the captured pipes
    open. The grandchild is what makes a test able to tell "the timeout killed
    the process tree" apart from "the timeout killed the one process it
    spawned"; its PID is published so the test can probe it after the kill.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
    )
    pid_path = os.environ.get("FAKE_CODEX_COMMIT_HANG_CHILD_PID")
    if pid_path:
        Path(pid_path).write_text(f"{child.pid}\n", encoding="utf-8")
    time.sleep(float(os.environ.get("FAKE_CODEX_COMMIT_HANG_SECONDS", "600")))
    return 0


def _edit_during_commit_turn() -> None:
    """Change a file while the commit message is being generated.

    A generation turn is the window between capturing the diff and running
    `git commit`, so writing here is how a test reproduces an edit that lands
    after the message evidence was fixed. The path is relative to the
    repository the turn runs in, which is `asl commit`'s own cwd.
    """
    target = os.environ.get("FAKE_CODEX_COMMIT_EDIT_PATH")
    if not target:
        return
    path = Path.cwd() / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        os.environ.get("FAKE_CODEX_COMMIT_EDIT_TEXT", "edited during generation\n"),
        encoding="utf-8",
    )


def _run_commit_message(prompt: str) -> int:
    """Answer an `asl commit` generation turn.

    Like the validation branch, this runs before the `--add-dir` requirement:
    the codex audit argv omits that flag, so a commit turn never carries one.
    """
    _edit_during_commit_turn()
    record_path = os.environ.get("FAKE_CODEX_COMMIT_RECORD")
    if record_path:
        Path(record_path).write_text(
            json.dumps({"argv": list(sys.argv), "prompt": prompt}),
            encoding="utf-8",
        )

    mode = os.environ.get("FAKE_CODEX_COMMIT_MODE", "success")
    if mode == "hang":
        return _hang_until_killed()
    fail_cwd = os.environ.get("FAKE_CODEX_COMMIT_FAIL_CWD_BASENAME")
    if fail_cwd and Path.cwd().name == fail_cwd:
        mode = "failure"
    if mode == "failure" and _commit_failure_budget_spent():
        mode = "success"
    _append_commit_log(mode)
    if mode == "failure":
        print("fake codex commit failure", file=sys.stderr)
        _write_last_message("fake codex commit failure\n")
        return 7
    if mode == "quota":
        message = "usage limit reached; retry-after: 3600"
        print(message, file=sys.stderr)
        _write_last_message(message + "\n")
        return 7
    if mode == "empty":
        _write_last_message("   \n")
        return 0
    if mode == "api-error":
        _write_last_message(
            'API Error: 500 {"type":"error","error":{"message":"overloaded"}}\n'
        )
        return 0

    _write_last_message(os.environ.get("FAKE_CODEX_COMMIT_MESSAGE", COMMIT_MESSAGE))
    return 0


def _validation_plan_dir(prompt: str) -> Path | None:
    """Derive the plan directory from the rendered validation packet.

    The codex audit argv intentionally omits ``--add-dir`` (on codex it grants
    write access), so the packet's shared-context path is the only plan-dir
    signal available on a validator turn.
    """
    marker = "- Shared context: "
    for line in prompt.splitlines():
        if line.startswith(marker):
            return Path(line[len(marker) :].strip()).parent
    return None


def _run_prepare_validator(plan_dir: Path, prompt: str) -> int:
    """Return a real packet-derived validation aggregate for prepare E2E tests."""
    mode = os.environ.get("FAKE_CODEX_VALIDATOR_MODE", "success")
    if mode == "failure":
        print("fake validator failure", file=sys.stderr)
        return 29
    if mode == "malformed":
        _write_last_message("not-json\n")
        return 0

    from agent_session_loop import prepare_models, prepare_packet

    heading = prompt.splitlines()[0]
    _prefix, _separator, raw_ids = heading.partition(" — sessions ")
    session_ids = tuple(token.strip() for token in raw_ids.split(",") if token.strip())
    packet = prepare_packet.build_validation_packet(
        plan_dir,
        session_ids,
        codebase_root=Path.cwd(),
    )
    sessions: list[dict[str, object]] = []
    for session_id, session_path in zip(
        packet.session_ids, packet.session_paths, strict=False
    ):
        title = f"Session {session_id}"
        for line in Path(session_path).read_text(encoding="utf-8").splitlines():
            match = re.match(r"^#+\s+Session\s+[^:]+:\s*(.+)$", line)
            if match:
                title = match.group(1).strip()
                break
        sessions.append(
            {
                "session_id": session_id,
                "title": title,
                "claims": [],
                "issues": [],
                "details": [],
                "patterns": [],
                "dependencies": [],
                "contract_checked": True,
                "contract_problems": [],
                "corrections": [],
            }
        )
    payload: dict[str, object] = {
        "version": packet.version,
        "sessions": sessions,
    }
    if packet.version == prepare_models.PREPARE_VALIDATION_VERSION:
        payload["workspace_root"] = packet.codebase_root
        payload["repositories"] = [
            repository.to_dict() for repository in packet.repositories
        ]
    else:
        payload["codebase_root"] = packet.codebase_root
    _write_last_message(json.dumps(payload, sort_keys=True) + "\n")
    return 0
