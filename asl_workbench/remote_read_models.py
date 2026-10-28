# ruff: noqa: F401
"""Correlated detail reads, from the side of the link that asks for them.

:mod:`asl_workbench.connector_runtime` publishes what the Mac decides to say. This is
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
    "DETAIL_REFUSAL_REASONS",
    "MAX_CACHED_BODIES",
    "MAX_DETAIL_ENTRIES",
    "MAX_PENDING_READS",
    "MAX_READ_TIMEOUT_SECONDS",
    "OUTCOME_ANSWERED",
    "OUTCOME_CANCELLED",
    "OUTCOME_OFFLINE",
    "OUTCOME_REFUSED",
    "OUTCOME_TIMEOUT",
    "OUTCOME_UNAVAILABLE",
    "READ_OUTCOMES",
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
#: asked it: plan, run, surface, selector, offset and bounds. A run id can
#: constrain a live-plan read without a run key; bounds distinguish a short
#: preview from a larger window at the same offset.
ReadKey = tuple[str, str, str, str, str, int, str, int | None, int | None]


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
    question_id: str | None = None
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
        if self.offset is not None and self.offset < 0:
            raise RemoteReadError("offset below 0")
        if self.limit is not None and not 1 <= self.limit <= MAX_DETAIL_ENTRIES:
            raise RemoteReadError(f"limit outside 1..{MAX_DETAIL_ENTRIES}")
        if self.max_bytes is not None and not 1 <= self.max_bytes <= MAX_DETAIL_BYTES:
            raise RemoteReadError(f"max_bytes outside 1..{MAX_DETAIL_BYTES}")

    @property
    def key(self) -> ReadKey:
        """What was asked, as the cache keys it."""
        return (
            self.plan_key,
            self.run_key or "",
            self.detail,
            self.question_id or "",
            self.artifact or self.stream or "",
            int(self.offset or 0),
            self.run_id or "",
            self.limit,
            self.max_bytes,
        )

    def target(self) -> dict[str, Any]:
        """The plan and run this read is about."""
        if self.detail == "archive":
            return {"archive": "list"}
        addressed: dict[str, Any] = {"plan_key": self.plan_key}
        if self.run_key is not None:
            addressed["run_key"] = self.run_key
        if self.run_id is not None:
            addressed["run_id"] = self.run_id
        if self.question_id is not None:
            addressed["question_id"] = self.question_id
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
