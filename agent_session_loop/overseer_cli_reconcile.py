"""Overseer cli reconcile."""

from __future__ import annotations

import argparse

from . import (
    overseer_cli_actions as actions,
)
from . import (
    overseer_cli_claims as claims,
)
from . import (
    overseer_cli_common as common,
)
from . import (
    overseer_cli_payloads as payloads,
)
from . import (
    overseer_decisions,
    overseer_dispatch,
    overseer_policy_client,
    responses,
)


def reconcile_from_args(
    args: argparse.Namespace,
    *,
    seams: common.Seams = common.DEFAULT_SEAMS,
) -> int:
    """Settle rows a previous dispatch left in doubt.

    A dispatch that dies between performing an action and recording its receipt
    leaves a row saying "maybe". Only the adapter that would have performed it
    can answer, and it answers from the ledger row alone, because the process
    that held the action is gone. Running this before acting again is what keeps
    a retry from becoming a duplicate.

    Answering that question means reading the run: state files, mailboxes, Git
    history. So this leaf asks AgentMux first, exactly as ``act`` does and under
    the same access category, and a refused or unreachable answer ends the
    invocation before an adapter is built. Reconciliation is the one operation
    whose whole job is to look at what a previous process touched, which makes
    it the easiest place for a withdrawn opt-in to be read straight past.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = common._state_dir(args)
    try:
        ledger = seams.open_ledger(state_dir=state_dir)
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = claims._resolve_candidate(inventory, args.plan_dir)
    except common.OverseerCliError as error:
        return common._failure("overseer-reconcile", error, as_json=as_json)

    decision, grant = claims._grant_for(
        candidate,
        access=overseer_policy_client.ACCESS_ACTION,
        seams=seams,
        timeout=float(args.timeout),
    )
    ledger.record_decision(decision)
    if decision.config_fingerprint:
        ledger.note_policy(config_fingerprint=decision.config_fingerprint)
    if grant is None:
        refusal = "unauthorized" if decision.available else "policy_unavailable"
        detail = decision.summary or decision.reason
        payload = responses.error_payload(
            "overseer-reconcile",
            reason=refusal,
            error=detail,
            plan_dir=candidate.plan_dir,
            authorized=False,
            settled=False,
            outcomes=[],
        )
        common._emit(
            payload,
            [f"reconcile on {candidate.plan_dir}: refused ({refusal})", f"  {detail}"],
            as_json=as_json,
        )
        return common.EXIT_REFUSED

    def authority(
        action: overseer_dispatch.Action,
    ) -> overseer_decisions.Ruling | None:
        return None

    outcomes = overseer_dispatch.reconcile(
        plan_dir=common.plan_dir_of(candidate),
        ledger=ledger,
        adapters=actions._adapters(
            authority=authority,
            ledger=ledger,
            state_dir=state_dir,
            seams=seams,
            timeout=float(args.timeout),
        ),
        grant=grant,
    )
    settled = all(item.settled for item in outcomes)
    payload = responses.success_payload(
        "overseer-reconcile",
        plan_dir=candidate.plan_dir,
        authorized=True,
        settled=settled,
        outcomes=[
            {
                "record": payloads._record_payload(item.record),
                "receipt": payloads._receipt_payload(item.receipt),
                "settled": item.settled,
                "detail": item.detail,
            }
            for item in outcomes
        ],
    )
    lines = [f"{len(outcomes)} in-doubt action(s) on {candidate.plan_dir}"]
    for item in outcomes:
        lines.append(
            f"  {item.record.operation} #{item.record.action_id}: "
            f"{'settled' if item.settled else 'still in doubt'} {item.detail}".rstrip()
        )
    common._emit(payload, lines, as_json=as_json)
    return common.EXIT_OK if settled else common.EXIT_REFUSED
