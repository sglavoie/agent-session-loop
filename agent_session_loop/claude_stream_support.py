# ruff: noqa: F401
"""Bidirectional stream-JSON transport for the Claude CLI.

``claude --print`` used to be a one-shot process: the prompt went on argv, the
turn ran, and nothing could be said to the model afterwards. With
``--input-format stream-json`` the same two pipes become a conversation — the
prompt is a message written to stdin, and further messages can be written while
the turn is running.

What that buys, and what it does not. A stdin write is a write to a pipe: it
proves the bytes left this process and nothing whatsoever about a model reading
them. With ``--replay-user-messages`` the CLI acknowledges a message when it
takes it up. Guidance may join an active tool loop or wait for a follow-up
turn, so counting one result per write does not tell us when it is done. This
module never reports a delivery from a successful write. It reports one only
from evidence:

- a **protocol acknowledgement**: the CLI echoes a user message back on the
  output stream as a ``{"type": "user"}`` record once it takes it up; or
- an **ASL-owned ``PreToolUse`` hook**, armed through an ephemeral settings
  overlay, which injects pending guidance as ``additionalContext`` for the
  model's next request and leaves a receipt saying it did.

The hook can reach the running turn before the stdin queue is consumed. Its
receipt confirms delivery but does not acknowledge consumption of the queued
stdin message. Until one of the routes reports in, an attempt is
``awaiting-confirmation`` — deferred, still deliverable, and never a receipt.

Control requests. The same stdin stream carries a second kind of message, and
it is the CLI's own rather than ASL's invention: a ``control_request`` envelope
naming a ``request_id``, answered by a ``control_response`` carrying that same
identifier back. That is how ``interrupt`` is expressed — read off the raw
protocol the ``claude-agent-sdk`` speaks rather than assumed from its
high-level method — and the correlation is the whole point of it. An
uncorrelated acknowledgement could only be attributed by arrival order, which is
exactly how a delayed answer to one request comes to stand in for the next.

Continuity. This conversation's identifier is *stated* rather than discovered.
``--session-id`` assigns one and ``--resume`` restores exactly one, so the turn
knows which conversation it runs on before its prompt is written and can make
that fact durable while there is still no work to lose. ``--fork-session`` is
never passed: it answers "continue conversation X" with a different one. The
``system``/``init`` record echoes the session the CLI actually opened, and
:meth:`ClaudeStreamSession.note_session_id` re-checks it against the stated one.

Privacy. Guidance bodies live in memory and on the stdin pipe and in the
turn-scoped spool, which is deleted with the turn. The CLI echoes user messages
back on stdout; :class:`ClaudeStreamSession` matches those echoes but the
output processor renders none of them, so no guidance body reaches a persisted
artifact. Control requests and responses carry no user text at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import guidance_delivery
from .claude_guidance_hook import GUIDANCE_CONTEXT_HEADING, HOOK_EVENT

#: The transport discriminator for a claude turn driven over stream JSON.
TRANSPORT_STREAM_JSON = "stream-json"

#: How long the CLI may spend running the hook before giving up on it. The hook
#: reads a handful of small files, so this is a wedged-filesystem bound rather
#: than a working one.
HOOK_TIMEOUT_SECONDS = 10

#: The CLI's control-channel envelope types, as spoken on the same two pipes
#: the conversation itself uses.
CONTROL_REQUEST = "control_request"
CONTROL_RESPONSE = "control_response"

#: The one control subtype ASL sends. Others exist; none of them is something
#: a supervised turn needs, and each one added is a message the CLI could
#: answer while a turn is winding down.
SUBTYPE_INTERRUPT = "interrupt"

#: The response subtype that means the CLI took the request.
SUBTYPE_SUCCESS = "success"

#: How long a control request may take to be answered. Bounded well under any
#: shutdown grace because every caller has already decided the turn is over:
#: the request is how the CLI is *told* so, and a forced termination stands
#: behind it either way.
INTERRUPT_TIMEOUT_SECONDS = 5.0

#: How often a bounded wait re-checks whether the CLI is still alive. A dead
#: process can never answer, so noticing quickly is what keeps an interrupt
#: sent at a corpse from costing the full timeout.
_LIVENESS_POLL_SECONDS = 0.05

#: Spool subdirectories. ``pending`` is what the hook may claim, ``claimed``
#: holds what it took, and ``receipts`` is the evidence ASL reads back.
_PENDING = "pending"
_CLAIMED = "claimed"
_RECEIPTS = "receipts"


def hook_script_path() -> Path:
    """Absolute path to the standalone hook script.

    Invoked by path rather than as ``-m agent_session_loop...`` so it runs
    under whatever interpreter and working directory the CLI hands it, with no
    dependency on the package being importable from the harness's environment.
    """
    return Path(__file__).resolve().parent / "claude_guidance_hook.py"


def guard_hook_script_path() -> Path:
    """Absolute path to the standalone pre-tool guard hook.

    Separate from the guidance hook, and invoked the same way, because the two
    do opposite things: one adds context to a call that proceeds, the other
    refuses the call. Folding them into one script would mean a bug in the
    guidance path could swallow a denial.
    """
    return Path(__file__).resolve().parent / "guard_pretool_hook.py"


class ClaudeGuidanceSpool:
    """The turn-scoped handoff between ASL and its ``PreToolUse`` hook.

    One directory tree, three states, and one atomic transition between them:
    a guidance file is offered into ``pending``, and either the hook renames it
    into ``claimed`` (and injects it) or ASL renames it away (and does not).
    ``os.replace`` decides, so the guidance is injected at most once no matter
    how the two race.

    The tree lives inside the turn's ephemeral overlay directory and is removed
    with it, which is what bounds the hook's reach to the turn that armed it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        for name in (_PENDING, _CLAIMED, _RECEIPTS):
            (root / name).mkdir(parents=True, exist_ok=True)

    def offer(self, *, guidance_id: str, seq: int, text: str) -> None:
        """Make one guidance record claimable by the hook.

        Written to a temporary name and renamed in, so the hook can never read
        a half-written record.
        """
        payload = json.dumps(
            {"id": guidance_id, "seq": seq, "text": text},
            ensure_ascii=False,
        )
        staging = self.root / f".{guidance_id}.tmp"
        staging.write_text(payload, encoding="utf-8")
        os.replace(staging, self.root / _PENDING / f"{guidance_id}.json")

    def withdraw(self, guidance_id: str) -> bool:
        """Take one record back before the hook can claim it.

        ``False`` means the hook won the race and the guidance is already on
        its way into the model's context.
        """
        try:
            (self.root / _PENDING / f"{guidance_id}.json").unlink()
        except OSError:
            return False
        return True

    def receipt(self, guidance_id: str) -> bool:
        """Whether the hook has reported injecting this guidance."""
        return (self.root / _RECEIPTS / f"{guidance_id}.json").exists()

    def settings_payload(
        self,
        *,
        executable: str,
        guard_policy_path: str | None = None,
    ) -> dict[str, Any]:
        """Build the ephemeral settings overlay that arms this turn's hooks.

        Deliberately minimal: one ``PreToolUse`` matcher and nothing else. The
        overlay is handed to the CLI with ``--settings`` for this turn only and
        is never written anywhere the user's own configuration lives.

        The guard hook is listed first when the run has a policy for it. Order
        is not what makes the denial hold — the CLI refuses the call if any
        hook denies it — but a call that is about to be refused has no business
        also being handed guidance meant for work it will not do.
        """
        hooks: list[dict[str, Any]] = []
        if guard_policy_path:
            hooks.append(
                {
                    "type": "command",
                    "command": " ".join(
                        shlex.quote(part)
                        for part in (
                            executable,
                            str(guard_hook_script_path()),
                            guard_policy_path,
                        )
                    ),
                    "timeout": HOOK_TIMEOUT_SECONDS,
                }
            )
        hooks.append(
            {
                "type": "command",
                "command": " ".join(
                    shlex.quote(part)
                    for part in (executable, str(hook_script_path()), str(self.root))
                ),
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        )
        return {"hooks": {HOOK_EVENT: [{"matcher": "*", "hooks": hooks}]}}


def write_settings_overlay(
    overlay_dir: Path,
    spool: ClaudeGuidanceSpool,
    *,
    executable: str | None = None,
    guard_policy_path: str | None = None,
) -> Path:
    """Materialize the overlay file and return the path ``--settings`` takes."""
    settings_path = overlay_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            spool.settings_payload(
                executable=executable or sys.executable,
                guard_policy_path=guard_policy_path,
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return settings_path


def user_message(text: str) -> dict[str, Any]:
    """One stream-JSON user message: the conversation half of ASL's stdin."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def control_request(request_id: str, subtype: str) -> dict[str, Any]:
    """One control request: the protocol half of ASL's stdin.

    ``request_id`` is what makes the answer attributable. It is the caller's to
    mint and the CLI's to echo back untouched, and nothing else in the envelope
    identifies the request — so a response that does not carry it belongs to no
    request this session is waiting on.
    """
    return {
        "type": CONTROL_REQUEST,
        "request_id": request_id,
        "request": {"subtype": subtype},
    }


def new_session_id() -> str:
    """Mint the conversation identifier a turn will be launched under.

    ``--session-id`` requires a UUID, and assigning one is what lets a claude
    turn persist its conversation *before* submitting work: the alternative is
    reading the identifier out of the CLI's first record, which arrives after
    the prompt has already gone.
    """
    return str(uuid.uuid4())


def normalized_echo_body(text: str) -> str:
    """The comparable form of a user message body.

    An echo is compared to what was written by body equality, not containment:
    containment lets any message whose text happens to span a short guidance
    phrase -- the turn's own prompt, most of all -- stand in as an
    acknowledgement for guidance the model has not seen. Whitespace is the only
    thing collapsed, because a re-rendered echo may rewrap lines but does not
    rewrite words.
    """
    return " ".join(text.split())


class _Offer:
    """One guidance record this turn has taken responsibility for.

    It carries no copy of the body: an echo is matched against the write that
    put the body on the pipe (see :class:`_EchoExpectation`), so an offer that
    was never written has nothing an echo could confirm.
    """

    def __init__(self, guidance_id: str) -> None:
        self.guidance_id = guidance_id
        self.confirmed_by: str | None = None
        self.write_failed = False


class _EchoExpectation:
    """One message written to stdin, awaiting the CLI echoing it back.

    ``guidance_id`` is ``None`` for the turn's prompt, which is expected and
    consumed like any other message precisely so that a late prompt echo is
    accounted for as the prompt rather than offered to a guidance record.
    """

    def __init__(self, body: str, guidance_id: str | None) -> None:
        self.body = normalized_echo_body(body)
        self.guidance_id = guidance_id
        self.consumed = False


class _PendingControl:
    """One control request written to stdin, awaiting its correlated answer.

    Resolved by the output processor's thread and waited on by whichever thread
    sent the request, so the answer is published under the session's state lock
    and read only after the event is set.
    """

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.event = threading.Event()
        self.acknowledged = False

    def resolve(self, *, acknowledged: bool) -> None:
        self.acknowledged = acknowledged
        self.event.set()


def remove_overlay(overlay_dir: Path | None) -> None:
    """Delete a turn's ephemeral overlay, hook spool and settings together.

    The overlay is the hook's entire reach: with it gone the command the CLI
    was told to run can claim nothing, which is what keeps a turn's guidance
    channel from outliving the turn.
    """
    if overlay_dir is None:
        return
    shutil.rmtree(overlay_dir, ignore_errors=True)
