# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F821
"""Correlated client for remote Workbench detail reads."""

from __future__ import annotations

from . import remote_read_cache as _cache
from . import remote_read_models as _models

globals().update(
    {name: value for name, value in vars(_models).items() if not name.startswith("__")}
)
globals().update(
    {name: value for name, value in vars(_cache).items() if not name.startswith("__")}
)
del _cache, _models


# --------------------------------------------------------------------------
# The correlation table
# --------------------------------------------------------------------------


@dataclass
class _Waiter:
    """One read this process is waiting on."""

    request: ReadRequest
    timeout: float
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
            self.bodies.clear()
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
        question_id: str | None = None,
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
        lifetime = self._lifetime(timeout)
        request = ReadRequest(
            request_id=request_id,
            detail=detail,
            plan_key=plan_key,
            run_key=run_key,
            run_id=run_id,
            question_id=question_id,
            artifact=artifact,
            stream=stream,
            offset=offset,
            limit=limit,
            max_bytes=max_bytes,
            reason=reason,
            requested_at=now,
            deadline=now + lifetime,
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
                request=request, timeout=lifetime, expires_at=request.deadline
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
        where an artifact stops. Keep the selected run and requested selectors
        and bounds so a later page still reads the same history window.
        """
        if answer.next_offset is None:
            return None
        return self.read(
            request_id=request_id,
            detail=answer.detail,
            plan_key=answer.plan_key,
            run_key=answer.run_key,
            run_id=answer.run_id,
            question_id=answer.question_id,
            artifact=answer.artifact,
            stream=answer.stream,
            offset=answer.next_offset,
            limit=answer.limit,
            max_bytes=answer.max_bytes,
            now=now,
            timeout=timeout,
        )

    def sent(
        self, request_id: str, *, now: float, timeout: float | None = None
    ) -> float | None:
        """Record that the request has left this process.

        The deadline is taken from this moment rather than from composition: a
        request that sat in a queue waiting for the next upload has not had its
        time yet, and starting the clock before it left would time it out for
        somebody else's slowness. Keep the duration chosen for this request
        unless the sender explicitly overrides it.

        Return the deadline the outgoing envelope must carry, or ``None`` if
        the read has already settled and must no longer be sent.
        """
        with self._lock:
            waiter = self._waiters.get(request_id)
            if waiter is None:
                return None
            lifetime = waiter.timeout if timeout is None else self._lifetime(timeout)
            waiter.sent = True
            waiter.expires_at = now + lifetime
            return waiter.expires_at

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
        A deadline reached before the expiry sweep settles as ``timeout`` here
        without retaining the late body.
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
            if now >= waiter.expires_at:
                return self._settle(
                    waiter.request,
                    OUTCOME_TIMEOUT,
                    now=now,
                    note="no answer before the deadline",
                )
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
        target = payload["target"]
        answer = ReadAnswer(
            request_id=request.request_id,
            outcome=OUTCOME_REFUSED if refusal is not None else OUTCOME_ANSWERED,
            detail=str(payload.get("detail", request.detail)),
            plan_key=request.plan_key,
            run_key=target.get("run_key") or request.run_key,
            run_id=target.get("run_id") or request.run_id,
            question_id=request.question_id,
            limit=request.limit,
            max_bytes=request.max_bytes,
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
