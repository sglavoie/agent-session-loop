"""Bidirectional stream-JSON transport for the Claude CLI.

``claude --print`` used to be a one-shot process: the prompt went on argv, the
turn ran, and nothing could be said to the model afterwards. With
``--input-format stream-json`` the same two pipes become a conversation — the
prompt is a message written to stdin, and further messages can be written while
the turn is running.

What that buys, and what it does not. A stdin write is a write to a pipe: it
proves the bytes left this process and nothing whatsoever about a model reading
them. The CLI acknowledges a message only when it starts processing it, and a
message written mid-turn is *queued* as its own follow-up turn rather than
folded into the running one. So this module never reports a delivery from a
successful write. It reports one only from evidence:

- a **protocol acknowledgement**: the CLI echoes a user message back on the
  output stream as a ``{"type": "user"}`` record once it takes it up; or
- an **ASL-owned ``PreToolUse`` hook**, armed through an ephemeral settings
  overlay, which injects pending guidance as ``additionalContext`` for the
  model's next request and leaves a receipt saying it did.

The two routes are complementary rather than redundant: the hook reaches the
turn that is running *now*, the stdin queue reaches the turn after it, and each
guidance record is claimed by exactly one of them (see
:class:`ClaudeGuidanceSpool`). Until one of them reports in, an attempt is
``awaiting-confirmation`` — deferred, still deliverable, and never a receipt.

Privacy. Guidance bodies live in memory and on the stdin pipe and in the
turn-scoped spool, which is deleted with the turn. The CLI echoes user messages
back on stdout; :class:`ClaudeStreamSession` matches those echoes but the
output processor renders none of them, so no guidance body reaches a persisted
artifact.
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
from pathlib import Path
from typing import Any

from . import guidance_delivery
from .claude_guidance_hook import GUIDANCE_CONTEXT_HEADING, HOOK_EVENT

#: The transport discriminator for a claude turn driven over stream JSON.
TRANSPORT_STREAM_JSON = "stream-json"

__all__ = [
    "GUIDANCE_CONTEXT_HEADING",
    "HOOK_EVENT",
    "TRANSPORT_STREAM_JSON",
    "ClaudeGuidanceSpool",
    "ClaudeStreamSession",
    "hook_script_path",
    "remove_overlay",
    "user_message",
    "write_settings_overlay",
]

#: How long the CLI may spend running the hook before giving up on it. The hook
#: reads a handful of small files, so this is a wedged-filesystem bound rather
#: than a working one.
HOOK_TIMEOUT_SECONDS = 10

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
    """One stream-JSON user message, the only shape ASL ever writes to stdin."""
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


class _Offer:
    """One guidance record this turn has taken responsibility for."""

    def __init__(self, guidance_id: str, text: str) -> None:
        self.guidance_id = guidance_id
        self.text = text
        self.confirmed_by: str | None = None
        self.write_failed = False


class ClaudeStreamSession:
    """One bidirectional stream-JSON conversation, scoped to one harness turn.

    Constructed around a spawned process, given its prompt immediately, fed
    observations by the output processor, and torn down once the process is
    gone. It owns the process's stdin outright: nothing else in the turn may
    write to it.
    """

    def __init__(
        self,
        *,
        process: subprocess.Popen,
        prompt: str,
        spool: ClaudeGuidanceSpool | None = None,
    ) -> None:
        self._process = process
        self._prompt = prompt
        self._spool = spool

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()

        self._offers: dict[str, _Offer] = {}
        self._messages_written = 0
        self._results_seen = 0
        self._stdin_closed = False
        self._prompt_error: str | None = None

    # -- state a caller reads ----------------------------------------------

    @property
    def prompt_error(self) -> str | None:
        """Why the prompt never reached the CLI, or ``None``.

        A claude turn has no fallback transport: if the prompt could not be
        written there is nothing for the model to do, and the turn's stderr is
        where an operator finds out why.
        """
        with self._state_lock:
            return self._prompt_error

    @property
    def stdin_closed(self) -> bool:
        with self._state_lock:
            return self._stdin_closed

    # -- the prompt ---------------------------------------------------------

    def send_prompt(self) -> bool:
        """Write the turn's initial prompt as the conversation's first message.

        Returns whether the write succeeded. A failure is recorded rather than
        raised: the process is already spawned and the turn has to be collected
        and reported like any other failed turn.
        """
        try:
            self._write(user_message(self._prompt))
        except OSError as error:
            with self._state_lock:
                self._prompt_error = f"{type(error).__name__}: {error}"
            # Without a prompt the CLI would sit on an open stdin waiting for
            # work that is never coming, and the turn would end at its timeout
            # rather than at the failure. Closing turns that into a prompt exit.
            self.close_stdin()
            return False
        return True

    # -- observations from the output processor -----------------------------

    def note_result(self) -> None:
        """Record one terminal ``result`` record and close stdin when idle.

        The CLI in streaming-input mode runs until its stdin reaches EOF, so
        something has to close it or the turn never ends. It is closed once
        every message written has been answered by a result: a guidance message
        queued before this result is a follow-up turn that has not run yet, and
        cutting stdin here would discard it.
        """
        with self._state_lock:
            self._results_seen += 1
            idle = self._results_seen >= self._messages_written
        if idle:
            self.close_stdin()

    def note_user_echo(self, text: str) -> None:
        """Record the CLI echoing a user message it has taken up.

        This is the protocol acknowledgement: the CLI emits the echo when it
        starts processing the message, so an echo carrying a guidance body is
        that body reaching the model. Matching is by containment because the
        CLI may wrap or re-render what it echoes.
        """
        if not text:
            return
        with self._state_lock:
            for offer in self._offers.values():
                if offer.confirmed_by is None and offer.text and offer.text in text:
                    offer.confirmed_by = "protocol-echo"

    # -- steering -----------------------------------------------------------

    def steer(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        """Offer one guidance record to this turn and report what is known.

        Called repeatedly for the same record while it stays unconfirmed: the
        first call arms both routes, and every later call reports whichever
        evidence has since arrived. It never blocks waiting for that evidence,
        because the caller is a poll tick holding the live-delivery endpoint
        lock that end-of-turn has to acquire.
        """
        with self._state_lock:
            offer = self._offers.get(request.guidance_id)
            first_attempt = offer is None
            if offer is None:
                offer = _Offer(request.guidance_id, request.text)
                self._offers[request.guidance_id] = offer
            confirmed_by = offer.confirmed_by
            stdin_closed = self._stdin_closed
            write_failed = offer.write_failed

        if confirmed_by is not None:
            return guidance_delivery.DELIVERED

        if first_attempt:
            self._arm_hook(offer, request)
            if stdin_closed:
                # The turn's own result already closed the conversation; the
                # hook is the only route left, and it can still fire while the
                # process winds down.
                return self._confirmation_outcome(offer, detail="stdin-closed")
            try:
                self._write(user_message(request.text))
            except OSError as error:
                with self._state_lock:
                    offer.write_failed = True
                return self._confirmation_outcome(
                    offer,
                    failed=True,
                    detail=f"{type(error).__name__}: {error}",
                )
            return self._confirmation_outcome(offer)

        return self._confirmation_outcome(offer, failed=write_failed)

    def _arm_hook(
        self,
        offer: _Offer,
        request: guidance_delivery.GuidanceRequest,
    ) -> None:
        spool = self._spool
        if spool is None:
            return
        with contextlib.suppress(OSError):
            spool.offer(
                guidance_id=request.guidance_id,
                seq=request.seq,
                text=request.text,
            )
            offer.text = request.text

    def _confirmation_outcome(
        self,
        offer: _Offer,
        *,
        failed: bool = False,
        detail: str | None = None,
    ) -> guidance_delivery.GuidanceOutcome:
        """Report the evidence that exists right now, and nothing more."""
        spool = self._spool
        if spool is not None and spool.receipt(offer.guidance_id):
            with self._state_lock:
                if offer.confirmed_by is None:
                    offer.confirmed_by = "pretooluse-hook"
            return guidance_delivery.DELIVERED
        with self._state_lock:
            if offer.confirmed_by is not None:
                return guidance_delivery.DELIVERED
        if failed and (spool is None or not self._hook_still_armed(offer)):
            # Neither route survives: the pipe is gone and the hook has nothing
            # left to claim. This attempt is over.
            return guidance_delivery.deferred(
                guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
                detail=detail,
            )
        return guidance_delivery.awaiting_confirmation(detail=detail)

    def _hook_still_armed(self, offer: _Offer) -> bool:
        spool = self._spool
        if spool is None:
            return False
        pending = spool.root / _PENDING / f"{offer.guidance_id}.json"
        claimed = spool.root / _CLAIMED / f"{offer.guidance_id}.json"
        return pending.exists() or claimed.exists()

    # -- transport primitives ----------------------------------------------

    def _write(self, payload: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise OSError("claude was spawned without a stdin pipe")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    raise OSError("claude stdin is closed")
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                raise OSError(str(error) or type(error).__name__) from error
            with self._state_lock:
                self._messages_written += 1

    def close_stdin(self) -> None:
        """End the conversation, which is how the CLI is asked to finish."""
        with self._write_lock:
            with self._state_lock:
                if self._stdin_closed:
                    return
                self._stdin_closed = True
            stdin = self._process.stdin
            if stdin is None:
                return
            # An already-gone CLI is the state closing stdin was trying to
            # reach, so every failure mode here is a success.
            with contextlib.suppress(BrokenPipeError, OSError, ValueError):
                stdin.close()

    def shutdown(self) -> None:
        """Release the conversation once the harness process is gone."""
        self.close_stdin()


def remove_overlay(overlay_dir: Path | None) -> None:
    """Delete a turn's ephemeral overlay, hook spool and settings together.

    The overlay is the hook's entire reach: with it gone the command the CLI
    was told to run can claim nothing, which is what keeps a turn's guidance
    channel from outliving the turn.
    """
    if overlay_dir is None:
        return
    shutil.rmtree(overlay_dir, ignore_errors=True)
