"""Harness-neutral live delivery of supervisor guidance into a running turn.

The mailbox owns what guidance *is* and what was durably done with it; this
module owns the one seam through which a live turn can be handed guidance while
its process is still running. It is deliberately free of mailbox and harness
imports: ``harnesses`` imports this module, and the mailbox reaches ``harnesses``
through ``state``, so importing the ledger here would close an import cycle.

The vocabulary below therefore restates the mailbox's delivery constants rather
than importing them, and
``tests/test_asl_guidance_delivery.py`` pins the two copies together so they
cannot drift.

The endpoint is turn-scoped by construction. It is opened by the code that owns
the harness process, closed when that process is gone, and refuses any request
whose turn identity is not the exact turn it was opened for. That is what makes
"delivered live" a claim about one process rather than about whichever turn
happened to be running when a poll tick fired.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

#: Mirrors ``mailbox.GUIDANCE_STATE_DELIVERED_LIVE`` / ``GUIDANCE_STATE_DEFERRED``.
DeliveryState = Literal["delivered-live", "deferred"]
STATE_DELIVERED_LIVE: DeliveryState = "delivered-live"
STATE_DEFERRED: DeliveryState = "deferred"

#: Mirrors ``mailbox.GUIDANCE_CHANNEL_LIVE``. This endpoint has exactly one
#: channel: an attempt it records is always an attempt at same-turn delivery.
CHANNEL_LIVE = "live"

#: Mirrors the mailbox's delivery reasons. ``REASON_DELIVERED`` is the only one
#: that may accompany a successful outcome.
REASON_DELIVERED = "delivered"
REASON_HARNESS_UNSUPPORTED = "harness-unsupported"
REASON_NO_ACTIVE_TURN = "no-active-turn"
REASON_STALE_TURN = "stale-turn"
REASON_TRANSPORT_UNAVAILABLE = "transport-unavailable"
#: The harness has a live process and a channel into it, but its protocol has
#: no same-turn steer at all. Distinct from ``harness-unsupported``, which says
#: nothing was attached: this is a harness that answered, and answered "no".
REASON_UNSUPPORTED_ACTIVE_STEER = "unsupported-active-steer"
#: The guidance was handed to the transport and the transport cannot yet prove
#: the model received it. The only reason that is *not* a verdict: the attempt
#: is still open, and the spine is expected to ask the same transport again.
REASON_AWAITING_CONFIRMATION = "awaiting-confirmation"
DEFERRAL_REASONS: tuple[str, ...] = (
    REASON_HARNESS_UNSUPPORTED,
    REASON_NO_ACTIVE_TURN,
    REASON_STALE_TURN,
    REASON_TRANSPORT_UNAVAILABLE,
    REASON_UNSUPPORTED_ACTIVE_STEER,
    REASON_AWAITING_CONFIRMATION,
)

#: Deferral reasons that describe an attempt still in progress rather than one
#: that finished. A transport that returns one of these is asking to be called
#: again inside the same turn; every other reason ends the turn's attempt.
RETRIABLE_IN_TURN_REASONS: tuple[str, ...] = (REASON_AWAITING_CONFIRMATION,)


@dataclass(frozen=True)
class TurnIdentity:
    """The exact turn a delivery may target.

    All four fields together are the identity: the mailbox refuses a delivery
    record carrying only part of it, and an endpoint refuses a request that
    does not match its own on every field.
    """

    run_id: str
    session_id: str
    turn_index: int
    harness: str


@dataclass(frozen=True)
class GuidanceRequest:
    """One guidance record offered to one live turn.

    Carries the body, because the whole point of the seam is to hand the
    worker the text a supervisor wrote. Nothing derived from ``text`` may leave
    a transport for the event stream or the ledger.
    """

    guidance_id: str
    seq: int
    text: str
    turn: TurnIdentity


@dataclass(frozen=True)
class GuidanceOutcome:
    """What one delivery attempt did, as a value the caller can record.

    ``detail`` exists for tests and local diagnosis only. It is never persisted
    and never evented: a transport error message is not evidence a supervisor
    needs, and it is the one field that could carry a fragment of the body.
    """

    delivered: bool
    reason: str
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.delivered:
            if self.reason != REASON_DELIVERED:
                raise ValueError(
                    f"a delivered outcome cannot claim reason {self.reason!r}"
                )
        elif self.reason not in DEFERRAL_REASONS:
            raise ValueError(f"unknown deferral reason {self.reason!r}")

    @property
    def delivery_state(self) -> DeliveryState:
        return STATE_DELIVERED_LIVE if self.delivered else STATE_DEFERRED

    @property
    def channel(self) -> str:
        return CHANNEL_LIVE

    @property
    def retriable_in_turn(self) -> bool:
        """Whether the same transport should be asked again this turn.

        True only while an attempt is genuinely still open. Everything else —
        delivered, unsupported, stale, broken — is this turn's last word, and
        the spine must not spin on it.
        """
        return not self.delivered and self.reason in RETRIABLE_IN_TURN_REASONS


#: A successful same-turn delivery. Immutable, so one value serves every caller.
DELIVERED = GuidanceOutcome(delivered=True, reason=REASON_DELIVERED)


def deferred(reason: str, *, detail: str | None = None) -> GuidanceOutcome:
    """Return the outcome of an attempt that delivered nothing.

    A deferral is always "not yet": the guidance stays deliverable, and no
    caller is expected to treat it as a terminal answer.
    """
    return GuidanceOutcome(delivered=False, reason=reason, detail=detail)


def awaiting_confirmation(*, detail: str | None = None) -> GuidanceOutcome:
    """Return the outcome of an attempt that is written but not yet confirmed.

    The distinction this exists for: a transport that wrote to a pipe has done
    everything it can do, and still knows nothing about whether a model read
    it. Saying so is the whole point — a write is not a delivery.
    """
    return deferred(REASON_AWAITING_CONFIRMATION, detail=detail)


#: What an adapter supplies to inject guidance into the process it owns. It is
#: called on the poller's thread while the turn is running, so it must return
#: promptly and must not block on the turn finishing.
GuidanceTransport = Callable[[GuidanceRequest], GuidanceOutcome]


class LiveDeliveryEndpoint:
    """A thread-safe, turn-scoped channel into one in-flight harness turn.

    Lifecycle, in the order the runner performs it: constructed for the turn it
    names, handed to both the mailbox poller and the harness, opened by the
    harness once the process exists, closed by the harness once the process is
    gone, and detached from the poller after the poll thread has been joined.
    Before the open and after the close the endpoint is inert but still
    answers, so a poll tick that lands in either window gets an honest
    deferral instead of a delivery into nothing.

    Locking: ``_lock`` is a leaf. It is held across the transport call, exactly
    as the harness holds its side-effect lock across a stream callback, so
    ``close`` cannot return while a transport is still running and no transport
    can start after it returns. Nothing under this lock may take the run
    control lock, the persistence lock, or the mailbox lock; the poller records
    the outcome only after ``submit`` has returned.
    """

    def __init__(self, turn: TurnIdentity) -> None:
        self._turn = turn
        self._lock = threading.Lock()
        self._transport: GuidanceTransport | None = None
        self._opened = False
        self._closed = False

    @property
    def turn(self) -> TurnIdentity:
        return self._turn

    @property
    def opened(self) -> bool:
        with self._lock:
            return self._opened

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def open(self, transport: GuidanceTransport | None) -> None:
        """Make the endpoint live, with ``transport`` or explicitly without one.

        ``None`` is the honest state for a harness with no live transport yet:
        the endpoint is open for the turn's whole life and every attempt on it
        defers with ``harness-unsupported``.
        """
        with self._lock:
            if self._closed:
                # The turn ended before its own process reported in. Opening
                # now would resurrect a channel into a dead process.
                return
            self._transport = transport
            self._opened = True

    def close(self) -> None:
        """Close the endpoint, waiting out any delivery already in flight."""
        with self._lock:
            self._closed = True
            self._transport = None

    @contextmanager
    def activated(self, transport: GuidanceTransport | None) -> Iterator[None]:
        """Open for the body and close on the way out, however the body ends."""
        self.open(transport)
        try:
            yield
        finally:
            self.close()

    def submit(self, request: GuidanceRequest) -> GuidanceOutcome:
        """Offer one guidance record to this turn and report what happened.

        Never raises: a transport that fails, or returns something that is not
        an outcome, is a deferral. The caller is a poll tick inside a live run,
        and no guidance-delivery failure may disturb the turn.
        """
        with self._lock:
            if request.turn != self._turn:
                return deferred(REASON_STALE_TURN)
            if self._closed:
                return deferred(REASON_STALE_TURN)
            if not self._opened:
                return deferred(REASON_NO_ACTIVE_TURN)
            transport = self._transport
            if transport is None:
                return deferred(REASON_HARNESS_UNSUPPORTED)
            try:
                outcome = transport(request)
            except Exception as error:  # noqa: BLE001 - transports must not escape
                return deferred(
                    REASON_TRANSPORT_UNAVAILABLE,
                    detail=f"{type(error).__name__}: {error}",
                )
        if not isinstance(outcome, GuidanceOutcome):
            return deferred(
                REASON_TRANSPORT_UNAVAILABLE,
                detail=f"transport returned {type(outcome).__name__}",
            )
        return outcome
