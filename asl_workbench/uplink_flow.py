"""The upward path (ingest) and the downward carry queue of the uplink."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .operations import (
    DETAIL_DEADLINE_PASSED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
)
from .remote_reads import RemoteReadError
from .store_types import IngestInvalid, IngestRefused
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
                self._undelivered(envelope, now=now, detail_code=DETAIL_DEADLINE_PASSED)
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
        self.store.advance_operation(
            operation_id,
            OPERATION_EXPIRED,
            now=now,
            detail_code=detail_code,
            settled_at=now,
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

    def _drop_carried(self, *, now: float) -> None:
        """Discard everything queued for a link that is gone."""
        with self._ready:
            dropped = list(self._carry)
            self._carry.clear()
        for envelope in dropped:
            self._undelivered(envelope, now=now, detail_code=None)
