"""Harness adapter boundaries.

Adapters construct argv lists and run harnesses without shell interpolation.
This module owns what every adapter shares -- the result and invocation types,
the cancellation and conversation seams, the generic turn loop on
:class:`HarnessAdapter` -- and the registry that selects one. Each adapter lives
in its own module (``harness_codex``, ``harness_claude``, ``harness_copilot``),
imports its base from here, and is imported back only lazily by the registry.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
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

    def input_hold_reason(self) -> str | None:
        """Why stdin is still held open after a terminal result, or ``None``.

        ``None`` -- the base answer -- means there is no held input to release.
        """
        return None

    def release_input(self) -> bool:
        """Close a held stdin; ``True`` when this call closed it."""
        return False


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


def _read_last_message(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _adapter_classes() -> tuple[type[HarnessAdapter], ...]:
    """The shipped adapter classes, in :data:`HARNESS_NAMES` order.

    Imported here rather than at module top because each adapter module
    subclasses :class:`HarnessAdapter` and so imports this one: deferring the
    reverse edge to call time keeps the import graph one-way, so any of the
    four modules can be imported first.
    """
    from . import harness_claude, harness_codex, harness_copilot

    return (
        harness_codex.CodexAdapter,
        harness_claude.ClaudeAdapter,
        harness_copilot.CopilotAdapter,
    )


def get_adapter(name: str) -> HarnessAdapter:
    """Return the adapter for a supported harness name."""
    for adapter in _adapter_classes():
        if adapter.name == name:
            return adapter()
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
    on_transport: Callable[[HarnessTransport | None], None] | None = None,
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
        on_transport=on_transport,
    )


# Single source for the skill-invocation prefix rule: derived from the adapters
# themselves so the mapping cannot drift from what each harness actually parses.
def skill_prefixes() -> dict[HarnessName, str]:
    """The skill-invocation prefix each harness parses, keyed by harness name."""
    return {adapter.name: adapter.skill_prefix for adapter in _adapter_classes()}


def continues_conversations(name: str) -> bool:
    """Whether ``name`` can carry one conversation across turns.

    Read from the adapter classes rather than restated, so the capability a
    continuity caller is told about and the capability
    :meth:`HarnessAdapter.run` enforces are the same fact: an adapter cannot be
    advertised as wired without also accepting the calls that make it wired.
    It needs no local harness configuration and answers the same for an
    operator who has never installed the harness.
    """
    for adapter in _adapter_classes():
        if adapter.name == name:
            return bool(
                adapter.captures_conversations and adapter.resumes_conversations
            )
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
    prefixes = skill_prefixes()
    return {
        name: f"{prefixes[name]}{skill}{suffix}" for name in _ordered_harness_names(env)
    }
