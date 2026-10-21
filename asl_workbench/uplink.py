"""The connector-facing half of the service: what a paired Mac posts, and
what rides back down in the acknowledgement.

Everything else in this package is written so the Pi can answer a browser.
This module is the other side of the same service: the one path a connector
uploads to, and the one place the Pi puts a frame it wants carried the other
way. Without it the remote provider is a reader of a store nobody fills.

**One path, both directions.** The connector posts a frame to
``POST /v1/ingest`` and the *response body* is the downward half of the link:
when the Pi is holding a request -- a detail read the operator is waiting on,
or a control they just submitted -- it hands that request back as the
acknowledgement of the upload. A connector that is idle keeps uploading
heartbeats, so a quiet link still has a carrier. This is why there is no poll
route and no socket the Pi holds open: the Companion's
``POST /v1/commands/poll`` was a second round trip for something the first one
could carry, and it is not migrated.

**The lanes are this object.** :class:`Uplink` implements both
:class:`~asl_workbench.service.DetailLane` and
:class:`~asl_workbench.service.SubmissionLane`, because the two lanes and the
ingest path are three views of one queue and one correlation table. Splitting
them into three collaborating objects would mean three objects sharing one
lock, which is the same thing said less clearly.

**The waiting happens here, and only here.**
:class:`~asl_workbench.remote_reads.DetailReadClient` is deliberately
non-blocking: it is the bookkeeping half, and it says so. A browser asking the
Pi for a log window has to be answered by one request thread, so that thread is
where the wait belongs. :meth:`Uplink.fetch` blocks on a condition that
:meth:`Uplink.ingest` notifies from whichever thread the connector's upload
landed on -- which is safe because the server is a
:class:`http.server.ThreadingHTTPServer` and every upload gets its own thread.
A read that nobody carries is settled by its own deadline rather than by a
verdict invented here, so ``offline``, ``timeout`` and ``refused`` stay the
three different things they are.

**A command's row moves when the command leaves.** The journal's ``pending``
means "admitted and still here". :meth:`Uplink.ingest` advances a mutation to
``dispatched`` at the instant it is handed to an acknowledgement, not when it
was queued: a submission that never found a carrier is one the operator may
still cancel, and one that left is one whose outcome is now the Mac's to
report.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .credentials import ROLE_CAPABILITIES, ROLE_CONNECTOR
from .remote_reads import DetailReadClient, ReadAnswer
from .store_workbench import WorkbenchStore
from .transport import TransportError, request_envelope
from .uplink_flow import UplinkFlowMixin
from .uplink_support import (
    _WAIT_SLICE_SECONDS,
    COMMAND_LIFETIME_SECONDS,
    LINK_SILENCE_SECONDS,
    MAX_CARRIED_FRAMES,
    READ_WAIT_SECONDS,
    UplinkRefusal,
    _HeldBody,
)

__all__ = [
    "COMMAND_LIFETIME_SECONDS",
    "LINK_SILENCE_SECONDS",
    "MAX_CARRIED_FRAMES",
    "READ_WAIT_SECONDS",
    "Uplink",
    "UplinkRefusal",
]


class Uplink(UplinkFlowMixin):
    """One paired connector, as the service sees it.

    Built with the store it fills and the identities the pairing is keyed by.
    Everything it holds -- the correlation table, the carry queue, the settled
    answers -- is in memory, because all of it is about a link that a restart
    ends. The durable half is the store, and it is the store's rows that a
    restarted service reconciles against.
    """

    def __init__(
        self,
        store: WorkbenchStore,
        *,
        clock: Callable[[], float],
        connector_id: str,
        target_id: str,
        capabilities: Iterable[str] | None = None,
        read_wait_seconds: float = READ_WAIT_SECONDS,
        command_lifetime_seconds: float = COMMAND_LIFETIME_SECONDS,
        silence_seconds: float = LINK_SILENCE_SECONDS,
        max_carried: int = MAX_CARRIED_FRAMES,
    ) -> None:
        self.store = store
        self.clock = clock
        self.connector_id = connector_id
        self.target_id = target_id
        # What the pairing may be asked for. Taken from the role the *Pi*
        # issued the credential under rather than from anything the connector
        # says about itself: the hello is evaluated on the Mac when the
        # installation is attached, and a service that trusted a claim in an
        # upload would be letting the peer widen its own grant.
        self.capabilities = frozenset(
            capabilities
            if capabilities is not None
            else ROLE_CAPABILITIES[ROLE_CONNECTOR]
        )
        self.read_wait_seconds = float(read_wait_seconds)
        self.command_lifetime_seconds = float(command_lifetime_seconds)
        self.silence_seconds = float(silence_seconds)
        self.max_carried = max(1, int(max_carried))

        self.reads = DetailReadClient(connector_id=connector_id, target_id=target_id)
        self._ready = threading.Condition()
        self._carry: deque[Mapping[str, Any]] = deque()
        self._settled: dict[str, ReadAnswer] = {}
        self._bodies: dict[str, _HeldBody] = {}
        self._last_upload_at: float | None = None
        # Unique across this process and across a restart within the same
        # second, so a request id the Mac has already seen and discarded is
        # never reused for a different question.
        self._reads_asked = itertools.count(1)
        self._epoch = f"{int(self.clock())}"

    # -- Liveness ------------------------------------------------------------

    @property
    def linked(self) -> bool:
        """Whether a connector has uploaded recently enough to be asked."""
        return self.reads.linked

    @property
    def last_upload_at(self) -> float | None:
        with self._ready:
            return self._last_upload_at

    @property
    def carried(self) -> int:
        """How many downward frames are waiting for a carrier."""
        return len(self._carry)

    def sweep(self, now: float) -> None:
        """Drop the link when it has gone quiet, and time out what it owes.

        Idempotent and cheap, because it is called from two places that cannot
        coordinate: every read composes against a swept link, and the runtime's
        own poll sweeps a service nobody is currently reading. A link that went
        quiet has to become ``offline`` without a request arriving to discover
        it, or the first read after an outage waits out its whole budget to
        learn something the service already knew.
        """
        with self._ready:
            last = self._last_upload_at
        if last is not None and now - last > self.silence_seconds:
            self._keep(
                self.reads.link_down(now=now, note="the connector stopped uploading")
            )
            self._drop_carried()
        self._expire_bodies(now)
        self._keep(self.reads.expire(now))

    # -- DetailLane ----------------------------------------------------------

    def fetch(
        self,
        *,
        detail: str,
        plan_key: str,
        run_key: str | None = None,
        question_id: str | None = None,
        artifact: str | None = None,
        stream: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        max_bytes: int | None = None,
        now: float,
    ) -> ReadAnswer:
        """Ask the Mac for one artifact window and wait for the answer.

        The two outcomes that cost no round trip are decided by the read client
        and returned straight back: ``offline`` when there is no link, and
        ``unavailable`` when there is one that never granted ``detail``.
        """
        self.sweep(now)
        request_id = self._next_read_id()
        ticket = self.reads.read(
            request_id=request_id,
            detail=detail,
            plan_key=plan_key,
            run_key=run_key,
            question_id=question_id,
            artifact=artifact,
            stream=stream,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            now=now,
        )
        if ticket.answer is not None:
            return ticket.answer
        envelope = ticket.envelope
        if envelope is None:  # pragma: no cover - a ticket is one or the other
            raise UplinkRefusal(
                500, "internal-error", "the read client composed nothing to send"
            )
        try:
            self._offer(envelope)
        except UplinkRefusal:
            self.reads.cancel(request_id, now=now)
            raise
        answer = self._wait(request_id, until=now + self.read_wait_seconds)
        if answer is not None:
            return answer
        return self._abandon(request_id)

    def _next_read_id(self) -> str:
        """One correlation id, unique across this process and its restarts."""
        with self._ready:
            return f"read-{self._epoch}-{next(self._reads_asked)}"

    def _wait(self, request_id: str, *, until: float) -> ReadAnswer | None:
        """Block until this read settles or the caller's budget runs out."""
        with self._ready:
            while True:
                answer = self._settled.pop(request_id, None)
                if answer is not None:
                    return answer
                remaining = until - self.clock()
                if remaining <= 0:
                    return None
                self._ready.wait(min(remaining, _WAIT_SLICE_SECONDS))

    def _abandon(self, request_id: str) -> ReadAnswer:
        """Settle a read nothing came back for, and say which way it ended.

        Escalated rather than decided: the request is pulled back out of the
        queue if it never left, then the client's own deadline is given its
        chance, and only a read that is still outstanding after both is
        withdrawn. Inventing a ``timeout`` for a read that was never carried
        would tell the operator the Mac was slow when the link was idle.
        """
        self._withdraw(request_id)
        answer = self._take(request_id)
        if answer is not None:
            return answer
        self._keep(self.reads.expire(self.clock()))
        answer = self._take(request_id)
        if answer is not None:
            return answer
        cancelled = self.reads.cancel(request_id, now=self.clock())
        if cancelled is not None:
            return cancelled
        answer = self._take(request_id)
        if answer is not None:
            return answer
        # Every path above settles or returns, so arriving here means the
        # correlation table and the answer table disagree -- worth a failure
        # rather than a plausible answer.
        raise UplinkRefusal(
            500, "internal-error", f"read {request_id} neither answered nor settled"
        )

    # -- SubmissionLane ------------------------------------------------------

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        """Queue one validated submission for the next acknowledgement.

        The request id is the operation id, not a counter. The connector drops
        a request id it has already seen, so keying the frame by the operation
        makes that dedupe and the journal's own idempotence the same rule
        rather than two that have to agree.
        """
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):  # pragma: no cover - parser refused it
            raise UplinkRefusal(
                400, "invalid-submission", "a submission must name an operation"
            )
        try:
            envelope = request_envelope(
                payload=payload,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=f"submit-{operation_id}",
                deadline=now + self.command_lifetime_seconds,
            )
        except TransportError as error:
            raise UplinkRefusal(400, error.code, str(error)) from error
        self._offer(envelope)
