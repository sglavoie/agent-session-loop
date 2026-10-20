"""Vocabulary, errors and value types for the remote mutation lane.

The constants that bound a command's life, the exceptions a refused submission
raises, and the plain records (:class:`MutationRequest`, :class:`MutationResult`,
:class:`Settlement`) the rest of :mod:`asl_workbench.remote_actions` passes
around. Nothing here parses a payload or holds a queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
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
    OPERATION_DISPATCHED,
    OPERATION_PENDING,
    OPERATION_REJECTED,
    REPLY_BEARING_METHODS,
)
from .transport import (
    CONNECTOR_REPORTED_STATUSES,
    RECEIPT_DETAIL_CODES,
    RECEIPT_OUTCOMES,
    RECEIPT_REFUSED,
)

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
