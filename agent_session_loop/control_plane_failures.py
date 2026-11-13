"""Worker-side control-plane command failures, reported back to the runner.

A worker talks to ASL through a few commands of its own (``asl inbox read``,
``asl inbox acknowledge``, ``asl ask``, ``asl notify``). When one of them
fails, the worker sees it in its shell and the runner sees nothing: the turn
still exits cleanly and is accepted as if every guidance read had happened
(ASI-169, where the final ``inbox read`` died on a ``NameError``).

The runner closes that gap by exporting :data:`ENV_VAR` into each worker turn,
naming a file in the turn's own artifact directory. ``cli.main`` appends one
record there when a watched command crashes, or when ``inbox read`` exits
nonzero, and turn finalization reads the file back into the turn's
``control_plane_degradation``. A record carries the same content-free fields
as the CLI trace -- the command, its outcome and exit code, the exception's
class and ASL source site -- never argv, paths, or messages.

Every step is fail-open and best effort. A worker whose sandbox cannot write
the run directory, or that strips the variable, simply leaves no record; the
turn's logs and the CLI trace are still the evidence then.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import cli_trace

ENV_VAR = "ASL_CONTROL_PLANE_FAILURE_LOG"
FILE_NAME = "control-plane-failures.jsonl"
MARKER = "worker-control-command-failed"

#: Worker-facing commands, by the ``handler_ref`` the parser binds them to.
#: Any of them crashing is recorded.
WATCHED_COMMANDS: Mapping[str, str] = {
    "mailbox_cli_inbox:inbox_read_from_args": "inbox read",
    "mailbox_cli_inbox:inbox_acknowledge_from_args": "inbox acknowledge",
    "mailbox_cli_ask:ask_from_args": "ask",
    "mailbox_cli_notify:notify_from_args": "notify",
}

#: Commands whose ordinary nonzero exit is itself a failed read. An
#: acknowledgement or question can be refused for reasons the worker is meant
#: to act on, so only a crash counts for those.
NONZERO_IS_FAILURE = frozenset({"inbox read"})

#: How many records a turn's degradation keeps; the count stays exact.
RECORD_LIMIT = 10
_READ_MAX_BYTES = 64 * 1024


def record(
    handler_ref: str | None,
    *,
    outcome: str,
    exit_code: int | None,
    exception: BaseException | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Append this process's failure to the turn's log, when it is one.

    Never raises: the command's own exit status is what the worker acts on,
    and this report must not be able to change it.
    """
    with suppress(Exception):
        command = WATCHED_COMMANDS.get(handler_ref or "")
        if command is None:
            return
        failed = outcome == cli_trace.OUTCOME_EXCEPTION or (
            command in NONZERO_IS_FAILURE and outcome == cli_trace.OUTCOME_FAILED
        )
        if not failed:
            return
        target = (os.environ if env is None else env).get(ENV_VAR, "").strip()
        if not target:
            return
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "command": command,
            "outcome": outcome,
            "exit_code": exit_code,
            "exception_type": (None if exception is None else type(exception).__name__),
            "exception_site": cli_trace.exception_site(exception),
        }
        blob = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        # No O_CREAT of the directory: a turn directory that is gone belongs to
        # a run that is gone, and recreating it would leave a stray turn behind.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, blob.encode("utf-8"))
        finally:
            os.close(fd)


def read_records(path: Path) -> list[dict[str, Any]]:
    """The well-formed records at ``path``; an absent or unreadable file is none."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(_READ_MAX_BYTES)
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        with suppress(ValueError):
            parsed = json.loads(line)
            if isinstance(parsed, dict) and isinstance(parsed.get("command"), str):
                records.append(parsed)
    return records


def turn_degradation(artifact_dir: Path) -> dict[str, Any] | None:
    """The turn's ``control_plane_degradation`` payload, or None when clean."""
    records = read_records(artifact_dir / FILE_NAME)
    if not records:
        return None
    return {
        "marker": MARKER,
        "failure_count": len(records),
        "commands": sorted({entry["command"] for entry in records}),
        "failures": records[-RECORD_LIMIT:],
        "log_path": str(artifact_dir / FILE_NAME),
    }
