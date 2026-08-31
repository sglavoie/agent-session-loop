#!/usr/bin/env python3
"""ASL-owned ``PreToolUse`` hook that injects supervisor guidance into a turn.

Run by the Claude CLI, not by ASL: the CLI executes this script before a tool
call, hands it the hook payload on stdin, and reads a hook result back on
stdout. Returning ``additionalContext`` is how guidance written mid-turn
reaches the model that is already working, and writing a receipt is how ASL
learns it did — a claim no amount of successful pipe writing could support.

Two properties this file exists to hold:

**It is stdlib-only and imports nothing from its own package.** The CLI invokes
it by absolute path under whatever interpreter and working directory it likes,
with no guarantee ``agent_session_loop`` is importable. A relative import here
would turn a delivery route into an ImportError at a tool boundary.

**It never blocks a tool call.** Every failure path exits 0 with no output, so
guidance stays undelivered rather than a turn stalling. A hook that can wedge
the worker is worse than a hook that occasionally injects nothing: the guidance
is still pending, and the next attempt or the next turn will carry it.

The spool contract is the other half of :mod:`agent_session_loop.claude_stream`:
``pending/<id>.json`` is claimable, ``os.replace`` into ``claimed/`` is the
claim, and ``receipts/<id>.json`` is the evidence. The rename is what makes a
guidance record injected at most once even if ASL withdraws it concurrently.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

HOOK_EVENT = "PreToolUse"
GUIDANCE_CONTEXT_HEADING = "Live supervisor guidance (delivered by ASL)"

#: A bound on one hook invocation, not on the run. More guidance than this in
#: the window between two tool calls is vanishingly unlikely; the remainder
#: stays pending and the next tool call takes it.
MAX_RECORDS_PER_INVOCATION = 16


def _claim(pending_dir: Path, claimed_dir: Path, name: str) -> dict[str, Any] | None:
    """Take one pending record, or return ``None`` if it is no longer ours.

    Read before the rename so a withdrawn record is simply lost to us, and
    renamed before anything is emitted so two concurrent hook invocations
    cannot both inject the same guidance.
    """
    source = pending_dir / name
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    try:
        os.replace(source, claimed_dir / name)
    except OSError:
        return None
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    return payload


def _write_receipt(receipts_dir: Path, guidance_id: str) -> None:
    """Record that this guidance is in the context ASL is about to return."""
    staging = receipts_dir / f".{guidance_id}.tmp"
    try:
        staging.write_text(
            json.dumps({"id": guidance_id, "event": HOOK_EVENT}),
            encoding="utf-8",
        )
        os.replace(staging, receipts_dir / f"{guidance_id}.json")
    except OSError:
        pass


def _render(records: list[dict[str, Any]]) -> str:
    lines = [GUIDANCE_CONTEXT_HEADING, ""]
    for record in records:
        text = str(record.get("text", "")).strip()
        if text:
            lines.append(text)
            lines.append("")
    return "\n".join(lines).strip()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    spool = Path(argv[1])
    pending_dir = spool / "pending"
    claimed_dir = spool / "claimed"
    receipts_dir = spool / "receipts"

    # Drain stdin whether or not we look at it: leaving the CLI's write
    # unread is a broken pipe on its side.
    with contextlib.suppress(OSError, ValueError):
        sys.stdin.read()

    try:
        names = sorted(entry.name for entry in pending_dir.iterdir())
    except OSError:
        return 0

    records: list[dict[str, Any]] = []
    for name in names[:MAX_RECORDS_PER_INVOCATION]:
        if not name.endswith(".json"):
            continue
        record = _claim(pending_dir, claimed_dir, name)
        if record is not None:
            records.append(record)
    if not records:
        return 0

    records.sort(key=lambda record: record.get("seq") or 0)
    context = _render(records)
    if not context:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": HOOK_EVENT,
                "additionalContext": context,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()

    # Receipts last, and only once the context is on its way out. The two
    # orderings fail differently and only one of them lies: emitting after a
    # receipt could claim a delivery this hook never made, while a receipt lost
    # after a successful emit leaves the guidance pending and delivered twice.
    for record in records:
        _write_receipt(receipts_dir, str(record["id"]))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 - a hook must never break a tool call
        sys.exit(0)
