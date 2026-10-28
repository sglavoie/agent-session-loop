"""The connector's outbound queue, its records, and its worker thread spawners.

What is waiting to go out, in the order it goes, and the three daemon-thread
launchers that keep a slow disk or a slow action off the delivery thread. The
runtime that drives them lives in :mod:`asl_workbench.connector_runtime`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .connector_protocols import ObservationCycle
from .transport import (
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
    KIND_DETAIL,
    KIND_OPERATION,
    KIND_RECEIPT,
)

#: The frame kinds that share the answer lane, as opposed to the four views the
#: connector publishes on its own schedule. All three are things somebody is
#: owed rather than things this Mac decided to say.
_ANSWER_KINDS = (KIND_DETAIL, KIND_OPERATION, KIND_RECEIPT)


@dataclass
class _Pass:
    """One observation attempt as the runtime sees it, finished or failed."""

    cycle: ObservationCycle | None = None
    error: str | None = None


@dataclass
class _Request:
    """One correlated read this connector has taken in but not yet answered."""

    request_id: str
    payload: dict[str, Any]
    #: The far end's own deadline. A request found past it is answered
    #: ``expired`` *without the read being performed*: the material would be
    #: built for a caller that has already stopped waiting, and building it
    #: would spend the disk on nobody.
    deadline: float | None = None


@dataclass
class _Answer:
    """One finished answer waiting for the channel.

    ``kind`` is a detail envelope carrying material, a receipt declining to, or
    an operation reporting what became of a mutation. The first two are
    correlated by ``request_id`` and both settle a read -- a request that
    produced neither would leave the far end waiting out a deadline for an
    answer this end already decided not to send.

    An operation has no ``request_id``: a mutation is correlated by its
    ``operation_id``, which is the only identifier that still means anything
    after the connector that took the submission in has been restarted.
    """

    request_id: str | None
    kind: str
    payload: dict[str, Any]
    deadline: float | None = None
    #: Which capability a receipt is declining on behalf of. A refused read and
    #: a refused mutation are different refusals and the far end routes them
    #: differently, so the word is carried rather than assumed.
    capability: str = CAPABILITY_DETAIL

    @property
    def settles_mutation(self) -> bool:
        """Whether losing this frame would strand a command's journal row."""
        return self.kind == KIND_OPERATION or (
            self.kind == KIND_RECEIPT and self.capability == CAPABILITY_MUTATE
        )

    def forget(self) -> None:
        """Drop whatever body this answer is holding.

        Called wherever an answer stops being deliverable. The payload is this
        connector's own dict, so emptying it releases the only reference to the
        text that was read -- which is the whole of "clear bodies after expiry
        or disconnect" on this end of the link.
        """
        self.payload.clear()


@dataclass
class _Queue:
    """What is waiting to go out, in the order it goes.

    Heartbeat and snapshot are single slots rather than lists: a newer one
    replaces an older one because the older one described a moment nobody needs
    any more. Pages and batches are queues because each carries material the
    next one does not. Answers are a queue of their own rather than a fifth
    observation view: they are correlated to a question somebody is waiting on,
    and burying one behind a hundred catalog pages is the starvation this lane
    exists to prevent.
    """

    heartbeat: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    answers: list[_Answer] = field(default_factory=list)

    def cycle_drained(self) -> bool:
        return not self.pages and not self.batches

    def cycle_waiting(self) -> bool:
        return self.snapshot is not None or bool(self.pages) or bool(self.batches)

    def clear_cycle(self) -> None:
        self.pages.clear()
        self.batches.clear()

    def clear_answers(self) -> None:
        for answer in self.answers:
            answer.forget()
        self.answers.clear()

    def drop_correlated(self) -> None:
        """Drop the answers a lost link made pointless, and keep the rest.

        Detail answers and detail receipts belong to waiters the far end has
        already told the link went down. Operation reports and mutation refusal
        receipts settle commands instead: the far end can still absorb both
        after reconnecting, even when it has marked a command unconfirmed.
        """
        live: list[_Answer] = []
        for answer in self.answers:
            if answer.settles_mutation:
                live.append(answer)
                continue
            answer.forget()
        self.answers = live


def _spawn_observation(attempt: Callable[[], None]) -> threading.Thread:
    """Run one observation pass off the delivery thread.

    A daemon thread: the pass reads and never writes, so an interpreter exiting
    while one is in flight loses a walk and nothing else.
    """
    thread = threading.Thread(target=attempt, name="workbench-observation", daemon=True)
    thread.start()
    return thread


def _spawn_answer(attempt: Callable[[], None]) -> threading.Thread:
    """Perform one correlated read off the delivery thread.

    A daemon thread for the same reason the observation pass is one: the read
    writes nothing, so an interpreter exiting mid-read loses an answer the
    caller will time out on and nothing else.
    """
    thread = threading.Thread(target=attempt, name="workbench-detail", daemon=True)
    thread.start()
    return thread


def _spawn_mutation(attempt: Callable[[], None]) -> threading.Thread:
    """Carry out one submitted mutation off the delivery thread.

    A daemon thread like the other two, and the only one where that costs
    something: an interpreter exiting mid-attempt loses the report of an action
    that may already have happened. It is still the right trade, because the
    durable row was written before the side effect -- so the next connector
    reads it, calls it ``unconfirmed`` rather than re-running it, and the far
    end learns the truth one restart late instead of never.
    """
    thread = threading.Thread(target=attempt, name="workbench-mutation", daemon=True)
    thread.start()
    return thread
