"""The dispatch and reconciliation orchestration flow."""

from __future__ import annotations

import time
from pathlib import Path

from . import (
    intervention,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
)
from .overseer_dispatch_checks import _examine, _locate, _permit
from .overseer_dispatch_execute import _perform, _refusal, _settle_refusal
from .overseer_dispatch_models import (
    ACTION_ACCESS,
    PAYLOAD_TYPES,
    Action,
    Authorize,
    BuildInventory,
    Dispatch,
    DispatchError,
    InspectRun,
    Now,
    Reconciliation,
    Registry,
    _Refused,
)


def dispatch(
    *,
    action: Action,
    watcher_id: str,
    ledger: overseer_store_ledger.Ledger,
    adapters: Registry,
    authorize: Authorize = overseer_policy_client.authorize,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    inspect_run: InspectRun = intervention.inspect_run,
    state_dir: str | Path | None = None,
    timeout: float = overseer_policy_client.DEFAULT_TIMEOUT_SECONDS,
    now: Now = time.time,
) -> Dispatch:
    """Perform one action, or refuse it and say which guard said no.

    The order of the guards is the order of the questions, and the pivot in the
    middle is AgentMux. First, from identity metadata alone: what is this, is
    the plan there, is it the same run, is it the same scope, is it ours. Then
    AgentMux is asked about that scope. Only under its answer is the run itself
    opened -- is our picture current, have we already burned our recoveries, is
    the incident real, is the question open, have we already done this -- and
    only then is anything in doubt reconciled and an adapter allowed to prepare.
    Then the metadata and the authorization are established a second time and
    required to match *before* the content is read again, because everything
    above was true before a wait.

    Nothing here rolls anything back. An action that executed stays executed
    and an action left in doubt stays in doubt until an adapter says otherwise;
    see :func:`reconcile`.
    """
    if not isinstance(action.payload, PAYLOAD_TYPES):
        return _refusal(
            action,
            "operation_unsupported",
            f"{type(action.payload).__name__} is not an overseer action payload; "
            f"an action carries one of {', '.join(t.__name__ for t in PAYLOAD_TYPES)}",
        )
    adapter = adapters.adapter(action.operation)
    if adapter is None:
        return _refusal(
            action,
            "operation_unsupported",
            f"no adapter is registered for {action.operation!r}",
        )

    located = _locate(
        action=action,
        watcher_id=watcher_id,
        ledger=ledger,
        build_inventory=build_inventory,
        state_dir=state_dir,
        now=now,
    )
    if isinstance(located, _Refused):
        return _settle_refusal(action, located, ledger)

    grant = _permit(
        action=action,
        repositories=located.repositories,
        ledger=ledger,
        authorize=authorize,
        timeout=timeout,
    )
    if isinstance(grant, _Refused):
        return _settle_refusal(action, grant, ledger)

    identity = _examine(
        action=action,
        located=located,
        ledger=ledger,
        inspect_run=inspect_run,
        state_dir=state_dir,
    )
    if isinstance(identity, _Refused):
        return _settle_refusal(action, identity, ledger)

    outstanding = reconcile(
        plan_dir=action.plan_dir, ledger=ledger, adapters=adapters, grant=grant
    )
    unsettled = tuple(item for item in outstanding if not item.settled)
    if unsettled:
        return _refusal(
            action,
            "outcome_uncertain",
            f"{len(unsettled)} dispatch(es) on this plan are in doubt and could "
            f"not be reconciled: {'; '.join(item.detail for item in unsettled)}",
        )

    preparation = adapter.prepare(action)
    if not preparation.ready:
        return _refusal(action, "preparation_declined", preparation.detail)

    relocated = _locate(
        action=action,
        watcher_id=watcher_id,
        ledger=ledger,
        build_inventory=build_inventory,
        state_dir=state_dir,
        now=now,
    )
    if isinstance(relocated, _Refused):
        return _settle_refusal(action, relocated, ledger)

    regrant = _permit(
        action=action,
        repositories=relocated.repositories,
        ledger=ledger,
        authorize=authorize,
        timeout=timeout,
    )
    if isinstance(regrant, _Refused):
        return _settle_refusal(action, regrant, ledger)
    if (
        regrant.request_digest != grant.request_digest
        or regrant.config_fingerprint != grant.config_fingerprint
    ):
        return _refusal(
            action,
            "policy_superseded",
            "authorization changed while the action was being prepared",
        )

    rechecked = _examine(
        action=action,
        located=relocated,
        ledger=ledger,
        inspect_run=inspect_run,
        state_dir=state_dir,
    )
    if isinstance(rechecked, _Refused):
        return _settle_refusal(action, rechecked, ledger)
    if rechecked.fingerprint() != identity.fingerprint():
        return _refusal(
            action,
            "stale_action",
            "the run changed while the action was being prepared",
        )

    return _perform(
        action=action,
        identity=rechecked,
        grant=regrant,
        adapter=adapter,
        ledger=ledger,
    )


def reconcile(
    *,
    plan_dir: str,
    ledger: overseer_store_ledger.Ledger,
    adapters: Registry,
    grant: overseer_policy_client.Grant,
) -> tuple[Reconciliation, ...]:
    """Ask the adapters what became of every dispatch left in doubt.

    Called before any retry, and safe to call on its own: it settles rows from
    receipts an adapter can produce now, and leaves alone every row it cannot
    settle. An in-doubt row is never rolled forward into ``executed`` on the
    grounds that it probably worked, and never rolled back on the grounds that
    it probably did not. Both are guesses, and the shared requirements say an
    already-dispatched action is not undone.

    ``grant`` is required and is checked, not merely accepted. Answering "did
    this happen?" means opening run state, mailboxes and Git history, so
    reconciliation is content access however little it writes -- and a caller
    that has not just asked AgentMux about this plan under
    :data:`ACTION_ACCESS` has nothing to pass. That is why it is a
    :class:`~agent_session_loop.overseer_policy_client.Grant` and not a
    boolean: a grant is only ever built from a live allowed answer, so it
    cannot be assembled by a caller who would like to proceed.
    """
    if grant.access != ACTION_ACCESS:
        raise DispatchError(
            f"reconciliation runs under {ACTION_ACCESS!r} permission; this grant "
            f"is for {grant.access!r}"
        )
    if grant.plan_dir != plan_dir:
        raise DispatchError(f"this grant permits {grant.plan_dir!r}, not {plan_dir!r}")
    results: list[Reconciliation] = []
    for record in ledger.in_doubt_actions(plan_dir=plan_dir):
        adapter = adapters.adapter(record.operation)
        if adapter is None:
            results.append(
                Reconciliation(
                    record=record,
                    receipt=None,
                    settled=False,
                    detail=(
                        f"action {record.action_id} was attempted as "
                        f"{record.operation!r}, which no registered adapter can "
                        "account for"
                    ),
                )
            )
            continue
        try:
            receipt = adapter.reconcile(record)
        except Exception as error:  # noqa: BLE001 - see below
            # An adapter that raises has said nothing about the world. Reporting
            # that as a failure would be inventing a receipt out of a stack
            # trace, so the row stays in doubt and the plan stays blocked.
            results.append(
                Reconciliation(
                    record=record,
                    receipt=None,
                    settled=False,
                    detail=(
                        f"reconciling action {record.action_id} raised "
                        f"{type(error).__name__}: {error}"
                    ),
                )
            )
            continue
        if receipt.outcome == "uncertain":
            results.append(
                Reconciliation(
                    record=record,
                    receipt=receipt,
                    settled=False,
                    detail=receipt.detail
                    or f"action {record.action_id} remains in doubt",
                )
            )
            continue
        settled = ledger.confirm_action(
            action_id=record.action_id,
            outcome=receipt.outcome,
            detail=receipt.detail,
            locator=receipt.locator,
            status="executed" if receipt.outcome == "executed" else "failed",
        )
        results.append(
            Reconciliation(
                record=settled,
                receipt=receipt,
                settled=True,
                detail=receipt.detail,
            )
        )
    return tuple(results)
