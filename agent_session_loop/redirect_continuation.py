"""One turn's redirect gate, and the correction the next attempt carries.

Sessions 9 and 10 built the two halves this module joins:
``redirect_admission`` decides whether a correction may be admitted at all and
appends it to the journal, and ``redirect_control`` owns the escalation clock
that force-stops a worker which ignores an accepted one. Neither of them knows
what the *run* does afterwards. This module is that seam, and it holds exactly
two things.

:class:`TurnRedirect` is the turn-scoped gate. It watches the urgent wakeup the
run's redirect endpoint sets, accepts what the journal offers against *this*
turn, and serializes acceptance against the turn's own settlement so the two
can never both win:

* An accepted redirect takes the latch first, so the turn it interrupted can no
  longer be settled as a completion -- ``turn_was_redirected`` reads the
  acceptance fact out of the turn record, and the disposition is decided from
  that rather than from the harness result. A worker that is killed mid-turn
  and one that exits cleanly a millisecond after the acceptance landed are the
  same outcome here, because in both cases an operator was told their
  correction reached a live turn.
* A settled turn takes the latch first, and every later submission is refused
  rather than accepted against a turn that is already over. The refusal is
  recorded, not swallowed: the request stays pending in the journal and expires
  on its own terms, which is how a correction aimed at a turn that has just
  ended stays visible to the operator who sent it.

:class:`PendingRedirect` is what survives the turn. It carries the operation
ids, the conversation the interrupted attempt ran on, and the attempt lineage
-- never the feedback text. The text stays in the journal, reachable only
through ``redirect_store_journal.read_feedback``, which is the single egress that
module exists to keep it behind; a copy in the run payload would put an
operator's words into every state file, projection and diagnostic export that
reads one.

Nothing here imports the runner or ``run_loop``. Unlike ``turn_dispositions``
this module may own a loop, and does: the urgent watch is a thread, which is
what lets the run loop keep its single ``while`` while still answering a
correction in the middle of a turn rather than at the next control poll.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    conversations,
    redirect_admission,
    redirect_control,
    redirect_restart,
    redirect_store_models,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: The terminating disposition an accepted redirect gives its turn. Named here
#: rather than in ``handoff_guards`` because the fact it reports is this
#: module's: the guard only reads the record back out.
DISPOSITION_REDIRECTED = "redirected"

#: Where the turn record keeps this gate's projection.
TURN_PAYLOAD_KEY = "redirect"

#: How long the watch waits between polls of the urgent event. The event is set
#: by the redirect endpoint's reader thread the moment a submission lands, so
#: this bound is what a *missed* wakeup costs, not what an ordinary one does.
WATCH_POLL_SECONDS = 0.05

#: How long an acceptance waits for the worker pid to be bound. A submission
#: can reach the endpoint before the harness process exists; the controller
#: cannot be built without the pid it is permitted to stop, so the acceptance
#: waits for it rather than being refused for a race the operator did not lose.
WORKER_BIND_WAIT_SECONDS = 5.0

#: How long teardown waits for the escalation's report. Sized off the two waits
#: the escalation itself performs -- the descendant sample and the collection --
#: plus slack, so a report is missing here only when the escalation is wedged.
TERMINATION_REPORT_WAIT_SECONDS = (
    redirect_control.EVIDENCE_WAIT_SECONDS
    + redirect_control.COLLECTION_WAIT_SECONDS
    + 5.0
)

#: The turn had already been settled when the correction reached the gate.
REFUSE_TURN_SETTLED = "turn-already-settled"

#: No worker pid was bound in time, so there was nothing to interrupt.
REFUSE_WORKER_UNBOUND = "worker-unbound"

#: The urgent channel could not be read at all.
REFUSE_CHANNEL_UNREADABLE = "redirect-channel-unreadable"

REFUSAL_REASONS: tuple[str, ...] = (
    REFUSE_TURN_SETTLED,
    REFUSE_WORKER_UNBOUND,
    REFUSE_CHANNEL_UNREADABLE,
)


def turn_was_redirected(turn_payload: Mapping[str, Any]) -> bool:
    """Whether an accepted correction ended this turn.

    Read off the acceptance the gate recorded, never off the harness result.
    A redirect arms the same abort signal an operator ``abort-turn`` does, so
    ``result.aborted`` cannot tell the two apart -- and a worker that finished
    inside the grace window sets neither flag while still owing the operator
    the attempt their correction was promised.
    """
    record = turn_payload.get(TURN_PAYLOAD_KEY)
    if not isinstance(record, Mapping):
        return False
    accepted = record.get("accepted")
    return isinstance(accepted, Sequence) and bool(accepted)


# ---------------------------------------------------------------------------
# What an acceptance leaves behind
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AcceptedRedirect:
    """One correction this turn was interrupted for.

    Carries the digest and the byte count rather than the feedback, for the
    reason ``redirect_store_models.RedirectRequest`` already states: they are enough to
    prove *which* correction this is in any surface, and the words themselves
    stay behind the journal's single reader.
    """

    operation_id: str
    target: redirect_store_models.RedirectTarget
    accepted_at: str
    feedback_digest: str
    feedback_bytes: int

    @classmethod
    def from_request(
        cls, request: redirect_store_models.RedirectRequest
    ) -> AcceptedRedirect:
        return cls(
            operation_id=request.operation_id,
            target=request.target,
            accepted_at=request.settled_at or clock.utc_iso_now(),
            feedback_digest=request.feedback_digest,
            feedback_bytes=request.feedback_bytes,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "accepted_at": self.accepted_at,
            "feedback_digest": self.feedback_digest,
            "feedback_bytes": self.feedback_bytes,
            "target": self.target.to_payload(),
        }


@dataclasses.dataclass(frozen=True)
class PendingRedirect:
    """The correction the next attempt of the same session has to honour.

    ``harness`` and ``conversation_id`` are the interrupted attempt's, and they
    are pinned rather than re-decided: the correction was written against what
    that attempt was doing, and a successor that silently switched harnesses
    would either lose the conversation the operator is correcting or fail the
    resume outright.

    ``attempt`` counts redirected attempts of this session, so a second
    correction against an attempt that was itself redirected extends a lineage
    instead of starting one.
    """

    session_id: str
    harness: str | None
    conversation_id: str | None
    predecessor_turn_index: int
    attempt: int
    operation_ids: tuple[str, ...]
    feedback_digests: tuple[str, ...]
    feedback_bytes: int

    @classmethod
    def after_turn(
        cls,
        accepted: Sequence[AcceptedRedirect],
        *,
        session_id: str,
        harness: str | None,
        conversation_id: str | None,
        turn_index: int,
        previous: PendingRedirect | None = None,
    ) -> PendingRedirect | None:
        """Fold this turn's acceptances into the correction to carry forward.

        Returns ``None`` when the turn accepted nothing. A ``previous`` from the
        same session is extended rather than replaced: an operator who sent a
        second correction while the first was still being applied meant both.
        """
        if not accepted:
            return previous
        carried = (
            previous
            if previous is not None and previous.session_id == session_id
            else None
        )
        operation_ids = list(carried.operation_ids if carried is not None else ())
        digests = list(carried.feedback_digests if carried is not None else ())
        total_bytes = carried.feedback_bytes if carried is not None else 0
        for record in accepted:
            if record.operation_id in operation_ids:
                continue
            operation_ids.append(record.operation_id)
            digests.append(record.feedback_digest)
            total_bytes += record.feedback_bytes
        return cls(
            session_id=session_id,
            harness=harness,
            conversation_id=conversation_id,
            predecessor_turn_index=turn_index,
            attempt=(carried.attempt + 1) if carried is not None else 1,
            operation_ids=tuple(operation_ids),
            feedback_digests=tuple(digests),
            feedback_bytes=total_bytes,
        )

    def resumable_operation_ids(self) -> tuple[str, ...]:
        """The ids a resumed conversation may be told were interrupted.

        The journal accepts any bounded non-empty string as an operation id and
        the conversation ledger accepts a narrower charset and at most
        ``conversations.MAX_INTERRUPTED_OPERATIONS`` of them, so the two sets
        are not the same. An id the ledger would refuse is dropped here rather
        than raised: the correction it names is still carried -- the worker
        reads its words out of the journal either way -- and failing the turn
        over the shape of an identifier would spend the operator's correction on
        an exception. The full list stays in the event the run records, so what
        was narrowed is visible rather than silent.
        """
        keep: list[str] = []
        for operation_id in self.operation_ids:
            try:
                keep.append(conversations.validate_operation_id(operation_id))
            except conversations.ConversationError:
                continue
        return tuple(keep[: conversations.MAX_INTERRUPTED_OPERATIONS])

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "harness": self.harness,
            "conversation_id": self.conversation_id,
            "predecessor_turn_index": self.predecessor_turn_index,
            "attempt": self.attempt,
            "operation_ids": list(self.operation_ids),
            "feedback_digests": list(self.feedback_digests),
            "feedback_bytes": self.feedback_bytes,
        }


# ---------------------------------------------------------------------------
# The turn-scoped gate
# ---------------------------------------------------------------------------


class TurnController(Protocol):
    """The escalation, as this gate needs it.

    Declared structurally rather than as
    :class:`~agent_session_loop.redirect_control.UrgentController` itself
    because the real one kills process groups: a test that wanted to drive the
    serialization would have to either aim a `SIGKILL` at its own runner or
    accept a stand-in here. Naming the five members reached is also what pins
    how little of the controller this gate is entitled to touch -- it may not
    arm, abandon, or join one, because those are the controller's own ordering
    and taking them apart from out here would let an acceptance be reported
    with nothing counting down.
    """

    def accept(
        self,
        plan_dir: str | Path,
        operation_id: str,
        *,
        target: redirect_store_models.RedirectTarget,
        state_dir: str | Path | None = None,
    ) -> redirect_store_models.RedirectRequest: ...

    def release(self) -> None: ...

    def wait_escalated(self, timeout: float | None = None) -> bool: ...

    def require_successor_allowed(
        self,
    ) -> redirect_control.TerminationReport | None: ...

    def to_payload(self) -> dict[str, Any]: ...


ControllerFactory = Callable[[redirect_control.OwnedWorker], TurnController]
PendingReader = Callable[..., tuple[redirect_store_models.RedirectRequest, ...]]


class TurnRedirect:
    """The latch a turn's completion and its corrections contend for.

    One per turn. The latch is a single lock guarding three facts -- whether
    the turn is settled, which corrections were accepted, and which were
    refused -- and the journal append happens *inside* it. That is deliberate
    and is the whole serialization: an acceptance that has begun appending
    blocks the settlement until the record is durable, and a settlement that
    won the latch refuses every later acceptance without reading the journal at
    all.

    Every collaborator is injectable because none of them can be exercised for
    real in a test: ``build_controller`` is how the escalation is driven without
    killing anything, ``read_pending`` is how the journal is offered without a
    live producer, and ``wakeup`` is the same event the run's redirect endpoint
    sets from its reader thread.
    """

    def __init__(
        self,
        *,
        plan_dir: str | Path,
        target: redirect_store_models.RedirectTarget,
        state_dir: str | Path | None = None,
        wakeup: threading.Event | None = None,
        cancellation: redirect_control.CancellableTurn | None = None,
        build_controller: ControllerFactory | None = None,
        read_pending: PendingReader = redirect_admission.pending_redirects,
        poll_seconds: float = WATCH_POLL_SECONDS,
        bind_wait_seconds: float = WORKER_BIND_WAIT_SECONDS,
        report_wait_seconds: float = TERMINATION_REPORT_WAIT_SECONDS,
    ) -> None:
        self._plan_dir = Path(plan_dir)
        self._target = target
        self._state_dir = state_dir
        self._wakeup = wakeup
        self._cancellation = cancellation
        self._build_controller = build_controller or self._default_controller
        self._read_pending = read_pending
        self._poll = max(0.001, float(poll_seconds))
        self._bind_wait = max(0.0, float(bind_wait_seconds))
        self._report_wait = max(0.0, float(report_wait_seconds))

        self._lock = threading.Lock()
        self._settled = False
        self._armed = False
        self._accepted: list[AcceptedRedirect] = []
        self._refused: list[dict[str, Any]] = []
        self._controller: TurnController | None = None
        self._report_wait_exhausted = False

        self._bound = threading.Event()
        self._stop = threading.Event()
        self._watch: threading.Thread | None = None

    # -- state ---------------------------------------------------------------

    @property
    def target(self) -> redirect_store_models.RedirectTarget:
        return self._target

    @property
    def settled(self) -> bool:
        with self._lock:
            return self._settled

    @property
    def redirected(self) -> bool:
        """Whether any correction was accepted against this turn."""
        with self._lock:
            return bool(self._accepted)

    @property
    def accepted(self) -> tuple[AcceptedRedirect, ...]:
        with self._lock:
            return tuple(self._accepted)

    def to_payload(self) -> dict[str, Any]:
        with self._lock:
            accepted = [record.to_payload() for record in self._accepted]
            refused = [dict(record) for record in self._refused]
            settled = self._settled
            exhausted = self._report_wait_exhausted
            controller = self._controller if self._armed else None
        return {
            "target": self._target.to_payload(),
            "accepted": accepted,
            "refused": refused,
            "settled": settled,
            "report_wait_exhausted": exhausted,
            "control": controller.to_payload() if controller is not None else None,
        }

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Begin watching the urgent channel for corrections against this turn.

        A gate with no wakeup event watches nothing. That is the ordinary state
        of a run whose redirect endpoint could not be opened: ``submit_redirect``
        refuses such a run outright, so there is nothing for a watch to find.
        """
        if self._wakeup is None or self._watch is not None:
            return
        self._watch = threading.Thread(
            target=self._watch_urgent_channel,
            name="asl-redirect-watch",
            daemon=True,
        )
        self._watch.start()

    def bind_worker(self, pid: int) -> None:
        """Name the process an accepted correction is permitted to stop.

        Called from the turn's spawn callback. Ownership is stated at spawn
        time for the reason ``OwnedWorker`` gives: a controller that looked the
        pid up at its deadline would be aiming at whatever that number means by
        then.

        A turn can spawn more than once: Codex falls back from its app server
        to ``codex exec`` and reports the new pid through the same callback,
        after the first process has exited. Until a correction has armed the
        controller, nothing has been promised about the first worker, so the
        gate rebinds to the live one rather than leave a redirect aimed at a
        dead pid. An armed controller keeps the worker it was armed against.
        """
        replaced: TurnController | None = None
        with self._lock:
            if not self._settled and not self._armed:
                replaced = self._controller
                self._controller = self._build_controller(
                    redirect_control.OwnedWorker.session_leader(pid)
                )
                configure = getattr(self._controller, "set_recovery_directory", None)
                if configure is not None:
                    configure(
                        redirect_restart.evidence_dir(
                            self._plan_dir, self._target, self._state_dir
                        )
                    )
        if replaced is not None:
            # Never armed, so releasing it stands it down without a report.
            replaced.release()
        self._bound.set()

    def settle(self) -> bool:
        """Close this turn to corrections, reporting whether it was clean.

        Returns ``True`` when this call closed a turn nothing had redirected --
        the ordinary outcome -- and ``False`` when a correction had already been
        accepted, which is the fact that denies this attempt its completion.
        Idempotent: the second caller sees the first one's answer.
        """
        with self._lock:
            self._settled = True
            return not self._accepted

    def close(self) -> None:
        """End the gate with the turn it governs.

        Ordered: settle first so nothing new can be accepted while the watch is
        being stopped, then stop the watch, then release the controller. The
        controller is released only after the turn's own owner has finished with
        the worker, which is what lets its report claim the predecessor was
        collected rather than merely killed.

        The report wait can expire, and that outcome is recorded rather than
        dropped. It does not decide anything here -- the escalation may still
        publish between this line and the gate, and admitting it then is
        correct -- but a teardown that waited out its whole bound and got
        nothing is the fact an operator reading a blocked continuation needs,
        and it is visible nowhere else once this call returns.
        """
        self.settle()
        self._stop.set()
        watch = self._watch
        if watch is not None:
            watch.join(timeout=self._poll * 4.0 + 1.0)
            self._watch = None
        with self._lock:
            controller = self._controller
            armed = self._armed
        if controller is None:
            return
        controller.release()
        # Only an armed controller has a watchdog that will ever publish a
        # report. Waiting on an unarmed one would spend the whole timeout on
        # every ordinary turn.
        if armed and not controller.wait_escalated(self._report_wait):
            with self._lock:
                self._report_wait_exhausted = True

    # -- acceptance ----------------------------------------------------------

    def accept(
        self, request: redirect_store_models.RedirectRequest
    ) -> AcceptedRedirect | None:
        """Accept one correction against this turn, or refuse and say why.

        The bind wait happens outside the latch: a correction that arrives
        before the harness process exists must not hold the settlement of some
        other turn hostage while it waits. Everything after it happens inside,
        because acceptance and settlement are the two things that may not
        interleave.
        """
        if not self._bound.wait(self._bind_wait):
            self._refuse(request.operation_id, REFUSE_WORKER_UNBOUND)
            return None
        with self._lock:
            if self._settled:
                self._refuse_locked(request.operation_id, REFUSE_TURN_SETTLED)
                return None
            controller = self._controller
            if controller is None:
                self._refuse_locked(request.operation_id, REFUSE_WORKER_UNBOUND)
                return None
            self._armed = True
            try:
                settled = controller.accept(
                    self._plan_dir,
                    request.operation_id,
                    target=self._target,
                    state_dir=self._state_dir,
                )
            except redirect_store_models.RedirectStoreError as error:
                self._refuse_locked(request.operation_id, error.reason)
                return None
            record = AcceptedRedirect.from_request(settled)
            self._accepted.append(record)
            return record

    def require_successor_allowed(self) -> redirect_control.TerminationReport | None:
        """Raise unless nothing of the redirected turn is still running.

        Returns ``None`` when this turn was never redirected, which is every
        turn but the ones this module exists for. Raises
        :class:`~agent_session_loop.redirect_control.SuccessorBlockedError`
        otherwise, and the caller turns that into a pause: a successor that
        started beside a live predecessor would have two workers holding the
        same plan lock.

        A turn that *did* accept a correction may not leave here without a
        report. The controller refuses that case on its own, and this gate
        refuses it again on its own record of what it accepted, because the two
        facts have different owners: the controller knows whether an acceptance
        was made durable, and only this latch knows which corrections this turn
        was interrupted for. A controller that answered ``None`` to an accepted
        redirect has contradicted the gate, and the one reading that is not the
        one entitled to resolve it in favour of continuing.
        """
        with self._lock:
            controller = self._controller
            armed = self._armed
            redirected = bool(self._accepted)
        if controller is None or not armed:
            return None
        report = controller.require_successor_allowed()
        if report is None and redirected:
            raise redirect_control.SuccessorBlockedError(
                redirect_control.BLOCK_UNREPORTED, None
            )
        return report

    # -- internals -----------------------------------------------------------

    def _default_controller(
        self, worker: redirect_control.OwnedWorker
    ) -> redirect_control.UrgentController:
        """Build this turn's escalation clock.

        ``transport`` is deliberately absent. The harness transport is internal
        to the adapter driving the turn and is never handed to the run loop, so
        there is nothing honest to pass; the protocol-level interrupt is already
        issued by the harness's own abort path, which the ``cancellation`` seam
        below arms. What the controller adds is the forced stop, which is the
        part no adapter performs.
        """
        return redirect_control.UrgentController(
            worker=worker, cancellation=self._cancellation
        )

    def _refuse(self, operation_id: str | None, reason: str) -> None:
        with self._lock:
            self._refuse_locked(operation_id, reason)

    def _refuse_locked(self, operation_id: str | None, reason: str) -> None:
        """Record a refusal without settling the request in the journal.

        Left pending on purpose. The runner refusing to act on a correction is
        not the same claim as the journal declaring it answered, and a request
        that stays submitted expires on the deadline its producer was told
        about rather than on this turn's schedule.
        """
        self._refused.append({"operation_id": operation_id, "reason": reason})

    def _watch_urgent_channel(self) -> None:
        """Drain the journal whenever the urgent endpoint says something landed.

        Woken rather than polled: ``redirect_admission`` refuses any submission
        it cannot deliver urgently, so an event is the only way a correction
        reaches a live turn and a timed poll would only add latency to the one
        path that is measured in seconds. The first pass runs unconditionally,
        because a submission can land between the endpoint opening and this
        turn starting.
        """
        assert self._wakeup is not None
        drain = True
        while not self._stop.is_set():
            if drain:
                self._drain()
            if self.settled:
                return
            drain = self._wakeup.wait(self._poll)
            if drain:
                self._wakeup.clear()

    def _drain(self) -> None:
        """Offer every pending correction aimed at this turn to the gate."""
        try:
            requests = self._read_pending(
                self._plan_dir, target=self._target, state_dir=self._state_dir
            )
        except redirect_store_models.RedirectStoreError as error:
            self._refuse(None, error.reason)
            return
        except OSError:
            self._refuse(None, REFUSE_CHANNEL_UNREADABLE)
            return
        for request in requests:
            if self.settled:
                return
            self.accept(request)
