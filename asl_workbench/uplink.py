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

import hmac
import itertools
import secrets
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .credentials import ROLE_CAPABILITIES, ROLE_CONNECTOR
from .operations import OPERATION_DISPATCHED
from .remote_actions import MAX_BODY_CHARS
from .remote_reads import DetailReadClient, ReadAnswer, RemoteReadError
from .store import IngestInvalid, IngestRefused, WorkbenchStore
from .transport import (
    DIRECTION_UP,
    KIND_DETAIL,
    KIND_OBSERVATION,
    KIND_OPERATION,
    KIND_RECEIPT,
    REQUEST_MUTATE,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
    TransportError,
    request_envelope,
    validate_envelope,
)

__all__ = [
    "COMMAND_LIFETIME_SECONDS",
    "LINK_SILENCE_SECONDS",
    "MAX_CARRIED_FRAMES",
    "READ_WAIT_SECONDS",
    "Uplink",
    "UplinkRefusal",
]

#: How long a carried command stays worth performing. Wide enough that a
#: connector on its idle cadence still collects it, short enough that a control
#: the operator submitted and forgot about does not act on a run that has since
#: moved on.
COMMAND_LIFETIME_SECONDS = 300.0

#: How long a request thread waits for the Mac's answer before settling the
#: read itself. The budget covers two things the caller cannot see apart: the
#: wait for the next upload to carry the request out, and the Mac's own read.
#: It is the browser's patience, so it is bounded by what a person will sit in
#: front of rather than by what the transport permits.
READ_WAIT_SECONDS = 45.0

#: How long the link may be silent before it is treated as down. A connector
#: uploads a heartbeat on its idle cadence, so silence past this is not a quiet
#: Mac, it is no Mac -- and a read composed against a dead link must be
#: refused ``offline`` rather than left to time out, because those two are
#: different answers to the operator.
LINK_SILENCE_SECONDS = 180.0

#: How many downward frames the Pi will hold for a connector that is not
#: collecting them. A bound rather than a queue, because the alternative to
#: refusing the newest is growing without limit on behalf of a Mac that is
#: gone.
MAX_CARRIED_FRAMES = 64

#: The slice a waiting request thread sleeps for between checks. Short enough
#: that a link declared down mid-wait is noticed, long enough that a hundred
#: idle readers are not a hundred spinning threads.
_WAIT_SLICE_SECONDS = 0.25


class UplinkRefusal(Exception):
    """An upload was refused. Carries the status and reason the route answers.

    Deliberately not a ``ValueError`` subclass: the reason travels with it
    because the connector's own retry decision depends on which refusal this
    was, and a message it has to parse is not a decision it can make.
    """

    def __init__(self, status: int, reason: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.details = details


@dataclass
class _HeldBody:
    """One operator-authored body kept only for this process's live command."""

    reply_ref: str
    action_token: str
    text: str
    expires_at: float


class Uplink:
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

    # -- Volatile mutation bodies ------------------------------------------

    def stage_body(
        self, operation_id: str, text: str, *, now: float
    ) -> tuple[str, str, bool]:
        """Hold one reply in memory until its dispatched command redeems it."""
        if not isinstance(text, str) or not text.strip():
            raise UplinkRefusal(400, "invalid-reply", "reply text must not be empty")
        if len(text) > MAX_BODY_CHARS:
            raise UplinkRefusal(
                400,
                "invalid-reply",
                f"reply text exceeds {MAX_BODY_CHARS} characters",
            )
        with self._ready:
            existing = self._bodies.get(operation_id)
            if existing is not None:
                return existing.reply_ref, existing.action_token, False
            held = _HeldBody(
                reply_ref=f"ref-{operation_id}",
                action_token=secrets.token_hex(16),
                text=text,
                expires_at=now + self.command_lifetime_seconds,
            )
            self._bodies[operation_id] = held
            return held.reply_ref, held.action_token, True

    def discard_body(self, operation_id: str) -> None:
        """Overwrite and remove any prose held for a command."""
        with self._ready:
            held = self._bodies.pop(operation_id, None)
            if held is not None:
                held.text = ""
                held.action_token = ""

    def redeem_body(
        self,
        operation_id: str,
        reply_ref: str,
        action_token: str,
        *,
        connector_id: str,
        now: float,
    ) -> str:
        """Return a dispatched command's body to its paired connector only."""
        self._expire_bodies(now)
        record = self.store.operation(operation_id)
        if record is None or record.get("connector_id") != connector_id:
            raise UplinkRefusal(
                404, "unknown-operation", "no dispatched command has that identity"
            )
        if record.get("status") != OPERATION_DISPATCHED:
            raise UplinkRefusal(
                409,
                "not-dispatched",
                f"operation {operation_id} is {record.get('status')}",
            )
        with self._ready:
            held = self._bodies.get(operation_id)
            if held is None or not held.text:
                raise UplinkRefusal(
                    409, "not-dispatched", "the command body is no longer available"
                )
            if held.reply_ref != reply_ref or not hmac.compare_digest(
                held.action_token, action_token
            ):
                raise UplinkRefusal(
                    403, "invalid-action-token", "the command capability does not match"
                )
            return held.text

    def _expire_bodies(self, now: float) -> None:
        with self._ready:
            expired = [
                operation_id
                for operation_id, held in self._bodies.items()
                if now >= held.expires_at
            ]
        for operation_id in expired:
            self.discard_body(operation_id)

    # -- The upward path -----------------------------------------------------

    def ingest(
        self, frame: Any, *, now: float, subject: str | None = None
    ) -> Mapping[str, Any] | None:
        """Apply one uploaded frame and return whatever rides back down.

        ``subject`` is the connector id the presented credential may speak for.
        It is checked against the envelope rather than trusted from it, because
        the store keys everything it holds by that id and a credential that
        could upload under any of them would make every row unattributable.
        """
        try:
            envelope = validate_envelope(
                frame, direction=DIRECTION_UP, target_id=self.target_id
            )
        except TransportError as error:
            raise UplinkRefusal(400, error.code, error.detail) from error
        sender = envelope.get("connector_id")
        if subject is not None and sender != subject:
            raise UplinkRefusal(
                403,
                "wrong-connector",
                "this credential may not upload for another connector",
            )
        if sender != self.connector_id:
            raise UplinkRefusal(
                403,
                "wrong-connector",
                "this service is paired with a different connector",
                connector_id=self.connector_id,
            )
        self._mark_upload(now)
        self._apply(envelope, now=now)
        return self._hand_out(now)

    def _mark_upload(self, now: float) -> None:
        """Record the upload, and bring the link up if it was not."""
        with self._ready:
            self._last_upload_at = now
        if not self.reads.linked:
            self.reads.link_up(self.capabilities, now=now)

    def _apply(self, envelope: Mapping[str, Any], *, now: float) -> None:
        kind = str(envelope["kind"])
        if kind == KIND_OBSERVATION:
            self._retain(envelope, now=now)
            return
        if kind == KIND_OPERATION:
            self._settle_operation(envelope, now=now)
            return
        if kind in (KIND_DETAIL, KIND_RECEIPT):
            try:
                answer = self.reads.accept(envelope, now=now)
            except RemoteReadError as error:
                raise UplinkRefusal(403, "wrong-connector", str(error)) from error
            if answer is not None:
                self._keep((answer,))
            return
        # Unreachable: `validate_envelope` refused every other kind already.
        raise UplinkRefusal(  # pragma: no cover
            400, "unknown-kind", f"nothing applies a {kind} frame"
        )

    def _retain(self, envelope: Mapping[str, Any], *, now: float) -> None:
        """One observation, through the store's own path for its view."""
        view = str(envelope["payload"]["view"])
        applied = {
            VIEW_HEARTBEAT: self.store.ingest_heartbeat,
            VIEW_SNAPSHOT: self.store.ingest_snapshot,
            VIEW_CATALOG_PAGE: self.store.ingest_catalog,
            VIEW_HISTORY_BATCH: self.store.ingest_history,
        }[view]
        try:
            applied(envelope, received_at=now)
        except IngestRefused as error:
            # Refused for what the store already holds. 409 rather than 400:
            # the frame was fine and the connector should observe again rather
            # than resend this one, which is what a conflict means.
            raise UplinkRefusal(409, error.code, error.detail, view=view) from error
        except IngestInvalid as error:
            raise UplinkRefusal(400, "malformed", str(error), view=view) from error

    def _settle_operation(self, envelope: Mapping[str, Any], *, now: float) -> None:
        """One settlement report, into the journal row the submission created.

        A report about an operation this service never admitted is dropped
        rather than inserted: the journal is the record of what *this* service
        handed down, and a row invented from an incoming frame would be a
        command nobody here can account for. Dropped, not refused -- the
        connector has nothing to fix and a retry would not help it.
        """
        payload = envelope["payload"]
        operation_id = str(payload["operation_id"])
        if self.store.operation(operation_id) is None:
            return
        self.store.advance_operation(
            operation_id,
            str(payload["status"]),
            now=now,
            detail_code=payload.get("detail_code"),
            dispatched_at=payload.get("dispatched_at"),
            settled_at=payload.get("settled_at"),
        )
        self.discard_body(operation_id)

    # -- The downward path ---------------------------------------------------

    def _hand_out(self, now: float) -> Mapping[str, Any] | None:
        """The next frame to carry down, or ``None`` when there is nothing.

        Expired frames are discarded here rather than carried and refused on
        arrival: a request whose deadline has passed is one the transport would
        reject at the far end, and sending it would spend the acknowledgement
        that the next live request needed.
        """
        while True:
            envelope = self._next_carried()
            if envelope is None:
                return None
            deadline = envelope.get("deadline")
            if isinstance(deadline, int | float) and now >= float(deadline):
                payload = envelope.get("payload")
                if isinstance(payload, Mapping):
                    operation_id = payload.get("operation_id")
                    if isinstance(operation_id, str):
                        self.discard_body(operation_id)
                continue
            # Recorded outside the lock: ``_departed`` writes to the journal,
            # and a database write held across the condition every reader
            # waits on would make one command's departure everybody's pause.
            self._departed(envelope, now=now)
            return envelope

    def _next_carried(self) -> Mapping[str, Any] | None:
        with self._ready:
            try:
                return self._carry.popleft()
            except IndexError:
                return None

    def _departed(self, envelope: Mapping[str, Any], *, now: float) -> None:
        """Record that this frame has actually left."""
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return
        request_id = envelope.get("request_id")
        if payload.get("request") != REQUEST_MUTATE:
            # A read's deadline starts when it leaves, not when it was
            # composed: a request that sat waiting for the next upload has not
            # had its time yet.
            if isinstance(request_id, str):
                self.reads.sent(request_id, now=now)
            return
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            return
        if self.store.operation(operation_id) is None:
            return
        self.store.advance_operation(
            operation_id, OPERATION_DISPATCHED, now=now, dispatched_at=now
        )

    def _offer(self, envelope: Mapping[str, Any]) -> None:
        """Queue one frame for the next acknowledgement, or refuse to.

        The bound is checked before the append rather than trimmed after it,
        because the frame worth dropping is the one nobody is waiting on yet.
        """
        with self._ready:
            if len(self._carry) >= self.max_carried:
                raise UplinkRefusal(
                    503,
                    "link-congested",
                    "the connector is not collecting what this service is holding",
                    carried=len(self._carry),
                )
            self._carry.append(envelope)

    def _withdraw(self, request_id: str) -> bool:
        """Take one not-yet-carried frame back out. True when it was still there."""
        with self._ready:
            kept = [
                envelope
                for envelope in self._carry
                if envelope.get("request_id") != request_id
            ]
            removed = len(kept) != len(self._carry)
            self._carry.clear()
            self._carry.extend(kept)
            return removed

    def _drop_carried(self) -> None:
        """Discard everything queued for a link that is gone."""
        with self._ready:
            self._carry.clear()

    # -- DetailLane ----------------------------------------------------------

    def fetch(
        self,
        *,
        detail: str,
        plan_key: str,
        run_key: str | None = None,
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

    def _keep(self, answers: Iterable[ReadAnswer]) -> None:
        """Hold settled answers where the waiting threads will find them."""
        answers = tuple(answers)
        if not answers:
            return
        with self._ready:
            for answer in answers:
                self._settled[answer.request_id] = answer
            self._ready.notify_all()

    def _take(self, request_id: str) -> ReadAnswer | None:
        with self._ready:
            return self._settled.pop(request_id, None)

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
