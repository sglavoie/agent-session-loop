"""Overseer cli actions."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from . import (
    overseer_checkpoint_groups,
    overseer_decisions,
    overseer_dispatch,
    overseer_mailbox,
    overseer_policy_client,
    overseer_resume,
    overseer_store_ledger,
    overseer_verify,
    responses,
)
from . import (
    overseer_cli_claims as claims_module,
)
from . import (
    overseer_cli_common as common,
)
from . import (
    overseer_cli_evidence as evidence_module,
)
from . import (
    overseer_cli_payloads as payloads,
)


def _adapters(
    *,
    authority: Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None],
    ledger: overseer_store_ledger.Ledger,
    state_dir: str | None,
    seams: common.Seams,
    timeout: float,
) -> overseer_dispatch.Registry:
    """The registry the dispatcher is handed. Built here, called only there.

    ``commit`` comes from :mod:`overseer_checkpoint_groups` rather than
    :mod:`overseer_checkpoint` because a run may declare more than one writable
    repository, and a crash between the first repository and the second is the
    case the durable group exists for. The group form handles a single
    repository as the degenerate case, so there is no reason to register the
    ungrouped one and every reason not to.

    ``verify`` comes from :mod:`overseer_verify`, which takes the check *name*
    the action carries and looks it up in the completion contract of the session
    the run is on. Nothing here composes a command, and no seam could pass one:
    a caller who wants a different argv has to change a plan file, in a
    repository, under the write rules that govern repositories.
    """
    root = None if state_dir is None else Path(state_dir)
    sources = (
        overseer_mailbox.adapters(authority=authority, state_dir=root),
        overseer_checkpoint_groups.adapters(
            authority=authority,
            ledger=ledger,
            state_dir=root,
            authorize=seams.authorize,
            timeout=timeout,
        ),
        overseer_resume.adapters(
            authority=authority,
            state_dir=root,
            build_inventory=seams.build_inventory,
            inspect_run=seams.inspect_run,
        ),
        overseer_verify.adapters(
            authority=authority,
            state_dir=root,
            build_inventory=seams.build_inventory,
            inspect_run=seams.inspect_run,
        ),
    )
    merged: dict[str, overseer_dispatch.Adapter] = {}
    for source in sources:
        merged.update({str(name): adapter for name, adapter in source.adapters.items()})
    return overseer_dispatch.registry(merged)


def _payload_for(args: argparse.Namespace, operation: str) -> overseer_dispatch.Payload:
    if operation == "reply":
        if not args.question_id or not args.text:
            raise common.OverseerCliError(
                "payload_incomplete", "reply needs --question-id and --text"
            )
        return overseer_dispatch.Reply(question_id=args.question_id, text=args.text)
    if operation == "guidance":
        if not args.text:
            raise common.OverseerCliError("payload_incomplete", "guidance needs --text")
        return overseer_dispatch.Guidance(text=args.text)
    if operation == "verify":
        if not args.check:
            raise common.OverseerCliError("payload_incomplete", "verify needs --check")
        return overseer_dispatch.Verify(check=args.check)
    if operation == "commit":
        if not args.message:
            raise common.OverseerCliError(
                "payload_incomplete", "commit needs --message"
            )
        return overseer_dispatch.Commit(message=args.message)
    return overseer_dispatch.Resume(session_id=args.session_id or "")


def act_from_args(
    args: argparse.Namespace,
    *,
    seams: common.Seams = common.DEFAULT_SEAMS,
) -> int:
    """Hand one action to the guarded dispatcher. The only mutating leaf.

    ``--token`` and ``--scope`` are required and are not recomputed here. They
    come from the ``asl overseer inspect`` that read the context this action is
    a response to; recomputing them would make them agree with themselves and
    turn ``stale_action`` into a guard that can never fire.

    The authority callback re-derives the ruling from live supervision each time
    the dispatcher asks, which it does twice: once before the adapter prepares
    and once immediately before it performs. Re-deriving catches a revocation,
    because a revoked grant is a missing grant. It does not on its own catch a
    *new* statement: the claims cite what the host read, and a hold nobody
    claimed never becomes a decision, so the second reading of the same claims
    agrees with the first however the record moved.

    ``--reviewed-evidence`` is what closes that, and a claim-bearing action
    requires it. It carries the digest ``asl overseer decide --list-statements``
    printed for the record it showed the host, and a mismatch is refused
    ``evidence_changed`` before any decision is weighed -- at the first check,
    which closes the ``decide``-to-``act`` window, and again at the second,
    which closes the one inside the dispatch. Claims arriving without it are
    refused ``evidence_unbound`` at every check rather than resolved against a
    digest read here: pinning what this invocation happens to observe would
    write the host's reading for it, and would silently absorb every hold that
    landed before the invocation into the record the host is then said to have
    reviewed.

    Nothing this leaf prints names the record it observed. ``evidence.observed``
    reports the binding the checks confirmed -- the caller's own digest, or
    nothing when the record moved -- because publishing the digest of a record
    the host never read would hand these same claims the replacement binding
    that makes them pass on the next invocation, which is the refusal undone by
    its own diagnostics.

    ``--raises`` is read once and handed to both rechecks, so the human
    questions this action depends on cannot be answered by the first reading and
    forgotten by the second. What the register itself raises is re-derived each
    time, because a hold claimed against live evidence is a dependency the
    invocation never had to know about.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = common._state_dir(args)
    try:
        ledger = seams.open_ledger(state_dir=state_dir)
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = claims_module._resolve_candidate(inventory, args.plan_dir)
        payload_for_action = _payload_for(args, args.operation)
        incident_id = int(args.incident_id)
        claims = claims_module.parse_claims(
            claims_module._claims_text(args),
            scope=claims_module._scope_for(candidate, incident_id=incident_id),
        )
    except common.OverseerCliError as error:
        return common._failure("overseer-act", error, as_json=as_json)
    timeout = float(args.timeout)
    raises = claims_module._raises(args)
    reviewed = str(getattr(args, "reviewed_evidence", "") or "")
    # What each authority check found, and never what it found it to be. The
    # digest of the record as it stands is the binding these claims are missing,
    # so the only thing that outlives the comparison is whether it held.
    freshness = {"checked": False, "holds": False}
    bound = bool(reviewed) or not claims

    def authority(
        action: overseer_dispatch.Action,
    ) -> overseer_decisions.Ruling | None:
        if not bound:
            # Before the grant and before the read: there is nothing to weigh,
            # and reading the record now is exactly what must not become the
            # binding.
            return evidence_module.unbound(
                scope=claims_module._scope_for(
                    candidate, incident_id=action.incident_id
                ),
                operation=action.payload.operation,
            )
        _decision, grant = claims_module._grant_for(
            candidate,
            access=overseer_policy_client.ACCESS_CONTEXT,
            seams=seams,
            timeout=timeout,
        )
        if grant is None:
            return None
        evidence = evidence_module.gather(
            candidate=candidate,
            grant=grant,
            incident_id=action.incident_id,
            seams=seams,
            state_dir=state_dir,
            history=bool(args.history),
            generated_classes=tuple(args.generated_classes or ()),
            timeout=timeout,
        )
        freshness["holds"] = reviewed == overseer_decisions.evidence_digest(
            evidence.statements, scope=evidence.scope
        )
        freshness["checked"] = True
        ruling, _register = evidence_module._rule(
            evidence=evidence,
            claims=claims,
            operation=action.payload.operation,
            raises=raises,
            reviewed=reviewed,
        )
        return ruling

    action = overseer_dispatch.Action(
        plan_dir=common.plan_dir_of(candidate),
        run_id=args.run_id,
        incident_id=incident_id,
        payload=payload_for_action,
        token=args.token,
        scope=args.scope,
        repositories=overseer_policy_client.repositories_for(candidate.repositories),
    )
    dispatch = overseer_dispatch.dispatch(
        action=action,
        watcher_id=args.watcher_id,
        ledger=ledger,
        adapters=_adapters(
            authority=authority,
            ledger=ledger,
            state_dir=state_dir,
            seams=seams,
            timeout=timeout,
        ),
        authorize=seams.authorize,
        build_inventory=seams.build_inventory,
        inspect_run=seams.inspect_run,
        state_dir=state_dir,
        timeout=timeout,
    )
    escalation: common.Escalation | None = None
    if dispatch.escalated or not dispatch.performed:
        escalation = common.Escalation(
            reason=dispatch.refusal or "outcome_uncertain",
            question=(
                f"The overseer could not {args.operation} on {candidate.plan_dir}: "
                f"{dispatch.detail or dispatch.refusal}. How should this proceed?"
            ),
            blocked=(f"{candidate.plan_dir}::{args.operation}",),
            independent=claims_module._independent_plans(
                inventory, excluding=common.plan_dir_of(candidate)
            ),
        )
    changed = freshness["checked"] and bool(reviewed) and not freshness["holds"]
    payload = responses.success_payload(
        "overseer-act",
        dispatch=payloads._dispatch_payload(dispatch),
        evidence={
            "reviewed": reviewed,
            # The binding this invocation confirmed, which is the caller's own
            # digest or nothing at all. A record that moved is reported as
            # having moved and is never named: naming it here would publish the
            # replacement binding these same claims are refused for lacking.
            "observed": reviewed if freshness["holds"] else "",
            "changed": changed,
            "bound": bound,
        },
        escalation=None if escalation is None else escalation.payload(),
    )
    lines = [
        f"{args.operation} on {candidate.plan_dir}: "
        f"{'performed' if dispatch.performed else f'refused ({dispatch.refusal})'}"
    ]
    if dispatch.detail:
        lines.append(f"  {dispatch.detail}")
    if not bound:
        lines.append(
            "  the claims name no reviewed evidence: re-read the record with "
            "`asl overseer decide --list-statements` and pass its "
            "`evidence_digest` as --reviewed-evidence"
        )
    if changed:
        lines.append(
            f"  evidence moved: the record is no longer {reviewed}, and the "
            "record it is now is named by reading it"
        )
        lines.append(
            "  re-run `asl overseer decide --list-statements` and read the "
            "record again before acting"
        )
    if dispatch.recoveries:
        lines.append(
            f"  recoveries: {dispatch.recoveries}/{overseer_dispatch.RECOVERY_LIMIT}"
        )
    if escalation is not None:
        lines.append(f"  ask a human: {escalation.question}")
        lines.append(
            "  meanwhile: "
            f"{', '.join(escalation.independent) or 'nothing else in scope'}"
        )
    common._emit(payload, lines, as_json=as_json)
    return common.EXIT_OK if dispatch.performed else common.EXIT_REFUSED
