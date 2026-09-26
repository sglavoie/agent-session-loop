"""Carrying out a mutation the service asked for, on the side that performs it.

:mod:`asl_workbench.remote_reads` is the service asking to *look*. This is the
connector's half of the service asking to *act*: an ``operation.submit`` rides
down inside the acknowledgement of an upload, is held here until it is either
carried out or abandoned, and the only thing that ever goes back up about it is
an ``operation`` frame naming the status ASL actually reached.

It is the Companion's ``CommandExecutor`` with the queue it drains kept
deliberately separate from the detail lane, and the same five refusals:

* **A command lives inside a bounded window and dies when it closes.**
  :data:`MAX_DELIVERY_WINDOW_SECONDS` clamps whatever deadline the service put
  on the envelope, so a service that asks for an hour gets a window this
  connector is willing to honour rather than one it merely promised. Past the
  window an unattempted command is :data:`~asl_workbench.operations.OPERATION_EXPIRED`
  with ``deadline-passed`` -- a word that means *it certainly did not run*, and
  is therefore never reachable once an attempt has begun.

* **Cancellation only reaches what has not been attempted.**
  :meth:`MutationQueue.cancel` drops a command that is still waiting and
  produces no frame at all, because a command that never left this queue is one
  the owner withdrew rather than one ASL refused. Once
  :meth:`MutationQueue.next_attempt` has handed it out, cancelling is a lie the
  queue will not tell: the attempt is already running and its result is the
  only honest answer.

* **A body is fetched at the moment of use and never stored.**
  :class:`MutationRequest` carries a ``reply_ref`` and the single-use
  ``action_token`` that redeems it -- never the prose. The body is fetched
  inside the frame that performs the mutation and dropped when that frame
  returns, which is what makes "a pending command body never survives a
  restart" structural rather than a rule somebody has to remember. A command
  that outlives its attempt has :meth:`MutationRequest.forget` called on it,
  dropping the reference *and* the authority to redeem it, in that order.

* **A restart settles what it finds, and retries none of it.**
  :func:`restart_settlement` reads a durable operation row and decides between
  two words that are not interchangeable: a row still ``pending`` was journalled
  and never dispatched, so it certainly did not run and is ``expired``; a row
  already ``dispatched`` may have run, so it is ``unconfirmed``. Neither is
  re-attempted. Automatically re-running a mutation whose outcome is unknown is
  the one failure this whole design exists to prevent.

* **Only words both ends already share go on the wire.** A status is one of
  :data:`~asl_workbench.transport.CONNECTOR_REPORTED_STATUSES` and a detail code
  one of :data:`~asl_workbench.transport.RECEIPT_DETAIL_CODES`. ASL's own
  refusal vocabulary stays on :attr:`MutationResult.reason`, which local callers
  read and :func:`operation_payload` never looks at.

Like the rest of the package this module composes and inspects payloads and
touches nothing else: no socket, no file, no clock reading. The moment is always
an argument, so a test asserts about the decision rather than racing it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
    DETAIL_CONNECTOR_RESTARTED,
    DETAIL_DEADLINE_PASSED,
    METHOD_ABORT,
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    METHOD_LAUNCH,
    METHOD_PLAN_ARCHIVE,
    METHOD_REDIRECT,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
    OPERATION_ACCEPTED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
    REPLY_BEARING_METHODS,
    OperationError,
    require_method,
    require_operation_id,
)
from .transport import (
    CONNECTOR_REPORTED_STATUSES,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNAUTHORIZED,
    ERROR_WRONG_TARGET,
    MAX_IDENTIFIER_CHARS,
    RECEIPT_DETAIL_CODES,
    RECEIPT_EXPIRED,
    RECEIPT_OUTCOMES,
    RECEIPT_REFUSED,
    REQUEST_MUTATE,
)

__all__ = [
    "BODY_BEARING_METHODS",
    "BodyUnavailable",
    "DELIVERABLE_METHODS",
    "FRESHNESS_PROVEN_METHODS",
    "MAX_BODY_CHARS",
    "MAX_DELIVERY_WINDOW_SECONDS",
    "MAX_PENDING_MUTATIONS",
    "MutationQueue",
    "MutationRequest",
    "MutationResult",
    "RemoteActionError",
    "Settlement",
    "operation_payload",
    "parse_submission",
    "reportable_status",
    "restart_settlement",
    "settlement_payload",
]


#: How long this connector is willing to hold a command it has not yet carried
#: out, whatever the service asked for. The envelope deadline is the service's
#: request and this is the answer to it: the smaller of the two wins, so a
#: generous service cannot leave a mutation live long enough for the state it
#: was decided against to have changed underneath it.
MAX_DELIVERY_WINDOW_SECONDS = 300.0

#: How many commands may wait at once. Small on purpose: mutations are carried
#: out one at a time and a deep queue only means more commands whose window
#: closes before they are reached, which is a worse answer than refusing the
#: submission while the service can still decide what to do about it.
MAX_PENDING_MUTATIONS = 4

#: The largest body this connector will redeem, mirroring the Companion's own
#: ``max_reply_chars``. A longer one is refused whole: a truncated reply is
#: prose the run would act on as though it were complete.
MAX_BODY_CHARS = 8192

#: Which methods need prose that does not travel in the envelope. Each of these
#: arrives as a ``reply_ref`` plus the token that redeems it, and the body is
#: fetched only once the mutation is actually being carried out.
BODY_BEARING_METHODS: frozenset[str] = frozenset(
    {*REPLY_BEARING_METHODS, METHOD_REDIRECT}
)

#: Which methods act on a run that is already going, and so may only be
#: submitted with the freshness proof ASL handed out with the state the caller
#: was looking at. Launching, unlocking and archiving are absent because none of
#: them is decided against a live run: there is no run yet, the run is already
#: gone, or the plan is not running at all. Selecting a harness is absent for
#: the same reason in a subtler form -- it changes what a *future* turn runs
#: under, so there is no live turn for a proof to be about, which is why
#: :data:`~asl_workbench.controls.ASL_ACTION_FOR_CONTROL` gives it no action to
#: validate against and why demanding one here would refuse it every time.
FRESHNESS_PROVEN_METHODS: frozenset[str] = frozenset(
    {
        *BODY_BEARING_METHODS,
        METHOD_ABORT,
        METHOD_STOP_AFTER_CURRENT,
    }
)

#: Every method this lane will carry, which is every retained control plus the
#: two that answer a run and the two that act on a plan at rest. Held as its own
#: name so a method added to the operation vocabulary does not silently become
#: remotely submittable.
DELIVERABLE_METHODS: frozenset[str] = frozenset(
    {
        METHOD_ABORT,
        METHOD_LAUNCH,
        METHOD_PLAN_ARCHIVE,
        METHOD_ARCHIVE_DELETE,
        METHOD_ARCHIVE_RESTORE,
        METHOD_ARCHIVE_SETTINGS,
        METHOD_REDIRECT,
        METHOD_SELECT_HARNESS,
        METHOD_STOP_AFTER_CURRENT,
        METHOD_UNLOCK,
        *BODY_BEARING_METHODS,
    }
)

#: The fields of a submission's ``target`` this lane carries forward, matching
#: :data:`~asl_workbench.transport.TARGET_SHAPE`.
_TARGET_FIELDS = ("run_key", "run_id", "question_id", "session_id")


class RemoteActionError(Exception):
    """A submission refused before anything was attempted.

    Carries the receipt the connector should send back rather than a message
    for a log, because the service is waiting on a correlated answer and a
    refusal it cannot read is the same as silence.
    """

    def __init__(
        self,
        detail_code: str,
        message: str,
        *,
        outcome: str = RECEIPT_REFUSED,
    ) -> None:
        super().__init__(message)
        if outcome not in RECEIPT_OUTCOMES:
            raise ValueError(f"unknown receipt outcome: {outcome!r}")
        if detail_code not in RECEIPT_DETAIL_CODES:
            raise ValueError(f"unknown receipt detail code: {detail_code!r}")
        self.outcome = outcome
        self.detail_code = detail_code
        self.message = message


class BodyUnavailable(Exception):
    """The prose a mutation needs could not be redeemed, so nothing was tried.

    Distinct from a mutation that failed: the attempt never began, so the
    operation settles ``expired`` and the service is free to submit again.
    """

    def __init__(
        self,
        message: str,
        *,
        detail_code: str = DETAIL_BRIDGE_UNAVAILABLE,
    ) -> None:
        super().__init__(message)
        if detail_code not in RECEIPT_DETAIL_CODES:
            raise ValueError(f"unknown receipt detail code: {detail_code!r}")
        self.detail_code = detail_code
        self.message = message


@dataclass
class MutationRequest:
    """One mutation this connector has been asked to carry out.

    Mutable on purpose, and for one reason: :meth:`forget` has to be able to
    take the redemption authority away from a copy somebody else is still
    holding. Everything else about it is written once, at parse time.
    """

    operation_id: str
    method: str
    plan_key: str
    requested_at: float
    deliver_by: float
    run_key: str | None = None
    run_id: str | None = None
    question_id: str | None = None
    session_id: str | None = None
    harness: str | None = None
    reason: str | None = None
    archive_id: str | None = None
    archive: str | None = None
    archive_retention_days: int | None = None
    #: Where the prose lives, never the prose itself.
    reply_ref: str | None = None
    #: Single-use authority to redeem :attr:`reply_ref`.
    action_token: str = ""
    #: ASL's own freshness proof, handed straight to the action it gates.
    bridge_token: str = ""

    @property
    def bears_body(self) -> bool:
        """Whether carrying this out needs prose fetched out of band."""
        return self.method in BODY_BEARING_METHODS

    @property
    def command_id(self) -> str:
        """What marks the durable row as having come from the far end.

        The operation id doubles as the command id: one identifier the service
        chose, recorded on the row, so a restart can tell a mutation this lane
        admitted from one the local surface is still carrying out.
        """
        return self.operation_id

    def target(self) -> dict[str, Any]:
        """The submission's target, as the transport's own nested object."""
        if self.archive_id is not None:
            return {"archive_id": self.archive_id}
        if self.archive is not None:
            return {"archive": self.archive}
        target: dict[str, Any] = {"plan_key": self.plan_key}
        for name in _TARGET_FIELDS:
            value = getattr(self, name)
            if value is not None:
                target[name] = value
        return target

    def forget(self) -> None:
        """Drop the reference to the body and the authority to fetch it.

        In that order, and both: a reference nobody may redeem is inert, and a
        token with nothing to redeem is a credential left lying around.
        """
        self.reply_ref = None
        self.action_token = ""
        self.bridge_token = ""


@dataclass(frozen=True)
class MutationResult:
    """What ASL actually did, in words the transport already knows.

    :attr:`reason` is the exception: it is ASL's own refusal vocabulary, kept
    for a local caller reassembling a batch, and :func:`operation_payload` never
    reads it. The transport's operation shape has nowhere to put it, and
    inventing a place would put ASL's private words on a wire that cannot
    validate them.
    """

    status: str
    detail_code: str | None = None
    run_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in CONNECTOR_REPORTED_STATUSES:
            raise ValueError(f"a connector may not report status: {self.status!r}")
        if self.detail_code is None:
            return
        if self.detail_code not in RECEIPT_DETAIL_CODES:
            raise ValueError(f"unknown receipt detail code: {self.detail_code!r}")


@dataclass(frozen=True)
class Settlement:
    """How a restart closed out one durable row it found still open."""

    operation_id: str
    status: str
    detail_code: str
    method: str | None = None


def reportable_status(status: str) -> str:
    """Map a journal status onto one a connector is allowed to report.

    Two of ASL's statuses never go out: ``pending`` is a row that has been
    journalled and not yet dispatched, which from the far end is
    indistinguishable from not having been submitted at all, and ``cancelled``
    is an owner's withdrawal rather than an outcome. Both are reported as the
    nearest true thing -- what was reached, or a refusal -- so the service never
    sees a word its own state machine cannot accept.
    """
    if status in CONNECTOR_REPORTED_STATUSES:
        return status
    if status == OPERATION_PENDING:
        return OPERATION_DISPATCHED
    return OPERATION_REJECTED


def _optional_identifier(payload: Mapping[str, Any], name: str) -> str | None:
    """Read an optional identifier field, or refuse a malformed one."""
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, f"{name} must be a non-empty string"
        )
    if len(value) > MAX_IDENTIFIER_CHARS:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD,
            f"{name} exceeds {MAX_IDENTIFIER_CHARS} characters",
        )
    return value


def parse_submission(
    payload: Mapping[str, Any],
    *,
    now: float,
    deadline: float | None = None,
    window: float = MAX_DELIVERY_WINDOW_SECONDS,
) -> MutationRequest:
    """Read one ``operation.submit`` into a command this lane can hold.

    The envelope has already been validated for shape by the transport; what is
    decided here is whether *this* connector will carry *this* method, with the
    authority attached to it, inside a window it is willing to honour.
    """
    if payload.get("request") != REQUEST_MUTATE:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, "this lane carries operation.submit only"
        )

    raw_method = payload.get("method")
    if not isinstance(raw_method, str):
        raise RemoteActionError(ERROR_FORBIDDEN_METHOD, "a submission needs a method")
    try:
        method = require_method(raw_method)
    except OperationError as error:
        raise RemoteActionError(ERROR_FORBIDDEN_METHOD, str(error)) from error
    if method not in DELIVERABLE_METHODS:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, f"this connector does not deliver {method}"
        )

    raw_id = payload.get("operation_id")
    if not isinstance(raw_id, str):
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, "a submission needs an operation_id"
        )
    try:
        operation_id = require_operation_id(raw_id)
    except OperationError as error:
        raise RemoteActionError(ERROR_FORBIDDEN_METHOD, str(error)) from error

    target = payload.get("target")
    if not isinstance(target, Mapping):
        raise RemoteActionError(ERROR_WRONG_TARGET, "a submission needs a target")
    archive_id = _optional_identifier(target, "archive_id")
    archive = _optional_identifier(target, "archive")
    plan_key = _optional_identifier(target, "plan_key")
    if method in (METHOD_ARCHIVE_RESTORE, METHOD_ARCHIVE_DELETE):
        if archive_id is None or plan_key is not None or archive is not None:
            raise RemoteActionError(
                ERROR_WRONG_TARGET, "archive action needs one archive_id"
            )
        plan_key = "archive:" + archive_id
    elif method == METHOD_ARCHIVE_SETTINGS:
        if archive != "settings" or plan_key is not None or archive_id is not None:
            raise RemoteActionError(
                ERROR_WRONG_TARGET, "archive settings needs archive=settings"
            )
        plan_key = "archive:settings"
    elif plan_key is None or archive_id is not None or archive is not None:
        raise RemoteActionError(ERROR_WRONG_TARGET, "plan action needs one plan_key")
    retention_days = payload.get("archive_retention_days")
    if method == METHOD_ARCHIVE_SETTINGS:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise RemoteActionError(
                ERROR_FORBIDDEN_METHOD, "archive_retention_days must be positive"
            )
    elif retention_days is not None:
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD, "retention is only for archive settings"
        )

    action_token = _optional_identifier(payload, "action_token") or ""
    reply_ref = _optional_identifier(payload, "reply_ref")
    bridge_token = _optional_identifier(payload, "bridge_token") or ""
    harness = _optional_identifier(payload, "harness")

    # A method that needs prose and arrived without a way to fetch it would be
    # carried out against an empty reply, which is not the mutation that was
    # submitted. Refuse it while the service can still resubmit.
    if method in BODY_BEARING_METHODS and (not reply_ref or not action_token):
        raise RemoteActionError(
            ERROR_FORBIDDEN_METHOD,
            f"{method} needs a reply_ref and the token that redeems it",
        )
    # Acting on a live run without the proof ASL handed out with the state the
    # caller read is acting on a guess about what that run is doing now.
    if method in FRESHNESS_PROVEN_METHODS and not bridge_token:
        raise RemoteActionError(
            ERROR_UNAUTHORIZED, f"{method} needs the freshness token ASL issued"
        )

    if window <= 0:
        raise RemoteActionError(
            ERROR_DEADLINE, "this connector holds no command", outcome=RECEIPT_EXPIRED
        )
    deliver_by = now + min(window, MAX_DELIVERY_WINDOW_SECONDS)
    if deadline is not None:
        deliver_by = min(deliver_by, deadline)
    if deliver_by <= now:
        raise RemoteActionError(
            ERROR_DEADLINE,
            "the delivery window had already closed on arrival",
            outcome=RECEIPT_EXPIRED,
        )

    reason = payload.get("reason")
    return MutationRequest(
        operation_id=operation_id,
        method=method,
        plan_key=plan_key,
        requested_at=now,
        deliver_by=deliver_by,
        run_key=_optional_identifier(target, "run_key"),
        run_id=_optional_identifier(target, "run_id"),
        question_id=_optional_identifier(target, "question_id"),
        session_id=_optional_identifier(target, "session_id"),
        harness=harness,
        reason=reason if isinstance(reason, str) and reason.strip() else None,
        archive_id=archive_id,
        archive=archive,
        archive_retention_days=retention_days,
        reply_ref=reply_ref,
        action_token=action_token,
        bridge_token=bridge_token,
    )


def operation_payload(
    request: MutationRequest,
    result: MutationResult,
    *,
    observed_at: float,
    dispatched_at: float | None = None,
    settled_at: float | None = None,
) -> dict[str, Any]:
    """The ``operation`` frame body reporting what became of one command."""
    payload: dict[str, Any] = {
        "operation_id": request.operation_id,
        "status": reportable_status(result.status),
        "observed_at": observed_at,
        "method": request.method,
        "target": request.target(),
        "deliver_by": request.deliver_by,
    }
    if result.detail_code is not None:
        payload["detail_code"] = result.detail_code
    if dispatched_at is not None:
        payload["dispatched_at"] = dispatched_at
    if settled_at is not None:
        payload["settled_at"] = settled_at
    return payload


def settlement_payload(
    settlement: Settlement,
    *,
    observed_at: float,
    target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``operation`` frame body for a row a restart closed out."""
    payload: dict[str, Any] = {
        "operation_id": settlement.operation_id,
        "status": reportable_status(settlement.status),
        "observed_at": observed_at,
        "detail_code": settlement.detail_code,
    }
    if settlement.method is not None:
        payload["method"] = settlement.method
    if target is not None:
        payload["target"] = dict(target)
    return payload


def restart_settlement(record: Mapping[str, Any], *, now: float) -> Settlement | None:
    """Decide what a connector restart owes one durable row it found open.

    Two words, and the distinction between them is the whole point. A row still
    ``pending`` was written before anything was attempted -- the journal exists
    precisely so this case is knowable -- so it certainly did not run and is
    ``expired``. A row that reached ``dispatched`` or beyond may have run and
    the process that would have said so is gone, so it is ``unconfirmed``:
    nobody can say, and claiming otherwise in either direction is worse than
    admitting it. ``None`` means the row already says everything true about it.
    """
    status = record.get("status")
    operation_id = record.get("operation_id")
    if not isinstance(status, str) or not isinstance(operation_id, str):
        return None
    method = record.get("method")
    if status == OPERATION_PENDING:
        return Settlement(
            operation_id=operation_id,
            status=OPERATION_EXPIRED,
            detail_code=DETAIL_CONNECTOR_RESTARTED,
            method=method if isinstance(method, str) else None,
        )
    # ``unconfirmed`` is already the admission this would make, and the state
    # machine refuses it as a transition onto itself.
    if status in {OPERATION_DISPATCHED, OPERATION_ACCEPTED}:
        return Settlement(
            operation_id=operation_id,
            status=OPERATION_UNCONFIRMED,
            detail_code=DETAIL_CONNECTOR_RESTARTED,
            method=method if isinstance(method, str) else None,
        )
    return None


@dataclass
class _Held:
    """One command the queue is holding, and whether it has been handed out."""

    request: MutationRequest
    accepted_at: float
    attempted_at: float | None = None

    @property
    def attempted(self) -> bool:
        return self.attempted_at is not None


@dataclass
class MutationQueue:
    """The commands this connector is holding, in the order they arrived.

    Driven by the delivery thread alone and therefore unlocked, exactly like the
    observation cycle it shares a step with. Everything that would need a lock
    -- performing the mutation, redeeming a body -- happens off this object, on
    a request the queue has already handed out.
    """

    limit: int = MAX_PENDING_MUTATIONS
    _held: list[_Held] = field(default_factory=list)

    @property
    def pending(self) -> int:
        """How many commands are waiting to be attempted."""
        return sum(1 for held in self._held if not held.attempted)

    @property
    def attempting(self) -> int:
        """How many commands have been handed out and not yet settled."""
        return sum(1 for held in self._held if held.attempted)

    def holds(self, operation_id: str) -> bool:
        """Whether this operation is already in hand.

        A resubmission of one that is answers itself: the operation frame this
        lane owes will name the status it actually reached, and a second copy
        would only mean carrying the same mutation out twice.
        """
        return any(held.request.operation_id == operation_id for held in self._held)

    def accept(self, request: MutationRequest, *, now: float) -> bool:
        """Take a parsed command, or refuse it while the service can react.

        ``False`` means the command was already in hand and this copy was
        dropped -- deliberately without a receipt, because the answer the
        service is waiting for is the operation's outcome and it is coming.
        """
        if self.holds(request.operation_id):
            request.forget()
            return False
        if len(self._held) >= self.limit:
            request.forget()
            raise RemoteActionError(
                DETAIL_BRIDGE_UNAVAILABLE,
                f"this connector is holding {self.limit} commands already",
            )
        if request.deliver_by <= now:
            request.forget()
            raise RemoteActionError(
                DETAIL_DEADLINE_PASSED,
                "the delivery window closed before the command was queued",
                outcome=RECEIPT_EXPIRED,
            )
        self._held.append(_Held(request=request, accepted_at=now))
        return True

    def next_attempt(self, now: float) -> MutationRequest | None:
        """Hand out the oldest command that has not been attempted.

        Marking it on the way out is what makes an attempt happen at most once:
        nothing this method has returned is ever returned again, cancellable, or
        expirable, whatever happens to the process that is carrying it out.
        """
        for held in self._held:
            if not held.attempted:
                held.attempted_at = now
                return held.request
        return None

    def cancel(self, operation_id: str) -> MutationRequest | None:
        """Withdraw a command that is still waiting.

        Returns the command when it was dropped and ``None`` when there was
        nothing to drop -- including when it is already being carried out, which
        is not cancellable and must not be reported as though it were.
        """
        for index, held in enumerate(self._held):
            if held.request.operation_id != operation_id:
                continue
            if held.attempted:
                return None
            del self._held[index]
            held.request.forget()
            return held.request
        return None

    def expire(self, now: float) -> list[MutationRequest]:
        """Drop every waiting command whose window has closed.

        Only waiting ones: a command being carried out is past the point where
        ``expired`` -- which means *it did not run* -- could still be true.
        """
        return self._drop(
            lambda held: not held.attempted and now >= held.request.deliver_by
        )

    def abandon(self) -> list[MutationRequest]:
        """Drop every waiting command, whatever its window still says.

        What a lost link owes: nothing waiting here can be carried out and its
        window is running out against a service that cannot be told, so the
        command is dropped -- tokens and body reference with it -- and the far
        end resubmits if it still wants the action. A command already being
        carried out is untouched, because it is happening.
        """
        return self._drop(lambda held: not held.attempted)

    def settle(self, operation_id: str) -> MutationRequest | None:
        """Drop a command whose outcome is now known."""
        for index, held in enumerate(self._held):
            if held.request.operation_id == operation_id:
                del self._held[index]
                held.request.forget()
                return held.request
        return None

    def clear(self) -> list[MutationRequest]:
        """Drop everything, forgetting every body reference on the way out."""
        held, self._held = self._held, []
        for entry in held:
            entry.request.forget()
        return [entry.request for entry in held]

    def waiting(self) -> Sequence[MutationRequest]:
        """The commands that have not been attempted, oldest first."""
        return [held.request for held in self._held if not held.attempted]

    def _drop(self, matches: Callable[[_Held], bool]) -> list[MutationRequest]:
        """Remove the commands a rule selects, forgetting each on the way out."""
        removed = [held for held in self._held if matches(held)]
        for held in removed:
            self._held.remove(held)
            held.request.forget()
        return [held.request for held in removed]
