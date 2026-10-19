"""Validate current authority and the exact question a reply will close."""

from __future__ import annotations

from . import mailbox_snapshot, mailbox_vocabulary, overseer_decisions
from .overseer_mailbox_models import Refusal


def _check_authority(
    ruling: overseer_decisions.Ruling | None,
    *,
    operation: str,
    plan_dir: str,
    run_id: str,
    incident_id: int,
) -> tuple[Refusal, str] | None:
    """Return why this ruling does not authorize this posting, or ``None``.

    Scope is checked field by field against what the caller is about to do
    rather than by :meth:`~agent_session_loop.overseer_decisions.Scope.covers`,
    because covers answers "may this decision reach that target" and the
    question here is the stricter one: was the ruling made about *this* plan,
    run and incident. A ruling that named none of them is a ruling that would
    equally authorize the next plan.
    """
    if ruling is None:
        return ("authority_absent", "no ruling was established")
    if ruling.operation != operation:
        return (
            "authority_silent",
            f"the ruling is about {ruling.operation!r}, not {operation!r}",
        )
    if ruling.refusal is not None:
        return ("authority_refused", ruling.refusal)
    if not ruling.authorized:
        return ("authority_refused", ruling.summary or "not authorized")
    if ruling.unresolved:
        return ("authority_held", ", ".join(ruling.unresolved))
    decision = ruling.decision
    if decision is None:
        return ("authority_absent", "the ruling names no governing decision")
    if not decision.speaks_to(operation):
        return (
            "authority_silent",
            f"{decision.decision_id} says nothing about {operation!r}",
        )
    if decision.stance != "allow":
        return ("authority_held", f"{decision.decision_id} is a {decision.stance}")
    scope = ruling.scope
    if not scope.named_projects:
        return ("authority_unscoped", "the ruling names no project")
    named = (
        ("plan", scope.plan_dir, plan_dir),
        ("run", scope.run_id, run_id),
        ("incident", scope.incident, str(incident_id)),
    )
    for field, ruled, wanted in named:
        if not ruled or ruled != wanted:
            return (
                "authority_unscoped",
                f"the ruling's {field} is {ruled!r}, not {wanted!r}",
            )
    if not decision.scope.covers(scope):
        return (
            "authority_unscoped",
            f"{decision.decision_id} was not made about this scope",
        )
    return None


def _decision_id(ruling: overseer_decisions.Ruling | None) -> str:
    if ruling is None or ruling.decision is None:
        return "no decision"
    return ruling.decision.decision_id


def _same_decision(
    first: overseer_decisions.Ruling | None,
    second: overseer_decisions.Ruling | None,
) -> bool:
    """Whether two rulings rest on the same governing decision, unchanged.

    The stance and the moment are compared as well as the identity, because a
    decision re-read from moved evidence can keep its id and mean the opposite.
    """
    if first is None or second is None:
        return False
    left, right = first.decision, second.decision
    if left is None or right is None:
        return False
    return (
        left.decision_id == right.decision_id
        and left.stance == right.stance
        and left.at == right.at
        and left.operations == right.operations
    )


def _check_question(
    view: mailbox_snapshot.MailboxView,
    *,
    question_id: str,
    run_id: str,
) -> tuple[Refusal, str] | None:
    """Return why this question cannot be answered now, or ``None``.

    This is the "somebody got there first" check, and it runs against the
    mailbox rather than a cached projection because the mailbox is what the
    append itself will be judged against a moment later.
    """
    closure = view.closure_for(question_id)
    if closure is not None:
        return (
            "question_closed",
            f"{closure.kind} {closure.id} already closed it",
        )
    if not view.accepts_replies:
        return ("mailbox_full", "the mailbox is not accepting replies")
    open_ids = (
        {message.id for message in view.open_questions(run_id)}
        if run_id
        else {message.id for message in view.unanswered_questions}
    )
    if question_id not in open_ids:
        known = any(
            message.kind == mailbox_vocabulary.KIND_QUESTION
            and message.id == question_id
            for message in view.messages
        )
        if not known:
            return ("question_unknown", question_id)
        return ("question_stale", f"{question_id} is no longer open")
    return None
