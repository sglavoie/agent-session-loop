"""The mutation lane: taking a submitted command in, carrying it out, reporting it.

Its own queue and its own worker, never the read ones: a mutation waits on ASL
taking an action and a read waits on a disk, and sharing a lane would let one
starve the other. Builds on :class:`~asl_workbench.connector_state.ConnectorState`.
"""

from __future__ import annotations

from .connector_lanes import _Answer, _Request
from .connector_state import ConnectorState
from .connector_support import (
    MAX_QUEUED_ANSWERS,
    MUTATION_SETTLEMENT_INTERVAL_SECONDS,
)
from .observation import MAX_LABEL_CHARS
from .operations import (
    DETAIL_DEADLINE_PASSED,
    DETAIL_NO_RESULT,
    OPERATION_ACCEPTED,
    OPERATION_EXPIRED,
    OPERATION_UNCONFIRMED,
)
from .remote_actions import (
    BodyUnavailable,
    MutationResult,
    RemoteActionError,
    operation_payload,
    parse_submission,
)
from .transport import CAPABILITY_MUTATE, KIND_OPERATION


class MutationLane(ConnectorState):
    """Submitted ``operation.submit`` commands, one at a time, off-thread."""

    # -- mutation delivery -----------------------------------------------
    def _submit(self, request: _Request, now: float) -> None:
        """Take one ``operation.submit`` into the mutation queue, or refuse it.

        Refusals here are the ones that can still be made honestly, because
        nothing has been journalled and nothing has been attempted: a method
        this connector will not carry, a submission missing the authority its
        method needs, a window that has already closed, a queue that is full.
        Every one of them is a receipt the service can act on immediately.

        A resubmission of a command already in hand is dropped without a
        receipt. The answer the service is waiting for is the operation's
        outcome, correlated by ``operation_id``, and it is coming; a second copy
        would only mean carrying the same mutation out twice.
        """
        try:
            submission = parse_submission(
                request.payload, now=now, deadline=request.deadline
            )
        except RemoteActionError as error:
            self._enqueue(
                self._receipt(
                    request,
                    error.outcome,
                    error.detail_code,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            )
            return
        try:
            accepted = self._mutations.accept(submission, now=now)
        except RemoteActionError as error:
            self._enqueue(
                self._receipt(
                    request,
                    error.outcome,
                    error.detail_code,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            )
            return
        if not accepted:
            return
        # Pull the carrier in: the operation frame reporting this command rides
        # the same outbound schedule every other answer does, and a service that
        # has just asked for an action is the one least willing to wait.
        self._next_heartbeat = min(
            self._next_heartbeat, now + self._carrier_interval(now)
        )

    def _cancel(self, operation_id: str) -> bool:
        """Withdraw a submitted mutation that has not been attempted yet.

        ``False`` when there was nothing to withdraw, including when the command
        is already being carried out -- which is not cancellable, because the
        attempt is running and its result is the only honest answer left.
        """
        return self._mutations.cancel(operation_id) is not None

    def _start_mutation(self, now: float) -> None:
        """Carry out the oldest submitted mutation, on a worker, one at a time.

        One at a time for a stronger reason than reads are: two mutations
        running together are two actions on plans whose state each was decided
        against, and serialising them is what keeps "the remote action behaves
        like the local one" true of a burst as well as of a single command.
        """
        if self._performer is None:
            return
        if len(self._queue.answers) >= MAX_QUEUED_ANSWERS:
            return
        with self._lock:
            if self._mutating is not None:
                return
        request = self._mutations.next_attempt(now)
        reconciling = False
        reconcile = getattr(self._performer, "reconcile", None)
        if request is None:
            if not callable(reconcile) or now < self._next_settlement:
                return
            request = next(iter(self._settling_mutations.values()), None)
            if request is None:
                return
            reconciling = True
            self._next_settlement = now + MUTATION_SETTLEMENT_INTERVAL_SECONDS
        with self._lock:
            self._mutating = request
        performer = self._performer
        source = self._body_source

        def attempt() -> None:
            fault: str | None = None
            body: str | None = None
            answer: _Answer | None = None
            try:
                if reconciling:
                    assert callable(reconcile)
                    result = reconcile(request, now=now)
                else:
                    if request.bears_body:
                        if source is None:
                            raise BodyUnavailable(
                                "no way to redeem this command's body"
                            )
                        body = source.fetch(request, now=now)
                    result = performer.perform(request, body=body, now=now)
            except BodyUnavailable as error:
                # Nothing was attempted, so the one word that is certainly true
                # is that it did not run.
                result = MutationResult(
                    status=OPERATION_EXPIRED, detail_code=error.detail_code
                )
                fault = error.message[:MAX_LABEL_CHARS]
            except Exception as error:  # a performer is arbitrary provider code
                # A failed first attempt may have acted before it raised, and
                # therefore reports unconfirmed. A failed passive reconciliation
                # changes no operation; retain it and try the evidence again.
                result = (
                    None
                    if reconciling
                    else MutationResult(
                        status=OPERATION_UNCONFIRMED, detail_code=DETAIL_NO_RESULT
                    )
                )
                fault = f"{type(error).__name__}: {error}"[:MAX_LABEL_CHARS]
            finally:
                # The body dies with the frame that used it, delivered or not.
                body = None
            if result is not None:
                answer = _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=operation_payload(request, result, observed_at=now),
                    capability=CAPABILITY_MUTATE,
                )
            with self._lock:
                self._mutation_result = answer
                self._mutating = None
                if result is not None:
                    if (
                        not reconciling
                        and result.status == OPERATION_ACCEPTED
                        and callable(reconcile)
                    ):
                        self._settling_mutations[request.operation_id] = request
                    elif result.status != OPERATION_ACCEPTED:
                        self._settling_mutations.pop(request.operation_id, None)
                if fault is not None:
                    self._mutation_fault = fault

        self._mutation_thread = self._run_mutation(attempt)

    def _collect_mutation(self) -> None:
        """Take a finished mutation's report into the outbound lane."""
        with self._lock:
            answer = self._mutation_result
            self._mutation_result = None
        if answer is None:
            return
        operation_id = answer.payload.get("operation_id")
        if isinstance(operation_id, str):
            self._mutations.settle(operation_id)
        self._enqueue(answer)

    def _expire_mutations(self, now: float) -> None:
        """Report every waiting command whose delivery window has closed.

        Only waiting ones. A command being carried out is past the point where
        ``expired`` could still be true, and saying it anyway would tell an
        owner their action did not happen while it was happening.
        """
        for request in self._mutations.expire(now):
            self._enqueue(
                _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=operation_payload(
                        request,
                        MutationResult(
                            status=OPERATION_EXPIRED,
                            detail_code=DETAIL_DEADLINE_PASSED,
                        ),
                        observed_at=now,
                    ),
                    capability=CAPABILITY_MUTATE,
                )
            )
