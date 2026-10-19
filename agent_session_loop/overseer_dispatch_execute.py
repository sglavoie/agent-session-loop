"""Ledger settlement and adapter execution for overseer actions."""

from __future__ import annotations

from . import (
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
)
from .overseer_dispatch_models import (
    RECOVERY_LIMIT,
    Action,
    Adapter,
    Dispatch,
    Identity,
    Refusal,
    _Refused,
)


def _perform(
    *,
    action: Action,
    identity: Identity,
    grant: overseer_policy_client.Grant,
    adapter: Adapter,
    ledger: overseer_store_ledger.Ledger,
) -> Dispatch:
    """Write the attempt, call the adapter, settle what came back."""
    try:
        record = ledger.attempt_action(
            grant=grant,
            plan_dir=action.plan_dir,
            operation=action.operation,
            target=action.target,
            incident_id=action.incident_id,
            progress_token=identity.progress,
        )
    except overseer_store_models.LedgerRefused as error:
        return _refusal(action, "ledger_refused", str(error))

    try:
        receipt = adapter.execute(action)
    except Exception as error:  # noqa: BLE001 - see below
        # The call did not return. That is not the same as the action not
        # happening, so the row stays ``attempted`` and the plan stays blocked
        # until an adapter can say which it was.
        return Dispatch(
            action=action,
            performed=False,
            refusal="outcome_uncertain",
            detail=f"dispatch raised {type(error).__name__}: {error}",
            record=record,
            receipt=None,
            escalated=False,
            recoveries=0,
        )

    if receipt.outcome == "uncertain":
        return Dispatch(
            action=action,
            performed=False,
            refusal="outcome_uncertain",
            detail=receipt.detail or "the adapter could not confirm the dispatch",
            record=record,
            receipt=receipt,
            escalated=False,
            recoveries=0,
        )

    settled = ledger.confirm_action(
        action_id=record.action_id,
        outcome=receipt.outcome,
        detail=receipt.detail,
        locator=receipt.locator,
        status="executed" if receipt.outcome == "executed" else "failed",
    )
    if receipt.outcome == "executed":
        return Dispatch(
            action=action,
            performed=True,
            refusal=None,
            detail=receipt.detail,
            record=settled,
            receipt=receipt,
            escalated=False,
            recoveries=0,
        )

    spent = len(
        ledger.recovery_attempts(
            plan_dir=action.plan_dir, progress_token=identity.progress
        )
    )
    escalated = (
        _escalate(ledger, action.incident_id) if spent >= RECOVERY_LIMIT else False
    )
    return Dispatch(
        action=action,
        performed=False,
        refusal=None,
        detail=receipt.detail or "the adapter reported the dispatch failed",
        record=settled,
        receipt=receipt,
        escalated=escalated,
        recoveries=spent,
    )


def _settle_refusal(
    action: Action, refused: _Refused, ledger: overseer_store_ledger.Ledger
) -> Dispatch:
    """Turn a guard's answer into a result, escalating if it asked for that."""
    escalated = _escalate(ledger, action.incident_id) if refused.escalate else False
    return Dispatch(
        action=action,
        performed=False,
        refusal=refused.refusal,
        detail=refused.detail,
        record=None,
        receipt=None,
        escalated=escalated,
        recoveries=refused.recoveries,
    )


def _refusal(action: Action, refusal: Refusal, detail: str) -> Dispatch:
    """A refusal that escalates nothing and recorded nothing."""
    return Dispatch(
        action=action,
        performed=False,
        refusal=refusal,
        detail=detail,
        record=None,
        receipt=None,
        escalated=False,
        recoveries=0,
    )


def _escalate(ledger: overseer_store_ledger.Ledger, incident_id: int) -> bool:
    """Escalate an open incident, and say whether this call is what did it.

    An incident that is already settled is left alone: escalating it again
    would restamp a settlement somebody else made, and the answer to "is this
    escalated?" is the same either way.
    """
    incident = ledger.incident(incident_id)
    if incident is None or incident.status != "open":
        return False
    ledger.settle_incident(incident_id=incident_id, status="escalated")
    return True
