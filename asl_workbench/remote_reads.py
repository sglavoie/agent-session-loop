"""Correlated detail reads, from the side of the link that asks for them.

:mod:`asl_workbench.connector` publishes what the Mac decides to say. This is
the other half of the conversation: the service's runtime for *asking*, one
question at a time, over the connector's own outbound channel. A request goes
down as the acknowledgement of an upload, the answer comes back up correlated
by ``request_id``, and nothing about it is written to a database at either end.

Four properties, and each is a refusal rather than a convention:

* **A body is held in memory, bounded twice, and never persisted.**
  :class:`BodyCache` is the Companion's ``QuestionCache`` applied to detail:
  bounded by count so a burst of reads cannot grow it without limit, bounded by
  age so a window whose run finished hours ago is gone even if nothing
  displaced it, blanked on the way out for the benefit of any copy somebody
  else is still holding, and emptied outright when the link drops. Restarting
  the process loses every body, which is the correct trade -- a body is
  refetched in one round trip, and a leaked one is not recoverable at all.

* **A read that was never answered says so, in the caller's vocabulary.**
  :data:`READ_OUTCOMES` keeps ``offline`` (nothing left this process),
  ``unavailable`` (the link is up and this pairing may not ask),
  ``timeout`` (asked, and the deadline passed), ``cancelled`` (withdrawn) and
  ``refused`` (the Mac answered, and the answer is a reason) apart from each
  other *and* from an answered read whose artifact happens to be empty. A
  cockpit that renders "no events" for a Mac that is not connected is the
  failure this vocabulary exists to prevent, and it is why
  :attr:`ReadAnswer.empty` is false for every outcome except ``answered``.

* **A window is paged, not streamed.** :meth:`DetailReadClient.next_page` reads
  the cursor the Mac sent -- ``next_offset``, which is ``None`` at the end of
  the artifact rather than an offset past it -- and composes the follow-up
  request from it. A caller pages forward because the answer said there was
  more, never because it read a short page and guessed.

* **Losing the link abandons the waiters rather than the session.**
  :meth:`DetailReadClient.link_down` answers every outstanding read ``offline``
  and empties the cache; :meth:`DetailReadClient.expire` times out one read
  without touching the others. Those are the Companion connector's
  ``_abandon_waiters`` and ``_expire_waiters`` rules, kept because they are the
  difference between "this read failed" and "the cockpit is dead".

This module composes and inspects envelopes through :mod:`asl_workbench.transport`
and touches nothing else: no socket, no file, no clock reading. The moment is
always an argument, so a test asserts about the schedule it chose rather than
the one it raced.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .details import DETAIL_KINDS
from .observation import COMPLETENESS_LABELS, COMPLETENESS_UNKNOWN
from .transport import (
    CAPABILITY_DETAIL,
    DETAIL_REFUSAL_REASONS,
    DIRECTION_UP,
    ERROR_UNSUPPORTED_CAPABILITY,
    KIND_DETAIL,
    KIND_RECEIPT,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_ENTRIES,
    RECEIPT_DELIVERED,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    REQUEST_DETAIL,
    TransportError,
    request_envelope,
    validate_envelope,
)

__all__ = [
    "BODY_TTL_SECONDS",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "MAX_CACHED_BODIES",
    "MAX_PENDING_READS",
    "MAX_READ_TIMEOUT_SECONDS",
    "OUTCOME_ANSWERED",
    "OUTCOME_CANCELLED",
    "OUTCOME_OFFLINE",
    "OUTCOME_REFUSED",
    "OUTCOME_TIMEOUT",
    "OUTCOME_UNAVAILABLE",
    "READ_OUTCOMES",
    "BodyCache",
    "DetailReadClient",
    "ReadAnswer",
    "ReadKey",
    "ReadRequest",
    "ReadTicket",
    "RemoteReadError",
]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class RemoteReadError(Exception):
    """A read this runtime refuses to compose, or a caller's own mistake.

    Never raised for a read that failed on the network: those are outcomes, not
    exceptions, because "the Mac is offline" is something a cockpit renders
    rather than something it recovers from.
    """


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

#: The Mac answered, and the answer is material.
OUTCOME_ANSWERED = "answered"
#: The Mac answered, and the answer is a reason from
#: :data:`asl_workbench.transport.DETAIL_REFUSAL_REASONS`. ASL declined; the
#: link is fine.
OUTCOME_REFUSED = "refused"
#: Nothing left this process. There is no link, so there is no question.
OUTCOME_OFFLINE = "offline"
#: The link is up and this read may not be asked: the pairing never granted
#: ``detail``, or the deployment is observing only.
OUTCOME_UNAVAILABLE = "unavailable"
#: It was asked and the deadline passed with no answer.
OUTCOME_TIMEOUT = "timeout"
#: The caller withdrew it. A late answer to a cancelled read is discarded
#: rather than cached.
OUTCOME_CANCELLED = "cancelled"

READ_OUTCOMES: tuple[str, ...] = (
    OUTCOME_ANSWERED,
    OUTCOME_REFUSED,
    OUTCOME_OFFLINE,
    OUTCOME_UNAVAILABLE,
    OUTCOME_TIMEOUT,
    OUTCOME_CANCELLED,
)

#: The outcomes that carry material. Everything else describes why there is
#: none, and none of them may be read as "the artifact was empty".
MATERIAL_OUTCOMES: frozenset[str] = frozenset({OUTCOME_ANSWERED})


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------

#: How long a read stays live once it has left this process. Short relative to
#: :data:`asl_workbench.transport.MAX_REQUEST_LIFETIME_SECONDS`: a detail read
#: is something somebody is waiting on, and a deadline an operator has already
#: given up on is a deadline that only costs the connector a frame.
DEFAULT_READ_TIMEOUT_SECONDS = 20.0

#: The longest deadline this runtime will compose. A read is interactive; a
#: caller asking for an hour has confused it with a mutation.
MAX_READ_TIMEOUT_SECONDS = 120.0

#: How many reads may be outstanding at once. A cockpit opening a plan asks for
#: a handful of surfaces; a table this size cannot be filled by a human, and a
#: caller that fills it is looping.
MAX_PENDING_READS = 32

#: How many answered bodies are held, and for how long. Both bounds are needed:
#: a count with no clock keeps the last sixteen forever, and a clock with no
#: count keeps everything that is recent.
MAX_CACHED_BODIES = 16
BODY_TTL_SECONDS = 120.0

#: One held body, identified by what was asked rather than by which request
#: asked it: plan, run, surface, selector, offset. Two reads of the same window
#: are the same body, and the second is served without a round trip.
ReadKey = tuple[str, str, str, str, int]


# --------------------------------------------------------------------------
# What a caller asks, and what it gets back
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadRequest:
    """One detail read, in the shape :data:`REQUEST_DETAIL` declares.

    Frozen, because the request is also the identity of the answer: a paging
    caller composes a *new* request from the cursor rather than advancing this
    one, so the table can never hold a request that no longer describes what
    was asked.
    """

    request_id: str
    detail: str
    plan_key: str
    run_key: str | None = None
    run_id: str | None = None
    artifact: str | None = None
    stream: str | None = None
    offset: int | None = None
    limit: int | None = None
    max_bytes: int | None = None
    reason: str | None = None
    requested_at: float = 0.0
    deadline: float = 0.0

    def __post_init__(self) -> None:
        if self.detail not in DETAIL_KINDS:
            raise RemoteReadError(f"unknown detail kind: {self.detail!r}")
        if self.limit is not None and self.limit > MAX_DETAIL_ENTRIES:
            raise RemoteReadError(f"limit above {MAX_DETAIL_ENTRIES}")
        if self.max_bytes is not None and self.max_bytes > MAX_DETAIL_BYTES:
            raise RemoteReadError(f"max_bytes above {MAX_DETAIL_BYTES}")

    @property
    def key(self) -> ReadKey:
        """What was asked, as the cache keys it."""
        return (
            self.plan_key,
            self.run_key or "",
            self.detail,
            self.artifact or self.stream or "",
            int(self.offset or 0),
        )

    def target(self) -> dict[str, Any]:
        """The plan and run this read is about."""
        addressed: dict[str, Any] = {"plan_key": self.plan_key}
        if self.run_key is not None:
            addressed["run_key"] = self.run_key
        if self.run_id is not None:
            addressed["run_id"] = self.run_id
        return addressed

    def to_payload(self) -> dict[str, Any]:
        """The request payload, carrying only the selectors that were given."""
        payload: dict[str, Any] = {
            "request": REQUEST_DETAIL,
            "detail": self.detail,
            "target": self.target(),
        }
        for name in ("artifact", "stream", "offset", "limit", "max_bytes", "reason"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


@dataclass(frozen=True)
class ReadAnswer:
    """What became of one read: material, or the reason there is none.

    ``complete`` is the Mac's own completeness word, so a caller can tell a
    whole artifact from a window that was cut. ``next_offset`` is the cursor
    and is ``None`` at the end. ``reason`` is set only when the Mac refused,
    and ``detail_code`` only when the transport did -- keeping "ASL said no"
    and "this pairing may not ask" in separate fields rather than in one string
    somebody has to parse.
    """

    request_id: str
    outcome: str
    detail: str
    plan_key: str
    run_key: str | None = None
    artifact: str | None = None
    stream: str | None = None
    complete: str = COMPLETENESS_UNKNOWN
    content: str | None = None
    entries: tuple[Mapping[str, Any], ...] = ()
    offset: int = 0
    next_offset: int | None = None
    total: int | None = None
    truncated: bool = False
    reason: str | None = None
    detail_code: str | None = None
    note: str = ""
    warnings: tuple[str, ...] = ()
    observed_at: float | None = None
    received_at: float = 0.0

    def __post_init__(self) -> None:
        if self.outcome not in READ_OUTCOMES:
            raise RemoteReadError(f"undeclared read outcome: {self.outcome!r}")
        if self.complete not in COMPLETENESS_LABELS:
            raise RemoteReadError(f"undeclared completeness: {self.complete!r}")
        if self.reason is not None and self.reason not in DETAIL_REFUSAL_REASONS:
            raise RemoteReadError(f"undeclared refusal reason: {self.reason!r}")

    @property
    def answered(self) -> bool:
        """Whether the Mac read the artifact and sent what it found."""
        return self.outcome in MATERIAL_OUTCOMES

    @property
    def empty(self) -> bool:
        """Whether the artifact itself is empty.

        False for every unanswered outcome. "There is nothing here" is a fact
        about a plan; "I could not ask" is a fact about a link, and a renderer
        that cannot tell them apart shows an operator an empty panel for a Mac
        that never answered.
        """
        return self.answered and not self.content and not self.entries

    @property
    def has_more(self) -> bool:
        """Whether a further page exists. Read off the cursor, not the size."""
        return self.next_offset is not None

    @property
    def body_bytes(self) -> int:
        """How much text this answer is carrying."""
        return len(self.content.encode("utf-8", "surrogatepass")) if self.content else 0

    def identity(self) -> dict[str, Any]:
        """The answer without its body: what a diagnostic may hold."""
        return {
            "request_id": self.request_id,
            "outcome": self.outcome,
            "detail": self.detail,
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "complete": self.complete,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "total": self.total,
            "entry_count": len(self.entries),
            "body_bytes": self.body_bytes,
            "reason": self.reason,
            "detail_code": self.detail_code,
        }


@dataclass(frozen=True)
class ReadTicket:
    """The result of asking: a frame to send, or an answer already decided.

    Exactly one of :attr:`envelope` and :attr:`answer` is set. A caller that
    has a frame sends it and waits; a caller that has an answer renders it,
    because the read failed before anything left this process and waiting would
    be waiting for nothing.
    """

    request: ReadRequest
    envelope: Mapping[str, Any] | None = None
    answer: ReadAnswer | None = None

    @property
    def outstanding(self) -> bool:
        return self.envelope is not None


# --------------------------------------------------------------------------
# The bodies this process is holding
# --------------------------------------------------------------------------


class BodyCache:
    """Answered bodies, in memory, bounded by count and by age.

    Nothing here reaches a store. A detail body is not a record of what
    happened, it is material somebody is reading right now, and material that
    outlives the moment is material sitting in a backup nobody audited.
    """

    def __init__(
        self,
        *,
        capacity: int = MAX_CACHED_BODIES,
        ttl_seconds: float = BODY_TTL_SECONDS,
    ) -> None:
        if capacity < 1:
            raise RemoteReadError("a body cache with no room holds nothing")
        self._capacity = int(capacity)
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        # Insertion ordered, and relied on: the oldest arrival is the first
        # key. Eviction is by arrival rather than by least-recent-read, because
        # tracking reads would mean recording who looked at which plan.
        self._bodies: dict[ReadKey, dict[str, Any]] = {}

    # -- writing ---------------------------------------------------------
    def store(self, key: ReadKey, answer: ReadAnswer, *, now: float) -> None:
        """Hold one answered read's material.

        The body is held *beside* the answer rather than inside it, so
        :meth:`_forget` can blank the text this cache is carrying at exactly
        the moment it stops carrying the entry -- which a frozen answer handed
        out earlier would otherwise keep alive.
        """
        if not answer.answered:
            raise RemoteReadError("only an answered read has a body to hold")
        entry: dict[str, Any] = {
            "answer": replace(answer, content=None, entries=()),
            "content": answer.content,
            "entries": list(answer.entries),
            "received_at": float(now),
            # Measured now, so :meth:`held` can say how much is being carried
            # without reaching for the text to find out.
            "body_bytes": answer.body_bytes,
            "entry_count": len(answer.entries),
        }
        with self._lock:
            self._expire(now)
            # Removed before reinsertion so a refetched window counts as a new
            # arrival: a plan somebody is re-reading is a plan somebody is
            # working on.
            self._bodies.pop(key, None)
            self._bodies[key] = entry
            while len(self._bodies) > self._capacity:
                self._forget(next(iter(self._bodies)))

    def drop(self, plan_key: str) -> int:
        """Forget every body held for one plan. Returns how many."""
        with self._lock:
            doomed = [key for key in self._bodies if key[0] == plan_key]
            for key in doomed:
                self._forget(key)
            return len(doomed)

    def expire(self, now: float) -> int:
        """Drop everything the clock alone has decided. Returns how many."""
        with self._lock:
            return self._expire(now)

    def clear(self) -> None:
        """Forget everything, blanking each body on the way out."""
        with self._lock:
            for key in list(self._bodies):
                self._forget(key)

    # -- reading ---------------------------------------------------------
    def get(self, key: ReadKey, *, now: float) -> ReadAnswer | None:
        """The answer held for one window, body and all, or ``None``."""
        with self._lock:
            self._expire(now)
            entry = self._bodies.get(key)
            if entry is None:
                return None
            held: ReadAnswer = entry["answer"]
            return replace(
                held,
                content=entry["content"],
                entries=tuple(entry["entries"]),
            )

    def held(self, *, now: float) -> tuple[dict[str, Any], ...]:
        """Every body still held, oldest arrival first, without the text."""
        with self._lock:
            self._expire(now)
            return tuple(
                {
                    **entry["answer"].identity(),
                    "received_at": entry["received_at"],
                    "body_bytes": entry["body_bytes"],
                    "entry_count": entry["entry_count"],
                }
                for entry in self._bodies.values()
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._bodies)

    # -- internals -------------------------------------------------------
    def _expire(self, now: float) -> int:
        stale = [
            key
            for key, entry in self._bodies.items()
            if now - float(entry["received_at"]) >= self._ttl
        ]
        for key in stale:
            self._forget(key)
        return len(stale)

    def _forget(self, key: ReadKey) -> None:
        entry = self._bodies.pop(key, None)
        if entry is not None:
            entry["content"] = None
            entry["entries"] = []


# --------------------------------------------------------------------------
# The correlation table
# --------------------------------------------------------------------------


@dataclass
class _Waiter:
    """One read this process is waiting on."""

    request: ReadRequest
    #: Set once the request has actually left. A request still queued when the
    #: link drops was never asked, which is a different thing from one that was
    #: asked and went unanswered.
    sent: bool = False
    expires_at: float = 0.0


@dataclass
class _Link:
    """What the current pairing permits. Replaced wholesale on reconnect."""

    up: bool = False
    capabilities: frozenset[str] = field(default_factory=frozenset)
    #: Incremented every time the link comes back, so an answer minted under a
    #: previous pairing can be recognised and dropped.
    generation: int = 0


class DetailReadClient:
    """Correlated detail reads over one connector's channel.

    One instance per paired connector. Every method takes the moment as an
    argument and none of them blocks: this is the bookkeeping half, and the
    waiting is the caller's own loop.
    """

    def __init__(
        self,
        *,
        connector_id: str,
        target_id: str,
        timeout: float = DEFAULT_READ_TIMEOUT_SECONDS,
        max_pending: int = MAX_PENDING_READS,
        capacity: int = MAX_CACHED_BODIES,
        ttl_seconds: float = BODY_TTL_SECONDS,
    ) -> None:
        if timeout <= 0 or timeout > MAX_READ_TIMEOUT_SECONDS:
            raise RemoteReadError(
                f"a read timeout must be positive and at most "
                f"{MAX_READ_TIMEOUT_SECONDS:.0f}s"
            )
        self.connector_id = connector_id
        self.target_id = target_id
        self.timeout = float(timeout)
        self._max_pending = max(1, int(max_pending))
        self._lock = threading.RLock()
        self._waiters: dict[str, _Waiter] = {}
        self._link = _Link()
        self.bodies = BodyCache(capacity=capacity, ttl_seconds=ttl_seconds)

    # -- the link --------------------------------------------------------
    @property
    def linked(self) -> bool:
        with self._lock:
            return self._link.up

    @property
    def capabilities(self) -> frozenset[str]:
        with self._lock:
            return self._link.capabilities

    @property
    def generation(self) -> int:
        """How many times this pairing has been established."""
        with self._lock:
            return self._link.generation

    def link_up(self, capabilities: Iterable[str], *, now: float) -> None:
        """Record a pairing, and start a new generation.

        Anything still outstanding from the previous generation is abandoned
        first: the connector on the other side cleared its own answer queue
        when the link dropped, so a waiter carried across a reconnect would
        wait for a frame nobody is going to build.
        """
        with self._lock:
            if self._waiters:
                self._abandon(now, note="the link was re-established")
            self._link = _Link(
                up=True,
                capabilities=frozenset(capabilities),
                generation=self._link.generation + 1,
            )

    def link_down(
        self, *, now: float, note: str = "the link dropped"
    ) -> tuple[ReadAnswer, ...]:
        """Abandon every outstanding read and drop every held body.

        Returns one ``offline`` answer per abandoned read, so a caller can
        settle its own callbacks rather than discovering later that a promise
        will never be kept. The bodies go with them: a body held across a
        disconnect is a body whose plan may have changed underneath it, and it
        is cheaper to refetch than to be wrong about.
        """
        with self._lock:
            self._link = replace(self._link, up=False, capabilities=frozenset())
            abandoned = self._abandon(now, note=note)
            self.bodies.clear()
            return abandoned

    # -- asking ----------------------------------------------------------
    def read(
        self,
        *,
        request_id: str,
        detail: str,
        plan_key: str,
        run_key: str | None = None,
        run_id: str | None = None,
        artifact: str | None = None,
        stream: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        max_bytes: int | None = None,
        reason: str | None = None,
        now: float,
        timeout: float | None = None,
    ) -> ReadTicket:
        """Compose one read, or decide it cannot be asked.

        The two decided-here outcomes are the ones the session's vocabulary
        insists stay apart: ``offline`` when there is no link, and
        ``unavailable`` when there is one that never granted ``detail``.
        Neither is an empty artifact, and neither costs a round trip.
        """
        request = ReadRequest(
            request_id=request_id,
            detail=detail,
            plan_key=plan_key,
            run_key=run_key,
            run_id=run_id,
            artifact=artifact,
            stream=stream,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            reason=reason,
            requested_at=now,
            deadline=now + self._lifetime(timeout),
        )
        with self._lock:
            if request_id in self._waiters:
                raise RemoteReadError(f"request {request_id!r} is already outstanding")
            refusal = self._refusal(request, now=now)
            if refusal is not None:
                return ReadTicket(request=request, answer=refusal)
            try:
                envelope = request_envelope(
                    payload=request.to_payload(),
                    connector_id=self.connector_id,
                    target_id=self.target_id,
                    sent_at=now,
                    request_id=request_id,
                    deadline=request.deadline,
                )
            except TransportError as error:
                raise RemoteReadError(str(error)) from error
            self._waiters[request_id] = _Waiter(
                request=request, expires_at=request.deadline
            )
            return ReadTicket(request=request, envelope=envelope)

    def next_page(
        self,
        answer: ReadAnswer,
        *,
        request_id: str,
        now: float,
        timeout: float | None = None,
    ) -> ReadTicket | None:
        """The follow-up read for the next window, or ``None`` at the end.

        Composed from the cursor the Mac sent rather than from arithmetic done
        here, which is what keeps local paging and remote paging agreeing about
        where an artifact stops.
        """
        if answer.next_offset is None:
            return None
        return self.read(
            request_id=request_id,
            detail=answer.detail,
            plan_key=answer.plan_key,
            run_key=answer.run_key,
            artifact=answer.artifact,
            stream=answer.stream,
            offset=answer.next_offset,
            now=now,
            timeout=timeout,
        )

    def sent(
        self, request_id: str, *, now: float, timeout: float | None = None
    ) -> None:
        """Record that the request has left this process.

        The deadline is taken from this moment rather than from composition: a
        request that sat in a queue waiting for the next upload has not had its
        time yet, and starting the clock before it left would time it out for
        somebody else's slowness.
        """
        with self._lock:
            waiter = self._waiters.get(request_id)
            if waiter is None:
                return
            waiter.sent = True
            waiter.expires_at = now + self._lifetime(timeout)

    def cancel(self, request_id: str, *, now: float) -> ReadAnswer | None:
        """Withdraw one read. A late answer to it is discarded, never held.

        There is no cancel *method* on the wire --
        :data:`asl_workbench.transport.REQUEST_METHODS` is closed and has none
        -- so cancellation is a decision this end makes about an answer it no
        longer wants. The Mac finishes the read it started and its frame is
        dropped on arrival, which costs one frame and keeps the connector free
        of a second correlation table.
        """
        with self._lock:
            waiter = self._waiters.pop(request_id, None)
            if waiter is None:
                return None
            return self._settle(
                waiter.request,
                OUTCOME_CANCELLED,
                now=now,
                note="withdrawn by the caller",
            )

    def expire(self, now: float) -> tuple[ReadAnswer, ...]:
        """Time out the reads whose deadline has passed, and only those.

        One slow read must not take the others with it, which is why this
        walks the table rather than failing the session the way a channel error
        does.
        """
        with self._lock:
            timed_out = [
                waiter for waiter in self._waiters.values() if now >= waiter.expires_at
            ]
            settled: list[ReadAnswer] = []
            for waiter in timed_out:
                self._waiters.pop(waiter.request.request_id, None)
                settled.append(
                    self._settle(
                        waiter.request,
                        OUTCOME_TIMEOUT,
                        now=now,
                        note="no answer before the deadline",
                    )
                )
            self.bodies.expire(now)
            return tuple(settled)

    # -- receiving -------------------------------------------------------
    def accept(self, message: Any, *, now: float) -> ReadAnswer | None:
        """Take one upward envelope and settle the read it correlates to.

        Returns ``None`` -- and holds nothing -- for a frame that answers no
        outstanding read: an answer to a cancelled read, a duplicate, or a
        frame for a request this process has already timed out. Caching it
        "just in case" is how a body outlives the question that justified it.
        """
        envelope = validate_envelope(
            message, direction=DIRECTION_UP, target_id=self.target_id
        )
        if envelope.get("connector_id") != self.connector_id:
            raise RemoteReadError(
                f"envelope from another connector: {envelope.get('connector_id')!r}"
            )
        kind = envelope["kind"]
        if kind not in (KIND_DETAIL, KIND_RECEIPT):
            return None
        request_id = envelope.get("request_id")
        if not isinstance(request_id, str):
            return None
        with self._lock:
            waiter = self._waiters.pop(request_id, None)
            if waiter is None:
                return None
            payload = envelope["payload"]
            if kind == KIND_DETAIL:
                return self._accept_detail(waiter.request, payload, now=now)
            return self._accept_receipt(waiter.request, payload, now=now)

    # -- introspection ---------------------------------------------------
    def pending(self) -> tuple[str, ...]:
        """Every outstanding request id, in the order it was composed."""
        with self._lock:
            return tuple(self._waiters)

    def outstanding(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._waiters

    def cached(self, request: ReadRequest, *, now: float) -> ReadAnswer | None:
        """The held answer for this exact window, or ``None``."""
        return self.bodies.get(request.key, now=now)

    def clear(self) -> None:
        """Drop every held body. The waiters are untouched."""
        self.bodies.clear()

    # -- internals -------------------------------------------------------
    def _lifetime(self, timeout: float | None) -> float:
        if timeout is None:
            return self.timeout
        if timeout <= 0 or timeout > MAX_READ_TIMEOUT_SECONDS:
            raise RemoteReadError(
                f"a read timeout must be positive and at most "
                f"{MAX_READ_TIMEOUT_SECONDS:.0f}s"
            )
        return float(timeout)

    def _refusal(self, request: ReadRequest, *, now: float) -> ReadAnswer | None:
        """Why this read cannot be asked at all, or ``None``."""
        if not self._link.up:
            return self._settle(
                request, OUTCOME_OFFLINE, now=now, note="no link to the connector"
            )
        if CAPABILITY_DETAIL not in self._link.capabilities:
            return self._settle(
                request,
                OUTCOME_UNAVAILABLE,
                now=now,
                detail_code=ERROR_UNSUPPORTED_CAPABILITY,
                note="the connector did not offer detail reads",
            )
        if len(self._waiters) >= self._max_pending:
            # No detail code: nothing was refused out there. The table is full
            # here, and saying so in a transport word would blame a Mac that
            # has not been asked yet.
            return self._settle(
                request,
                OUTCOME_UNAVAILABLE,
                now=now,
                note=f"{self._max_pending} reads are already outstanding",
            )
        return None

    def _settle(
        self,
        request: ReadRequest,
        outcome: str,
        *,
        now: float,
        reason: str | None = None,
        detail_code: str | None = None,
        note: str = "",
    ) -> ReadAnswer:
        """One unanswered read, described in the caller's vocabulary."""
        return ReadAnswer(
            request_id=request.request_id,
            outcome=outcome,
            detail=request.detail,
            plan_key=request.plan_key,
            run_key=request.run_key,
            artifact=request.artifact,
            stream=request.stream,
            offset=int(request.offset or 0),
            reason=reason,
            detail_code=detail_code,
            note=note,
            received_at=now,
        )

    def _accept_detail(
        self, request: ReadRequest, payload: Mapping[str, Any], *, now: float
    ) -> ReadAnswer:
        """One detail payload as an answer, held only when it carries material."""
        refusal = payload.get("reason")
        entries = payload.get("entries")
        answer = ReadAnswer(
            request_id=request.request_id,
            outcome=OUTCOME_REFUSED if refusal is not None else OUTCOME_ANSWERED,
            detail=str(payload.get("detail", request.detail)),
            plan_key=request.plan_key,
            run_key=request.run_key,
            artifact=payload.get("artifact") or request.artifact,
            stream=payload.get("stream") or request.stream,
            complete=str(payload.get("complete", COMPLETENESS_UNKNOWN)),
            content=payload.get("content"),
            entries=tuple(entries) if isinstance(entries, Sequence) else (),
            offset=int(payload.get("offset") or 0),
            next_offset=payload.get("next_offset"),
            total=payload.get("total"),
            truncated=bool(payload.get("truncated", False)),
            reason=refusal,
            warnings=tuple(payload.get("warnings") or ()),
            observed_at=payload.get("observed_at"),
            received_at=now,
        )
        if answer.answered:
            self.bodies.store(request.key, answer, now=now)
        return answer

    def _accept_receipt(
        self, request: ReadRequest, payload: Mapping[str, Any], *, now: float
    ) -> ReadAnswer:
        """One receipt as an answer.

        A receipt on a detail read is the connector declining to do it, so the
        outcomes are the two that describe a link rather than a plan:
        ``expired`` is the deadline this end set having passed at the far end,
        and everything else is ``unavailable``. A ``delivered`` receipt is
        nonsense on a read -- the detail envelope is what delivery means -- and
        it is reported as unavailable rather than quietly read as success.
        """
        outcome = (
            OUTCOME_TIMEOUT
            if payload.get("outcome") == RECEIPT_EXPIRED
            else OUTCOME_UNAVAILABLE
        )
        note = {
            RECEIPT_REFUSED: "the connector refused the read",
            RECEIPT_EXPIRED: "the deadline passed before the connector read it",
            RECEIPT_DELIVERED: "the connector acknowledged without answering",
        }.get(str(payload.get("outcome")), "the connector could not answer")
        return self._settle(
            request,
            outcome,
            now=now,
            detail_code=payload.get("detail_code"),
            note=note,
        )

    def _abandon(self, now: float, *, note: str) -> tuple[ReadAnswer, ...]:
        """Answer every outstanding read ``offline`` and empty the table."""
        abandoned = tuple(
            self._settle(waiter.request, OUTCOME_OFFLINE, now=now, note=note)
            for waiter in self._waiters.values()
        )
        self._waiters.clear()
        return abandoned
