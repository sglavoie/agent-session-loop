#!/usr/bin/env python3
"""Scripted fake harness: run commands, then hand the turn to ``fake_codex``.

The deterministic tests need a worker whose *check-running* behaviour they
choose — one that leaves the broad gate alone, one that runs it, one that
retries a focused check after a failure. Reimplementing a harness protocol to
get that would be a second thing to keep correct, so this wrapper adds the one
missing capability and delegates everything else to the fixture that already
speaks the protocol.

``ASL_EVAL_FAKE_COMMANDS`` is a JSON list of argv lists, run in order in the
turn's working directory before the delegation. ``ASL_EVAL_FAKE_MARKER`` names
a file that makes the sequence run once per trial rather than once per harness
invocation: a run also spawns the harness for other purposes, and a commit-
message turn is not a worker.

This is a test fixture. It never speaks to a model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FAKE_CODEX = Path(__file__).resolve().parents[1] / "fake_codex.py"

COMMANDS_ENV_VAR = "ASL_EVAL_FAKE_COMMANDS"
MARKER_ENV_VAR = "ASL_EVAL_FAKE_MARKER"
LOG_ENV_VAR = "ASL_EVAL_FAKE_LOG"
CONVERSATION_ENV_VAR = "ASL_EVAL_FAKE_CONVERSATION"
#: Where the scripted commands run. A harness process is not always started in
#: the repository it is pointed at -- the Codex adapter passes the directory on
#: argv instead -- so the working directory is named rather than assumed.
CWD_ENV_VAR = "ASL_EVAL_FAKE_CWD"


def _claimed() -> bool:
    """Claim the one scripted pass, or report that it is already spent."""
    marker = os.environ.get(MARKER_ENV_VAR)
    if not marker:
        return True
    path = Path(marker)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.open("x", encoding="utf-8").close()
    except FileExistsError:
        return False
    return True


def _run_scripted_commands() -> None:
    raw = os.environ.get(COMMANDS_ENV_VAR)
    if not raw:
        return
    commands = json.loads(raw)
    if not isinstance(commands, list):
        raise RuntimeError(f"{COMMANDS_ENV_VAR} must contain a list of argv lists")
    if not _claimed():
        return
    log = os.environ.get(LOG_ENV_VAR)
    # A real harness exports these into every command its worker runs, and the
    # evaluator's attribution reads them. Standing in for the harness means
    # standing in for that too, or the scripted checks would be counted as the
    # runner's.
    env = os.environ.copy()
    env["CLAUDECODE"] = "1"
    env["CLAUDE_CODE_SESSION_ID"] = os.environ.get(
        CONVERSATION_ENV_VAR, "fake-worker-conversation"
    )
    for command in commands:
        if not isinstance(command, list):
            raise RuntimeError(f"{COMMANDS_ENV_VAR} entries must be argv lists")
        argv = [str(token) for token in command]
        cwd = os.environ.get(CWD_ENV_VAR) or os.getcwd()
        completed = subprocess.run(
            argv, check=False, capture_output=True, text=True, env=env, cwd=cwd
        )
        if log:
            with Path(log).open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "argv": argv,
                            "cwd": cwd,
                            "returncode": completed.returncode,
                            "stderr": completed.stderr[-500:],
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )


def main() -> int:
    _run_scripted_commands()
    os.execv(sys.executable, [sys.executable, str(FAKE_CODEX), *sys.argv[1:]])
    return 1  # unreachable: a successful execv never returns


if __name__ == "__main__":
    raise SystemExit(main())
