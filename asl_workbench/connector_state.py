"""The connector's state, introspection and shared helpers.

The base the lanes of :class:`~asl_workbench.connector_runtime.ObservationConnector`
build on: construction, the read-only properties a caller inspects, the hello,
restart recovery, and the few helpers every lane uses to queue an answer.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .connector_lanes import (
    _Answer,
    _Pass,
    _Queue,
    _Request,
    _spawn_answer,
    _spawn_mutation,
    _spawn_observation,
)
from .connector_payloads import connector_hello
from .connector_protocols import (
    Channel,
    DetailResponder,
    MutationBodySource,
    MutationPerformer,
    ObservationCycle,
    ObservationSource,
)
from .connector_support import (
    ACTIVE_READ_INTERVAL_SECONDS,
    ACTIVE_READ_WINDOW_SECONDS,
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_OBSERVATION_INTERVAL_SECONDS,
    MAX_QUEUED_ANSWERS,
    OBSERVATION_CAPABILITIES,
    STATE_STARTING,
    Backoff,
    ConnectorError,
)
from .observation import MAX_LABEL_CHARS
from .remote_actions import MutationQueue, MutationRequest
from .transport import (
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
    KIND_OPERATION,
    KIND_RECEIPT,
)


class ConnectorState:
    """Everything one connector holds between two steps."""

    def __init__(
        self,
        *,
        connector_id: str,
        target_id: str,
        source: ObservationSource,
        channel: Channel,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        observation_interval: float = DEFAULT_OBSERVATION_INTERVAL_SECONDS,
        backoff: Backoff | None = None,
        asl_version: str | None = None,
        responder: DetailResponder | None = None,
        performer: MutationPerformer | None = None,
        body_source: MutationBodySource | None = None,
        run_observation: (
            Callable[[Callable[[], None]], threading.Thread | None] | None
        ) = None,
        run_answer: (
            Callable[[Callable[[], None]], threading.Thread | None] | None
        ) = None,
        run_mutation: (
            Callable[[Callable[[], None]], threading.Thread | None] | None
        ) = None,
    ) -> None:
        if heartbeat_interval <= 0 or observation_interval <= 0:
            raise ConnectorError("the connector schedules need positive intervals")
        self.connector_id = connector_id
        self.target_id = target_id
        self.asl_version = asl_version
        self._source = source
        self._channel = channel
        self._responder = responder
        self._performer = performer
        self._body_source = body_source
        # Derived rather than declared: a connector with nothing to read from
        # says so in its hello instead of accepting reads it would refuse, and
        # one with nothing that performs a mutation stays observation-only.
        offered: list[str] = list(OBSERVATION_CAPABILITIES)
        if responder is not None:
            offered.append(CAPABILITY_DETAIL)
        if performer is not None:
            offered.append(CAPABILITY_MUTATE)
        self.capabilities: tuple[str, ...] = tuple(offered)
        self._heartbeat_interval = float(heartbeat_interval)
        self._observation_interval = float(observation_interval)
        self._backoff = backoff if backoff is not None else Backoff()
        self._run_observation = run_observation or _spawn_observation
        self._state = STATE_STARTING
        self._queue = _Queue()
        # The cycle whose frames are still in the queue. Held until the last of
        # them lands, because that is the moment it may be committed.
        self._cycle: ObservationCycle | None = None
        self._last_cycle: ObservationCycle | None = None
        self._degraded_reason: str | None = None
        # A session counter rather than a clock reading. The store's ordering
        # rule is "time decides, and the generation refuses only what time
        # cannot", so this has to move when a link is re-established and must
        # never pretend to be a timestamp.
        self._generation = 1
        self._next_heartbeat = 0.0
        self._next_observation = 0.0
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self._result: _Pass | None = None
        self._observing = False
        self._thread: threading.Thread | None = None
        # -- the correlated half ------------------------------------------
        self._run_answer = run_answer or _spawn_answer
        self._requests: list[_Request] = []
        self._answering: _Request | None = None
        self._answer_result: _Answer | None = None
        self._answer_thread: threading.Thread | None = None
        self._answer_fault: str | None = None
        self._request_fault: str | None = None
        self._last_request_at: float | None = None
        # Which lane went last, so a busy reader and a busy corpus take turns
        # instead of one of them never being reached.
        self._answered_last = False
        # -- the mutation half --------------------------------------------
        # Its own queue and its own worker, never the read ones. A mutation
        # waits on ASL taking an action and a read waits on a disk, and putting
        # them in one lane would let a slow archive hold up a cockpit's refresh
        # -- or, worse, let a flood of reads run a command's window out.
        self._run_mutation = run_mutation or _spawn_mutation
        self._mutations = MutationQueue()
        self._mutating: MutationRequest | None = None
        self._mutation_result: _Answer | None = None
        self._mutation_thread: threading.Thread | None = None
        self._mutation_fault: str | None = None
        self._settling_mutations: dict[str, MutationRequest] = {}
        self._next_settlement = 0.0
        self._recovered = False

    # -- introspection ---------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def observing(self) -> bool:
        """Whether an observation pass is in flight."""
        with self._lock:
            return self._observing

    @property
    def retry_at(self) -> float:
        """When the next delivery may be attempted. Meaningful in backoff."""
        return self._retry_at

    @property
    def answer_fault(self) -> str | None:
        """Why the last answer was dropped, or ``None``.

        Deliberately not ``degraded_reason``: that word describes the
        observation the heartbeat is carrying, and an unsendable answer says
        nothing about whether the corpus was read. Reporting one as the other
        would have a single malformed read make a healthy Mac look degraded, and
        the far end's own deadline is already the recovery path.
        """
        return self._answer_fault

    @property
    def request_fault(self) -> str | None:
        """Why the last carried request was not taken in, or ``None``."""
        return self._request_fault

    @property
    def answering(self) -> bool:
        """Whether a read is being performed right now."""
        with self._lock:
            return self._answering is not None

    def pending(self) -> int:
        """How many frames are waiting to be delivered."""
        queued = len(self._queue.pages) + len(self._queue.batches)
        queued += len(self._queue.answers)
        if self._queue.heartbeat is not None:
            queued += 1
        if self._queue.snapshot is not None:
            queued += 1
        return queued

    def pending_reads(self) -> int:
        """How many correlated reads are taken in and not yet answered."""
        with self._lock:
            in_flight = 1 if self._answering is not None else 0
        return len(self._requests) + in_flight + len(self._queue.answers)

    def pending_mutations(self) -> int:
        """How many submitted mutations are in hand and not yet settled."""
        return (
            self._mutations.pending
            + self._mutations.attempting
            + len(self._settling_mutations)
        )

    @property
    def mutation_fault(self) -> str | None:
        """Why the last mutation could not be carried out, or ``None``.

        Separate from :attr:`answer_fault` for the same reason that one is
        separate from ``degraded_reason``: a performer that raised says nothing
        about whether reads work or the corpus was walked.
        """
        return self._mutation_fault

    @property
    def observation_only(self) -> bool:
        """Whether this connector will act on anything the far end sends."""
        return CAPABILITY_MUTATE not in self.capabilities

    def hello(self) -> dict[str, Any]:
        """This connector's announcement.

        ``observation_only`` is read off the capabilities rather than passed in:
        the two are the same claim said twice, and a hello that offered
        ``mutate`` while calling itself observation-only would be asking the
        service to refuse every frame it then sent.
        """
        return connector_hello(
            connector_id=self.connector_id,
            target_id=self.target_id,
            asl_version=self.asl_version,
            observation_only=self.observation_only,
            capabilities=self.capabilities,
        )

    def recover(self, now: float) -> int:
        """Settle and report what a previous connector left open.

        Called once, before the loop starts, because that is the only moment at
        which "every open row belongs to a process that is gone" is something
        this connector can act on. Returns how many rows were closed out.

        Nothing is re-attempted. A row that was journalled and never dispatched
        is reported ``expired``; one that got further is reported
        ``unconfirmed``. Carrying a pending command forward and running it now
        -- against state that has moved, on behalf of somebody who has stopped
        waiting -- is the single failure the durable journal exists to make
        impossible.
        """
        if self._performer is None or self._recovered:
            return 0
        self._recovered = True
        try:
            settled = self._performer.recover(now=now)
        # A performer is arbitrary provider code.
        except Exception as error:  # noqa: BLE001
            self._mutation_fault = f"{type(error).__name__}: {error}"[:MAX_LABEL_CHARS]
            return 0
        for payload in settled:
            self._enqueue(
                _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=dict(payload),
                    capability=CAPABILITY_MUTATE,
                )
            )
        return len(settled)

    # -- helpers every lane shares ---------------------------------------
    def _carrier_interval(self, now: float) -> float:
        """How long until the next heartbeat, given whether anyone is reading.

        The heartbeat is also the carrier a request rides back on, so its
        cadence is the floor on read latency. It tightens while a service is
        asking and relaxes once it stops.
        """
        asked = self._last_request_at
        if asked is not None and now - asked <= ACTIVE_READ_WINDOW_SECONDS:
            return min(self._heartbeat_interval, ACTIVE_READ_INTERVAL_SECONDS)
        return self._heartbeat_interval

    def _receipt(
        self,
        request: _Request,
        outcome: str,
        detail_code: str,
        now: float,
        *,
        capability: str = CAPABILITY_DETAIL,
    ) -> _Answer:
        """One correlated refusal, in the shape a receipt declares."""
        return _Answer(
            request_id=request.request_id,
            kind=KIND_RECEIPT,
            payload={
                "outcome": outcome,
                "observed_at": now,
                "detail_code": detail_code,
            },
            deadline=request.deadline,
            capability=capability,
        )

    def _enqueue(self, answer: _Answer) -> None:
        """Put one finished answer in the lane, or drop it if the lane is full."""
        if len(self._queue.answers) >= MAX_QUEUED_ANSWERS:
            answer.forget()
            self._answer_fault = "the answer queue is full"
            return
        self._queue.answers.append(answer)
