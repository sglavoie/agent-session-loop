"""Overseer cli decide."""

from __future__ import annotations

import argparse

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
from . import (
    overseer_decisions,
    overseer_policy_client,
    responses,
)


@common.leaf("overseer-decide")
def decide_from_args(
    args: argparse.Namespace,
    *,
    seams: common.Seams = common.DEFAULT_SEAMS,
) -> int:
    """Resolve an operation against what humans already decided on this plan.

    ``--list-statements`` prints the supervision record the register will read,
    with the stable statement ids a claim has to name. The host reads those,
    writes claims, and passes them back: that round trip is where judgement
    lives, and the ids being derived rather than invented is what keeps a claim
    from citing a statement nobody made.

    ``evidence_digest`` names the record those claims were read from, and only
    the reading that *shows* the record issues one: ``--list-statements`` prints
    the statements and their digest together, so a digest a host holds is a
    record a host was shown. It is printed rather than stored, because storing
    it would be the cached permission this boundary refuses to keep; the host
    carries it back through ``--reviewed-evidence`` here and at
    ``asl overseer act``, and either is refused ``evidence_changed`` if human
    evidence in scope moved in the meantime.

    Claims therefore have to name one. A claim-bearing invocation that names no
    digest is refused ``evidence_unbound`` rather than resolved against a record
    read here and now, which would be this leaf writing the host's reading for
    it. A refused reading publishes no digest either: the whole point of the
    window between the listing and the claims is that a hold can land in it, so
    handing back the *new* record's name would let the old claims travel on to
    ``act`` as though that record had been reviewed. That holds for the whole
    answer and not only for the ``evidence_digest`` field: the ruling summary
    and the escalation this prints are read by the same host and carried by the
    same hands, so neither names the record the refusal observed either.

    A refusal is not a failure to try harder. ``held`` and ``human_decision``
    both mean a person already settled this, or has not, and the honest next
    move is the escalation this prints rather than an action.
    """
    as_json = bool(getattr(args, "json", False))
    state_dir = common._state_dir(args)
    try:
        inventory = seams.build_inventory(state_dir=state_dir)
        candidate = claims_module._resolve_candidate(inventory, args.plan_dir)
        incident_id = None if args.incident_id is None else int(args.incident_id)
        scope = claims_module._scope_for(candidate, incident_id=incident_id)
        claims = claims_module.parse_claims(
            claims_module._claims_text(args), scope=scope
        )
        decision, grant = claims_module._grant_for(
            candidate,
            access=overseer_policy_client.ACCESS_CONTEXT,
            seams=seams,
            timeout=float(args.timeout),
        )
    except common.OverseerCliError as error:
        return common._failure("overseer-decide", error, as_json=as_json)
    if grant is None:
        # Without a context grant no statement is readable, so the record is
        # not empty -- it is unseen. Listing it as zero statements under a
        # digest would hand the host a binding for a reading nobody did, and
        # ruling on it would turn a refused permission into ``no_decision``,
        # which reads as a question for a human.
        refusal = "unauthorized" if decision.available else "policy_unavailable"
        detail = decision.summary or decision.reason
        payload = responses.error_payload(
            "overseer-decide",
            reason=refusal,
            error=detail,
            plan_dir=candidate.plan_dir,
            authorized=False,
            evidence_digest=None,
        )
        common._emit(
            payload,
            [f"decide on {candidate.plan_dir}: refused ({refusal})", f"  {detail}"],
            as_json=as_json,
        )
        return common.EXIT_REFUSED
    evidence = evidence_module.gather(
        candidate=candidate,
        grant=grant,
        incident_id=incident_id,
        seams=seams,
        state_dir=state_dir,
        history=bool(args.history),
        generated_classes=tuple(args.generated_classes or ()),
        timeout=float(args.timeout),
    )
    if args.list_statements:
        listed = overseer_decisions.evidence_digest(evidence.statements, scope=scope)
        payload = responses.success_payload(
            "overseer-decide",
            listed=True,
            scope=common._scope_payload(scope),
            evidence_digest=listed,
            history=payloads._history_payload(evidence.history),
            statements=[
                payloads._statement_payload(item) for item in evidence.statements
            ],
        )
        lines = [f"{len(evidence.statements)} statement(s) in scope"]
        lines.extend(
            f"  {item.statement_id} [{item.voice}] {item.text[:72]}"
            for item in evidence.statements
        )
        lines.append(f"  evidence digest: {listed}")
        common._emit(payload, lines, as_json=as_json)
        return common.EXIT_OK
    reviewed = str(getattr(args, "reviewed_evidence", "") or "")
    ruling: overseer_decisions.Ruling
    register: overseer_decisions.Register
    if claims and not reviewed:
        ruling = evidence_module.unbound(scope=scope, operation=args.operation)
        # Still registered, so a host that forgot the binding and miswrote its
        # claims hears about both in one answer.
        register = overseer_decisions.register(
            statements=evidence.statements, claims=claims
        )
    else:
        ruling, register = evidence_module._rule(
            evidence=evidence,
            claims=claims,
            operation=args.operation,
            raises=claims_module._raises(args),
            reviewed=reviewed,
        )
    # Echoed back, never minted: the digest a refused reading would be handed
    # is the name of the record that refused it.
    published = "" if ruling.refusal in evidence_module.UNUSABLE_READING else reviewed
    settlements = [
        overseer_decisions.settle(register=register, topic=topic, scope=scope)
        for topic in overseer_decisions.HUMAN_TOPICS
    ]
    escalation: common.Escalation | None = None
    if not ruling.authorized:
        escalation = common.Escalation(
            reason=ruling.refusal or "no_decision",
            question=(
                f"May the overseer {args.operation} on {candidate.plan_dir}? "
                f"{ruling.summary}".strip()
            ),
            blocked=(f"{candidate.plan_dir}::{args.operation}",),
            independent=claims_module._independent_plans(
                inventory, excluding=common.plan_dir_of(candidate)
            ),
        )
    payload = responses.success_payload(
        "overseer-decide",
        listed=False,
        authorized=ruling.authorized,
        ruling=payloads._ruling_payload(ruling),
        evidence_digest=published or None,
        history=payloads._history_payload(evidence.history),
        human_settlements=[
            {
                "topic": settlement.topic,
                "settled": settlement.decision is not None,
                "stance": (
                    None if settlement.decision is None else settlement.decision.stance
                ),
            }
            for settlement in settlements
        ],
        rejected_claims=[
            {
                "statement_id": rejection.claim.statement_id,
                "reason": rejection.reason,
                "detail": rejection.detail,
            }
            for rejection in register.rejections
        ],
        escalation=None if escalation is None else escalation.payload(),
    )
    lines = [
        f"{args.operation} on {candidate.plan_dir}: "
        f"{'authorized' if ruling.authorized else f'refused ({ruling.refusal})'}",
        f"  {ruling.summary}",
        (
            f"  evidence digest: {published}"
            if published
            else "  evidence digest: none issued; read the record with "
            "--list-statements and claim what it says"
        ),
    ]
    for rejection in register.rejections:
        lines.append(f"  rejected {rejection.claim.statement_id}: {rejection.reason}")
    if escalation is not None:
        lines.append(f"  ask a human: {escalation.question}")
        lines.append(f"  blocked: {', '.join(escalation.blocked)}")
        lines.append(
            "  meanwhile: "
            f"{', '.join(escalation.independent) or 'nothing else in scope'}"
        )
    common._emit(payload, lines, as_json=as_json)
    return common.EXIT_OK if ruling.authorized else common.EXIT_REFUSED
