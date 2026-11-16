"""A desktop notification when a run ends and someone should know.

The pauses that need a person are the ones that wait longest: nothing tells
the person. This posts one macOS notification as ``asl run`` exits, for the
outcomes worth interrupting someone for, and stays silent for the rest so the
notification keeps meaning something:

* the plan finished, after a run long enough that nobody was watching it;
* the run paused and only a person can clear it;
* a directly launched run stopped for any other reason after that same while.

A recoverable pause under a supervisor is not announced: the supervisor
retries it. Nothing is posted off macOS or when ``ASL_DESKTOP_NOTIFY`` is
``0``, which is also how someone whose Agent Mux cockpit already announces
these turns the duplicate off. A notification is a courtesy and never a
precondition: every failure here is swallowed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import pause_policy, plans_files

ENV_VAR = "ASL_DESKTOP_NOTIFY"
DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

#: A run shorter than this ended while its launcher was still looking at it.
UNATTENDED_AFTER_SECONDS = 30.0

_BODY_LIMIT = 180

#: ``display notification`` with both strings passed as arguments, so no part
#: of a plan title or pause message is ever parsed as AppleScript.
_SCRIPT = (
    "on run argv",
    "display notification (item 1 of argv) with title (item 2 of argv)",
    "end run",
)


def enabled(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(ENV_VAR, "").strip().lower() not in DISABLED_VALUES


def _requires_user(payload: Mapping[str, Any]) -> bool:
    policy = payload.get("pause_policy")
    if not isinstance(policy, Mapping):
        details = payload.get("pause_details")
        policy = pause_policy.classify_pause(
            payload.get("pause_reason"),
            details if isinstance(details, Mapping) else None,
        )
    return bool(policy.get("requires_user"))


def message_for(
    payload: Mapping[str, Any], *, supervised: bool
) -> tuple[str, str] | None:
    """The ``(title, body)`` to post for a run's terminal payload, or ``None``.

    ``supervised`` is whether a machine launched the run (``--json``): its
    recoverable pauses are the supervisor's to retry, and its launcher is not
    a person watching a terminal, so duration says nothing about attention.
    """
    if payload.get("dry_run"):
        return None
    duration = payload.get("duration_seconds")
    unattended = (
        isinstance(duration, int | float)
        and not isinstance(duration, bool)
        and duration >= UNATTENDED_AFTER_SECONDS
    )
    plan_dir = str(payload.get("plan_dir") or "")
    name = plans_files.read_plan_title(plan_dir) or Path(plan_dir).name or "plan"
    title = f"ASL: {name}"
    snapshot = payload.get("final_snapshot")
    next_pending = (
        snapshot.get("next_pending") if isinstance(snapshot, Mapping) else "unknown"
    )
    if payload.get("paused"):
        needs_person = _requires_user(payload)
        if not (needs_person and (supervised or unattended)) and not (
            unattended and not supervised
        ):
            return None
        lead = "Needs you" if needs_person else "Paused"
        detail = payload.get("pause_message") or payload.get("pause_reason") or ""
        return title, f"{lead}: {detail}"[:_BODY_LIMIT]
    if not unattended:
        return None
    if payload.get("ok") and next_pending is None:
        verdict = payload.get("review_verdict")
        status = (
            str(verdict.get("status_line") or "").removeprefix("Status: ")
            if isinstance(verdict, Mapping)
            else ""
        )
        suffix = f" Review: {status}." if status else ""
        return title, f"Finished: every session is complete.{suffix}"[:_BODY_LIMIT]
    if supervised:
        return None
    if payload.get("ok"):
        return title, f"Stopped with session {next_pending} next."[:_BODY_LIMIT]
    return title, f"Stopped: {payload.get('reason') or 'unknown'}"[:_BODY_LIMIT]


def notify_run_outcome(
    payload: Mapping[str, Any],
    *,
    supervised: bool,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Post the notification for ``payload`` if one is due; never raises."""
    with suppress(Exception):
        if sys.platform != "darwin" or not enabled(env):
            return False
        message = message_for(payload, supervised=supervised)
        osascript = shutil.which("osascript")
        if message is None or osascript is None:
            return False
        title, body = message
        argv = [osascript]
        for line in _SCRIPT:
            argv.extend(["-e", line])
        argv.extend([body, title])
        # Detached and unawaited: the run's exit must not wait on a banner.
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    return False
