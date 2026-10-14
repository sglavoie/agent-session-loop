"""The read lane: taking a correlated request in, answering it, expiring it.

A request rides back on the acknowledgement of an upload; this lane validates
it, refuses what it must, and hands a ``detail.read`` to the answering worker.
An ``operation.submit`` is handed on to the mutation lane it builds on, in
:mod:`asl_workbench.connector_mutations`.
"""

from __future__ import annotations

from .connector_lanes import _Answer, _Request
from .connector_mutations import MutationLane
from .connector_support import MAX_PENDING_REQUESTS, MAX_QUEUED_ANSWERS
from .observation import MAX_LABEL_CHARS
from .operations import DETAIL_BRIDGE_UNAVAILABLE
from .transport import (
    CAPABILITY_BY_REQUEST,
    CAPABILITY_DETAIL,
    CAPABILITY_MUTATE,
    DIRECTION_DOWN,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNSUPPORTED_CAPABILITY,
    KIND_DETAIL,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    REQUEST_DETAIL,
    REQUEST_MUTATE,
    TransportError,
    validate_frame,
)


class ReadLane(MutationLane):
    """Correlated detail reads, one at a time, off the delivery thread."""

    # -- correlated reads ------------------------------------------------
    def _receive(self, carried: bytes | None, now: float) -> None:
        """Take in whatever the acknowledgement carried back.

        Everything is checked before anything is believed: the frame against the
        transport, its direction against ``down``, its target against this
        deployment, and its connector id against this connector. A service that
        is paired with somebody else does not get to queue work here.
        """
        if not carried:
            return
        try:
            envelope = validate_frame(
                carried, direction=DIRECTION_DOWN, target_id=self.target_id
            )
        except TransportError as error:
            self._request_fault = f"undecodable request: {error}"[:MAX_LABEL_CHARS]
            return
        if envelope.get("connector_id") != self.connector_id:
            self._request_fault = "request addressed to another connector"
            return
        request_id = envelope.get("request_id")
        if not isinstance(request_id, str):
            self._request_fault = "request without a correlation id"
            return
        self._request_fault = None
        self._last_request_at = now
        # Pull the next carrier in. The heartbeat that brought this request was
        # scheduled before anybody was asking, so leaving its successor where
        # the idle cadence put it would make the *first* read of a quiet period
        # wait out a whole heartbeat interval -- which is exactly the read a
        # person is sitting in front of.
        self._next_heartbeat = min(
            self._next_heartbeat, now + self._carrier_interval(now)
        )
        if self._known(request_id):
            # A resend of something already in hand. Dropped rather than
            # queued twice: the answer is correlated, so one is all the far end
            # can match, and a second would be a body built for nobody.
            return
        request = _Request(
            request_id=request_id,
            payload=dict(envelope["payload"]),
            deadline=envelope.get("deadline"),
        )
        refusal = self._decline(request, now)
        if refusal is not None:
            self._enqueue(refusal)
            return
        if request.payload.get("request") == REQUEST_MUTATE:
            self._submit(request, now)
            return
        if len(self._requests) >= MAX_PENDING_REQUESTS:
            self._enqueue(
                self._receipt(request, RECEIPT_REFUSED, DETAIL_BRIDGE_UNAVAILABLE, now)
            )
            return
        self._requests.append(request)

    def _known(self, request_id: str) -> bool:
        """Whether this correlation id is already somewhere in the pipeline.

        All four places it can be, including the finished-but-not-yet-collected
        slot: a worker hands its answer back between two steps, and a resend
        arriving in that gap would otherwise be read as a new question and the
        disk walked a second time for a caller who is about to be answered.
        """
        with self._lock:
            in_flight = self._answering
            finished = self._answer_result
        return (
            any(pending.request_id == request_id for pending in self._requests)
            or (in_flight is not None and in_flight.request_id == request_id)
            or (finished is not None and finished.request_id == request_id)
            or any(done.request_id == request_id for done in self._queue.answers)
        )

    def _decline(self, request: _Request, now: float) -> _Answer | None:
        """The receipt refusing this request outright, or ``None`` to take it.

        This is where detail traffic is kept apart from mutation delivery. The
        two are told apart here and never meet again: a ``detail.read`` goes to
        the read queue and the answering worker, an ``operation.submit`` goes to
        the mutation queue and its own worker, and neither can be starved or
        delayed by a backlog of the other.

        A capability this connector did not offer is refused on arrival with the
        word that says which one is missing, so a service that ignored the hello
        gets an answer it can route rather than silence.
        """
        method = request.payload.get("request")
        capability = CAPABILITY_BY_REQUEST.get(str(method))
        if capability is None or capability not in self.capabilities:
            return self._receipt(
                request,
                RECEIPT_REFUSED,
                ERROR_UNSUPPORTED_CAPABILITY,
                now,
                capability=capability or CAPABILITY_DETAIL,
            )
        if method == REQUEST_DETAIL:
            if self._responder is None:
                return self._receipt(
                    request, RECEIPT_REFUSED, ERROR_UNSUPPORTED_CAPABILITY, now
                )
            if request.deadline is not None and now >= request.deadline:
                # Answered expired without the read being performed. The
                # material would be built for a caller that has already given up.
                return self._receipt(request, RECEIPT_EXPIRED, ERROR_DEADLINE, now)
            return None
        if method == REQUEST_MUTATE:
            if self._performer is None:
                return self._receipt(
                    request,
                    RECEIPT_REFUSED,
                    ERROR_UNSUPPORTED_CAPABILITY,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            if request.deadline is not None and now >= request.deadline:
                # The window closed in flight. Refused without being journalled,
                # which is what makes ``expired`` here mean *it did not run*.
                return self._receipt(
                    request,
                    RECEIPT_EXPIRED,
                    ERROR_DEADLINE,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            return None
        return self._receipt(request, RECEIPT_REFUSED, ERROR_FORBIDDEN_METHOD, now)

    def _start_answer(self, now: float) -> None:
        """Perform the oldest taken-in read, on a worker, one at a time.

        One at a time because the worker is reading a disk and a fan-out of
        them is how a cockpit turns a slow mount into a stalled Mac. Off the
        delivery thread for the reason observation is: a read that blocks must
        not take the heartbeat down with it.
        """
        if not self._requests or self._responder is None:
            return
        if len(self._queue.answers) >= MAX_QUEUED_ANSWERS:
            return
        with self._lock:
            if self._answering is not None:
                return
            request = self._requests.pop(0)
            self._answering = request
        responder = self._responder

        def attempt() -> None:
            answer: _Answer
            try:
                payload = responder.answer(request.payload, now=now)
                answer = _Answer(
                    request_id=request.request_id,
                    kind=KIND_DETAIL,
                    payload=dict(payload),
                    deadline=request.deadline,
                )
            except Exception as error:  # a responder is arbitrary provider code
                # Still an answer. A provider bug costs the caller one read
                # rather than a deadline's worth of silence, and the code says
                # the connector could not reach what it was asked for.
                answer = self._receipt(
                    request, RECEIPT_REFUSED, DETAIL_BRIDGE_UNAVAILABLE, now
                )
                fault: str | None = f"{type(error).__name__}: {error}"[:MAX_LABEL_CHARS]
            else:
                fault = None
            with self._lock:
                self._answer_result = answer
                self._answering = None
                if fault is not None:
                    self._answer_fault = fault

        self._answer_thread = self._run_answer(attempt)

    def _collect_answer(self) -> None:
        """Take the finished answer, if there is one, into the outbound lane."""
        with self._lock:
            answer = self._answer_result
            self._answer_result = None
        if answer is not None:
            self._enqueue(answer)

    def _expire_reads(self, now: float) -> None:
        """Drop everything correlated whose deadline has passed.

        A pending request goes: its deadline passed before the disk was walked,
        so walking it now would build material for somebody who has stopped
        listening.

        A queued *detail* goes for the same reason, and its body goes with it.
        A queued *receipt* stays. It carries no body, it is the frame that
        settles the read, and the two ends do not share a clock -- a caller
        whose deadline this Mac thinks has passed may still be waiting, and one
        small frame ends that wait immediately instead of leaving it to run out.
        """
        self._requests = [
            request
            for request in self._requests
            if request.deadline is None or now < request.deadline
        ]
        live: list[_Answer] = []
        for answer in self._queue.answers:
            expired = answer.deadline is not None and now >= answer.deadline
            if expired and answer.kind == KIND_DETAIL:
                answer.forget()
                continue
            live.append(answer)
        self._queue.answers = live

    def _forget_reads(self) -> None:
        """Abandon every correlated read that has not been answered."""
        self._requests.clear()
        with self._lock:
            self._answer_result = None
