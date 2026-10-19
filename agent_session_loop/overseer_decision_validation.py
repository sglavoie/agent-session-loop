"""Overseer decision validation."""

from __future__ import annotations

from . import (
    overseer_decision_evidence as evidence,
)
from . import (
    overseer_decision_models as models,
)
from . import (
    overseer_decision_scope as scope,
)
from . import (
    overseer_decision_vocab as vocab,
)

# -- validation ------------------------------------------------------------


def _validate(
    claim: models.Claim,
    statement: models.Statement | None,
    known: dict[str, models.Statement],
    seen: set[str],
) -> models.Decision | models.Rejection:
    """One claim against the statement it cites, in refusal order."""
    if statement is None:
        return models.Rejection(
            claim=claim,
            reason="unknown_statement",
            detail=f"{claim.statement_id} is not in the evidence",
        )
    if not statement.authorizing:
        return models.Rejection(
            claim=claim,
            reason="not_user_authored",
            detail=(
                f"{statement.statement_id} is a {statement.voice}, "
                "which cannot carry a decision"
            ),
        )
    if statement.moment is None:
        return models.Rejection(
            claim=claim,
            reason="undated",
            detail=(
                f"{statement.statement_id} carries no readable moment, so no "
                "later decision could be shown to replace it"
            ),
        )
    if not claim.scope.named_projects:
        return models.Rejection(
            claim=claim,
            reason="scope_unnamed",
            detail="a decision that names no project covers nothing",
        )
    if not statement.scope.narrows(claim.scope):
        return models.Rejection(
            claim=claim,
            reason="scope_widened",
            detail=(
                f"{statement.statement_id} was made about "
                f"{evidence._scope_text(statement.scope)}, which does not reach "
                f"{evidence._scope_text(claim.scope)}"
            ),
        )
    supporting = scope._supporting(claim, statement, known)
    covered = {project for item in supporting for project in item.scope.named_projects}
    uncovered = tuple(
        project for project in claim.scope.named_projects if project not in covered
    )
    if uncovered:
        return models.Rejection(
            claim=claim,
            reason="scope_widened",
            detail=(
                f"{statement.statement_id} was made about "
                f"{evidence._scope_text(statement.scope)}, which does not reach "
                f"{evidence._scope_text(claim.scope)}"
            )
            if len(supporting) == 1
            else (
                "no statement this claim rests on was made about "
                f"{', '.join(uncovered)}"
            ),
        )
    if claim.topic not in vocab.TOPICS:
        return models.Rejection(
            claim=claim,
            reason="unknown_topic",
            detail=f"{claim.topic!r} is not a topic a decision can be about",
        )
    unknown = tuple(item for item in claim.operations if item not in vocab.OPERATIONS)
    if unknown:
        return models.Rejection(
            claim=claim,
            reason="unknown_operation",
            detail=f"{', '.join(unknown)} is outside the operational scope",
        )
    if claim.topic == "operations":
        if claim.stance == "allow" and not claim.operations:
            return models.Rejection(
                claim=claim,
                reason="operations_unnamed",
                detail="an operational allowance has to name what it allows",
            )
    elif claim.operations:
        return models.Rejection(
            claim=claim,
            reason="operations_named",
            detail=(
                f"a {claim.topic} decision names no operation; naming one would "
                "turn a human judgement into an operational permission"
            ),
        )
    decision_id = f"{claim.statement_id}:{claim.topic}"
    if decision_id in seen:
        return models.Rejection(
            claim=claim,
            reason="duplicate_claim",
            detail=f"{claim.statement_id} was already read as a {claim.topic} decision",
        )
    sources = (claim.statement_id, *claim.corroborating)
    return models.Decision(
        decision_id=decision_id,
        statement_id=claim.statement_id,
        topic=claim.topic,
        stance=claim.stance,
        scope=claim.scope,
        operations=tuple(claim.operations),
        at=statement.at,
        moment=statement.moment,
        sources=sources,
        text=statement.text,
        reading=claim.reading,
    )
