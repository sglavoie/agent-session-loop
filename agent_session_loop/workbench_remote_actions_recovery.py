"""Restart recovery and reconciliation of delivered mutations.

Split out of :mod:`agent_session_loop.workbench_remote_actions`; the performer's
``recover`` and ``reconcile`` methods delegate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from asl_workbench import operations as portable_operations
from asl_workbench.details import DetailError
from asl_workbench.remote_actions import (
    MutationRequest,
    MutationResult,
    Settlement,
    reportable_status,
    restart_settlement,
    settlement_payload,
)

from . import workbench_archive, workbench_operations
from .workbench_remote_actions_journal import _detail_of

if TYPE_CHECKING:
    from .workbench_remote_actions import AslMutationPerformer


def recover(performer: AslMutationPerformer, *, now: float) -> list[dict[str, Any]]:
    """Settle and report what a previous connector left open.

    Two filters, and dropping either one would make this dangerous. A row
    without a ``command_id`` was submitted by a local surface, and settling
    it would have the connector answer for a browser's launch. A row whose
    owner process is still alive is a mutation that is *happening*, and
    settling it would report an outcome for something in flight.

    What survives both is a row this connector's predecessor admitted and
    nobody is carrying out. Durable evidence is reconciled first, so a
    consumed mailbox reply remains consumed; only an otherwise unresolved
    row is closed with the word its recorded status earns: ``expired`` when
    it never reached dispatch, ``unconfirmed`` when it did. Never
    re-attempted.
    """
    state_dir = performer.state_dir
    settled: list[dict[str, Any]] = []
    subjects = [
        *performer._index.items(now=now),
        (None, workbench_archive.archive_root(state_dir)),
    ]
    for plan_key, plan_dir in subjects:
        try:
            rows = workbench_operations.open_operations(plan_dir, state_dir=state_dir)
        except OSError:
            continue
        eligible = {
            str(record["operation_id"])
            for record in rows
            if record.get("command_id") and not workbench_operations.owner_alive(record)
        }
        if not eligible:
            continue
        try:
            reconciled = (
                workbench_operations.reconcile_plan(
                    plan_dir, state_dir=state_dir, now=now
                )
                if plan_key is not None
                else []
            )
        except OSError:
            reconciled = []
        for record in reconciled:
            operation_id = str(record.get("operation_id") or "")
            if operation_id not in eligible:
                continue
            settled.append(
                settlement_payload(
                    Settlement(
                        operation_id=operation_id,
                        status=str(record["status"]),
                        detail_code=str(
                            record.get("detail_code")
                            or portable_operations.DETAIL_NO_RESULT
                        ),
                        method=str(record.get("method") or "") or None,
                    ),
                    observed_at=now,
                    target={"plan_key": plan_key} if plan_key else None,
                )
            )
            eligible.discard(operation_id)
        if not eligible:
            continue
        try:
            rows = workbench_operations.open_operations(plan_dir, state_dir=state_dir)
        except OSError:
            continue
        for record in rows:
            if str(record.get("operation_id") or "") not in eligible:
                continue
            settlement = restart_settlement(record, now=now)
            if settlement is None:
                continue
            try:
                workbench_operations.settle_operation(
                    plan_dir,
                    settlement.operation_id,
                    settlement.status,
                    detail_code=settlement.detail_code,
                    state_dir=state_dir,
                    now=now,
                )
            except portable_operations.OperationError, OSError:
                # The row moved between the read and the write, or the
                # journal is unreadable. Either way somebody else's answer
                # is now the record, and overwriting it would be worse than
                # saying nothing.
                continue
            settled.append(
                settlement_payload(
                    settlement,
                    observed_at=now,
                    target={"plan_key": plan_key} if plan_key else None,
                )
            )
    return settled


def reconcile(
    performer: AslMutationPerformer, request: MutationRequest, *, now: float
) -> MutationResult | None:
    """Report when ASL's evidence advances one accepted remote mutation.

    The first result says only that a live reply was appended.  The runner
    later records that it consumed those words.  Reconcile the local
    operation journal against that record and return a second result only
    when the operation has reached a terminal state.
    """
    state_dir = performer.state_dir
    try:
        plan_dir = performer._index.resolve(request.plan_key, now=now)
    except DetailError:
        return None
    try:
        workbench_operations.reconcile_plan(plan_dir, state_dir=state_dir, now=now)
        record = next(
            (
                row
                for row in workbench_operations.read_operations(
                    plan_dir, state_dir=state_dir
                )
                if row.get("operation_id") == request.operation_id
            ),
            None,
        )
    except OSError, portable_operations.OperationError:
        return None
    if record is None:
        return None
    status = str(record.get("status", ""))
    if status in {
        portable_operations.OPERATION_PENDING,
        portable_operations.OPERATION_DISPATCHED,
        portable_operations.OPERATION_ACCEPTED,
    }:
        return None
    return MutationResult(
        status=reportable_status(status),
        detail_code=_detail_of(record),
        run_id=request.run_id,
    )
