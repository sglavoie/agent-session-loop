"""Shared network harness for the full-stack Workbench tests.

Holds the clock, the pumper, the link and the linked lanes.

The constants and the :class:`Link` here are the only pieces the full-stack
modules write for themselves; everything else they drive is the real deployment.
"""

from __future__ import annotations

import contextlib
import threading
from collections import deque
from collections.abc import Mapping
from typing import Any

from asl_workbench import connector_runtime as portable_runtime
from asl_workbench import connector_support as portable_support
from asl_workbench import (
    observation,
    operations,
    remote_reads,
    transport,
)
from asl_workbench.store_workbench import WorkbenchStore
from tests.workbench_support import (
    SHELL_FROZEN_NOW,
)

#: The Mac and the Pi, named the way the transport requires.
CONNECTOR_ID = "mac-studio"
TARGET_ID = "pi-1"

#: One fixed moment. Every clock in this module is an argument, so the whole
#: stack can be advanced deliberately instead of waited on.
NOW = SHELL_FROZEN_NOW

#: Wide enough that no test here has to age a fixture by a week, and tight
#: enough that :class:`LinkFailureTests` can make a heartbeat stale in seconds.
POLICY = observation.ObservationPolicy(
    observation_stale_seconds=60.0,
    lost_observation_alert_seconds=120.0,
    unanswered_question_alert_seconds=120.0,
    summary_retention_days=7.0,
)

#: How many steps a cycle needs to drain. One frame leaves per step, and a
#: cycle over this corpus is a heartbeat, a snapshot, a catalog page and a
#: history batch, so this is several cycles' worth of headroom.
DRAIN_STEPS = 24

#: The heartbeat cadence the harness runs the connector at.
#:
#: Tighter than the shipped 15s default, because the heartbeat is also the
#: carrier a downward request rides on: at the default, every correlated read
#: here would wait most of its 20s deadline for a carrier and the module would
#: spend its time proving the arithmetic of a schedule that
#: ``tests/test_workbench_connector.py`` already covers. The deployment tightens
#: to :data:`~asl_workbench.connector_support.ACTIVE_READ_INTERVAL_SECONDS` -- one
#: second -- whenever a service is asking, so this is a cadence the real link
#: runs at and not one invented for the test.
HEARTBEAT_SECONDS = 2.0

#: How far the shared clock advances between two connector steps.
#:
#: Finer than the carrier cadence on purpose. Liveness outranks everything in
#: the connector's own selection order, so a step that advances the clock by a
#: full carrier interval finds a fresh heartbeat waiting every single time and
#: no answer is ever chosen -- the harness would starve the read lane by the
#: granularity of its own clock rather than by anything the deployment does. A
#: real link steps far more often than it heartbeats, and this is that ratio.
STEP_SECONDS = 0.25

#: How many steps one correlated read is given before its own deadline settles
#: it. At :data:`STEP_SECONDS` this is a handful of carrier intervals, which is
#: several chances at a carrier and still well inside the read's deadline.
READ_STEPS = 64


class Clock:
    """One moment, shared by the Mac, the link and the Pi.

    Every clock in the stack is an injected argument, so the alternative to
    sharing one is three that disagree -- and a Pi whose clock trails the Mac's
    turns every deadline in the transport into a race. Sharing it also makes the
    deadline arithmetic real: a read issued at ``now`` and answered twelve
    stepped seconds later has actually spent twelve seconds of its deadline.
    """

    def __init__(self, start: float) -> None:
        self.now = float(start)

    def tick(self, seconds: float = 1.0) -> float:
        self.now += float(seconds)
        return self.now

    def reading(self) -> float:
        return self.now


class Pumper:
    """Steps the connector once per round, for however many threads are waiting.

    A request thread that is waiting for an answer needs the connector to keep
    stepping, and several of them wait at once. Letting each take its own step
    would be wrong twice: it races inside the connector, which a deployment
    never does because its loop is one thread, and it advances the *shared*
    clock once per waiter, so six concurrent reads would burn every deadline six
    times as fast and the harness would time out reads the deployment answers.
    So a step is a round: whoever gets there first takes it, and everybody else
    waits for that round rather than taking one of their own.
    """

    def __init__(
        self,
        *,
        connector: portable_runtime.ObservationConnector,
        clock: Clock,
        step_seconds: float = 0.25,
    ) -> None:
        self.connector = connector
        self.clock = clock
        self.step_seconds = step_seconds
        self._cond = threading.Condition()
        self._stepping = False
        self._rounds = 0

    @property
    def rounds(self) -> int:
        with self._cond:
            return self._rounds

    def pump(self, seen: int) -> int:
        """Advance one round past ``seen``, and return the round now completed."""
        with self._cond:
            if self._rounds > seen:
                return self._rounds
            if self._stepping:
                self._cond.wait(timeout=5.0)
                return self._rounds
            self._stepping = True
        try:
            moment = self.clock.tick(self.step_seconds)
            # A down link is reported through the connector's state, not raised
            # at whoever happened to be stepping it.
            with contextlib.suppress(portable_support.ChannelError):
                self.connector.step(moment)
        finally:
            with self._cond:
                self._stepping = False
                self._rounds += 1
                self._cond.notify_all()
        return self.rounds


def inline(attempt: Any) -> None:
    """Run a connector worker on the calling thread.

    Every test here drives the clock, so a real thread would only add a join
    and a race. The isolation property those threads exist for is proven in
    ``tests/test_workbench_connector.py`` against an executor that never runs
    the pass at all; repeating it here would test the executor twice and the
    assembly never.
    """
    attempt()
    return None


# ---------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------


class LinkDown(portable_support.ChannelError):
    """The link is not there. Raised as the channel contract requires."""


class Link:
    """The network between the Mac and the Pi, with faults it can be given.

    A :class:`~asl_workbench.connector_protocols.Channel`, and on the far side of it the
    Pi's real ingestion. Everything that crosses is validated by
    :func:`~asl_workbench.transport.validate_frame` in the direction it is
    travelling and then handed to the object that owns it -- the store for an
    observation, the read client for an answer, the journal for a settlement --
    so this class decides *whether* bytes arrive and never *what they mean*.

    The faults are the ones a tailnet actually has, and they are kept apart on
    purpose. ``offline`` is a link that is not there, which the connector must
    back off from. ``losing`` is a link that carried the frame and lost the
    acknowledgement, which is the case where the sender cannot tell delivery
    from loss and the receiver has already acted. Collapsing them into one flag
    is how a retry turns into a second mutation.
    """

    def __init__(
        self,
        store: WorkbenchStore,
        *,
        clock: Clock | None = None,
        target_id: str = TARGET_ID,
        reads: remote_reads.DetailReadClient | None = None,
    ) -> None:
        self.store = store
        self.clock = clock if clock is not None else Clock(NOW)
        self.target_id = target_id
        self.reads = reads
        #: Every upward envelope that landed, in order, as the Pi saw it.
        self.landed: list[dict[str, Any]] = []
        #: Every answer the read client settled, for assertions about lanes.
        self.answers: list[remote_reads.ReadAnswer] = []
        #: Frames to hand back inside the next acknowledgement.
        self.carry: deque[Mapping[str, Any]] = deque()
        self.attempts = 0
        self.offline = False
        self.losing = False
        self.refused: list[str] = []
        #: Every operation id this link actually handed down, in order.
        self.dispatched: list[str] = []
        #: Several request threads read :attr:`answers` while the stepping
        #: thread appends to it, which is exactly the shape a real service has.
        self.lock = threading.Lock()

    def take(self, request_id: str) -> remote_reads.ReadAnswer | None:
        """The settled answer to one request, removed, or ``None``."""
        with self.lock:
            for answer in self.answers:
                if answer.request_id == request_id:
                    self.answers.remove(answer)
                    return answer
        return None

    # -- faults ----------------------------------------------------------
    def down(self) -> None:
        self.offline = True

    def up(self) -> None:
        self.offline = False

    # -- the channel -----------------------------------------------------
    def send(self, frame: bytes) -> bytes | None:
        self.attempts += 1
        if self.offline:
            raise LinkDown("the link is down")
        checked = transport.validate_frame(
            frame, direction=transport.DIRECTION_UP, target_id=self.target_id
        )
        self.landed.append(checked)
        self.deliver(checked)
        if self.losing:
            # Delivered, and the sender will never know. The frame is gone
            # after the far end acted on it, which is the only ordering that
            # makes a duplicate possible.
            raise LinkDown("the acknowledgement was lost")
        if self.carry:
            handed = self.carry.popleft()
            self.dispatch(handed)
            return transport.encode_frame(handed)
        return None

    def dispatch(self, envelope: Mapping[str, Any]) -> None:
        """Mark a command dispatched at the moment it actually leaves.

        The journal's ``pending`` means "admitted and still here", so the row
        moves the instant the command is handed to an acknowledgement and not
        when it was queued: a submission that never found a carrier is one the
        operator may still cancel, and a submission that left is one whose
        outcome is now the Mac's to report. Doing it here also makes the
        dispatch boundary a real place -- a report lost after this point leaves
        a row that says dispatched and unsettled, which is the state the
        operator must never be shown as finished.
        """
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return
        if payload.get("request") != transport.REQUEST_MUTATE:
            return
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            return
        if self.store.operation(operation_id) is None:
            return
        self.store.advance_operation(
            operation_id,
            operations.OPERATION_DISPATCHED,
            now=self.clock.now,
            dispatched_at=self.clock.now,
        )
        self.dispatched.append(operation_id)

    # -- the far end -----------------------------------------------------
    def deliver(self, envelope: Mapping[str, Any]) -> None:
        """Apply one arrived frame the way the Pi's own service would."""
        kind = str(envelope["kind"])
        if kind == transport.KIND_OBSERVATION:
            self.ingest(envelope)
            return
        if kind == transport.KIND_OPERATION:
            self.settle(envelope)
            return
        if self.reads is not None:
            answer = self.reads.accept(envelope, now=self.clock.now)
            if answer is not None:
                with self.lock:
                    self.answers.append(answer)

    def ingest(self, envelope: Mapping[str, Any]) -> Any:
        """One observation, through the store's own path for its view."""
        view = str(envelope["payload"]["view"])
        applied = {
            transport.VIEW_HEARTBEAT: self.store.ingest_heartbeat,
            transport.VIEW_SNAPSHOT: self.store.ingest_snapshot,
            transport.VIEW_CATALOG_PAGE: self.store.ingest_catalog,
            transport.VIEW_HISTORY_BATCH: self.store.ingest_history,
        }[view]
        return applied(envelope, received_at=self.clock.now)

    def settle(self, envelope: Mapping[str, Any]) -> None:
        """One settlement, into the journal row the submission created.

        A report about an operation this Pi never admitted is dropped rather
        than inserted: the journal is the record of what *this* service handed
        down, and a row invented from an incoming frame would be a command
        nobody here can account for.
        """
        payload = envelope["payload"]
        operation_id = str(payload["operation_id"])
        if self.store.operation(operation_id) is None:
            self.refused.append(operation_id)
            return
        self.store.advance_operation(
            operation_id,
            str(payload["status"]),
            now=self.clock.now,
            detail_code=payload.get("detail_code"),
            dispatched_at=payload.get("dispatched_at"),
            settled_at=payload.get("settled_at"),
        )


class LinkedDetailLane:
    """The Pi's read lane, wired through the connector instead of past it.

    ``fetch`` is called on a request thread and must return an answer, so the
    waiting has to happen somewhere. Here it happens by pumping the connector:
    the request goes into the link's carry queue, the next acknowledgement hands
    it to the Mac, the Mac answers it inline, and the answer comes back up the
    same link into
    :meth:`~asl_workbench.remote_reads.DetailReadClient.accept`. A deployment
    spends that interval blocked on a socket; this spends it advancing a clock,
    and the path the frames take is identical.

    The pumping goes through a shared :class:`Pumper` rather than happening
    here, so that concurrent readers cooperate instead of each driving their own
    connector and their own clock.
    """

    def __init__(
        self,
        *,
        client: remote_reads.DetailReadClient,
        link: Link,
        pumper: Pumper,
        steps: int = READ_STEPS,
    ) -> None:
        self.client = client
        self.link = link
        self.pumper = pumper
        self.steps = steps
        self.lock = threading.Lock()
        self.asked = 0

    def next_id(self) -> str:
        with self.lock:
            self.asked += 1
            return f"read-{self.asked}"

    def fetch(self, **request: Any) -> remote_reads.ReadAnswer:
        request_id = self.next_id()
        now = float(request.pop("now"))
        ticket = self.client.read(request_id=request_id, now=now, **request)
        if ticket.answer is not None:
            return ticket.answer
        assert ticket.envelope is not None
        # A deque's append and popleft are each atomic, which is the whole of
        # what the carry queue needs: one thread hands a request in while
        # another takes the next one out.
        self.link.carry.append(ticket.envelope)
        self.client.sent(request_id, now=now)
        seen = self.pumper.rounds
        for _ in range(self.steps):
            seen = self.pumper.pump(seen)
            answer = self.link.take(request_id)
            if answer is not None:
                return answer
        # Nothing came back inside the window the caller was given. The client's
        # own deadline is what settles it, so the outcome is its word and not a
        # verdict invented here.
        self.client.sent(request_id, now=self.pumper.clock.now, timeout=0.001)
        settled = self.client.expire(self.pumper.clock.now + 1.0)
        for answer in settled:
            if answer.request_id == request_id:
                return answer
        answer = self.link.take(request_id)
        if answer is not None:
            return answer
        raise AssertionError(f"read {request_id} neither answered nor expired")


class LinkedSubmissionLane:
    """The Pi's mutation lane, carried down the link the connector holds.

    Deliberately *not* stepping the connector the way the read lane does. A
    submission is answered ``202`` the moment it is journalled, and the command
    leaves on a later acknowledgement -- so a lane that waited for the outcome
    would be inventing a synchronous mutation the deployment does not have, and
    would hide the very window :class:`MutationBoundaryTests` is about.
    """

    #: How long a carried command stays worth performing. The Pi's own
    #: ``deliver_by`` is the authority in the deployment; this is the same idea
    #: on the envelope, wide enough that a stepped clock cannot expire a command
    #: the test has not finished carrying.
    LIFETIME_SECONDS = 120.0

    def __init__(self, link: Link) -> None:
        self.link = link
        self.handed: list[dict[str, Any]] = []

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        self.handed.append(dict(payload))
        self.link.carry.append(
            transport.request_envelope(
                payload=payload,
                connector_id=CONNECTOR_ID,
                target_id=TARGET_ID,
                sent_at=now,
                request_id=f"submit-{len(self.handed)}",
                deadline=now + self.LIFETIME_SECONDS,
            )
        )
