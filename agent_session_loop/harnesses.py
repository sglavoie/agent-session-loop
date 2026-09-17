"""Harness adapter boundaries.

Adapters construct argv lists and run harnesses without shell interpolation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from . import (
    claude_stream,
    codex_app_server,
    config,
    copilot_acp,
    guards,
    guidance_delivery,
    restore_diagnosis,
)
from .process_tree import (
    clear_process_tree,
    descendant_pids,
    last_process_tree,
    sample_process_table,
    sweep_enabled,
    sweep_survivors,
)

HarnessName = Literal["codex", "claude", "copilot"]
HARNESS_NAMES: tuple[HarnessName, ...] = ("codex", "claude", "copilot")
StreamName = Literal["stdout", "stderr"]
StreamCallback = Callable[[StreamName, str], None]

AccessProfileName = Literal["worker", "audit-read-only"]
ACCESS_PROFILE_NAMES: tuple[AccessProfileName, ...] = ("worker", "audit-read-only")

#: Claude tools that read or search without mutating anything.
CLAUDE_AUDIT_TOOLS = "Read,Glob,Grep"

#: Claude tools denied outright, so an audit turn cannot edit, shell out, reach
#: the network, spawn a subagent, or write session state.
CLAUDE_AUDIT_DENIED_TOOLS = (
    "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
)

#: Copilot tools that read or search. Verified against the installed CLI rather
#: than inferred from its help text, because an unknown name in
#: ``--available-tools`` silently yields an agent with no tools at all.
COPILOT_AUDIT_TOOLS = "view,rg,glob"

#: Copilot tool families denied outright. Denial beats any allow rule, so these
#: hold even if a future default widens ``--available-tools``.
COPILOT_AUDIT_DENIED_TOOLS = ("write", "shell", "url")

#: The bypass flag a worker turn is launched with, named here because the ACP
#: permission responder reads it back off argv to decide what it may answer.
COPILOT_ALLOW_ALL_TOOLS = "--allow-all-tools"


# A dead harness process closes its pipes, so the reader threads see EOF at
# once. This bound only exists for a descendant that outlives the harness and
# keeps the inherited pipes open. The post-turn sweep runs after this join and
# kills such a descendant if it is still in the turn's process group, but one
# that started a session of its own outlives both, so this caps what a turn
# pays to walk away from it (mirrors verification._ABANDONED_READER_JOIN_SECONDS).
_ABANDONED_READER_JOIN_SECONDS = 5.0

#: How often a turn with an abort signal attached re-checks its deadlines. The
#: wait is otherwise a single blocking call, so this is the whole cost of being
#: cancellable and the upper bound on how late a kill lands.
_ABORT_POLL_INTERVAL_SECONDS = 0.1

#: The four windows a cancellation request can land in, named on the result and
#: on the signal itself. A caller that only saw "aborted" could not tell a turn
#: that was killed mid-flight from one that never ran, and the two mean
#: different things to a continuation: nothing was submitted in the first two
#: windows, so there is no interrupted work to resume from.
CancelStage = Literal["unstarted", "handshake", "execution", "shutdown"]
#: Requested before the harness process was spawned. Nothing ever ran.
CANCEL_STAGE_UNSTARTED: CancelStage = "unstarted"
#: Requested while the turn's conversation was being opened, before its work was
#: submitted. The process existed; the turn did not.
CANCEL_STAGE_HANDSHAKE: CancelStage = "handshake"
#: Requested against a running turn. This is the window that kills a process
#: group and the only one that interrupts work in progress.
CANCEL_STAGE_EXECUTION: CancelStage = "execution"
#: Requested once the turn's outcome was already decided. Nothing was killed and
#: the verdict stands; the request is complete because the turn already is.
CANCEL_STAGE_SHUTDOWN: CancelStage = "shutdown"
CANCEL_STAGES: tuple[CancelStage, ...] = (
    CANCEL_STAGE_UNSTARTED,
    CANCEL_STAGE_HANDSHAKE,
    CANCEL_STAGE_EXECUTION,
    CANCEL_STAGE_SHUTDOWN,
)

#: Exit code for a turn abandoned because its conversation was not recorded.
#: Distinct from the cancellation and timeout codes because it is neither: the
#: turn was refused before it submitted anything, and a caller reading only the
#: return code still has to be able to tell that apart from a turn that ran.
_CONVERSATION_ABANDON_RETURNCODE = 70


@dataclass
class AbortSignal:
    """A request to kill one in-flight harness turn at a monotonic deadline.

    Armed from another thread; read by the turn's wait loop. It lives in this
    module because the kill has to happen where the turn's ``process`` is in
    scope: signalling a bare PID from outside would bypass the pre-kill
    descendant snapshot and the post-turn sweep, which is how a descendant that
    left the process group is reached at all.

    Initiation and completion are deliberately two different observations.
    :meth:`arm` records the request and returns; it takes this signal's own leaf
    lock and nothing else, so an initiation can never queue behind a guidance
    transport holding the live-delivery endpoint's lock. Whether the worker
    actually stopped is read afterwards through :meth:`wait_completed`, which
    the turn's owner satisfies only once the process is reaped and its transport
    is shut down.
    """

    event: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None
    reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _completed: threading.Event = field(default_factory=threading.Event, repr=False)
    _stage: CancelStage | None = None
    _turn_over: bool = False

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None:
        """Request the kill, giving the harness ``grace_seconds`` to wind down.

        Returns as soon as the request is recorded, whatever the turn is doing.
        A request that arrives after the turn is already over cannot revive it:
        it is recorded, staged as ``shutdown``, and completed on the spot, so a
        caller waiting on it is answered rather than left waiting for a kill
        that will never be issued.
        """
        with self._lock:
            self.deadline = time.monotonic() + max(0.0, grace_seconds)
            self.reason = reason
            already_over = self._turn_over
            if already_over and self._stage is None:
                self._stage = CANCEL_STAGE_SHUTDOWN
        self.event.set()
        if already_over:
            self._completed.set()

    @property
    def armed(self) -> bool:
        return self.event.is_set()

    def expires_at(self, now: float) -> float | None:
        """When the kill is due, or ``None`` while this signal is unarmed.

        An armed signal carrying no deadline is due immediately, so a caller
        that only sets the event still gets a kill.
        """
        if not self.event.is_set():
            return None
        with self._lock:
            deadline = self.deadline
        return now if deadline is None else deadline

    def is_due(self, now: float | None = None) -> bool:
        """Whether the kill is due, rather than merely requested.

        A request inside its grace period is not due: the grace exists so the
        harness gets to wind down and possibly finish on its own, and a turn
        that finishes inside it was never aborted. Every lifecycle checkpoint
        asks this rather than :attr:`armed`, so an unexpired request stops
        nothing that the wait loop would have let run.
        """
        moment = time.monotonic() if now is None else now
        deadline = self.expires_at(moment)
        return deadline is not None and moment >= deadline

    def begin_turn(self) -> None:
        """Bind this signal to a turn that is about to start.

        A signal belongs to one turn, but the codex adapter runs a second turn
        on the same one when an app-server handshake falls back to ``codex
        exec``. An unarmed signal therefore starts each turn with no stage and
        no completion; an armed one keeps both, because that request predates
        the turn and the spawn gate is about to refuse it rather than run it.
        """
        with self._lock:
            self._turn_over = False
            if not self.event.is_set():
                self._stage = None
                self._completed.clear()
            elif self._stage == CANCEL_STAGE_SHUTDOWN:
                # That stage belongs to the turn that just ended, not to the
                # one starting now; this turn names its own window.
                self._stage = None

    def observed(self, stage: CancelStage) -> None:
        """Name the window this request was acted on in. First naming wins."""
        with self._lock:
            if self._stage is None:
                self._stage = stage

    @property
    def cancel_stage(self) -> CancelStage | None:
        """The window the request was acted on in, or ``None`` if it never was."""
        with self._lock:
            return self._stage

    def release(self) -> None:
        """Report that the turn this signal governs has stopped.

        Called once by the boundary that owns the process, after the process is
        reaped and its transport is shut down, so completion means the worker is
        gone rather than that a kill was merely issued. It also closes the
        signal: any later :meth:`arm` completes immediately instead of arming a
        turn that no longer exists.
        """
        with self._lock:
            self._turn_over = True
            if self.event.is_set() and self._stage is None:
                self._stage = CANCEL_STAGE_SHUTDOWN
        self._completed.set()

    @property
    def completed(self) -> bool:
        """Whether the turn this signal governs has stopped."""
        return self._completed.is_set()

    def wait_completed(self, timeout: float | None = None) -> bool:
        """Wait out the turn, reporting whether it stopped within ``timeout``.

        The only blocking call on this signal, and the counterpart to
        :meth:`arm`: cancellation is initiated without waiting for anything and
        observed here, so the caller decides how long it is willing to wait for
        the worker to be gone.
        """
        return self._completed.wait(timeout)


def _wait_for_turn_exit(
    process: subprocess.Popen,
    *,
    timeout: float | None,
    abort: AbortSignal | None,
) -> Literal["timeout", "abort"] | None:
    """Wait for ``process``; name the deadline that fired, or ``None`` if it exited.

    The two deadlines are mutually exclusive: the earlier one is reported and
    the other is never looked at again. Without an abort signal this is the
    single blocking wait it replaced, so an uncancellable turn wakes no more
    often than it used to.
    """
    if abort is None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout"
        return None

    timeout_deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            return None
        now = time.monotonic()
        abort_deadline = abort.expires_at(now)
        abort_due = abort_deadline is not None and now >= abort_deadline
        timeout_due = timeout_deadline is not None and now >= timeout_deadline
        if abort_due and timeout_due:
            return "timeout" if timeout_deadline <= abort_deadline else "abort"
        if abort_due:
            return "abort"
        if timeout_due:
            return "timeout"
        interval = _ABORT_POLL_INTERVAL_SECONDS
        if abort_deadline is not None:
            interval = min(interval, abort_deadline - now)
        if timeout_deadline is not None:
            interval = min(interval, timeout_deadline - now)
        try:
            process.wait(timeout=max(interval, 0.0))
        except subprocess.TimeoutExpired:
            continue
        return None


def _sweep_process_table() -> dict[int, dict[str, Any]] | None:
    """The table to snapshot descendants from, or ``None`` when sweeping is off."""
    if not sweep_enabled():
        return None
    return sample_process_table()


def _snapshot_descendants(
    root_pid: int, table: Mapping[int, Mapping[str, Any]] | None
) -> tuple[Mapping[str, Any], ...]:
    """Record ``root_pid``'s descendants while it is still alive.

    Killing the group reparents an escapee to pid 1, after which no descendant
    walk can reach it. A snapshot taken beforehand is the sweep's only route to
    one, so this must run before the kill, not after it.
    """
    if table is None:
        return ()
    return tuple(table[pid] for pid in descendant_pids(root_pid, table))


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill ``process`` and every descendant still in its process group.

    Harness turns spawn with ``start_new_session=True``, so the child's process
    group ID is its PID and every descendant inherits that group unless it
    starts a session of its own. Killing only the direct child leaves a harness
    that fanned out into helper processes running, still holding the captured
    pipes open (mirrors :func:`verification._kill_process_group`).
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        # The group is already empty, or the child left it for one of its own.
        with contextlib.suppress(OSError):
            process.kill()


def _sweep_turn_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any] | None:
    """Kill whatever outlived the turn, or ``None`` when the sweep is off.

    ``expected_pgid`` is the child's PID because turns spawn with
    ``start_new_session=True``. ``root_pid`` is already reaped by the caller, so
    the sweep excludes it rather than signalling a PID that may be reused.

    Any tree the hang sentinel sampled for this same root is unioned into the
    snapshot: on the paths that take no pre-kill snapshot of their own it is
    the sweep's only route to a descendant that left the process group, and it
    exists exactly for the silent turns where such a descendant is suspected.
    It is consumed here, so a later turn that reuses this pid starts with no
    sample rather than one describing a tree that is gone.
    """
    try:
        if not sweep_enabled():
            return None
        return sweep_survivors(
            root_pid=process.pid,
            expected_pgid=process.pid,
            prior_snapshot=(*prior_snapshot, *last_process_tree(process.pid)),
        )
    finally:
        clear_process_tree(process.pid)


def _join_readers(
    readers: tuple[threading.Thread, ...], *, timeout: float | None
) -> None:
    """Join the pipe readers, optionally under a shared deadline."""
    if timeout is None:
        for reader in readers:
            reader.join()
        return
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


class HarnessError(Exception):
    """Raised when a harness adapter cannot build or run a command."""


class ConversationCaptureError(HarnessError):
    """Raised when a turn's native conversation could not be recorded.

    The one failure a transport may not work around. Continuity requires the
    native identifier to be durable *before* work is submitted, so a transport
    that cannot record it has to abandon the turn rather than run one that
    could never be continued.
    """


class TurnCancelled(HarnessError):
    """Raised inside a transport when the turn it is opening was cancelled.

    Never escapes :meth:`HarnessAdapter.run`, which turns it into a cancelled
    result. It exists so a handshake can stop at its next checkpoint without
    submitting anything, which is the difference between a cancellation that
    costs nothing and one that has to interrupt work already in flight.
    """


#: What a turn calls with the native conversation identifier its harness
#: reported. Unlike ``on_spawn`` and the other turn hooks, an exception from
#: this one is *not* suppressed: it is the persistence failure that must stop
#: the turn before it submits work.
ConversationCallback = Callable[[str], None]


class TurnConversation:
    """One turn's conversation-capture seam.

    A protocol harness learns its native conversation identifier during the
    handshake, before the turn's work is submitted, and that ordering is the
    whole point: a conversation recorded afterwards would be one a crash in
    between could lose while the work went ahead. A transport therefore reports
    the identifier here and only submits if this call returns.

    Turn-scoped by construction, exactly like
    :class:`~agent_session_loop.guidance_delivery.LiveDeliveryEndpoint`: the
    lock is held across the sink call, so :meth:`close` cannot return while a
    capture is still running and no capture can start after it returns. A late
    callback from an abandoned reader or a straggling protocol thread therefore
    cannot persist a conversation for a turn that is already over.
    """

    def __init__(self, sink: ConversationCallback | None = None) -> None:
        self._sink = sink
        self._requested = sink is not None
        self._lock = threading.Lock()
        self._native_id: str | None = None
        self._closed = False

    @property
    def requested(self) -> bool:
        """Whether this turn's caller asked to be told the conversation at all.

        Fixed at construction rather than read off the sink, which :meth:`close`
        drops: a turn that ended still has to be able to say whether continuity
        was ever wanted, because "none was captured" only means something was
        lost when someone was waiting to hear it.
        """
        return self._requested

    @property
    def native_id(self) -> str | None:
        """The conversation this turn runs on, or ``None`` if none was captured."""
        with self._lock:
            return self._native_id

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def capture(self, native_id: str) -> None:
        """Record ``native_id`` durably, or raise so nothing is submitted.

        Idempotent for the identifier already captured, because a transport may
        see the same conversation named more than once in its protocol. A
        *different* identifier is refused: one turn runs on one conversation,
        and continuing the wrong one is the failure this whole seam exists to
        prevent.
        """
        if not isinstance(native_id, str) or not native_id.strip():
            raise ConversationCaptureError(
                f"a native conversation id must be non-empty text; got {native_id!r}"
            )
        native_id = native_id.strip()
        with self._lock:
            if self._closed:
                raise ConversationCaptureError(
                    f"the turn is over; refusing to record conversation {native_id}"
                )
            recorded = self._native_id
            if recorded is not None:
                if recorded == native_id:
                    return
                raise ConversationCaptureError(
                    f"this turn already runs on conversation {recorded}; "
                    f"refusing to also record {native_id}"
                )
            sink = self._sink
            if sink is not None:
                try:
                    sink(native_id)
                except Exception as error:
                    raise ConversationCaptureError(
                        f"conversation {native_id} was not persisted: "
                        f"{type(error).__name__}: {error}"
                    ) from error
            self._native_id = native_id

    def close(self) -> None:
        """Close the seam, waiting out a capture already in flight."""
        with self._lock:
            self._closed = True
            self._sink = None


#: What a turn calls once its prompt has actually reached the worker. Unlike
#: :data:`ConversationCallback`, an exception from this one *is* suppressed:
#: by the time it fires the words are already in flight, so there is nothing
#: left for a failure here to stop.
SubmissionCallback = Callable[[], None]


class TurnSubmission:
    """One turn's prompt-submission seam.

    Spawning a process is not submitting a prompt. A one-shot turn carries its
    prompt on argv, so for it the two coincide; a protocol turn spawns, then
    handshakes, and only then sends the prompt -- and everything in between can
    refuse, be cancelled, or fail. Whoever is holding the operator's words needs
    to know which of the two happened, because words consumed by a worker that
    never received them are words the operator silently lost.

    Turn-scoped and locked exactly like :class:`TurnConversation`: the lock is
    held across the sink call, so :meth:`close` cannot return while a
    notification is still running and none can start after it returns. A
    straggling protocol thread therefore cannot report a submission for a turn
    that is already over.

    The seam has three states rather than two, because a prompt on its way out
    is neither. A transport that has begun handing the words over cannot take
    them back and cannot yet say they arrived, and that window is long enough to
    lose a process in: see :attr:`verdict`.
    """

    def __init__(self, sink: SubmissionCallback | None = None) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._submitted = False
        self._in_flight = False
        self._closed = False

    @property
    def submitted(self) -> bool:
        """Whether this turn's prompt reached the worker."""
        with self._lock:
            return self._submitted

    @property
    def verdict(self) -> bool | None:
        """What this seam is willing to say about the prompt: sent, not, unknown.

        ``True`` and ``False`` are both statements about the turn. ``None`` is
        the refusal to make one, and it is reached the moment a transport says
        the words are on the wire: from there a silence has two readings -- the
        server never got them, or it got them and whatever would have said so
        did not survive -- and the seam has no way to choose between them.

        Closing does not resolve it. That is the point: a turn that ends in this
        window ends unanswered, so nothing downstream can read the end of the
        turn as proof the prompt never went out.
        """
        with self._lock:
            if self._submitted:
                return True
            return None if self._in_flight else False

    def note_submission_in_flight(self) -> None:
        """Record that the prompt is going out and can no longer be recalled.

        No sink fires here, because nothing has been received: a caller told
        "delivered" on this would be claiming a worker holds words that may
        still be sitting in a pipe. What changes is only what this seam will
        claim afterwards, which is nothing at all.
        """
        with self._lock:
            if self._closed or self._submitted:
                return
            self._in_flight = True

    def note_submitted(self) -> None:
        """Report that the prompt is the worker's now, at most once.

        The sink's failure is swallowed rather than raised: the prompt has
        already gone out, and a caller told "that did not happen" would withdraw
        words a worker is reading. Idempotent because the fact is about the
        prompt, not about the attempt that carried it.
        """
        with self._lock:
            if self._closed or self._submitted:
                return
            self._submitted = True
            sink = self._sink
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink()

    def close(self) -> None:
        """Close the seam, waiting out a notification already in flight."""
        with self._lock:
            self._closed = True
            self._sink = None


class _SubmissionOnce:
    """A caller's submission callback, reached from both sides of a turn.

    An adapter that cannot state the submission from inside the turn has to be
    able to state it after -- past the point where :class:`TurnSubmission` has
    closed itself against exactly that. Two reporters means the same callback
    can be reached twice, and a caller's callback is a durable write, so the
    deduplication belongs here rather than in each caller.

    :attr:`reported` is what the late reporter reads: not "did this turn
    submit", which the result answers, but "has the caller already been told",
    which is the only question a second report has to ask.
    """

    def __init__(self, sink: SubmissionCallback | None) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._reported = False

    @property
    def reported(self) -> bool:
        """Whether the caller has been told this turn's prompt went out."""
        with self._lock:
            return self._reported

    def __call__(self) -> None:
        with self._lock:
            if self._reported:
                return
            self._reported = True
            sink = self._sink
        if sink is None:
            return
        # Swallowed for the reason :meth:`TurnSubmission.note_submitted` gives:
        # the prompt is out, and a caller told otherwise withdraws words a
        # worker is already reading.
        with contextlib.suppress(Exception):
            sink()


@dataclass(frozen=True)
class TurnLifecycle:
    """What a transport may say and ask about the turn it is opening.

    Handed to :meth:`HarnessAdapter.open_transport` so a protocol conversation
    has somewhere to report its native identifier, somewhere to look before it
    submits, and somewhere to say that it did. All three halves are turn-scoped
    and all three are closed by the boundary that owns the process, which is
    what keeps a late protocol callback from reviving a turn that has already
    ended.
    """

    conversation: TurnConversation
    abort: AbortSignal | None = None
    submission: TurnSubmission = field(default_factory=TurnSubmission)

    @property
    def cancel_due(self) -> bool:
        """Whether a cancellation is due for this turn.

        Due, not merely requested: a request still inside its grace period is
        one the wait loop would let the turn finish under, and a checkpoint
        that stopped on it would abort turns that used to complete.
        """
        return self.abort is not None and self.abort.is_due()

    @property
    def cancel_stage(self) -> CancelStage | None:
        """Which window this turn's cancellation was acted on in, if any."""
        return None if self.abort is None else self.abort.cancel_stage

    def check_not_cancelled(self) -> None:
        """Stop the handshake if a cancellation is already due.

        A transport calls this at every point where it is about to submit
        something. Raising here means the turn is abandoned without work having
        been sent, so there is nothing to interrupt and nothing to continue.
        """
        abort = self.abort
        if abort is not None and abort.is_due():
            raise TurnCancelled(abort.reason or "cancellation requested")

    @property
    def wants_conversation(self) -> bool:
        """Whether this turn's caller asked which conversation it runs on."""
        return self.conversation.requested

    def capture_conversation(self, native_id: str) -> None:
        """Record the turn's native conversation; raises if it was not stored."""
        self.conversation.capture(native_id)

    @property
    def prompt_submitted(self) -> bool | None:
        """What this turn can prove about its prompt: sent, not sent, unknown.

        Three-valued because :class:`TurnSubmission` is: a turn stopped while
        its prompt was crossing the wire answers ``None``, which is the only
        honest answer and the one a caller holding an operator's words has to be
        given rather than a ``False`` it would act on.
        """
        return self.submission.verdict

    def note_submission_in_flight(self) -> None:
        """Report that the prompt is going out, before it can be observed to.

        Called by a transport at the last instant it can still say nothing has
        been sent -- immediately before the write, not after it, because a write
        that fails partway through has already put some of the prompt on the
        wire. From here the turn proves nothing about its prompt either way.
        """
        self.submission.note_submission_in_flight()

    def note_prompt_submitted(self) -> None:
        """Report that the prompt has been submitted, and is now the worker's.

        Called at each transport's own submission point -- the spawn for a turn
        whose prompt is argv, the accepted ``turn/start`` or the written
        ``session/prompt`` for one that speaks a protocol. Never before: every
        checkpoint above it exists so a turn can still be declined, and a
        decline after this one would be withdrawing work already in flight.
        """
        self.submission.note_submitted()

    def refuse_submission(self) -> None:
        """Stop a handshake here, naming this window rather than a later one.

        The two calls are one act: a transport whose handshake runs on its own
        thread would otherwise have its cancellation named by the wait loop,
        which can only see ``execution`` -- the window that means work was
        interrupted, and precisely what stopping here avoided.
        """
        if self.cancel_due:
            self.note_cancelled(CANCEL_STAGE_HANDSHAKE)
        self.check_not_cancelled()

    def begin(self) -> None:
        """Bind the lifecycle to a turn that is about to start."""
        if self.abort is not None:
            self.abort.begin_turn()

    def note_cancelled(self, stage: CancelStage) -> None:
        """Name the window a pending cancellation was acted on in."""
        if self.abort is not None:
            self.abort.observed(stage)

    def close(self) -> None:
        """End the turn: no further capture, and any request is complete.

        Ordered so completion is the last thing that happens. The conversation
        and submission seams close first, so nothing can still be recorded for
        this turn, and the cancellation is reported complete only once there is
        nothing left of the turn at all.
        """
        self.conversation.close()
        self.submission.close()
        if self.abort is not None:
            self.abort.release()


@dataclass(frozen=True)
class AccessProfile:
    """What one harness turn is allowed to reach.

    This is deliberately separate from ``unsafe_bypass``: bypass widens a worker
    turn, while the profile decides which access model the turn runs under at
    all. Folding the audit profile into the bypass flag would make "not
    bypassed" mean two different things and let a worker default leak into an
    audit.
    """

    name: AccessProfileName
    read_roots: tuple[Path, ...] = ()

    @property
    def is_audit(self) -> bool:
        return self.name == "audit-read-only"


#: The profile every implementation and review turn runs under.
WORKER_ACCESS = AccessProfile(name="worker")


def audit_read_only_access(read_roots: tuple[Path, ...] | list[Path]) -> AccessProfile:
    """Build the restricted profile an auditor runs under.

    ``read_roots`` are the directories the auditor may read outside its own
    working directory — the evidence bundle and the read-only ASL and skill
    trees. They are never writable roots, and the application workspace is
    never among them.
    """
    return AccessProfile(name="audit-read-only", read_roots=tuple(read_roots))


def audit_read_only_unavailable_reason(name: str) -> str | None:
    """Say why ``name`` cannot run an audit read-only turn, or ``None``.

    A harness that cannot enforce the profile must be declared unavailable
    rather than run under a weaker one, so this returns a reason instead of a
    fallback.
    """
    try:
        adapter = get_adapter(name)
    except HarnessError:
        return "unsupported-harness"
    if not adapter.enforces_audit_read_only:
        return "profile-unenforceable"
    if adapter.resolve_executable() is None:
        return "harness-not-installed"
    return None


#: Transport discriminators. ``argv`` alone stopped describing a turn once a
#: harness could be driven over a protocol instead of a command line, so every
#: invocation and every result now says which of the two it was.
TRANSPORT_EXEC = codex_app_server.TRANSPORT_EXEC
TRANSPORT_APP_SERVER = codex_app_server.TRANSPORT_APP_SERVER
TRANSPORT_STREAM_JSON = claude_stream.TRANSPORT_STREAM_JSON
TRANSPORT_ACP = copilot_acp.TRANSPORT_ACP


@dataclass(frozen=True)
class HarnessInvocation:
    """A concrete subprocess invocation for one harness turn.

    ``argv`` remains the invocation's identity for every caller that had one.
    Under a protocol transport it says only which binary is spawned, and
    ``turn_plan`` carries what argv used to: the access profile, the writable
    roots, the model, and the effort. Neither ever carries the prompt.
    """

    name: HarnessName
    argv: list[str]
    cwd: Path
    last_message_path: Path | None = None
    model: str | None = None
    effort: str | None = None
    transport: str = TRANSPORT_EXEC
    turn_plan: codex_app_server.TurnPlan | None = None
    #: The turn's ephemeral harness-configuration directory, when it has one.
    #: It holds settings a turn needs and must not outlive — a hook overlay and
    #: its guidance spool — and is created by ``run`` and deleted by it, so a
    #: turn's private channel cannot be reached from the next turn.
    overlay_dir: Path | None = None
    #: The native conversation this turn intends to continue, when the caller
    #: has one to continue. Intent only: an adapter that cannot honour it
    #: refuses the turn rather than quietly starting a fresh conversation, so
    #: this never silently degrades into "resumed nothing".
    resume_conversation_id: str | None = None
    #: The native conversation this turn will actually run on, when its
    #: transport states that on the command line instead of learning it from a
    #: handshake. Distinct from ``resume_conversation_id``, which is a request:
    #: this is the identity, whether freshly minted or restored, and it is
    #: known before the process is spawned — which is what lets such a turn
    #: persist its conversation before submitting any work.
    assigned_conversation_id: str | None = None


@dataclass(frozen=True)
class HarnessResult:
    """Captured result from a single harness turn."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    last_message: str = ""
    name: HarnessName | None = None
    timed_out: bool = False
    aborted: bool = False
    interrupted: bool = False
    process_sweep: dict[str, Any] | None = None
    observed_model: str | None = None
    stream_io: dict[str, int | float] | None = None
    transport: str = TRANSPORT_EXEC
    #: The native conversation this turn actually ran on, when its transport
    #: reported one, or ``None`` for a turn that never named one — because its
    #: transport cannot report one, because it stopped before its handshake got
    #: there, or because its output never stated it.
    conversation_id: str | None = None
    #: Why this turn has no conversation to continue, when one was asked for
    #: and none could be had. Set only where the work ran anyway: a legacy
    #: one-shot turn submits its prompt on argv, so continuity can only be
    #: observed after the fact and its absence cannot retract the turn. A turn
    #: that could have refused instead reports ``conversation_error``.
    continuity_unavailable: str | None = None
    #: Which window a cancellation was acted on in, or ``None`` when the turn
    #: was not cancelled. Distinguishes the cancellations that cost nothing
    #: (``unstarted``, ``handshake``) from the ones that had to interrupt work
    #: already submitted (``execution``, ``shutdown``).
    cancel_stage: CancelStage | None = None
    #: Why the turn refused to submit work, when a conversation could not be
    #: recorded. Present only on a turn abandoned by that failure.
    conversation_error: str | None = None
    #: Why the provider would not restore the native conversation this turn was
    #: told to continue. Set only when the provider itself refused the restore
    #: request -- the history is gone on its side, so no later launch can
    #: continue it either. Settings drift, a substituted conversation, and a
    #: transport that cannot restore are all excluded: those leave the history
    #: intact and are ordinary failures of this turn.
    conversation_restore_refused: str | None = None
    #: What the turn's own submission seam says about whether its prompt went
    #: out: ``True`` submitted, ``False`` ended without submitting, ``None`` no
    #: statement either way. The three values are not two, and ``None`` is
    #: reached from both ends: a turn whose process died before a result could
    #: be assembled leaves the field where it started, and a turn stopped while
    #: its prompt was crossing the wire has a seam that deliberately refuses to
    #: answer. Only a turn that *declined* reads ``False``, which is why the
    #: absence of a statement is its own value rather than a default of
    #: ``False``.
    prompt_submitted: bool | None = None


def prompt_provably_unsubmitted(result: HarnessResult) -> bool:
    """Whether this result *proves* its turn put no prompt in front of a worker.

    Not the negation of :attr:`TurnSubmission.submitted` as a caller observes
    it. A caller sees that flag only through a callback, and the absence of a
    callback covers two very different things: a turn that refused before
    sending anything, and a turn that sent something and then lost the thread
    that would have said so. A caller holding an operator's words has to tell
    those apart, because acting on the second as if it were the first hands the
    same words to a second worker.

    ``prompt_submitted`` is the whole answer wherever there is one: the adapter
    stamps it from the submission seam as it returns, so it reports what this
    turn actually did rather than what its exit path implies. That matters in
    both directions -- it covers the protocol handshakes that fail without a
    refusal field of their own, a provider that was merely unreachable most of
    all, and it is the only field that knows a one-shot turn submitted its
    prompt on argv before the window its cancellation was named for. A seam that
    answers ``None`` has answered: a turn stopped with its prompt on the wire
    proves nothing, and that is the answer, not a missing one.

    A result nothing stamped falls back to what a turn sets when it *declines*,
    each written on a path that has not sent a prompt -- and each, for that
    reason, unreachable once a transport has begun writing the prompt out, so
    reading them under a ``None`` seam cannot resurrect the proof the seam
    withheld:

    - a cancellation acted on in the ``unstarted`` or ``handshake`` window, the
      two windows :data:`CancelStage` documents as costing nothing;
    - ``conversation_error``, set only where a turn refused to submit because it
      could not record the conversation it would have run on;
    - ``conversation_restore_refused``, set only where the provider declined to
      restore the history the prompt was going to continue.

    Everything else -- a nonzero exit, a timeout, a transport that died at an
    unknown point, and every result assembled by something other than an
    adapter -- answers ``False``, which means "not proven", not "it was
    submitted". The caller's job on ``False`` is to leave the question open.
    """
    if result.prompt_submitted is not None:
        return not result.prompt_submitted
    if result.cancel_stage in (CANCEL_STAGE_UNSTARTED, CANCEL_STAGE_HANDSHAKE):
        return True
    if result.conversation_error is not None:
        return True
    return result.conversation_restore_refused is not None


class _OutputProcessor:
    """Normalize harness output before it is logged and persisted."""

    @property
    def final_result_seen(self) -> bool:
        """Whether the current request has emitted a terminal result record.

        Only the claude stream-json processor can report one; processors that
        do not parse structured output never do, which correctly disables any
        result-based detection for their harnesses. A processor that can carry
        more than one request resets this at the next request boundary.
        """
        return False

    @property
    def observed_model(self) -> str | None:
        """The model the harness reported it actually executed with.

        Only the claude stream-json processor can report one; processors that
        do not parse structured output never do, which correctly leaves the
        observed model unknown for their harnesses.
        """
        return None

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        del stream_name
        return text

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        del stderr
        return _read_last_message(last_message_path) or stdout


class _ClaudeStreamJsonOutputProcessor(_OutputProcessor):
    """Render Claude stream-json records as readable line-oriented progress.

    Also the turn's only reader of the CLI's protocol acknowledgements: when a
    session is attached, the terminal ``result`` records and the ``user`` echoes
    are reported to it. Rendering and acknowledgement share this one parse
    because there is only ever one reader of the stream.
    """

    def __init__(
        self, session: claude_stream.ClaudeStreamSession | None = None
    ) -> None:
        self._session = session
        self._text_buffer = ""
        self._emitted_text = False
        self._assistant_messages: list[str] = []
        self._final_result: str | None = None
        self._final_result_seen = False
        self._observed_model: str | None = None

    @property
    def final_result_seen(self) -> bool:
        return self._final_result_seen

    @property
    def observed_model(self) -> str | None:
        return self._observed_model

    def _note_observed_model(self, model: object) -> None:
        # First wins: a resumed session's later records can name a different
        # model than the one this turn started under.
        if self._observed_model is None and isinstance(model, str) and model:
            self._observed_model = model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text

        rendered: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                rendered.append(line)
                continue
            if not isinstance(record, dict):
                rendered.append(line)
                continue
            rendered.extend(self._render_record(record))
        return "".join(f"{line}\n" for line in rendered)

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        saved = _read_last_message(last_message_path)
        if saved:
            return saved
        if self._final_result:
            return self._ensure_trailing_newline(self._final_result)
        if self._assistant_messages:
            return self._ensure_trailing_newline("\n".join(self._assistant_messages))
        del stderr
        return stdout

    def _render_record(self, record: dict[str, object]) -> list[str]:
        record_type = record.get("type")
        if record_type == "system":
            return self._render_system(record)
        if record_type == "stream_event":
            event = record.get("event")
            if isinstance(event, dict):
                return self._render_stream_event(event)
            return []
        if record_type == "assistant":
            text = self._assistant_text(record.get("message"))
            if text:
                self._assistant_messages.append(text)
                if not self._emitted_text:
                    return self._format_text_lines(text)
            return []
        if record_type == "user":
            # The CLI echoes a user message back when it takes it up, which is
            # the protocol acknowledgement a stdin write on its own is not.
            # Never rendered: an echo carries the prompt or a guidance body,
            # and neither belongs in a turn log.
            if self._session is not None:
                self._session.note_user_echo(
                    self._assistant_text(record.get("message"))
                )
            self._final_result_seen = False
            return []
        if record_type == claude_stream.CONTROL_RESPONSE:
            # Protocol bookkeeping rather than turn output: the session
            # correlates it by ``request_id`` and nothing about it belongs in a
            # turn log, where an operator reading arrival order would be
            # guessing at the very attribution the identifier settles.
            if self._session is not None:
                self._session.note_control_response(record.get("response"))
            return []
        if record_type == "result":
            self._final_result_seen = True
            if self._session is not None:
                origin = record.get("origin")
                self._final_result_seen = self._session.note_result(
                    background_followup=(
                        isinstance(origin, dict)
                        and origin.get("kind") == "task-notification"
                    ),
                    is_error=record.get("is_error") is True,
                )
            result = record.get("result")
            if isinstance(result, str):
                self._final_result = result
            subtype = record.get("subtype") or "unknown"
            details = self._result_details(record)
            if record.get("is_error") is True:
                details.insert(0, f"provider subtype {subtype}")
                suffix = "" if not details else f" ({', '.join(details)})"
                message = self._result_error_message(result)
                return [f"claude result: error{suffix}: {message}"]
            suffix = "" if not details else f" ({', '.join(details)})"
            return [f"claude result: {subtype}{suffix}"]
        if record_type == "error":
            message = record.get("message") or record.get("error") or "unknown"
            return [f"claude error: {message}"]
        return []

    def _render_system(self, record: dict[str, object]) -> list[str]:
        subtype = record.get("subtype")
        if subtype in ("task_started", "task_notification", "task_progress"):
            task_id = record.get("task_id")
            if isinstance(task_id, str) and task_id:
                self._final_result_seen = False
                if self._session is not None and subtype != "task_progress":
                    self._session.note_background_task(
                        task_id, finished=subtype == "task_notification"
                    )
                # Also invalidates a prior result in the heartbeat sentinel.
                # Do not render task output paths, summaries, or identifiers.
                return ["claude status: requesting"]
        if subtype == "init":
            # A long-lived CLI can emit another initialization after an early
            # result. From this point that result belongs to the prior request,
            # so the reader may notify the next terminal result independently.
            self._final_result_seen = False
            parts = ["claude init"]
            model = record.get("model")
            if model:
                self._note_observed_model(model)
                parts.append(f"model {model}")
            session_id = record.get("session_id")
            mismatch: str | None = None
            if session_id:
                parts.append(f"session {session_id}")
                if self._session is not None:
                    mismatch = self._session.note_session_id(str(session_id))
            rendered = ["; ".join(str(part) for part in parts)]
            if mismatch is not None:
                # Named on its own line rather than folded into the init line:
                # this is the one thing in that record an operator has to act
                # on, and it says the turn's recorded conversation is not the
                # one the work is about to go into.
                rendered.append(f"claude session mismatch: {mismatch}")
            return rendered
        if subtype == "status":
            status = record.get("status") or "unknown"
            if status == "requesting":
                self._final_result_seen = False
            return [f"claude status: {status}"]
        return []

    def _render_stream_event(self, event: dict[str, object]) -> list[str]:
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("model"):
                self._note_observed_model(message["model"])
                return [f"claude response: model {message['model']}"]
            return ["claude response started"]
        if event_type == "content_block_start":
            content_block = event.get("content_block")
            if (
                isinstance(content_block, dict)
                and content_block.get("type") == "tool_use"
            ):
                name = content_block.get("name") or "unknown"
                return [f"claude tool: {name}"]
            return []
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    self._text_buffer += text
                    return self._drain_text_buffer(force=False)
            return []
        if event_type == "content_block_stop":
            return self._drain_text_buffer(force=True)
        return []

    def _drain_text_buffer(self, *, force: bool) -> list[str]:
        lines: list[str] = []
        while "\n" in self._text_buffer:
            line, self._text_buffer = self._text_buffer.split("\n", 1)
            if line.strip():
                lines.append(f"claude text: {line}")
        if force and self._text_buffer.strip():
            lines.append(f"claude text: {self._text_buffer}")
            self._text_buffer = ""
        if lines:
            self._emitted_text = True
        return lines

    def _format_text_lines(self, text: str) -> list[str]:
        lines = [f"claude text: {line}" for line in text.splitlines() if line.strip()]
        if lines:
            self._emitted_text = True
        return lines

    def _assistant_text(self, raw_message: object) -> str:
        if not isinstance(raw_message, dict):
            return ""
        content = raw_message.get("content")
        if isinstance(content, str):
            # User echoes may carry bare text where an assistant message
            # always carries blocks.
            return content.strip()
        if not isinstance(content, list):
            return ""
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
        return "".join(chunks).strip()

    def _result_details(self, record: dict[str, object]) -> list[str]:
        details: list[str] = []
        turns = record.get("num_turns")
        if isinstance(turns, int):
            label = "turn" if turns == 1 else "turns"
            details.append(f"{turns} {label}")
        duration_ms = record.get("duration_ms")
        if isinstance(duration_ms, int | float):
            details.append(f"{duration_ms / 1000:.1f}s")
        return details

    def _result_error_message(self, result: object) -> str:
        """Render one bounded line from a terminal error result."""
        if not isinstance(result, str) or not result.strip():
            return "unknown"
        message = " ".join(result.split())
        if len(message) > 500:
            return f"{message[:499].rstrip()}…"
        return message

    def _ensure_trailing_newline(self, text: str) -> str:
        return text if text.endswith("\n") else f"{text}\n"


class HarnessTransport:
    """The in-turn conversation an adapter has with the process it spawned.

    A one-shot harness has none: argv said everything, and stdout is a report
    rather than a dialogue. A protocol harness has exactly one, opened right
    after the process exists and released once it is gone, and it is the only
    thing in the turn that may write to that process's stdin.

    The four methods are what the generic turn loop needs to know about a
    conversation it otherwise ignores: how to read the process's output, how
    (or whether) to inject guidance into it, how to stop the work in flight,
    and how to let go at the end.
    """

    def output_processor(self) -> _OutputProcessor:
        """The processor that turns this transport's stdout into log text."""
        raise NotImplementedError

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        """The live-delivery channel, or ``None`` when there is none."""
        return None

    def interrupt(self) -> bool:
        """Tell this turn's conversation to stop the work it has in flight.

        ``False`` -- the base answer -- means the transport has no native
        interruption at all, which is the honest report for a one-shot harness:
        stopping it *is* the process kill and nothing else. A transport that
        returns ``True`` has told its harness to stop; it has not waited for it
        to have stopped, which is what the forced termination behind this call
        is for.
        """
        return False

    def shutdown(self) -> None:
        """Release the conversation. Called once, on every path out of a turn."""


def _request_native_interrupt(transport: HarnessTransport | None) -> None:
    """Tell a protocol harness its turn was interrupted, without waiting for it.

    Issued on the way to the forced kill, never instead of it. The termination
    contract is that the deadline fires independently of any blocked protocol
    call, so this runs on a daemon thread the kill does not join: at worst the
    interrupt is still in flight when the process group dies under it, which is
    the same outcome as not sending one.

    It exists because a killed pipe says nothing. An app-server that records
    its own thread state ends the turn as *interrupted* when told so, and as a
    turn that simply stopped answering when it is only killed -- and a later
    turn resuming that conversation reads the difference.
    """
    if transport is None:
        return

    def send() -> None:
        with contextlib.suppress(Exception):
            transport.interrupt()

    threading.Thread(target=send, name="harness-interrupt", daemon=True).start()


class HarnessAdapter:
    """Base class for harness-specific command construction."""

    name: HarnessName
    skill_prefix: str

    #: Whether this adapter can express the audit read-only profile in argv
    #: alone. An adapter that cannot must leave this false so callers declare
    #: the audit unavailable instead of running it with worker access.
    enforces_audit_read_only: bool = False

    #: Whether guidance reaches this harness by riding the next turn's prompt
    #: instead of by a same-turn steer. True only for a harness whose protocol
    #: has no in-turn channel at all, so the prompt genuinely is the delivery
    #: and can be recorded as one. A harness that *can* be steered must leave
    #: this false even when a particular attempt defers: there, the prompt is
    #: a fallback and the ledger already has the live attempt's own verdict.
    delivers_guidance_next_turn: bool = False

    #: Whether this adapter arms an ASL-owned pre-tool hook that reads the
    #: run's guard policy and can refuse a call before it executes. False is
    #: the honest default: a harness that exposes no hook ASL can arm without
    #: touching the operator's own configuration has no pre-tool lane, and
    #: coverage must say so rather than imply the guard is enforced twice.
    enforces_guard_pretool: bool = False

    #: Whether this adapter's transport reports the native conversation its
    #: turn runs on. False until a protocol path actually produces an
    #: identifier: the seam alone is not capability.
    captures_conversations: bool = False

    #: Whether this adapter can start a turn on a conversation it is handed.
    #: :meth:`run` refuses a resume intent while this is false — an adapter that
    #: quietly started a fresh conversation instead would leave the caller
    #: believing it had continued one.
    resumes_conversations: bool = False

    def __init__(self, executable: str | None = None) -> None:
        try:
            self.executable = executable or config.configured_harness_executable(
                self.name
            )
        except config.ConfigError as error:
            raise HarnessError(str(error)) from error

    def resolve_executable(self) -> str | None:
        """Return the resolved executable path if it is on PATH."""
        return shutil.which(self.executable)

    @staticmethod
    def _with_stderr_note(result: HarnessResult, note: str) -> HarnessResult:
        """Append one adapter-authored line to the turn's stderr.

        The stderr artifact is written from ``result.stderr`` while
        ``stream_io`` counts what the readers saw, and the two are required to
        agree byte for byte. A note added here is stderr the turn really has,
        so it is accounted for as stderr rather than smuggled in beside it.
        """
        text = f"{note}\n"
        stream_io = dict(result.stream_io or {})
        if stream_io:
            stream_io["stderr_chars"] = int(stream_io.get("stderr_chars", 0)) + len(
                text
            )
            stream_io["stderr_chunks"] = int(stream_io.get("stderr_chunks", 0)) + 1
        return dataclasses.replace(
            result,
            stderr=result.stderr + text,
            stream_io=stream_io or result.stream_io,
        )

    def output_processor(self) -> _OutputProcessor:
        return _OutputProcessor()

    def _worker_plan_dir(self, plan_dir: Path | None) -> str:
        """The worker's writable grant, refusing to stringify a missing one.

        Every worker branch renders ``plan_dir`` through here so a ``None``
        cannot reach argv as the literal ``"None"`` — a path the harness would
        create rather than reject.
        """
        if plan_dir is None:
            raise HarnessError(f"{self.name} worker turns require a plan_dir")
        return str(plan_dir)

    def check_access(
        self,
        access: AccessProfile,
        *,
        plan_dir: Path | None,
        unsafe_bypass: bool,
    ) -> None:
        """Reject a turn whose access profile this adapter will not honor.

        Raising here rather than dropping a flag is the point: a misconfigured
        audit must fail loudly instead of quietly running with worker access.
        """
        if not access.is_audit:
            self._worker_plan_dir(plan_dir)
            return
        if unsafe_bypass:
            raise HarnessError(
                f"{self.name} audit turns cannot inherit unsafe bypass access"
            )
        if plan_dir is not None:
            raise HarnessError(
                f"{self.name} audit turns take read roots, not a writable plan_dir"
            )
        if not self.enforces_audit_read_only:
            raise HarnessError(
                f"{self.name} cannot enforce the audit read-only access profile"
            )

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def _guard_policy(self, env: Mapping[str, str] | None) -> dict[str, Any] | None:
        """The run's derived guard policy, as the turn environment points at it.

        The environment is the only place argv construction can learn about
        guards: the controller writes the policy atomically and exports its
        path, so an adapter that builds a command line reads the same document
        the ``PATH`` shim and the pre-tool hook read. ``None`` means there is
        nothing to compile — no guards, no policy, or a policy this process
        cannot read — and callers must degrade to the postflight lane rather
        than refuse the turn.
        """
        if not env:
            return None
        policy_path = env.get(guards.GUARD_POLICY_ENV_VAR)
        if not policy_path:
            return None
        try:
            document = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        return document if isinstance(document, dict) else None

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        return HarnessInvocation(
            name=self.name,
            argv=self.build_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                env=env,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            resume_conversation_id=resume_conversation_id,
        )

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        """Open this turn's protocol conversation, if the transport has one.

        ``None`` — the base implementation — is the one-shot case: argv said
        everything, and the turn is a process to watch rather than a dialogue
        to hold. Called once, immediately after the process exists and before
        the output readers start, so a conversation can be underway by the
        time the first byte arrives.

        ``lifecycle`` is this turn's handle on the two things a handshake has
        to be able to do: report the native conversation it just learned, and
        find out that the turn was cancelled while it was talking. A transport
        that submits work calls ``lifecycle.check_not_cancelled()`` immediately
        before doing so, which is what keeps a cancellation arriving mid-
        handshake from costing a whole turn's work. Optional because a caller
        may open a transport outside a turn, where there is no lifecycle to
        speak of.
        """
        del process, invocation, prompt, lifecycle
        return None

    def live_delivery_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        transport: HarnessTransport | None = None,
    ) -> guidance_delivery.GuidanceTransport | None:
        """Return the transport that injects guidance into this live turn.

        A protocol harness answers from its open conversation; a one-shot
        harness has no live channel at all, so every attempt defers with
        ``harness-unsupported`` rather than claiming a delivery it cannot
        make.
        """
        del process, invocation
        if transport is None:
            return None
        return transport.guidance_transport()

    def _cancelled_result(
        self,
        invocation: HarnessInvocation,
        stage: CancelStage,
        *,
        process_sweep: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> HarnessResult:
        """The result for a turn stopped before it produced any output.

        Reported with the same ``aborted``/137 shape a mid-execution
        cancellation uses, so nothing downstream has to learn a second spelling
        of "the operator stopped this"; ``cancel_stage`` is what says how much
        work it cost.
        """
        return HarnessResult(
            name=self.name,
            argv=invocation.argv,
            transport=invocation.transport,
            returncode=137,
            stdout="",
            stderr="",
            aborted=True,
            process_sweep=process_sweep,
            conversation_id=conversation_id,
            cancel_stage=stage,
        )

    def _stop_before_submission(
        self,
        process: subprocess.Popen,
    ) -> dict[str, Any] | None:
        """Take down a spawned process that never got work, and account for it.

        The output readers have not started on this path, so the pipes are
        closed here rather than by them. The descendant snapshot is still taken
        before the kill for the same reason it is on the execution path: a
        child that left the process group is only reachable from a list made
        while it was still running.
        """
        prior_snapshot: tuple[Mapping[str, Any], ...] = ()
        if process.poll() is None:
            prior_snapshot = _snapshot_descendants(process.pid, _sweep_process_table())
            _kill_process_group(process)
            process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()
        return _sweep_turn_descendants(process, prior_snapshot)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
        on_conversation: ConversationCallback | None = None,
        on_prompt_submitted: SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessResult:
        if invocation is None:
            invocation = self.build_invocation(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                # The turn environment is where argv learns about guards: an
                # adapter whose only pre-execution lane is its command line
                # reads the policy this env points at. Built here rather than
                # by the caller so the argv that runs is the argv the guards
                # were compiled into.
                env=env,
                resume_conversation_id=resume_conversation_id,
            )
        elif (
            resume_conversation_id is not None
            and resume_conversation_id != invocation.resume_conversation_id
        ):
            # A prebuilt invocation is the argv that will actually run, so it
            # wins — but it would win silently. A caller that named one
            # conversation while handing over an invocation naming another (or
            # none) would be told it continued something it did not, which is
            # the substitution this whole path exists to prevent. Saying
            # nothing is still allowed: a caller that passes only an invocation
            # asked for nothing, so the invocation contradicts nothing.
            raise HarnessError(
                f"{self.name} was asked to resume {resume_conversation_id} but "
                "the invocation it was given resumes "
                f"{invocation.resume_conversation_id or 'no conversation'}"
            )
        # Both refusals are the same rule read from both ends: a caller must
        # not be told a conversation was captured or continued by an adapter
        # that cannot do it. Raising is deliberate — a turn that ran anyway
        # would be one the caller believed was continuable and is not.
        if on_conversation is not None and not self.captures_conversations:
            raise HarnessError(
                f"{self.name} cannot report the conversation a turn runs on"
            )
        if invocation.resume_conversation_id is not None and (
            not self.resumes_conversations
        ):
            raise HarnessError(
                f"{self.name} cannot resume conversation "
                f"{invocation.resume_conversation_id}"
            )
        if invocation.last_message_path is not None:
            invocation.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        lifecycle = TurnLifecycle(
            conversation=TurnConversation(on_conversation),
            abort=abort,
            submission=TurnSubmission(on_prompt_submitted),
        )

        def stated(result: HarnessResult) -> HarnessResult:
            """Answer, on the way out, whether this turn's prompt went out.

            For the early returns, which build their result before there is a
            turn to describe; the ones further down state it beside the other
            facts they read off the lifecycle. Every return from this method
            answers one way or the other, because the caller that has to know is
            holding words it will otherwise hand to somebody else, and the only
            results that stay silent are the ones this method never produced.
            Read from the seam rather than from the exit path, so a failure mode
            nobody anticipated reports the submission it actually made rather
            than the one its branch assumed.
            """
            return dataclasses.replace(
                result, prompt_submitted=lifecycle.prompt_submitted
            )

        lifecycle.begin()
        if lifecycle.cancel_due:
            # Cancelled before spawn: the cheapest window there is. Nothing is
            # started, so nothing has to be killed, and the completion the
            # caller waits on is satisfied immediately.
            lifecycle.note_cancelled(CANCEL_STAGE_UNSTARTED)
            lifecycle.close()
            return stated(self._cancelled_result(invocation, CANCEL_STAGE_UNSTARTED))

        try:
            process = subprocess.Popen(
                invocation.argv,
                cwd=invocation.cwd,
                # A protocol transport talks back to the process it spawned, so
                # it needs a pipe of its own. A one-shot turn leaves stdin
                # inherited, exactly as it always was.
                stdin=(
                    subprocess.PIPE if invocation.transport != TRANSPORT_EXEC else None
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                # Its own session, so every descendant that inherits the turn's
                # process group can be killed at once on timeout and enumerated
                # by the post-turn sweep. Descendants that start a session of
                # their own are out of reach: see :func:`_kill_process_group`.
                start_new_session=True,
                # ``None`` inherits this process's environment, which is what
                # every turn did before command guards existed and what every
                # turn still does when none are active. A supplied mapping is
                # the complete child environment, already derived from the
                # parent's, so a guard's ``PATH`` prefix reaches the harness and
                # everything it spawns.
                env=None if env is None else dict(env),
            )
        except FileNotFoundError as error:
            # Nothing started, so nothing is left to stop -- but a caller
            # waiting on a cancellation must be answered rather than left
            # waiting for a turn that does not exist.
            lifecycle.close()
            raise HarnessError(
                f"{self.name} executable not found: {self.executable}"
            ) from error

        if on_spawn is not None:
            with contextlib.suppress(Exception):
                on_spawn(process.pid)

        if invocation.transport == TRANSPORT_EXEC:
            # The one transport for which the spawn *is* the submission: the
            # prompt is on the argv this process was started with, so by the
            # time there is a pid the worker already has the words. Every other
            # transport still has a handshake to get through -- and a handshake
            # that can refuse -- so each reports its own submission from the
            # point where its prompt actually goes out.
            lifecycle.note_prompt_submitted()

        # The conversation and the live-delivery endpoint are both opened here,
        # inside the boundary that owns the process, and both are closed in the
        # ``finally`` below on every path out of the turn. A delivery can
        # therefore only ever reach this exact live process: once the turn is
        # over the endpoint answers ``stale-turn`` instead of injecting into a
        # dead PID or into whatever turn started next.
        try:
            transport = self.open_transport(
                process=process,
                invocation=invocation,
                prompt=prompt,
                lifecycle=lifecycle,
            )
        except TurnCancelled:
            # Cancelled mid-handshake: the process exists but the transport
            # stopped short of submitting, so this stops a spawn rather than
            # interrupting work.
            lifecycle.note_cancelled(CANCEL_STAGE_HANDSHAKE)
            sweep = self._stop_before_submission(process)
            lifecycle.close()
            return stated(
                self._cancelled_result(
                    invocation,
                    CANCEL_STAGE_HANDSHAKE,
                    process_sweep=sweep,
                )
            )
        except ConversationCaptureError as error:
            # The turn is abandoned rather than run: a conversation that was
            # not recorded before submission is one no later session could
            # continue, and running the work anyway would spend it into a
            # conversation nothing can find again.
            sweep = self._stop_before_submission(process)
            lifecycle.close()
            return stated(
                HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    returncode=_CONVERSATION_ABANDON_RETURNCODE,
                    stdout="",
                    stderr=f"{self.name} turn abandoned: {error}\n",
                    process_sweep=sweep,
                    conversation_error=str(error),
                )
            )
        except BaseException:
            # A handshake that failed for any other reason leaves a spawned
            # process nobody will ever wait on, and a lifecycle nobody will
            # ever close. Both are ended here before the failure is reported.
            self._stop_before_submission(process)
            lifecycle.close()
            raise
        if on_transport is not None:
            on_transport(transport)
        if live_delivery is not None:
            live_delivery.open(
                self.live_delivery_transport(
                    process=process,
                    invocation=invocation,
                    transport=transport,
                )
            )
        try:
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            stream_io: dict[str, int | float] = {
                "stdout_chunks": 0,
                "stderr_chunks": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "callback_seconds": 0.0,
            }
            reader_errors: list[BaseException] = []
            output_processor = (
                self.output_processor()
                if transport is None
                else transport.output_processor()
            )
            final_result_fired = threading.Event()
            observed_model_fired = threading.Event()
            # Quiescence guard for abandoned readers: a reader that outlives the
            # bounded join still holds the stream callback, hooks, and output
            # state, so a late write from a pipe-holding descendant could fire
            # them after this turn has finished. Every per-chunk side effect runs
            # under this lock behind an abandonment check, and abandonment is
            # flipped under the same lock, so once ``abandon_readers`` returns no
            # reader can start a new side effect and any in-flight one is done.
            side_effect_lock = threading.Lock()
            abandoned = threading.Event()

            def abandon_readers() -> None:
                with side_effect_lock:
                    abandoned.set()

            def read_stream(
                stream_name: StreamName,
                chunks: list[str],
                pipe: IO[str],
            ) -> None:
                try:
                    for chunk in iter(pipe.readline, ""):
                        with side_effect_lock:
                            if abandoned.is_set():
                                break
                            processed_chunk = output_processor.process_chunk(
                                stream_name,
                                chunk,
                            )
                            if (
                                stream_name == "stdout"
                                and not output_processor.final_result_seen
                            ):
                                # A protocol request boundary re-arms the hook;
                                # duplicate result records within one request
                                # remain suppressed by the set Event below.
                                final_result_fired.clear()
                            # Only stdout parsing can flip final_result_seen, so
                            # this branch runs on a single thread and the Event
                            # guard makes the hook fire exactly once.
                            if (
                                stream_name == "stdout"
                                and on_final_result is not None
                                and not final_result_fired.is_set()
                                and output_processor.final_result_seen
                            ):
                                final_result_fired.set()
                                with contextlib.suppress(Exception):
                                    on_final_result()
                            if (
                                stream_name == "stdout"
                                and on_observed_model is not None
                                and not observed_model_fired.is_set()
                            ):
                                observed = output_processor.observed_model
                                if observed is not None:
                                    observed_model_fired.set()
                                    with contextlib.suppress(Exception):
                                        on_observed_model(observed)
                            if not processed_chunk:
                                continue
                            chunks.append(processed_chunk)
                            chunks_key = f"{stream_name}_chunks"
                            chars_key = f"{stream_name}_chars"
                            stream_io[chunks_key] = int(stream_io[chunks_key]) + 1
                            stream_io[chars_key] = int(stream_io[chars_key]) + len(
                                processed_chunk
                            )
                            if stream_callback is not None:
                                callback_started = time.monotonic()
                                try:
                                    stream_callback(stream_name, processed_chunk)
                                except Exception as error:
                                    reader_errors.append(error)
                                finally:
                                    stream_io["callback_seconds"] = float(
                                        stream_io["callback_seconds"]
                                    ) + (time.monotonic() - callback_started)
                except Exception as error:
                    reader_errors.append(error)
                finally:
                    pipe.close()

            if process.stdout is None or process.stderr is None:
                raise HarnessError(f"{self.name} did not expose stdout/stderr pipes")

            stdout_thread = threading.Thread(
                target=read_stream,
                args=("stdout", stdout_chunks, process.stdout),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=read_stream,
                args=("stderr", stderr_chunks, process.stderr),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            timed_out = False
            aborted = False
            prior_snapshot: tuple[Mapping[str, Any], ...] = ()
            process_sweep: dict[str, Any] | None = None
            try:
                expired = _wait_for_turn_exit(process, timeout=timeout, abort=abort)
                if expired is not None:
                    timed_out = expired == "timeout"
                    aborted = expired == "abort"
                    if aborted:
                        # Work was already submitted, so this is the expensive
                        # window: the kill below is what stops it.
                        lifecycle.note_cancelled(CANCEL_STAGE_EXECUTION)
                        _request_native_interrupt(transport)
                    prior_snapshot = _snapshot_descendants(
                        process.pid, _sweep_process_table()
                    )
                    _kill_process_group(process)
                    process.wait()

                _join_readers(
                    (stdout_thread, stderr_thread),
                    timeout=_ABANDONED_READER_JOIN_SECONDS,
                )
                abandon_readers()
                # After the join, so an in-group straggler gets to flush what it
                # was writing before the sweep takes it down.
                process_sweep = _sweep_turn_descendants(process, prior_snapshot)
            except KeyboardInterrupt:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                abandon_readers()
                # This branch returns early, so it needs its own sweep: the one on
                # the normal path is never reached from here. Explicit signalling
                # above is unchanged; the sweep only reaches what it left behind.
                interrupt_sweep = _sweep_turn_descendants(process)
                return HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    returncode=130,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
                    last_message=output_processor.last_message(
                        stdout="".join(stdout_chunks),
                        stderr="".join(stderr_chunks),
                        last_message_path=invocation.last_message_path,
                    ),
                    interrupted=True,
                    process_sweep=interrupt_sweep,
                    observed_model=output_processor.observed_model,
                    stream_io=dict(stream_io),
                    conversation_id=lifecycle.conversation.native_id,
                    cancel_stage=lifecycle.cancel_stage,
                    prompt_submitted=lifecycle.prompt_submitted,
                )

            if reader_errors:
                raise HarnessError("stream callback failed") from reader_errors[0]

            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
            if aborted:
                return HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    # 128 + SIGKILL, not the timeout path's 124: a caller reading
                    # only the returncode still has to tell the two apart.
                    returncode=137,
                    stdout=stdout,
                    stderr=stderr,
                    last_message=output_processor.last_message(
                        stdout=stdout,
                        stderr=stderr,
                        last_message_path=invocation.last_message_path,
                    ),
                    aborted=True,
                    process_sweep=process_sweep,
                    observed_model=output_processor.observed_model,
                    stream_io=dict(stream_io),
                    conversation_id=lifecycle.conversation.native_id,
                    cancel_stage=lifecycle.cancel_stage,
                    prompt_submitted=lifecycle.prompt_submitted,
                )
            if timed_out:
                return HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    last_message=output_processor.last_message(
                        stdout=stdout,
                        stderr=stderr,
                        last_message_path=invocation.last_message_path,
                    ),
                    timed_out=True,
                    process_sweep=process_sweep,
                    observed_model=output_processor.observed_model,
                    stream_io=dict(stream_io),
                    conversation_id=lifecycle.conversation.native_id,
                    cancel_stage=lifecycle.cancel_stage,
                    prompt_submitted=lifecycle.prompt_submitted,
                )

            interrupted = process.returncode == -signal.SIGINT
            return HarnessResult(
                name=self.name,
                argv=invocation.argv,
                transport=invocation.transport,
                returncode=130 if interrupted else process.returncode,
                stdout=stdout,
                stderr=stderr,
                last_message=output_processor.last_message(
                    stdout=stdout,
                    stderr=stderr,
                    last_message_path=invocation.last_message_path,
                ),
                interrupted=interrupted,
                process_sweep=process_sweep,
                observed_model=output_processor.observed_model,
                stream_io=dict(stream_io),
                conversation_id=lifecycle.conversation.native_id,
                cancel_stage=lifecycle.cancel_stage,
                prompt_submitted=lifecycle.prompt_submitted,
            )
        finally:
            # Endpoint first: closing it waits out any delivery already in
            # flight, so no steer can still be using the conversation the
            # transport shutdown is about to tear down. The lifecycle closes
            # last, so a request arriving in this window is staged
            # ``shutdown`` and completed rather than left waiting: by the time
            # completion is reported there is no turn left to cancel, and a
            # protocol thread that outlives the shutdown can no longer record
            # a conversation against it either.
            if live_delivery is not None:
                live_delivery.close()
            if transport is not None:
                transport.shutdown()
            lifecycle.close()


class _CodexAppServerOutputProcessor(_OutputProcessor):
    """Render one app-server conversation as the turn's readable stdout.

    Thin by design: every decision about the protocol lives in the session, and
    this only routes stdout into it and reports what it learned. stderr is left
    alone, because the app-server writes its own diagnostics there in prose.
    """

    def __init__(self, session: codex_app_server.CodexAppServerSession) -> None:
        self._session = session

    @property
    def final_result_seen(self) -> bool:
        # ``turn/completed`` is the app-server's terminal record, and it is as
        # authoritative as claude's ``result`` record: the turn is over even if
        # the process has not exited yet.
        return self._session.turn_status is not None

    @property
    def observed_model(self) -> str | None:
        return self._session.observed_model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text
        return self._session.feed(text)

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        # The protocol reports the final message directly, so it is preferred
        # over the file the legacy transport had to be told to write.
        final = self._session.final_message
        if final:
            return final if final.endswith("\n") else f"{final}\n"
        del stderr
        return _read_last_message(last_message_path) or stdout


class _FallbackWindowStream:
    """Hold an app-server turn's output back while a fallback is still possible.

    Anything printed before ``turn/start`` is accepted is handshake noise, and
    if the conversation fails there the legacy transport runs the same turn for
    real. Streaming both would show an operator two turns' output for one turn
    and would leave the discarded attempt's bytes in the persisted log, so the
    early chunks are buffered until acceptance forecloses the fallback -- after
    which every chunk passes straight through, live, as it always did.

    Called only from the reader threads, which serialize on the harness's
    side-effect lock, and released or discarded from the turn's own thread once
    the readers are done with it.
    """

    def __init__(self, downstream: StreamCallback | None) -> None:
        self._downstream = downstream
        self._session: codex_app_server.CodexAppServerSession | None = None
        self._buffer: list[tuple[StreamName, str]] = []
        self._passing = False

    def attach(self, session: codex_app_server.CodexAppServerSession) -> None:
        self._session = session

    def __call__(self, stream_name: StreamName, text: str) -> None:
        if not self._passing:
            session = self._session
            if session is None or not (session.turn_accepted or session.work_observed):
                self._buffer.append((stream_name, text))
                return
            self.release()
        if self._downstream is not None:
            self._downstream(stream_name, text)

    def release(self) -> None:
        """Emit what was withheld: this turn is the one that really ran."""
        self._passing = True
        buffered, self._buffer = self._buffer, []
        if self._downstream is None:
            return
        for stream_name, text in buffered:
            self._downstream(stream_name, text)

    def discard(self) -> None:
        """Drop what was withheld: another run of this turn will be streamed."""
        self._passing = True
        self._buffer.clear()


class _CodexAppServerTransport(HarnessTransport):
    """The adapter's handle on one live app-server conversation."""

    def __init__(self, session: codex_app_server.CodexAppServerSession) -> None:
        self.session = session
        self._processor = _CodexAppServerOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self.session.steer

    def interrupt(self) -> bool:
        return self.session.interrupt()

    def shutdown(self) -> None:
        self.session.shutdown()


#: The session identifier ``codex exec`` states in its own header. Anchored on
#: the UUID shape the app-server uses for the same value, so the only thing
#: this can match is a conversation id -- and a header spelled any other way
#: reports unavailable continuity instead of capturing a guess.
_CODEX_EXEC_SESSION_ID = re.compile(
    r"session[ _-]?id[^\n]{0,24}?"
    r"(?P<session_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class _CodexExecOutputProcessor(_OutputProcessor):
    """Read the legacy transport's one continuity fact out of its own output.

    ``codex exec`` takes its prompt on argv, so the work is submitted the
    moment the process is spawned and there is no handshake in which to learn a
    conversation first. The session it ran on is stated in its header instead,
    which is continuity observed *after* the fact -- the opposite of the
    ordering the app-server transport keeps, and the reason a capture failure
    here cannot retract anything.

    Everything else is the plain processor's behaviour: the text is passed
    through untouched, because this reads the output rather than rendering it.
    """

    def __init__(self, lifecycle: TurnLifecycle | None) -> None:
        self._lifecycle = lifecycle
        self.native_id: str | None = None
        self.capture_error: str | None = None

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if self.native_id is None and self.capture_error is None:
            self._observe(text)
        return text

    def _observe(self, text: str) -> None:
        """Capture the first session id the turn names, and only the first.

        The header is one line and the readers deliver whole lines, so a match
        is either in this chunk or in none. A second id would be a different
        conversation for the same turn, which the capture seam refuses anyway;
        stopping at the first keeps that refusal from being reached at all.
        """
        match = _CODEX_EXEC_SESSION_ID.search(text)
        if match is None:
            return
        native_id = match.group("session_id")
        lifecycle = self._lifecycle
        if lifecycle is not None:
            try:
                lifecycle.capture_conversation(native_id)
            except ConversationCaptureError as error:
                # Raising would surface as a stream-callback failure and lose
                # the turn, which already ran. Reported as continuity that
                # could not be had instead -- the honest outcome for work that
                # cannot be un-submitted.
                self.capture_error = str(error)
                return
        self.native_id = native_id


class _CodexExecTransport(HarnessTransport):
    """The legacy one-shot turn's continuity observer.

    It holds no conversation -- ``codex exec`` has none to hold -- and exists
    only so the turn's own output still yields the session it ran on. There is
    no live channel and nothing to interrupt beyond the process itself, so
    guidance defers and interruption declines exactly as they did when this
    transport was ``None``.
    """

    def __init__(self, lifecycle: TurnLifecycle | None) -> None:
        self._processor = _CodexExecOutputProcessor(lifecycle)
        self._wanted = lifecycle is not None and lifecycle.wants_conversation

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    @property
    def continuity_note(self) -> str | None:
        """Why this turn has no conversation to continue, if it has none.

        ``None`` when one was captured, and also when nobody asked: an ordinary
        one-shot turn is not missing something it was never meant to have. The
        identifier is still read out of the output either way, so a result
        reports the session it ran on whether or not a sink wanted it.
        """
        if not self._wanted:
            return None
        if self._processor.capture_error is not None:
            return self._processor.capture_error
        if self._processor.native_id is not None:
            return None
        return "codex exec output named no session id"


class _ExecContinuityObserver:
    """Keep hold of a legacy turn's transport so its result can report it.

    ``open_transport`` builds the transport and ``run`` returns the result, and
    nothing in between hands one to the other -- so this rides along as the
    ``on_transport`` hook, passing every call through to the caller's own hook
    and keeping the one transport it recognizes.
    """

    def __init__(
        self,
        downstream: Callable[[HarnessTransport | None], None] | None,
    ) -> None:
        self._downstream = downstream
        self.transport: _CodexExecTransport | None = None

    def __call__(self, transport: HarnessTransport | None) -> None:
        if isinstance(transport, _CodexExecTransport):
            self.transport = transport
        if self._downstream is not None:
            self._downstream(transport)

    def apply(self, result: HarnessResult) -> HarnessResult:
        """Report what continuity the turn's output did or did not name.

        The identifier itself is already on the result: it was captured through
        the lifecycle, which is what fills ``conversation_id``. Only its
        *absence* needs stating, because a caller that asked to continue this
        conversation later would otherwise read "none" as "not yet looked".
        """
        transport = self.transport
        if transport is None:
            return result
        note = transport.continuity_note
        if note is None:
            return result
        return dataclasses.replace(result, continuity_unavailable=note)


class CodexAdapter(HarnessAdapter):
    name = "codex"
    skill_prefix = "$"
    enforces_audit_read_only = True
    #: Both halves of continuity, on both transports. The app-server captures
    #: its thread id between ``thread/resume``/``thread/start`` and
    #: ``turn/start`` and can restore exactly the thread it is handed; the
    #: legacy transport captures the session id its own output names, and
    #: refuses a resume outright rather than starting a fresh session under one.
    captures_conversations = True
    resumes_conversations = True

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """The app-server argv, which deliberately says almost nothing.

        Everything argv used to carry -- cwd, sandbox, writable roots, model,
        effort, and the prompt itself -- moves into the turn plan and the
        protocol. The prompt in particular is no longer a command-line
        argument, so it no longer appears in any process listing.
        """
        del cwd, plan_dir, prompt, unsafe_bypass, model, effort, access, env
        if last_message_path is None:
            raise HarnessError("codex adapter requires a last_message_path")
        return [self.executable, "app-server"]

    def build_exec_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
    ) -> list[str]:
        """The legacy one-shot argv, kept for the pre-start fallback only.

        This is the transport codex ran under before the app-server, unchanged.
        It is reachable only when the app-server conversation failed before the
        turn was accepted and before any work was emitted.
        """
        if last_message_path is None:
            raise HarnessError("codex adapter requires a last_message_path")

        if access.is_audit:
            # No ``--add-dir``: for codex it grants *write* access, and the
            # read-only sandbox already permits reading outside the cwd, so the
            # read roots need no flag at all. ``--skip-git-repo-check`` lets the
            # audit run from a dedicated directory that is not a repository.
            argv = [
                self.executable,
                "exec",
                "-C",
                str(cwd),
                "-s",
                "read-only",
                "--skip-git-repo-check",
            ]
        else:
            argv = [
                self.executable,
                "exec",
                "-C",
                str(cwd),
                "--add-dir",
                self._worker_plan_dir(plan_dir),
            ]
            if unsafe_bypass:
                argv.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                argv.extend(["-s", "workspace-write"])
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        argv.extend(["-o", str(last_message_path), prompt])
        return argv

    def build_turn_plan(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        unsafe_bypass: bool = False,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
    ) -> codex_app_server.TurnPlan:
        """Restate the access profile as app-server thread and turn parameters.

        Each branch is the protocol spelling of the legacy flag it replaces:
        ``-s read-only`` and ``-s workspace-write`` become the ``sandbox``
        parameter, ``--dangerously-bypass-approvals-and-sandbox`` becomes
        ``danger-full-access`` with approvals off, and ``--add-dir`` becomes the
        one writable root under ``sandbox_workspace_write``. ``expected_sandbox``
        is what the session then demands the app-server confirm it applied, so
        the profile is verified rather than assumed.
        """
        thread_params: dict[str, Any] = {
            "cwd": str(cwd),
            # The turn is unattended on both transports; the legacy argv said
            # the same thing by never offering an approval path at all.
            "approvalPolicy": "never",
        }
        if access.is_audit:
            # No writable root: the read-only sandbox already permits reading
            # outside the cwd, which is what the audit's read roots need, and
            # the app-server needs no equivalent of --skip-git-repo-check.
            thread_params["sandbox"] = "read-only"
            expected_sandbox = codex_app_server.SANDBOX_READ_ONLY
        elif unsafe_bypass:
            thread_params["sandbox"] = "danger-full-access"
            expected_sandbox = codex_app_server.SANDBOX_DANGER_FULL_ACCESS
        else:
            thread_params["sandbox"] = "workspace-write"
            thread_params["config"] = {
                "sandbox_workspace_write": {
                    "writable_roots": [self._worker_plan_dir(plan_dir)]
                }
            }
            expected_sandbox = codex_app_server.SANDBOX_WORKSPACE_WRITE
        if model is not None:
            thread_params["model"] = model
        turn_params: dict[str, Any] = {}
        if effort is not None:
            turn_params["effort"] = effort
        return codex_app_server.TurnPlan(
            thread_params=thread_params,
            turn_params=turn_params,
            expected_sandbox=expected_sandbox,
        )

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        return HarnessInvocation(
            name=self.name,
            argv=self.build_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                env=env,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            resume_conversation_id=resume_conversation_id,
            transport=TRANSPORT_APP_SERVER,
            turn_plan=self.build_turn_plan(
                cwd=cwd,
                plan_dir=plan_dir,
                unsafe_bypass=unsafe_bypass,
                model=model,
                effort=effort,
                access=access,
            ),
        )

    def build_exec_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        if resume_conversation_id is not None:
            # `codex exec` has no resume: it starts a new session every time.
            # Building this invocation anyway would produce a turn that claims
            # to continue a conversation and silently starts a fresh one, which
            # is the exact substitution the app-server path refuses to make.
            raise HarnessError(
                "codex exec cannot resume conversation "
                f"{resume_conversation_id}; it starts a fresh session"
            )
        return HarnessInvocation(
            name=self.name,
            argv=self.build_exec_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            transport=TRANSPORT_EXEC,
        )

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_APP_SERVER:
            # The legacy transport holds no conversation, but its own output
            # names the session it ran on, and that is the only place a turn
            # whose prompt was argv can learn its continuity from.
            return _CodexExecTransport(lifecycle)
        if invocation.turn_plan is None:
            raise HarnessError("codex app-server turns require a turn plan")
        session = codex_app_server.CodexAppServerSession(
            process=process,
            plan=invocation.turn_plan,
            prompt=prompt,
            resume_conversation_id=invocation.resume_conversation_id,
            # Both hooks run on the session's driver thread, between the thread
            # call and `turn/start`. Their failures cannot be raised out of
            # here -- the handshake is already backgrounded by then -- so the
            # session records them and `run` below restates them as the
            # abandoned and cancelled results they are.
            on_conversation=None
            if lifecycle is None
            else lifecycle.capture_conversation,
            before_submit=None if lifecycle is None else lifecycle.refuse_submission,
            # The third of the same set, on the far side of the submission: it
            # fires once `turn/start` has been accepted, which is where a codex
            # turn stops being one this process could still decline. A restore
            # the server refuses never reaches it.
            on_submitted=None if lifecycle is None else lifecycle.note_prompt_submitted,
            # And the boundary between the two, which is the one an app-server
            # that dies mid-turn leaves behind: the prompt went out, the answer
            # never came, and the turn must end saying it cannot tell rather
            # than saying it sent nothing.
            on_submission_in_flight=(
                None if lifecycle is None else lifecycle.note_submission_in_flight
            ),
        )
        # The last checkpoint before the thread and the turn are created. A
        # cancellation seen here costs an app-server spawn; the same
        # cancellation a moment later costs the turn.
        if lifecycle is not None:
            lifecycle.check_not_cancelled()
        session.start()
        return _CodexAppServerTransport(session)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
        on_conversation: ConversationCallback | None = None,
        on_prompt_submitted: SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessResult:
        """Run the turn on the app-server, falling back only before it starts.

        The fallback window is narrow on purpose. ``session.fallback_reason``
        is non-``None`` only while the conversation failed before ``turn/start``
        was accepted *and* before the app-server emitted anything the turn did,
        which is the only state in which re-running the prompt cannot execute
        it twice. Once the turn is accepted, a protocol failure is reported as
        a failed turn -- never retried. A turn asked to *resume* a conversation
        has no fallback window at all: the legacy transport starts a fresh
        session, so falling back would answer "continue this conversation" with
        one that continues nothing.
        """
        if invocation is not None and invocation.transport == TRANSPORT_EXEC:
            # An explicitly legacy invocation (a fallback re-entry, or a caller
            # that built one deliberately) is run as-is.
            observer = _ExecContinuityObserver(on_transport)
            return observer.apply(
                super().run(
                    cwd=cwd,
                    plan_dir=plan_dir,
                    prompt=prompt,
                    unsafe_bypass=unsafe_bypass,
                    last_message_path=last_message_path,
                    timeout=timeout,
                    stream_callback=stream_callback,
                    model=model,
                    effort=effort,
                    on_spawn=on_spawn,
                    on_final_result=on_final_result,
                    on_observed_model=on_observed_model,
                    access=access,
                    abort=abort,
                    live_delivery=live_delivery,
                    env=env,
                    invocation=invocation,
                    on_transport=observer,
                    on_conversation=on_conversation,
                    on_prompt_submitted=on_prompt_submitted,
                    resume_conversation_id=invocation.resume_conversation_id,
                )
            )

        if invocation is None:
            invocation = self.build_invocation(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                resume_conversation_id=resume_conversation_id,
            )

        # Captured rather than stored on ``self``: one adapter instance serves
        # every turn of a run, and the session belongs to exactly one of them.
        opened: list[codex_app_server.CodexAppServerSession] = []
        window = _FallbackWindowStream(stream_callback)

        def capture(transport: HarnessTransport | None) -> None:
            if isinstance(transport, _CodexAppServerTransport):
                opened.append(transport.session)
                window.attach(transport.session)
            if on_transport is not None:
                on_transport(transport)

        result = super().run(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            timeout=timeout,
            stream_callback=window,
            model=model,
            effort=effort,
            on_spawn=on_spawn,
            on_final_result=on_final_result,
            on_observed_model=on_observed_model,
            access=access,
            abort=abort,
            live_delivery=live_delivery,
            env=env,
            invocation=invocation,
            on_transport=capture,
            on_conversation=on_conversation,
            on_prompt_submitted=on_prompt_submitted,
            resume_conversation_id=invocation.resume_conversation_id,
        )
        if not opened:
            window.release()
            return result
        session = opened[0]

        abandoned = self._abandoned_result(result, session)
        if abandoned is not None:
            # Nothing was submitted, so there is nothing to stream and nothing
            # to fall back to: this turn was stopped on purpose, before it
            # became a turn at all.
            window.discard()
            return abandoned

        if result.timed_out or result.aborted or result.interrupted:
            # The turn was stopped rather than failed. Re-running it would
            # spend the same wall clock again, and the operator asked for it to
            # stop, so these codes stand exactly as the base run reported them.
            window.release()
            return self._app_server_result(result, session)

        fallback = session.fallback_reason
        if fallback is None:
            window.release()
            return self._app_server_result(result, session)

        window.discard()
        legacy = self.build_exec_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
        )
        observer = _ExecContinuityObserver(on_transport)
        fallback_result = observer.apply(
            super().run(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                timeout=timeout,
                stream_callback=stream_callback,
                model=model,
                effort=effort,
                on_spawn=on_spawn,
                on_final_result=on_final_result,
                on_observed_model=on_observed_model,
                access=access,
                abort=abort,
                live_delivery=live_delivery,
                env=env,
                invocation=legacy,
                on_transport=observer,
                on_conversation=on_conversation,
                on_prompt_submitted=on_prompt_submitted,
                resume_conversation_id=legacy.resume_conversation_id,
            )
        )
        return self._with_stderr_note(
            fallback_result,
            f"codex app-server unavailable, ran codex exec instead: {fallback}",
        )

    def _abandoned_result(
        self,
        result: HarnessResult,
        session: codex_app_server.CodexAppServerSession,
    ) -> HarnessResult | None:
        """Restate a turn the session refused to submit, or ``None``.

        Both refusals happen on the session's driver thread, after ``run`` has
        stopped being able to raise, so the process exits normally and the base
        result reads like an ordinary short turn. That result cannot stand: a
        conversation that was never recorded, or a turn cancelled a moment
        before ``turn/start``, must not be reported as work that ran.

        The base run's exit code is deliberately overwritten rather than
        consulted. Closing stdin with nothing submitted races the wait loop, so
        the app-server may well have exited cleanly first -- and a clean exit
        from a turn that never started is the one thing it must not be read as.
        """
        conversation_error = session.conversation_error
        if conversation_error is not None:
            return self._with_stderr_note(
                dataclasses.replace(
                    result,
                    returncode=_CONVERSATION_ABANDON_RETURNCODE,
                    conversation_error=conversation_error,
                    # No conversation was recorded, so the turn has none to
                    # report -- the whole reason it refused to run.
                    conversation_id=None,
                    timed_out=False,
                    aborted=False,
                    interrupted=False,
                ),
                f"{self.name} turn abandoned: {conversation_error}",
            )
        if session.cancelled_before_submission is None:
            return None
        return dataclasses.replace(
            result,
            # The same code and the same window the base run reports for a
            # handshake cancellation it *could* raise out of, because it is the
            # same event: a turn stopped before it submitted anything.
            returncode=137,
            aborted=True,
            timed_out=False,
            interrupted=False,
            cancel_stage=CANCEL_STAGE_HANDSHAKE,
        )

    def _app_server_result(
        self,
        result: HarnessResult,
        session: codex_app_server.CodexAppServerSession,
    ) -> HarnessResult:
        """Restate the turn's outcome from the protocol rather than the exit code.

        An app-server exits 0 whether the turn it ran succeeded or failed, and
        exits by our own ``terminate`` on the shutdown path, so its return code
        carries no verdict. The turn's own terminal status does.
        """
        error = session.protocol_error
        if error is not None:
            result = self._with_stderr_note(result, f"codex app-server: {error}")
        refused = session.restore_refused
        if refused is not None:
            result = dataclasses.replace(result, conversation_restore_refused=refused)
        if result.timed_out or result.aborted or result.interrupted:
            return result
        if (
            session.turn_status == codex_app_server.TURN_STATUS_COMPLETED
            and error is None
        ):
            returncode = 0
        elif result.returncode > 0:
            returncode = result.returncode
        else:
            # 0 (a clean app-server exit after an unfinished turn) and any
            # negative signal code from our own shutdown both mean "no verdict";
            # neither may be reported as success.
            returncode = 1
        return dataclasses.replace(result, returncode=returncode)


class _ClaudeStreamTransport(HarnessTransport):
    """The adapter's handle on one live claude stream-JSON conversation."""

    def __init__(self, session: claude_stream.ClaudeStreamSession) -> None:
        self.session = session
        self._processor = _ClaudeStreamJsonOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self.session.steer

    def interrupt(self) -> bool:
        return self.session.interrupt()

    def shutdown(self) -> None:
        # No interrupt here, unlike the app-server transport. A claude turn is
        # the process itself rather than a turn inside a server, and by the time
        # the harness reaches its ``finally`` that process has been waited on
        # and the stdout readers abandoned — so a control request sent from here
        # could never be correlated and would have nothing left to stop. The
        # execution window sends it, where a reader is still there to answer.
        self.session.shutdown()


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    skill_prefix = "/"
    enforces_audit_read_only = True
    enforces_guard_pretool = True
    captures_conversations = True
    resumes_conversations = True

    def output_processor(self) -> _OutputProcessor:
        return _ClaudeStreamJsonOutputProcessor()

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        # Claude's guard lane is the settings overlay written at spawn time,
        # not argv, so the turn environment says nothing this command line
        # needs.
        del cwd, last_message_path, env
        argv = [
            self.executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # Input is a stream too, which is what makes a turn a conversation
            # rather than a one-shot: the prompt below is written to stdin as
            # the first message, and guidance can follow it while the turn runs.
            "--input-format",
            "stream-json",
            # Echoes prove which messages a result covers, including guidance
            # consumed inside the current tool loop rather than a new turn.
            "--replay-user-messages",
        ]
        if access.is_audit:
            for root in access.read_roots:
                argv.extend(["--add-dir", str(root)])
            argv.extend(
                [
                    "--permission-mode",
                    "plan",
                    "--tools",
                    CLAUDE_AUDIT_TOOLS,
                    "--allowed-tools",
                    CLAUDE_AUDIT_TOOLS,
                    "--disallowed-tools",
                    CLAUDE_AUDIT_DENIED_TOOLS,
                    "--strict-mcp-config",
                    "--no-session-persistence",
                    "--disable-slash-commands",
                ]
            )
        else:
            argv.extend(
                [
                    "--add-dir",
                    self._worker_plan_dir(plan_dir),
                    "--permission-mode",
                    "bypassPermissions" if unsafe_bypass else "dontAsk",
                ]
            )
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        # The prompt is deliberately absent from argv: under streaming input it
        # is the conversation's first stdin message, so it never reaches a
        # process listing, a crash report, or a printed invocation.
        del prompt
        return argv

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        invocation = super().build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            env=env,
            resume_conversation_id=resume_conversation_id,
        )
        argv, assigned = self._with_session_controls(
            invocation.argv,
            access=access,
            resume_conversation_id=resume_conversation_id,
        )
        return dataclasses.replace(
            invocation,
            argv=argv,
            transport=TRANSPORT_STREAM_JSON,
            assigned_conversation_id=assigned,
        )

    def _with_session_controls(
        self,
        argv: list[str],
        *,
        access: AccessProfile,
        resume_conversation_id: str | None,
    ) -> tuple[list[str], str | None]:
        """Name this turn's conversation on argv, and say which one it is.

        Two supported controls and a deliberate omission. ``--resume`` restores
        exactly the conversation asked for; ``--session-id`` assigns one the CLI
        would otherwise have minted itself, which is what makes the identifier
        knowable before the prompt is written rather than only after the CLI's
        first record arrives. ``--fork-session`` is never passed, because it
        answers "continue conversation X" with a different one — the silent
        substitution this whole path exists to prevent.

        An audit turn gets neither: it runs with ``--no-session-persistence``,
        so there is no conversation to name, assign, or come back to.
        """
        if access.is_audit:
            if resume_conversation_id is not None:
                raise HarnessError(
                    "a claude audit turn cannot resume conversation "
                    f"{resume_conversation_id}; it persists no session"
                )
            return list(argv), None
        if resume_conversation_id is not None:
            return [*argv, "--resume", resume_conversation_id], resume_conversation_id
        assigned = claude_stream.new_session_id()
        return [*argv, "--session-id", assigned], assigned

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_STREAM_JSON:
            return None
        spool = None
        if invocation.overlay_dir is not None:
            spool = claude_stream.ClaudeGuidanceSpool(invocation.overlay_dir / "spool")
        assigned = invocation.assigned_conversation_id
        # A turn that asked to continue a conversation holds its submission
        # report until the CLI announces a session: see
        # :meth:`_restore_refusal` for why that record, and only that record,
        # separates a ``--resume`` the CLI honoured from one it threw out.
        restorable = invocation.resume_conversation_id is not None
        opened: list[claude_stream.ClaudeStreamSession] = []

        def announce_submission() -> None:
            """The CLI took the conversation up, so the prompt is its now.

            Both halves have to hold. The write below put the words in the
            pipe, and this record says the CLI got past the ``--resume`` it
            could have thrown them out over; a turn missing either half has not
            handed anything to anybody.
            """
            if lifecycle is None or not opened or opened[0].prompt_error is not None:
                return
            lifecycle.note_prompt_submitted()

        session = claude_stream.ClaudeStreamSession(
            process=process,
            prompt=prompt,
            spool=spool,
            session_id=assigned,
            on_session_observed=announce_submission if restorable else None,
        )
        opened.append(session)
        if lifecycle is not None and assigned is not None:
            # Before the prompt, which is the submission: the identifier was
            # settled on argv, so unlike a handshake transport this turn has
            # nothing to learn first and no reason to submit before persisting.
            # A sink that raises comes out of here as a ``ConversationCapture
            # Error`` and the turn is abandoned with nothing written.
            lifecycle.capture_conversation(assigned)
        # The prompt is the submission here, so this is the checkpoint: past it
        # the CLI is working, and a cancellation has to interrupt rather than
        # decline.
        if lifecycle is not None:
            lifecycle.check_not_cancelled()
        # The prompt goes out before the readers start, so the CLI has work to
        # do from its first byte. A write failure here is not raised: the
        # process exists and has to be collected like any other failed turn,
        # with ``prompt_error`` explaining the empty output.
        if session.send_prompt() and lifecycle is not None and not restorable:
            # Only on the write that succeeded. A prompt the pipe refused is one
            # the CLI never saw, and the turn that follows it is a spawned
            # process sitting on a closed stdin -- not a worker holding the
            # operator's words.
            #
            # And only where nothing can still refuse the prompt this write put
            # in flight. A CLI that will not honour a ``--resume`` exits without
            # ever reading its stdin, so the write succeeds against a pipe whose
            # reader is on its way out: the bytes left this process and reached
            # nobody. Such a turn reports its submission from the session
            # announcement above, or from :meth:`_settle_deferred_submission`
            # once the turn is over and the refusal it would have shown is not
            # there.
            lifecycle.note_prompt_submitted()
        return _ClaudeStreamTransport(session)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
        on_conversation: ConversationCallback | None = None,
        on_prompt_submitted: SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessResult:
        """Run the turn, arming its guidance hook for the turn's lifetime only.

        The overlay is the one piece of a claude turn that exists on disk and
        must not survive it. It is created here, named on argv with
        ``--settings``, and deleted in the ``finally`` — so the hook the CLI was
        told to run has nothing left to inject the moment the turn is over, and
        the operator's own configuration is never touched at any point.
        """
        if invocation is None:
            invocation = self.build_invocation(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                resume_conversation_id=resume_conversation_id,
            )
        overlay_dir: Path | None = None
        if invocation.transport == TRANSPORT_STREAM_JSON and not access.is_audit:
            # Audit turns get no overlay: an audit observes, and a hook that
            # injects operator guidance into one would make it something else.
            overlay_dir = Path(tempfile.mkdtemp(prefix="asl-claude-turn-"))
            spool = claude_stream.ClaudeGuidanceSpool(overlay_dir / "spool")
            # The policy path arrives in the turn environment the guard
            # controller prepared, so the hook reads whatever is denied *now*
            # rather than a copy taken when the overlay was written: a guard
            # added mid-turn is enforced by the next tool call, not the next
            # turn.
            settings_path = claude_stream.write_settings_overlay(
                overlay_dir,
                spool,
                guard_policy_path=(env or {}).get(guards.GUARD_POLICY_ENV_VAR),
            )
            invocation = dataclasses.replace(
                invocation,
                argv=[*invocation.argv, "--settings", str(settings_path)],
                overlay_dir=overlay_dir,
            )

        opened: list[HarnessTransport | None] = []

        def capture(transport: HarnessTransport | None) -> None:
            opened.append(transport)
            if on_transport is not None:
                on_transport(transport)

        # Wrapped because a resumed turn can report its submission from either
        # side of the turn boundary: from the session announcement while it
        # runs, or from the settlement below once it is over.
        submission = _SubmissionOnce(on_prompt_submitted)
        try:
            result = super().run(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                timeout=timeout,
                stream_callback=stream_callback,
                model=model,
                effort=effort,
                on_spawn=on_spawn,
                on_final_result=on_final_result,
                on_observed_model=on_observed_model,
                access=access,
                abort=abort,
                live_delivery=live_delivery,
                env=env,
                invocation=invocation,
                on_transport=capture,
                on_conversation=on_conversation,
                on_prompt_submitted=submission,
                resume_conversation_id=invocation.resume_conversation_id,
            )
        finally:
            claude_stream.remove_overlay(overlay_dir)

        transport = opened[0] if opened else None
        session = (
            transport.session if isinstance(transport, _ClaudeStreamTransport) else None
        )
        # A prompt that never reached the CLI produces a turn with no output
        # and no explanation. Saying so on stderr is the difference between a
        # diagnosable failure and a mysteriously empty one.
        if session is not None and session.prompt_error:
            result = self._with_stderr_note(
                result,
                f"claude prompt was not delivered: {session.prompt_error}",
            )
        return self._with_continuity_note(
            result,
            session=session,
            invocation=invocation,
            access=access,
            on_conversation=on_conversation,
            submission=submission,
        )

    def _with_continuity_note(
        self,
        result: HarnessResult,
        *,
        session: claude_stream.ClaudeStreamSession | None,
        invocation: HarnessInvocation,
        access: AccessProfile,
        on_conversation: ConversationCallback | None,
        submission: _SubmissionOnce | None = None,
    ) -> HarnessResult:
        """Report a turn whose conversation is not the one it was promised.

        Two ways that happens, and neither aborts the turn. The identifier is
        settled on argv, so by the time the CLI contradicts it in ``system``/
        ``init`` the prompt is long gone and the workspace is being edited —
        killing the turn there would throw away real work to protest a label.
        And an audit turn persists no session at all, so a caller that asked
        for one is owed the word ``unavailable`` rather than silence.

        A mismatch also drops ``conversation_id``. The turn ran somewhere, but
        not demonstrably where it said it would, and an identifier a later run
        would resume on is exactly the thing that must not be guessed.
        """
        if session is not None and session.session_mismatch is not None:
            note = f"claude session mismatch: {session.session_mismatch}"
            return self._with_stderr_note(
                dataclasses.replace(
                    result,
                    continuity_unavailable=note,
                    conversation_id=None,
                ),
                note,
            )
        if (
            on_conversation is not None
            and access.is_audit
            and invocation.assigned_conversation_id is None
        ):
            note = "claude audit turn persists no session, so it names none"
            return self._with_stderr_note(
                dataclasses.replace(result, continuity_unavailable=note),
                note,
            )
        refused = self._restore_refusal(result, session=session, invocation=invocation)
        if refused is not None:
            # The refusal is also the answer to "did this prompt go out": the
            # gates it passed include the CLI never announcing a session, so
            # nothing on the other side of the pipe ever took the words up. A
            # write into a pipe whose reader is leaving must not read as a
            # worker holding an operator's correction.
            return dataclasses.replace(
                result,
                conversation_restore_refused=refused,
                prompt_submitted=False,
            )
        return self._settle_deferred_submission(
            result,
            session=session,
            invocation=invocation,
            submission=submission,
        )

    def _settle_deferred_submission(
        self,
        result: HarnessResult,
        *,
        session: claude_stream.ClaudeStreamSession | None,
        invocation: HarnessInvocation,
        submission: _SubmissionOnce | None,
    ) -> HarnessResult:
        """Close a resumed turn's held-back submission report, fail-closed.

        A resumed claude turn writes its prompt and then waits to see whether the
        CLI takes the conversation up, because only the CLI can say. It says so
        by announcing a session, and that announcement reports the submission
        while the turn is still running -- so this settles the turns where it
        never came.

        Exactly one of those is proof of anything: the turn whose own output
        diagnoses a refused restore, which is handled by the caller and reports
        nothing. Every other silence is ambiguous -- a timeout, a kill, a CLI
        that died before its first record, a reader that stopped reading -- and
        the prompt was written into that silence. So each of them reports the
        submission here rather than leaving it unreported: an unproven turn that
        claimed non-submission would hand the same words to a second worker,
        which is the one outcome this seam exists to prevent.

        The caller is told directly rather than through the turn's lifecycle,
        which is closed by now precisely so a straggling thread cannot reach it.
        This is not a straggler: the turn is over, its process is collected, and
        this is the boundary that owned it stating what it did.
        """
        if session is None or invocation.resume_conversation_id is None:
            return result
        if result.prompt_submitted:
            return result
        if session.prompt_error is not None:
            # The write itself failed, so there is nothing to settle: no bytes
            # left this process and the result already says so.
            return result
        if submission is not None:
            submission()
        return dataclasses.replace(result, prompt_submitted=True)

    def _restore_refusal(
        self,
        result: HarnessResult,
        *,
        session: claude_stream.ClaudeStreamSession | None,
        invocation: HarnessInvocation,
    ) -> str | None:
        """Read a ``--resume`` the CLI would not honour out of its own output.

        Claude has no restore handshake to fail: ``--resume`` goes on argv and
        the CLI either continues the conversation or complains and exits. So the
        refusal has to be recognized after the fact, and the recognition is
        gated three ways rather than one, because the cost of a false positive
        is a conversation retired while the CLI still holds it:

        - the turn asked to resume something, so there is a restore to refuse;
        - the CLI never announced a session of its own, which every turn it
          actually started does before it runs anything -- this is what
          separates a refused restore from a resumed conversation that then
          failed;
        - the complaint names the very conversation that was asked for, so a
          generic startup error about something else cannot be read as one;
        - and the complaint is a supported missing-history diagnosis in the
          shared vocabulary of :mod:`~agent_session_loop.restore_diagnosis`,
          which is the same one the two protocol transports weigh their error
          responses against -- so a line that names the conversation while
          saying the service is down, the credential expired, or access was
          refused is the outage it says it is.

        A stopped turn is excluded outright. A timeout or an abort says nothing
        about whether the history exists, and both can leave the CLI having
        printed nothing at all.
        """
        resume = invocation.resume_conversation_id
        if resume is None or result.returncode == 0:
            return None
        if result.timed_out or result.aborted or result.interrupted:
            return None
        if session is not None and session.session_observed:
            return None
        text = f"{result.stderr}\n{result.stdout}"
        for line in text.splitlines():
            if resume not in line:
                continue
            if restore_diagnosis.text_diagnoses_missing_history(line):
                return f"could not restore conversation {resume}: {line.strip()}"
        return None


def _decline_active_steer(
    request: guidance_delivery.GuidanceRequest,
) -> guidance_delivery.GuidanceOutcome:
    """Copilot's answer to a live steer, which is a considered "no".

    Neither copilot transport exposes a same-turn steer. The one-shot form
    takes its whole prompt on argv and has nowhere to put a second one; the ACP
    form holds a conversation, but the only in-turn control it offers is
    cancellation, and cancelling a turn to say something to it would destroy
    the work the guidance was meant to redirect.

    Declining explicitly is the point: it turns a silent ``harness-unsupported``
    into ``unsupported-active-steer``, which says the harness was asked and the
    answer is next turn. The guidance stays deliverable, and the next turn's
    prompt carries it.
    """
    del request
    return guidance_delivery.deferred(guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER)


class _CopilotOneShotTransport(HarnessTransport):
    """The audit turn's transport: no conversation, only a steer to decline."""

    def output_processor(self) -> _OutputProcessor:
        return _OutputProcessor()

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return _decline_active_steer


class _CopilotAcpOutputProcessor(_OutputProcessor):
    """Render one ACP conversation as the turn's readable stdout.

    Thin by design: every decision about the protocol lives in the session, and
    this only routes stdout into it and reports what it learned. stderr is left
    alone, because copilot writes its own diagnostics there in prose.
    """

    def __init__(self, session: copilot_acp.CopilotAcpSession) -> None:
        self._session = session

    @property
    def final_result_seen(self) -> bool:
        # The stop reason is copilot's terminal record, and it is as
        # authoritative as claude's ``result`` record: the turn is over even if
        # the process has not exited yet.
        return self._session.stop_reason is not None

    @property
    def observed_model(self) -> str | None:
        return self._session.observed_model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text
        rendered = self._session.feed(text)
        if text and not text.endswith("\n"):
            # The reader hands over whatever ``readline`` produced, and that
            # only lacks a trailing newline at EOF -- so this is the agent's
            # last word. Flushing it is what keeps prose printed on the way out
            # of the process from sitting in the decoder's buffer unlogged.
            rendered += self._session.flush()
        return rendered

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        # The protocol streams the assistant message directly, so the
        # reassembled text is preferred over whatever prose the one-shot form
        # used to leave on stdout.
        final = self._session.final_message
        if final:
            return final if final.endswith("\n") else f"{final}\n"
        del stderr
        return _read_last_message(last_message_path) or stdout


class _CopilotAcpTransport(HarnessTransport):
    """The adapter's handle on one live copilot ACP conversation."""

    def __init__(self, session: copilot_acp.CopilotAcpSession) -> None:
        self.session = session
        self._processor = _CopilotAcpOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return _decline_active_steer

    def interrupt(self) -> bool:
        return self.session.cancel()

    def shutdown(self) -> None:
        # No cancel here, like the claude transport and unlike the app-server.
        # By the time the harness reaches its ``finally`` the process has been
        # waited on and the stdout readers abandoned, so a cancellation sent
        # from here would have nothing left to stop and no reader to observe
        # the ``cancelled`` stop reason it exists to produce. The execution
        # window sends it, where both are still true.
        self.session.shutdown()


class CopilotAdapter(HarnessAdapter):
    name = "copilot"
    skill_prefix = "/"
    enforces_audit_read_only = True
    delivers_guidance_next_turn = True
    captures_conversations = True
    resumes_conversations = True

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        del last_message_path
        argv = [
            self.executable,
            "-C",
            str(cwd),
        ]
        # Copilot has no hook ASL can arm, so its only pre-execution lane is
        # the command line it is spawned with. That fixes the rules at process
        # start: a guard added mid-turn cannot reach this argv, and a
        # protected-path rule has no `--deny-tool` form at all. Both gaps are
        # reported as such by `guards.guard_coverage` rather than papered over
        # -- this is a narrowing of the postflight guarantee, not a second one.
        #
        # The ``=`` form is load-bearing here for the same reason as in the
        # audit profile below: ``--deny-tool`` takes a variadic value, so the
        # space form swallows the next argument.
        for denied in guards.copilot_deny_tool_rules_from_policy(
            self._guard_policy(env)
        ):
            argv.append(f"--deny-tool={denied}")
        if access.is_audit:
            for root in access.read_roots:
                argv.extend(["--add-dir", str(root)])
            argv.append(f"--available-tools={COPILOT_AUDIT_TOOLS}")
            # The ``=`` form is load-bearing: ``--deny-tool`` takes a variadic
            # value, so the space form can swallow the next argument.
            argv.extend(
                f"--deny-tool={denied}" for denied in COPILOT_AUDIT_DENIED_TOOLS
            )
            argv.extend(
                [
                    "--disable-builtin-mcps",
                    "--no-custom-instructions",
                    "--no-remote",
                    "--no-remote-export",
                    "--disallow-temp-dir",
                ]
            )
        else:
            argv.extend(["--add-dir", self._worker_plan_dir(plan_dir)])
            if unsafe_bypass:
                argv.extend([COPILOT_ALLOW_ALL_TOOLS, "--allow-all-paths"])
        argv.extend(["--no-ask-user", "--silent"])
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        if access.is_audit:
            # The audit turn keeps the one-shot form exactly as it was. Its
            # restrictions are the argv above -- a fixed tool set, denied
            # families, no MCPs, no remote, no temp dir -- and moving it onto a
            # protocol would put a permission responder in front of tools that
            # currently cannot be asked for at all. Nothing about an audit needs
            # a conversation, so it does not get one.
            argv.extend(["-p", prompt])
            return argv
        # The prompt is deliberately absent from a worker argv: it becomes the
        # ACP ``session/prompt`` payload, so it never reaches a process listing,
        # a crash report, or a printed invocation.
        del prompt
        argv.append("--acp")
        return argv

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        invocation = super().build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            env=env,
            resume_conversation_id=resume_conversation_id,
        )
        if access.is_audit:
            if resume_conversation_id is not None:
                # The audit profile keeps the one-shot form, which holds no
                # conversation at all. Refusing here rather than in
                # ``open_transport`` is what stops the adapter's now-true
                # ``resumes_conversations`` from letting an audit turn through
                # to a transport that would ignore the request and run fresh.
                raise HarnessError(
                    "a copilot audit turn cannot resume conversation "
                    f"{resume_conversation_id}; it runs one-shot and holds none"
                )
            return invocation
        return dataclasses.replace(invocation, transport=TRANSPORT_ACP)

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_ACP:
            if invocation.resume_conversation_id is not None:
                # ``build_invocation`` already refuses this, so reaching it means
                # a caller assembled the invocation itself. Refusing again is
                # cheaper than the alternative, which is a turn that ran fresh
                # while its caller believed it had continued something.
                raise HarnessError(
                    "a copilot one-shot turn cannot resume conversation "
                    f"{invocation.resume_conversation_id}"
                )
            # The audit turn submits nothing here: its prompt already went out
            # on argv, so there is no checkpoint to offer and a cancellation is
            # handled by the execution window like any other one-shot turn.
            del process, prompt, lifecycle
            return _CopilotOneShotTransport()
        session = copilot_acp.CopilotAcpSession(
            process=process,
            prompt=prompt,
            cwd=str(invocation.cwd),
            # Read back off the argv the process was actually spawned with,
            # which is what makes "the responder restates the launch grant and
            # never exceeds it" structurally true rather than a convention two
            # call sites have to keep in step.
            permissions=copilot_acp.PermissionPolicy.for_turn(
                is_audit=False,
                unsafe_bypass=COPILOT_ALLOW_ALL_TOOLS in invocation.argv,
            ),
            expected_model=invocation.model,
            expected_effort=invocation.effort,
            # Intent, carried through unchanged. The session either restores
            # exactly this conversation or fails the turn; there is no path
            # from here to a fresh one.
            resume_conversation_id=invocation.resume_conversation_id,
            # Runs on the driver thread between the session and the prompt, so
            # the identifier is durable before any work is submitted. Like the
            # gate below it cannot raise out of here, so the session records
            # the failure and ``run`` restates it as the abandonment it is.
            on_conversation=(
                None if lifecycle is None else lifecycle.capture_conversation
            ),
            # Runs on the session's driver thread, immediately before
            # ``session/prompt``. Its failure cannot be raised out of here --
            # the handshake is already backgrounded by then -- so the session
            # records it and ``run`` below restates it as the cancellation it
            # is. Session 2's handoff noted that the one-shot adapter ignored
            # the lifecycle entirely, so a copilot cancellation could only ever
            # be seen ``unstarted`` or ``execution``; this is the missing
            # ``handshake`` window.
            before_submit=None if lifecycle is None else lifecycle.refuse_submission,
            # Runs on the driver thread the instant ``session/prompt`` is
            # written, which is the only moment a copilot turn can honestly be
            # called submitted: the request itself does not answer until the
            # turn is over, and every refusal above it -- a declined
            # ``session/load`` most of all -- leaves the prompt unsent.
            on_submitted=None if lifecycle is None else lifecycle.note_prompt_submitted,
        )
        # The last checkpoint before the session is created. A cancellation seen
        # here costs a copilot spawn; the same cancellation a moment later costs
        # the turn.
        if lifecycle is not None:
            lifecycle.check_not_cancelled()
        session.start()
        return _CopilotAcpTransport(session)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
        on_conversation: ConversationCallback | None = None,
        on_prompt_submitted: SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessResult:
        """Run the turn, then report what the protocol said rather than argv.

        There is no fallback to the one-shot form. Copilot's ACP mode is the
        same binary with the same flags reading its prompt from a pipe instead
        of argv, so a conversation that fails after ``session/prompt`` has spent
        the turn -- and re-running it would execute the same work twice.
        """
        # Captured rather than stored on ``self``: one adapter instance serves
        # every turn of a run, and the session belongs to exactly one of them.
        opened: list[copilot_acp.CopilotAcpSession] = []

        def capture(transport: HarnessTransport | None) -> None:
            if isinstance(transport, _CopilotAcpTransport):
                opened.append(transport.session)
            if on_transport is not None:
                on_transport(transport)

        result = super().run(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            timeout=timeout,
            stream_callback=stream_callback,
            model=model,
            effort=effort,
            on_spawn=on_spawn,
            on_final_result=on_final_result,
            on_observed_model=on_observed_model,
            access=access,
            abort=abort,
            live_delivery=live_delivery,
            env=env,
            invocation=invocation,
            on_transport=capture,
            on_conversation=on_conversation,
            on_prompt_submitted=on_prompt_submitted,
            resume_conversation_id=(
                resume_conversation_id
                if invocation is None
                else invocation.resume_conversation_id
            ),
        )
        if not opened:
            if on_conversation is not None and access.is_audit:
                # The audit turn never opened a conversation, so nothing
                # captured one. A caller that asked which conversation the turn
                # ran on is owed the word "unavailable" rather than a silent
                # ``None`` that reads like a transport that simply failed.
                note = "copilot audit turn runs one-shot, so it names no conversation"
                return self._with_stderr_note(
                    dataclasses.replace(result, continuity_unavailable=note),
                    note,
                )
            return result
        return self._acp_result(result, opened[0])

    def _acp_result(
        self,
        result: HarnessResult,
        session: copilot_acp.CopilotAcpSession,
    ) -> HarnessResult:
        """Restate the turn's outcome from the protocol rather than the exit code.

        An ACP agent exits 0 whether the turn it ran succeeded or failed, and
        exits by our own ``terminate`` on the shutdown path, so its return code
        carries no verdict. The turn's own stop reason does -- and only
        ``end_turn`` is one: ``refusal``, ``cancelled`` and either of the two
        budget exhaustions all describe a turn that stopped short of the work it
        was given.

        Two outcomes are settled before that, because in both of them nothing
        was submitted at all and the agent's clean exit describes a turn that
        never started rather than one that succeeded.
        """
        conversation_error = session.conversation_error
        if conversation_error is not None:
            # The session was refused the moment its identifier could not be
            # persisted, before ``session/prompt``. Running the work anyway
            # would spend the turn into a conversation nothing could find again,
            # so the turn is reported abandoned and names no conversation --
            # which is the whole reason it refused to run.
            return self._with_stderr_note(
                dataclasses.replace(
                    result,
                    returncode=_CONVERSATION_ABANDON_RETURNCODE,
                    conversation_error=conversation_error,
                    conversation_id=None,
                    timed_out=False,
                    aborted=False,
                    interrupted=False,
                ),
                f"{self.name} turn abandoned: {conversation_error}",
            )
        cancelled = session.cancelled_before_submission
        if cancelled is not None:
            # The same code and the same window the base run reports for a
            # handshake cancellation it *could* raise out of, because it is the
            # same event: a turn stopped before it submitted anything. The
            # base run's exit code is overwritten rather than consulted --
            # closing stdin with nothing submitted races the wait loop, and a
            # clean exit from a turn that never started is the one thing it
            # must not be read as.
            return dataclasses.replace(
                result,
                returncode=137,
                aborted=True,
                timed_out=False,
                interrupted=False,
                cancel_stage=CANCEL_STAGE_HANDSHAKE,
            )
        for note in self._acp_notes(session):
            result = self._with_stderr_note(result, note)
        refused = session.restore_refused
        if refused is not None:
            result = dataclasses.replace(result, conversation_restore_refused=refused)
        if result.timed_out or result.aborted or result.interrupted:
            # The turn was stopped rather than failed, and the operator asked
            # for it, so these codes stand exactly as the base run reported.
            return result
        if (
            session.stop_reason == copilot_acp.STOP_REASON_END_TURN
            and session.protocol_error is None
        ):
            returncode = 0
        elif result.returncode > 0:
            returncode = result.returncode
        else:
            # 0 (a clean agent exit after an unfinished turn) and any negative
            # signal code from our own shutdown both mean "no verdict"; neither
            # may be reported as success.
            returncode = 1
        return dataclasses.replace(result, returncode=returncode)

    def _acp_notes(self, session: copilot_acp.CopilotAcpSession) -> list[str]:
        """Everything the conversation learned that the turn's log should say."""
        notes: list[str] = []
        error = session.protocol_error
        if error is not None:
            notes.append(f"copilot acp: {error}")
        if session.resumed:
            # Both counts, because only their difference says what happened: a
            # replayed transcript proves the conversation came back, and new
            # activity is the only evidence the resumed worker did anything
            # with it. A turn that replayed a hundred updates and produced none
            # of its own must not read like a turn that worked.
            notes.append(
                f"copilot acp resumed conversation {session.session_id}: "
                f"{session.replayed_updates} transcript updates replayed, "
                f"{session.live_updates} new"
            )
        stop_reason = session.stop_reason
        if stop_reason is not None and stop_reason != copilot_acp.STOP_REASON_END_TURN:
            notes.append(f"copilot acp stopped early: {stop_reason}")
        if stop_reason is None and session.prompt_submitted:
            notes.append("copilot acp turn ended without a stop reason")
        if session.cancel_requested:
            # Said whatever the turn ended as. The cancellation is issued
            # alongside the forced kill, so the process usually dies before the
            # prompt can answer ``cancelled`` -- and a turn's log that recorded
            # only the kill would not say the conversation had been told.
            notes.append("copilot acp cancelled the turn in flight")
        for mismatch in session.option_mismatches:
            notes.append(f"copilot acp launch control mismatch: {mismatch}")
        rejected = [
            decision
            for decision in session.permission_decisions
            if decision.decision != copilot_acp.DECISION_ALLOWED
        ]
        for decision in rejected:
            notes.append(
                f"copilot acp {decision.decision} a permission request "
                f"({decision.reason}): {decision.tool}"
            )
        return notes


def _read_last_message(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def get_adapter(name: str) -> HarnessAdapter:
    """Return the adapter for a supported harness name."""
    if name == "codex":
        return CodexAdapter()
    if name == "claude":
        return ClaudeAdapter()
    if name == "copilot":
        return CopilotAdapter()
    raise HarnessError(f"unsupported harness: {name}")


def delivers_guidance_next_turn(name: str) -> bool:
    """Whether ``name``'s guidance arrives with the next turn's prompt.

    Asked by the delivery spine rather than answered there, so the fact lives
    with the adapter that knows its harness's protocol.
    """
    try:
        return get_adapter(name).delivers_guidance_next_turn
    except HarnessError:
        return False


def run_harness(
    name: str,
    *,
    cwd: Path,
    plan_dir: Path | None,
    prompt: str,
    unsafe_bypass: bool = False,
    last_message_path: Path | None = None,
    timeout: int | None = None,
    stream_callback: StreamCallback | None = None,
    model: str | None = None,
    effort: str | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_final_result: Callable[[], None] | None = None,
    on_observed_model: Callable[[str], None] | None = None,
    access: AccessProfile = WORKER_ACCESS,
    abort: AbortSignal | None = None,
    live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
    env: Mapping[str, str] | None = None,
    on_conversation: ConversationCallback | None = None,
    on_prompt_submitted: SubmissionCallback | None = None,
    resume_conversation_id: str | None = None,
) -> HarnessResult:
    """Run one harness turn through the selected adapter.

    ``resume_conversation_id`` is the request half of continuity: the adapter
    builds its invocation to continue exactly that native conversation, or
    raises when it cannot. ``on_conversation`` is the capture half, called with
    the identifier the turn actually runs on before any work is submitted. They
    are independent -- a first turn captures without resuming -- and neither
    ever falls back to a different conversation.

    ``on_prompt_submitted`` is called at most once, from the point where this
    turn's prompt actually went out: the spawn for a turn that carries it on
    argv, the accepted ``turn/start`` or the written ``session/prompt`` for one
    that speaks a protocol. A turn that spawned and then refused -- a restore
    the provider declined, a cancellation caught mid-handshake -- never calls
    it, which is what lets a caller tell a delivered prompt from a started
    process.
    """
    return get_adapter(name).run(
        cwd=cwd,
        plan_dir=plan_dir,
        prompt=prompt,
        unsafe_bypass=unsafe_bypass,
        last_message_path=last_message_path,
        timeout=timeout,
        stream_callback=stream_callback,
        model=model,
        effort=effort,
        on_spawn=on_spawn,
        on_final_result=on_final_result,
        on_observed_model=on_observed_model,
        access=access,
        abort=abort,
        live_delivery=live_delivery,
        env=env,
        on_conversation=on_conversation,
        on_prompt_submitted=on_prompt_submitted,
        resume_conversation_id=resume_conversation_id,
    )


# Single source for the skill-invocation prefix rule: derived from the adapters
# themselves so the mapping cannot drift from what each harness actually parses.
_SKILL_PREFIX_ADAPTERS: tuple[type[HarnessAdapter], ...] = (
    CodexAdapter,
    ClaudeAdapter,
    CopilotAdapter,
)
SKILL_PREFIXES: dict[HarnessName, str] = {
    adapter.name: adapter.skill_prefix for adapter in _SKILL_PREFIX_ADAPTERS
}

#: Which harnesses can both report and resume a native conversation. Read off
#: the adapter classes rather than restated, so the capability a continuity
#: caller is told about and the capability :meth:`HarnessAdapter.run` enforces
#: are the same fact: an adapter cannot be advertised as wired without also
#: accepting the calls that make it wired.
CONTINUES_CONVERSATIONS: dict[HarnessName, bool] = {
    adapter.name: bool(adapter.captures_conversations and adapter.resumes_conversations)
    for adapter in _SKILL_PREFIX_ADAPTERS
}


def continues_conversations(name: str) -> bool:
    """Whether ``name`` can carry one conversation across turns.

    Read from the adapter classes, so it needs no local harness configuration
    and answers the same for an operator who has never installed the harness.
    """
    for harness, wired in CONTINUES_CONVERSATIONS.items():
        if harness == name:
            return wired
    return False


def detect_invoking_harness(
    env: Mapping[str, str] | None = None,
) -> HarnessName | None:
    """Best-effort detection of the harness that launched this process.

    Returns the single harness whose environment markers are present, or
    ``None`` when zero or more than one match (ambiguous). Detection only
    orders human-facing output — it never gates behavior — so unknown or
    conflicting environments fall back to ``None`` and the default ordering.
    """
    source = os.environ if env is None else env
    detected: list[HarnessName] = []
    if source.get("CLAUDECODE"):
        detected.append("claude")
    if any(key.startswith("CODEX_") for key in source):
        detected.append("codex")
    if any(
        key.startswith("COPILOT_") or key.startswith("GITHUB_COPILOT_")
        for key in source
    ):
        detected.append("copilot")
    if len(detected) == 1:
        return detected[0]
    return None


def _ordered_harness_names(
    env: Mapping[str, str] | None = None,
) -> list[HarnessName]:
    """Harness names with the detected invoking harness (if any) first."""
    detected = detect_invoking_harness(env)
    if detected is None:
        return list(HARNESS_NAMES)
    return [detected, *(name for name in HARNESS_NAMES if name != detected)]


def skill_invocations(
    skill: str,
    argument: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[HarnessName, str]:
    """Return ready-to-paste ``<prefix><skill>[ <argument>]`` per harness.

    The mapping is ordered with the best-effort invoking harness first (when it
    is unambiguously detected) so callers can surface the most likely command at
    the top; ordering never changes which harnesses appear.
    """
    suffix = f" {argument}" if argument else ""
    return {
        name: f"{SKILL_PREFIXES[name]}{skill}{suffix}"
        for name in _ordered_harness_names(env)
    }
