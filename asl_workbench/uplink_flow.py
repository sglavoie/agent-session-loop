"""The upward path (ingest) and the downward carry queue of the uplink."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .operations import (
    DETAIL_DEADLINE_PASSED,
    OPERATION_DETAIL_CODES,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
    OPERATION_TRANSITIONS,
    OPERATION_UNCONFIRMED,
    reconcile,
    terminal,
)
from .remote_reads import RemoteReadError
from .store_types import IngestInvalid, IngestRefused
from .transport import (
    CAPABILITY_MUTATE,
    DIRECTION_UP,
    KIND_DETAIL,
    KIND_OBSERVATION,
    KIND_OPERATION,
    KIND_RECEIPT,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    RECEIPT_UNCONFIRMED,
    REQUEST_DETAIL,
    REQUEST_MUTATE,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
    TransportError,
    validate_envelope,
)
from .uplink_bodies import UplinkBodiesMixin
from .uplink_support import UplinkRefusal


class UplinkFlowMixin(UplinkBodiesMixin):
    """Apply uploaded frames and hand queued frames back in the acknowledgement."""

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
            self._last_upload_mono = self.monotonic()
            # Two uploads must not both observe a down link and each start a
            # generation: the second link_up would abandon the first's reads.
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
        if kind == KIND_RECEIPT and envelope.get("capability") == CAPABILITY_MUTATE:
            self._settle_refusal(envelope, now=now)
            return
        if kind in (KIND_DETAIL, KIND_RECEIPT):
            # Removing the waiter and publishing its answer are one change
            # from a browser thread's perspective, including at its deadline.
            with self._ready:
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
        reported = str(payload["status"])
        while True:
            row = self.store.operation(operation_id)
            if row is None or row.get("connector_id") != self.connector_id:
                return
            current = str(row.get("status", ""))
            if reported == current:
                break
            if reported not in OPERATION_TRANSITIONS.get(current, frozenset()):
                # A late report against a row this service already concluded --
                # the stranded sweep settles what nobody reported in time.
                # Refusing it would make the connector resend it forever.
                return
            if self.store.settle_operation_if(
                operation_id,
                current,
                reported,
                now=now,
                detail_code=payload.get("detail_code"),
                dispatched_at=payload.get("dispatched_at"),
                settled_at=payload.get("settled_at"),
            ):
                break
            # A sweep or another upload advanced the row since our read.
            # Recheck: terminal evidence may still settle an unconfirmed row.
            # Status transitions are acyclic, so contention cannot loop forever.
        self.discard_body(operation_id)

    def _settle_refusal(self, envelope: Mapping[str, Any], *, now: float) -> None:
        """A receipt the connector sent instead of taking a submission.

        The connector refuses a submission it will not queue -- a closed
        window, a full queue, a missing token -- with a receipt rather than an
        operation frame, because nothing was attempted. That receipt is the
        only report the dispatched row will ever get, so it settles the row
        here: left ``dispatched``, the plan would refuse every later mutation
        as still outstanding.
        """
        status = {
            RECEIPT_REFUSED: OPERATION_REJECTED,
            RECEIPT_EXPIRED: OPERATION_EXPIRED,
            RECEIPT_UNCONFIRMED: OPERATION_UNCONFIRMED,
        }.get(str(envelope["payload"]["outcome"]))
        request_id = envelope.get("request_id")
        if (
            status is None
            or not isinstance(request_id, str)
            or not request_id.startswith("submit-")
        ):
            return
        operation_id = request_id.removeprefix("submit-")
        claimed_id = envelope["payload"].get("operation_id")
        if claimed_id is not None and claimed_id != operation_id:
            return
        row = self.store.operation(operation_id)
        if (
            row is None
            or row.get("connector_id") != self.connector_id
            or row.get("status") != OPERATION_DISPATCHED
        ):
            return
        detail_code = envelope["payload"].get("detail_code")
        self.store.settle_operation_if(
            operation_id,
            OPERATION_DISPATCHED,
            status,
            now=now,
            # A receipt may cite a transport code the journal has no word for.
            detail_code=detail_code if detail_code in OPERATION_DETAIL_CODES else None,
            settled_at=None if status == OPERATION_UNCONFIRMED else now,
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
                self._undelivered(envelope, now=now, detail_code=DETAIL_DEADLINE_PASSED)
                continue
            payload = envelope.get("payload")
            if (
                isinstance(payload, Mapping)
                and payload.get("request") == REQUEST_DETAIL
            ):
                request_id = envelope.get("request_id")
                if not isinstance(request_id, str):
                    continue
                deadline = self.reads.sent(request_id, now=now)
                if deadline is None:
                    # Cancellation can win after dequeue, when withdrawal can
                    # no longer remove the frame. Spend this upload on the
                    # next live request instead.
                    continue
                # The connector expires requests and queued answers using the
                # wire deadline. It must agree with the client's renewed wait.
                envelope = {**envelope, "sent_at": now, "deadline": deadline}
            # Recorded outside the lock: ``_departed`` writes to the journal,
            # and a database write held across the condition every reader
            # waits on would make one command's departure everybody's pause.
            if not self._departed(envelope, now=now):
                continue
            return envelope

    def _next_carried(self) -> Mapping[str, Any] | None:
        with self._ready:
            try:
                return self._carry.popleft()
            except IndexError:
                return None

    def _departed(self, envelope: Mapping[str, Any], *, now: float) -> bool:
        """Record that this frame has actually left. False when it must not.

        A submission whose row is no longer ``pending`` was settled without
        it -- expired by the stranded sweep -- and carrying it now would run a
        mutation its own journal row says did not happen.
        """
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return True
        if payload.get("request") != REQUEST_MUTATE:
            return True
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            return True
        if self.store.operation(operation_id) is None:
            return True
        if not self.store.settle_operation_if(
            operation_id,
            OPERATION_PENDING,
            OPERATION_DISPATCHED,
            now=now,
            dispatched_at=now,
        ):
            self.discard_body(operation_id)
            return False
        return True

    def _undelivered(
        self, envelope: Mapping[str, Any], *, now: float, detail_code: str | None
    ) -> None:
        """Expire the row of a submission this service will never carry.

        A frame discarded here never left, so the mutation demonstrably did not
        run. Leaving its row ``pending`` would hold the plan's one outstanding
        mutation forever: nothing else on this side ever moves a row nobody
        dispatched, and retention keeps every row that is still open.
        """
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping) or payload.get("request") != REQUEST_MUTATE:
            return
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str):
            return
        row = self.store.operation(operation_id)
        if row is None or row.get("status") != OPERATION_PENDING:
            return
        self.store.settle_operation_if(
            operation_id,
            OPERATION_PENDING,
            OPERATION_EXPIRED,
            now=now,
            detail_code=detail_code,
            settled_at=now,
        )

    def _settle_stranded(self, now: float, *, restarted: bool) -> None:
        """Settle journal rows that nothing on the link will ever report.

        The carry queue is memory, so a restart strands every ``pending`` row
        it held, and a lost acknowledgement strands a ``dispatched`` one the
        connector never received. Either would hold its plan's one outstanding
        mutation for good, since retention keeps every open row. The verdicts
        are :func:`~asl_workbench.operations.reconcile`'s for an owner that is
        gone:

        * ``pending`` and not in the carry queue settles as ``expired``: it
          never left, so it never ran. Only once its window has closed, unless
          ``restarted`` -- a submission between its journal write and its
          hand-down is briefly not queued either.
        * ``dispatched`` with no report one command lifetime past its window
          settles as ``unconfirmed``: it may have run and nobody can say. The
          grace covers an attempt the connector started inside the window.
        """
        with self._ready:
            carried = {
                str(envelope["payload"].get("operation_id"))
                for envelope in self._carry
                if isinstance(envelope.get("payload"), Mapping)
            }
        for row in self.store.operations_in_status(
            OPERATION_PENDING, connector_id=self.connector_id
        ):
            if str(row["operation_id"]) in carried:
                continue
            if restarted or now >= self._window_end(row):
                self._settle_one(row, now=now)
        for row in self.store.operations_in_status(
            OPERATION_DISPATCHED, connector_id=self.connector_id
        ):
            if now >= self._window_end(row) + self.command_lifetime_seconds:
                self._settle_one(row, now=now)

    def _window_end(self, row: Mapping[str, Any]) -> float:
        deliver_by = row.get("deliver_by")
        if isinstance(deliver_by, int | float):
            return float(deliver_by)
        return float(row.get("requested_at") or 0.0) + self.command_lifetime_seconds

    def _settle_one(self, row: Mapping[str, Any], *, now: float) -> None:
        verdict = reconcile(row, now=now)
        if verdict is None:
            return
        operation_id = str(row["operation_id"])
        if self.store.settle_operation_if(
            operation_id,
            str(row["status"]),
            verdict.status,
            now=now,
            detail_code=verdict.detail_code,
            settled_at=now if terminal(verdict.status) else None,
        ):
            self.discard_body(operation_id)

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
