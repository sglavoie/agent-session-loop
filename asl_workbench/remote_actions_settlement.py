"""The ``operation`` frames reporting a command's fate, and restart settlement.

Composes the outbound payloads for a carried-out command or a row a restart
closed out, and decides which of the two non-interchangeable words a restart
owes a row it found open.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .operations import (
    DETAIL_CONNECTOR_RESTARTED,
    OPERATION_ACCEPTED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_UNCONFIRMED,
)
from .remote_actions_models import (
    MutationRequest,
    MutationResult,
    Settlement,
    reportable_status,
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
