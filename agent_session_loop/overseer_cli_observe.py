"""Overseer cli observe."""

from __future__ import annotations

import argparse
from datetime import timedelta
from typing import Any

from . import (
    intervention,
    overseer_context,
    overseer_policy_client,
    overseer_watch,
    responses,
)
from . import (
    overseer_cli_claims as claims,
)
from . import (
    overseer_cli_common as common_module,
)
from . import (
    overseer_cli_payloads as payloads,
)


def scan_from_args(
    args: argparse.Namespace,
    *,
    seams: common_module.Seams = common_module.DEFAULT_SEAMS,
) -> int:
    """List every run discovery can see, with why each one is or is not in play.

    Discovery only. Nothing here reads a run's content, so nothing here asks
    AgentMux: the eligibility a candidate reports is ASL's own, and a plan that
    looks eligible may still be refused the moment ``watch`` asks about it.
    """
    as_json = bool(getattr(args, "json", False))
    try:
        state_dir = common_module._state_dir(args)
        window = common_module._pause_window_seconds(args)
        selection = common_module._selection(args)
        inventory = seams.build_inventory(state_dir=state_dir)
    except common_module.OverseerCliError as error:
        return common_module._failure("overseer-scan", error, as_json=as_json)
    matching = [
        candidate for candidate in inventory.candidates if selection.selects(candidate)
    ]
    payload = responses.success_payload(
        "overseer-scan",
        state_root=str(inventory.state_root),
        complete=inventory.complete,
        warnings=list(inventory.warnings),
        considered=len(matching),
        eligible=sum(1 for candidate in matching if candidate.eligible),
        candidates=[
            payloads._candidate_payload(candidate, window_seconds=window)
            for candidate in matching
        ],
    )
    lines = [
        f"state root: {inventory.state_root}",
        f"candidates: {len(matching)} "
        f"({sum(1 for c in matching if c.eligible)} eligible)"
        f"{'' if inventory.complete else ' [partial view]'}",
    ]
    for candidate in matching:
        mark = "+" if candidate.eligible else "-"
        detail: str = candidate.lifecycle
        if candidate.ineligible_reasons:
            detail = f"{detail}: {', '.join(candidate.ineligible_reasons)}"
        lines.append(f"  {mark} {candidate.plan_dir} [{detail}]")
    for warning in inventory.warnings:
        lines.append(f"  ! {warning}")
    common_module._emit(payload, lines, as_json=as_json)
    return common_module.EXIT_OK if inventory.complete else common_module.EXIT_REFUSED


def watch_from_args(
    args: argparse.Namespace,
    *,
    seams: common_module.Seams = common_module.DEFAULT_SEAMS,
) -> int:
    """Enrol the runs AgentMux authorizes and hold a lease on each of them.

    One sweep by default. ``--for-seconds`` keeps sweeping until the deadline,
    which is what "watch while invoked" means: newly started runs and eligible
    pauses join on a later sweep without the operator re-invoking anything.
    ``--release`` gives the leases back, which is what ending an invocation
    means -- an abandoned lease expires on its own, but only after it has kept
    another watcher out for the rest of its term.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = common_module._state_dir(args)
    ledger = seams.open_ledger(state_dir=state_dir)
    watcher = overseer_watch.Watcher.here(args.watcher_id)
    if getattr(args, "release", False):
        released = overseer_watch.release(watcher=watcher, ledger=ledger)
        payload = responses.success_payload(
            "overseer-watch",
            watcher_id=watcher.watcher_id,
            released=released,
            sweeps=[],
        )
        common_module._emit(
            payload,
            [f"released {released} lease(s) held by {watcher.watcher_id}"],
            as_json=as_json,
        )
        return common_module.EXIT_OK
    common: dict[str, Any] = {
        "watcher": watcher,
        "ledger": ledger,
        "selection": common_module._selection(args),
        "state_dir": state_dir,
        "now": seams.utc_now,
        "pause_window_seconds": common_module._pause_window_seconds(args),
        "lease_seconds": float(args.lease_seconds),
        "authorize": seams.authorize,
        "authorize_timeout": float(args.timeout),
        "build_inventory": seams.build_inventory,
    }
    if args.for_seconds:
        until = seams.utc_now() + timedelta(seconds=float(args.for_seconds))
        invocation = overseer_watch.watch(
            until=until,
            sleep=seams.sleep,
            sweep_seconds=float(args.sweep_seconds),
            interval_seconds=float(args.interval_seconds),
            **common,
        )
        sweeps = list(invocation.sweeps)
    else:
        sweeps = [overseer_watch.sweep(**common)]
    complete = all(item.complete for item in sweeps) and bool(sweeps)
    payload = responses.success_payload(
        "overseer-watch",
        watcher_id=watcher.watcher_id,
        released=0,
        complete=complete,
        sweeps=[payloads._sweep_payload(item) for item in sweeps],
    )
    lines = [f"watcher {watcher.watcher_id}: {len(sweeps)} sweep(s)"]
    for item in sweeps:
        lines.append(
            f"  enrolled {len(item.enrolled)}, excluded {len(item.excluded)}, "
            f"considered {item.considered}"
            f"{'' if item.complete else ' [partial]'}"
        )
        for enrollment in item.enrolled:
            lines.append(f"    + {enrollment.plan_dir} ({enrollment.lifecycle})")
        for excluded in item.excluded:
            lines.append(f"    - {excluded.plan_dir} [{excluded.reason}]")
    common_module._emit(payload, lines, as_json=as_json)
    return common_module.EXIT_OK if complete else common_module.EXIT_REFUSED


def inspect_from_args(
    args: argparse.Namespace,
    *,
    seams: common_module.Seams = common_module.DEFAULT_SEAMS,
) -> int:
    """Open an incident and capture a bounded packet of a run's context.

    The packet is a summary, not a transcript. ``--expand`` widens exactly one
    source at a time under a byte and record ceiling, so context grows because
    the host asked for a named thing and not because a run happened to be
    verbose. Every expansion re-checks the grant, so a revocation between the
    packet and the expansion stops the expansion.

    The ``scope`` and ``token`` this prints are what ``asl overseer act`` must
    be given later. They are captured here, at the moment the context was read,
    precisely so a run that moves between reading and acting produces a
    ``stale_action`` refusal instead of an action aimed at a state that is gone.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = common_module._state_dir(args)
    try:
        ledger = seams.open_ledger(state_dir=state_dir)
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = claims._resolve_candidate(inventory, args.plan_dir)
        decision, grant = claims._grant_for(
            candidate,
            access=overseer_policy_client.ACCESS_CONTEXT,
            seams=seams,
            timeout=float(args.timeout),
        )
        if args.incident_id is not None:
            packet = overseer_context.load_packet(
                grant=grant,
                ledger=ledger,
                incident_id=int(args.incident_id),
                state_dir=state_dir,
                now=seams.utc_iso_now,
                read_mailbox=seams.read_mailbox,
                build_inventory=seams.build_inventory,
            )
        else:
            packet = overseer_context.build_packet(
                grant=grant,
                ledger=ledger,
                candidate=candidate,
                kind=args.kind,
                summary=args.summary,
                state_dir=state_dir,
                now=seams.utc_iso_now,
                read_mailbox=seams.read_mailbox,
            )
        excerpts = [
            overseer_context.expand(
                grant=grant,
                ledger=ledger,
                incident_id=packet.incident_id,
                kind=kind,
                state_dir=state_dir,
                read_mailbox=seams.read_mailbox,
                limit=int(args.limit),
            )
            for kind in (args.expand or ())
        ]
    except common_module.OverseerCliError as error:
        return common_module._failure("overseer-inspect", error, as_json=as_json)
    revalidation = overseer_context.revalidate(packet=packet, candidate=candidate)
    facts = seams.inspect_run(candidate.plan_dir, state_dir=state_dir)
    fresh = revalidation.fresh and packet.refusal is None
    payload = responses.success_payload(
        "overseer-inspect",
        authorized=decision.allowed,
        authorization=decision.summary,
        packet=payloads._packet_payload(packet),
        excerpts=[payloads._excerpt_payload(item) for item in excerpts],
        fresh=fresh,
        revalidation={
            "fresh": revalidation.fresh,
            "reason": revalidation.reason,
            "detail": revalidation.detail,
        },
        token=intervention.action_token(facts),
        scope=overseer_context.scope_digest(candidate),
        run_id=candidate.run_id,
        expansion_limits={
            "chars": overseer_context.MAX_EXCERPT_CHARS,
            "records": overseer_context.MAX_EXCERPT_RECORDS,
        },
    )
    lines = [
        f"incident {packet.incident_id} on {packet.plan_dir} ({packet.kind})",
        f"  summary: {packet.summary}",
        f"  sources: {len(packet.sources)}, gaps: {len(packet.gaps)}, "
        f"withheld: {packet.withheld}",
        f"  scope: {payload['scope']}",
        f"  token: {payload['token']}",
    ]
    if packet.refusal:
        lines.append(f"  refused: {packet.refusal}")
    if not revalidation.fresh:
        lines.append(f"  stale: {revalidation.reason} {revalidation.detail}".rstrip())
    for excerpt in excerpts:
        lines.append(
            f"  expanded {excerpt.kind}: {excerpt.size} chars"
            f"{' (truncated)' if excerpt.truncated else ''}"
            f"{'' if excerpt.refusal is None else f' refused {excerpt.refusal}'}"
        )
    common_module._emit(payload, lines, as_json=as_json)
    return common_module.EXIT_OK if fresh else common_module.EXIT_REFUSED
