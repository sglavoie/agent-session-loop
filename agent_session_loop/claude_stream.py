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
from pathlib import Path
from typing import Any

from . import guidance_delivery
from .claude_guidance_hook import GUIDANCE_CONTEXT_HEADING, HOOK_EVENT

#: The transport discriminator for a claude turn driven over stream JSON.
TRANSPORT_STREAM_JSON = "stream-json"

__all__ = [
    "CONTROL_REQUEST",
    "CONTROL_RESPONSE",
    "GUIDANCE_CONTEXT_HEADING",
    "HOOK_EVENT",
    "INTERRUPT_TIMEOUT_SECONDS",
    "SUBTYPE_INTERRUPT",
    "TRANSPORT_STREAM_JSON",
    "ClaudeGuidanceSpool",
    "ClaudeStreamSession",
    "control_request",
    "hook_script_path",
    "new_session_id",
    "normalized_echo_body",
    "remove_overlay",
    "user_message",
    "write_settings_overlay",
]

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
        session_id: str | None = None,
        interrupt_timeout: float = INTERRUPT_TIMEOUT_SECONDS,
    ) -> None:
        self._process = process
        self._prompt = prompt
        self._spool = spool
        self._session_id = session_id
        self._interrupt_timeout = interrupt_timeout

        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()

        self._offers: dict[str, _Offer] = {}
        self._echo_expectations: list[_EchoExpectation] = []
        self._messages_written = 0
        self._messages_answered = 0
        self._stdin_closed = False
        self._prompt_error: str | None = None
        self._session_mismatch: str | None = None
        self._session_observed = False

        self._pending_control: dict[str, _PendingControl] = {}
        self._control_seq = 0
        self._interrupt_requested = False
        self._turn_interrupted = False

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

    @property
    def session_id(self) -> str | None:
        """The conversation this turn was launched under, if it was told one."""
        with self._state_lock:
            return self._session_id

    @property
    def session_observed(self) -> bool:
        """Whether the CLI ever named a session of its own on this turn.

        ``False`` means the CLI never got as far as announcing a conversation,
        which is what separates a ``--resume`` the CLI *refused* from one it
        honoured and then failed inside: the second announced its session first.
        """
        with self._state_lock:
            return self._session_observed

    @property
    def session_mismatch(self) -> str | None:
        """Why the conversation this turn ran on is not the one it was given.

        ``None`` on every ordinary turn, including one that stated no session at
        all. A value here means the recorded identifier does not name the
        conversation the work went into, so nothing may continue from it.
        """
        with self._state_lock:
            return self._session_mismatch

    @property
    def interrupt_requested(self) -> bool:
        """Whether an ``interrupt`` control request was written for this turn."""
        with self._state_lock:
            return self._interrupt_requested

    @property
    def turn_interrupted(self) -> bool:
        """Whether the turn's terminal result was the one it was stopped at.

        Kept separate from :attr:`interrupt_requested` because they answer
        different questions: one says ASL asked, the other says the CLI
        answered with the result of a turn that was cut short. Only the second
        is a statement about what the turn produced.
        """
        with self._state_lock:
            return self._turn_interrupted

    # -- the prompt ---------------------------------------------------------

    def send_prompt(self) -> bool:
        """Write the turn's initial prompt as the conversation's first message.

        Returns whether the write succeeded. A failure is recorded rather than
        raised: the process is already spawned and the turn has to be collected
        and reported like any other failed turn.
        """
        try:
            self._write(user_message(self._prompt), echo_body=self._prompt)
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
        every written message has been answered. A result covers all messages
        echoed before it, including guidance taken up in the active tool loop.
        Without a new echo it covers one message, preserving the older CLI
        behavior. Unacknowledged queued guidance keeps stdin open.
        """
        # Serialize the decision and close with writes: a guidance write must
        # either be counted before the decision or observe the closed pipe.
        with self._write_lock:
            with self._state_lock:
                if self._interrupt_requested:
                    # This result reports the turn the CLI was told to stop, so
                    # it is drained rather than counted: it answers whatever it
                    # answers, but crediting it against the messages still
                    # queued would let an interrupted turn settle guidance the
                    # model never ran, and would leave a later result looking
                    # like the resumed turn's own. The conversation is over
                    # either way, so stdin closes regardless of the queue.
                    self._turn_interrupted = True
                    idle = True
                else:
                    echoed_through = max(
                        (
                            index + 1
                            for index, expectation in enumerate(self._echo_expectations)
                            if expectation.consumed
                        ),
                        default=0,
                    )
                    self._messages_answered = max(
                        self._messages_answered + 1, echoed_through
                    )
                    idle = self._messages_answered >= self._messages_written
            if idle:
                self._close_stdin_locked()

    def note_user_echo(self, text: str) -> None:
        """Record the CLI echoing a user message it has taken up.

        This is the protocol acknowledgement: the CLI emits the echo when it
        starts processing the message, so an echo carrying a guidance body is
        that body reaching the model. An echo is matched against the messages
        this session wrote, by body equality and in write order, and each echo
        is spent on at most one of them. That is what makes the
        acknowledgement specific: the turn's prompt claims its own echo however
        late it arrives, and an unrelated message that merely happens to
        contain a guidance phrase matches nothing at all.
        """
        if not text:
            return
        body = normalized_echo_body(text)
        if not body:
            return
        with self._state_lock:
            interrupted = self._interrupt_requested
            for expectation in self._echo_expectations:
                if expectation.consumed or expectation.body != body:
                    continue
                # Consumed either way, so this echo can never be spent twice:
                # the accounting is about which write it belongs to, and that
                # does not change because the turn is ending.
                expectation.consumed = True
                if expectation.guidance_id is None or interrupted:
                    # An echo the CLI emits while draining its stdin on the way
                    # out of an interrupted turn says it read the message, not
                    # that the model acted on it. Recording a delivery from it
                    # would spend a guidance record on work that was stopped.
                    return
                offer = self._offers.get(expectation.guidance_id)
                if offer is not None and offer.confirmed_by is None:
                    offer.confirmed_by = "protocol-echo"
                return

    def note_session_id(self, reported: str) -> str | None:
        """Re-check the session the CLI opened against the one it was given.

        Returns a description of the disagreement, or ``None`` when there is
        none — including when this turn stated no session at all, which is the
        ordinary shape of an audit turn that persists none.

        The turn is *not* stopped here. The prompt was written before the first
        record arrived, so by the time this can be checked the model is already
        changing the workspace, and abandoning it mid-edit to correct a
        bookkeeping fact would cost more than the fact is worth. What the
        mismatch does is disqualify the recorded identifier: the result reports
        it, and nothing continues from a conversation the work did not go into.
        """
        with self._state_lock:
            if reported:
                self._session_observed = True
            expected = self._session_id
            if expected is None or not reported or reported == expected:
                return None
            if self._session_mismatch is None:
                self._session_mismatch = (
                    f"launched under session {expected} "
                    f"but the CLI reported session {reported}"
                )
            return self._session_mismatch

    def note_control_response(self, response: object) -> None:
        """Resolve the control request this response names, if it names one.

        Attribution is by ``request_id`` and by nothing else. A response
        carrying an identifier this session is not waiting on — one already
        given up on, one belonging to a request the CLI raised itself — resolves
        nothing, because the alternative is letting arrival order decide, and
        arrival order is what makes a late answer look like the next one.
        """
        if not isinstance(response, dict):
            return
        request_id = response.get("request_id")
        if not isinstance(request_id, str):
            return
        with self._state_lock:
            pending = self._pending_control.pop(request_id, None)
        if pending is None:
            return
        pending.resolve(acknowledged=response.get("subtype") == SUBTYPE_SUCCESS)

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
                offer = _Offer(request.guidance_id)
                self._offers[request.guidance_id] = offer
            confirmed_by = offer.confirmed_by
            stdin_closed = self._stdin_closed
            write_failed = offer.write_failed
            interrupted = self._interrupt_requested

        if confirmed_by is not None:
            return guidance_delivery.DELIVERED

        if interrupted:
            # The turn is being stopped, so neither route can still deliver:
            # a message written now would be echoed by a CLI draining its queue
            # on the way out, and a hook armed now has no further tool call to
            # inject into. A receipt already on disk is different -- the hook
            # writes one only after handing the text to a call that then ran --
            # so it still counts, and everything else waits for the next turn.
            if self._receipt_confirms(offer):
                return guidance_delivery.DELIVERED
            return guidance_delivery.deferred(guidance_delivery.REASON_STALE_TURN)

        if first_attempt:
            self._arm_hook(request)
            if stdin_closed:
                # The turn's own result already closed the conversation; the
                # hook is the only route left, and it can still fire while the
                # process winds down.
                return self._confirmation_outcome(offer, detail="stdin-closed")
            try:
                self._write(
                    user_message(request.text),
                    echo_body=request.text,
                    guidance_id=request.guidance_id,
                )
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

    def _arm_hook(self, request: guidance_delivery.GuidanceRequest) -> None:
        spool = self._spool
        if spool is None:
            return
        with contextlib.suppress(OSError):
            spool.offer(
                guidance_id=request.guidance_id,
                seq=request.seq,
                text=request.text,
            )

    def _confirmation_outcome(
        self,
        offer: _Offer,
        *,
        failed: bool = False,
        detail: str | None = None,
    ) -> guidance_delivery.GuidanceOutcome:
        """Report the evidence that exists right now, and nothing more."""
        if self._receipt_confirms(offer):
            return guidance_delivery.DELIVERED
        with self._state_lock:
            if offer.confirmed_by is not None:
                return guidance_delivery.DELIVERED
        if failed and not self._hook_still_armed(offer):
            # Neither route survives: the pipe is gone and the hook has nothing
            # left to claim. This attempt is over.
            return guidance_delivery.deferred(
                guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
                detail=detail,
            )
        return guidance_delivery.awaiting_confirmation(detail=detail)

    def _receipt_confirms(self, offer: _Offer) -> bool:
        """Whether the hook has reported injecting this offer, recording it once."""
        spool = self._spool
        if spool is None or not spool.receipt(offer.guidance_id):
            return False
        with self._state_lock:
            if offer.confirmed_by is None:
                offer.confirmed_by = "pretooluse-hook"
        return True

    def _hook_still_armed(self, offer: _Offer) -> bool:
        spool = self._spool
        if spool is None:
            return False
        pending = spool.root / _PENDING / f"{offer.guidance_id}.json"
        claimed = spool.root / _CLAIMED / f"{offer.guidance_id}.json"
        return pending.exists() or claimed.exists()

    # -- interruption -------------------------------------------------------

    def interrupt(self) -> bool:
        """Tell the CLI to stop the work this turn has in flight.

        Reports whether the CLI *acknowledged* the request, which is neither a
        promise that the work has stopped nor a substitute for the forced
        termination standing behind every caller. What it buys is the
        distinction: a turn told to stop ends as an interrupted one, while a
        turn whose pipes were merely taken away ends as one that stopped
        answering.

        Sent at most once per turn. A second request would be a second
        identifier to correlate for an outcome already decided, and the CLI's
        answer to it could only arrive after the first turned this turn into an
        interrupted one.

        Never raises. Every caller reaches for this while tearing a turn down,
        where an exception would only obscure why the turn ended.
        """
        with self._state_lock:
            if self._interrupt_requested:
                return False
            if self._stdin_closed or self._messages_written == 0:
                # Nothing was submitted, or the conversation is already over:
                # either way there is no work in flight to stop, and the
                # cancellation cost nothing to begin with.
                return False
            self._interrupt_requested = True
            self._control_seq += 1
            # ``req_<n>_<hex>`` is the shape the official SDK mints, and the
            # CLI treats it as opaque; matching it keeps ASL's traffic
            # indistinguishable from the traffic this control channel is
            # exercised with upstream.
            request_id = f"req_{self._control_seq}_{os.urandom(4).hex()}"
            pending = _PendingControl(request_id)
            self._pending_control[request_id] = pending

        try:
            self._write(control_request(request_id, SUBTYPE_INTERRUPT))
        except OSError:
            self._drop_pending(request_id)
            return False
        return self._await_control(pending)

    def _await_control(self, pending: _PendingControl) -> bool:
        """Wait out one control request, bounded by the clock and by liveness."""
        deadline = time.monotonic() + self._interrupt_timeout
        while not pending.event.wait(_LIVENESS_POLL_SECONDS):
            if self._process.poll() is not None:
                # The CLI is gone, which is the state the interrupt was asking
                # for. It is still not an acknowledgement: nothing said the turn
                # was stopped rather than killed.
                self._drop_pending(pending.request_id)
                return False
            if time.monotonic() >= deadline:
                self._drop_pending(pending.request_id)
                return False
        return pending.acknowledged

    def _drop_pending(self, request_id: str) -> None:
        """Stop waiting on a request, so a late answer correlates with nothing."""
        with self._state_lock:
            self._pending_control.pop(request_id, None)

    # -- transport primitives ----------------------------------------------

    def _write(
        self,
        payload: dict[str, Any],
        *,
        echo_body: str | None = None,
        guidance_id: str | None = None,
    ) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise OSError("claude was spawned without a stdin pipe")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._write_lock:
            expectation: _EchoExpectation | None = None
            with self._state_lock:
                if self._stdin_closed:
                    raise OSError("claude stdin is closed")
                # The echo can come back while ``flush`` is still returning: the
                # output processor is another thread reading the same CLI. So
                # the expectation is published before the bytes move, and the
                # write is undone -- expectation included -- if they never do.
                # Recording it afterwards leaves a window in which the message's
                # own echo matches nothing and is discarded for good.
                if echo_body is not None:
                    expectation = _EchoExpectation(echo_body, guidance_id)
                    self._echo_expectations.append(expectation)
            try:
                stdin.write(line)
                stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                with self._state_lock:
                    if expectation is not None:
                        # A message that never reached the pipe has no echo
                        # coming, and a stale expectation would let some later
                        # unrelated echo of the same words confirm it.
                        self._echo_expectations.remove(expectation)
                raise OSError(str(error) or type(error).__name__) from error
            with self._state_lock:
                if echo_body is not None:
                    # Only a conversation message is counted. A control request
                    # is neither echoed back nor answered by a ``result``, so
                    # counting one would leave the conversation permanently
                    # short an answer and stdin permanently open.
                    self._messages_written += 1

    def close_stdin(self) -> None:
        """End the conversation, which is how the CLI is asked to finish."""
        with self._write_lock:
            self._close_stdin_locked()

    def _close_stdin_locked(self) -> None:
        """Close with the caller holding the write lock."""
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
