"""Overseer decision resolution."""

from __future__ import annotations

from collections.abc import Sequence

from . import (
    overseer_decision_evidence as evidence,
)
from . import (
    overseer_decision_models as models,
)
from . import (
    overseer_decision_scope as scope_module,
)
from . import (
    overseer_decision_validation as validation,
)
from . import (
    overseer_decision_vocab as vocab,
)


def register(
    *,
    statements: Sequence[models.Statement],
    claims: Sequence[models.Claim],
) -> models.Register:
    """Turn the host agent's readings into decisions, refusing what cannot hold.

    Every claim produces exactly one outcome -- a decision or a named rejection
    -- so a caller can account for each reading it submitted. Rejections carry
    the claim itself rather than an identifier, since a claim citing a statement
    that does not exist has no identity to look up.
    """
    known = {item.statement_id: item for item in statements}
    decisions: list[models.Decision] = []
    rejections: list[models.Rejection] = []
    seen: set[str] = set()
    for claim in claims:
        statement = known.get(claim.statement_id)
        outcome = validation._validate(claim, statement, known, seen)
        if isinstance(outcome, models.Rejection):
            rejections.append(outcome)
            continue
        seen.add(outcome.decision_id)
        decisions.append(outcome)
    decisions.sort(key=lambda item: item.order)
    return models.Register(
        statements=tuple(statements),
        decisions=tuple(decisions),
        rejections=tuple(rejections),
    )


def settle(
    *,
    register: models.Register,
    topic: vocab.Topic,
    scope: models.Scope,
) -> models.Settlement:
    """Which decision on one topic is in force for one scope.

    Applicable decisions are ordered, a hold still standing over any
    participating repository governs, and otherwise the last allowance covering
    the target does; the rest are superseded, and decisions on the topic that
    were made about something else are precedents. It is the same partition and
    the same governing rule :func:`resolve` performs for an operation, so a topic
    and an operation are answered by one rule rather than two that could drift
    apart -- which is what makes a per-repository release of a human hold read
    the same way as a release of an operational one.
    """
    applicable, precedents = scope_module._partition(
        (item for item in register.decisions if item.topic == topic), scope
    )
    governing = scope_module._governing(applicable, scope)
    return models.Settlement(
        topic=topic,
        scope=scope,
        decision=governing,
        superseded=scope_module._superseded(applicable, governing),
        precedents=precedents,
    )


def dependencies(
    *,
    register: models.Register,
    scope: models.Scope,
    raises: Sequence[vocab.Topic] = (),
) -> tuple[vocab.Topic, ...]:
    """Which human questions one request depends on.

    A human topic never :meth:`Decision.speaks_to` an operation, so an accepted
    decision about acceptance, scope, design or safety is invisible to
    :func:`resolve` unless the request names its topic. That is the right shape
    for a caller who knows a question is open and the wrong one for a caller who
    was handed the answer: a user who typed a safety hold about this run said the
    safety question governs it, and a caller that had to name the topic to be
    stopped by it could be stopped only by holds it already knew about.

    So the topics a caller names are unioned with the human topics some in-scope
    decision was claimed on, in that order and without repeats. Scope is the
    same :func:`_participates` relation :func:`settle` partitions by, so a topic
    is raised exactly when :func:`settle` could report a decision on it: a human
    decision made about another project, plan, run or incident stays a precedent
    rather than becoming a question about this one, because the anti-transfer
    rule reads in both directions -- a neighbour's hold no more raises this run's
    questions than a neighbour's allowance settles them. A decision in one of
    *this* target's own repositories is not a neighbour's, and raises the topic
    even when the action spans repositories it does not name; otherwise adding a
    second repository would hide the question entirely. That reaches an
    acceptance given in one repository as well as a hold typed in one, and
    raising it is what makes the missing half a visible unsettled question rather
    than a silence: settling still takes an allowance covering every repository.

    Raising a topic is not settling it. A topic reaches :attr:`Request.raises`
    whether the claimed decision allows or holds, and :func:`resolve` refuses
    ``human_decision`` for a hold and for a withheld or absent settlement alike,
    which is what keeps an operational allowance from standing in for an answer
    nobody gave.
    """
    claimed = tuple(
        decision.topic
        for decision in register.decisions
        if decision.topic in vocab.HUMAN_TOPICS
        and scope_module._participates(decision, scope)
    )
    return tuple(dict.fromkeys((*raises, *claimed)))


def resolve(
    *,
    register: models.Register,
    request: models.Request,
) -> models.Ruling:
    """Whether one operation is authorized for one target, and on what.

    Refusals are ordered by how fundamental they are rather than by how they
    were discovered: an unscoped request and an unsupported operation are
    questions that cannot be asked, a reading of evidence that has moved is a
    reading nobody made, a hold is a decision to respect, an unsettled human
    question outranks any operational allowance, and only then is the absence of
    a decision -- or the presence of one made about something else -- reported.

    Which decisions are in the partition at all is :func:`_participates`: every
    decision reaching the target or any single repository it names, so a hold
    reaches a target that names its repository among others and the allowance
    that later releases that repository is in the same history rather than
    filed away as somebody else's business. What each of them can *do* is
    :func:`_governing`: a hold still standing over any participating repository
    refuses, and authority remains the last allowance covering every one of them,
    so an action does not escape a hold by growing and a release does not become
    a permission by arriving.

    ``evidence_changed`` sits above the decision checks because it is not a
    verdict on them: the claims can all be in force and the allowance can be the
    last word among them while the record they were read from has gained a hold
    nobody claimed, which is precisely the case re-resolving the same claims
    cannot see.

    That refusal names the record the caller says it read and never the one it
    found instead. The digest of the record as it stands is what these very
    claims would need in order to pass, so printing it beside "your reading is
    out of date" would issue the replacement binding in the same breath as the
    refusal. The new record has a name only for somebody who has read it, and
    reading it is :func:`evidence_digest` over a listing, not this refusal.
    """
    settlements = tuple(
        settle(register=register, topic=topic, scope=request.scope)
        for topic in request.raises
    )
    if not request.scope.named_projects:
        return scope_module._refused(
            request,
            "target_unscoped",
            "the request names no project, so no decision can be applicable",
            settlements=settlements,
        )
    if request.operation not in vocab.OPERATIONS:
        return scope_module._refused(
            request,
            "operation_unsupported",
            f"{request.operation!r} is not an operation an overseer performs",
            settlements=settlements,
        )
    # Computed, compared, and dropped. The digest of the record as it stands is
    # the name of a record nobody has read, and a refusal that printed it would
    # be handing the caller the very binding its stale claims need. Only the
    # comparison survives these lines.
    if request.reviewed and request.reviewed != evidence.evidence_digest(
        register.statements, scope=request.scope
    ):
        return scope_module._refused(
            request,
            "evidence_changed",
            f"the claims were read from evidence {request.reviewed}, which is "
            "no longer the record in scope; the record in scope now is named "
            "only by reading it",
            settlements=settlements,
        )

    applicable, precedents = scope_module._partition(
        (item for item in register.decisions if item.speaks_to(request.operation)),
        request.scope,
    )
    governing = scope_module._governing(applicable, request.scope)
    superseded = scope_module._superseded(applicable, governing)

    if governing is None:
        partial = tuple(item for item in applicable if item.stance == "allow")
        unauthorized = scope_module._unauthorized_projects(applicable, request.scope)
        refusal: vocab.Refusal = (
            "unsupported_recovery" if precedents or partial else "no_decision"
        )
        detail = scope_module._no_authority_detail(
            request.operation,
            precedents=precedents,
            partial=partial,
            unauthorized=unauthorized,
        )
        return scope_module._refused(
            request,
            refusal,
            detail,
            superseded=superseded,
            precedents=precedents,
            settlements=settlements,
        )

    if governing.stance == "hold":
        return scope_module._refused(
            request,
            "held",
            f"{governing.decision_id} holds this run",
            decision=governing,
            superseded=superseded,
            precedents=precedents,
            settlements=settlements,
        )

    unsettled = tuple(item.topic for item in settlements if not item.settled)
    if unsettled:
        return scope_module._refused(
            request,
            "human_decision",
            f"unsettled human {'question' if len(unsettled) == 1 else 'questions'}: "
            f"{', '.join(unsettled)}",
            decision=governing,
            superseded=superseded,
            precedents=precedents,
            settlements=settlements,
        )

    return models.Ruling(
        operation=request.operation,
        scope=request.scope,
        authorized=True,
        refusal=None,
        decision=governing,
        superseded=superseded,
        precedents=precedents,
        settlements=settlements,
        summary=(
            f"{request.operation} authorized by {governing.decision_id} "
            f"({governing.at})"
        ),
    )
