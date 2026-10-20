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

from .remote_actions_models import (
    BODY_BEARING_METHODS,
    DELIVERABLE_METHODS,
    FRESHNESS_PROVEN_METHODS,
    MAX_BODY_CHARS,
    MAX_DELIVERY_WINDOW_SECONDS,
    MAX_PENDING_MUTATIONS,
    BodyUnavailable,
    MutationRequest,
    MutationResult,
    RemoteActionError,
    Settlement,
    reportable_status,
)
from .remote_actions_queue import MutationQueue
from .remote_actions_settlement import (
    operation_payload,
    restart_settlement,
    settlement_payload,
)
from .remote_actions_submission import parse_submission

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
