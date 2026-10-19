"""Checkpoint authority and drift checks shared by adapter phases."""

from __future__ import annotations

from collections.abc import Mapping

from . import commit_message, overseer_decisions
from .overseer_checkpoint_models import AuthorityRefusal, Operation, Refusal


def authority_refusal(
    ruling: overseer_decisions.Ruling | None,
    *,
    plan_dir: str,
    run_id: str,
    incident_id: int,
    operation: Operation = "commit",
) -> tuple[AuthorityRefusal, str] | None:
    """Return why this ruling does not authorize this action, or ``None``.

    Scope is checked field by field against what the caller is about to do
    rather than by :meth:`~agent_session_loop.overseer_decisions.Scope.covers`,
    because covers answers "may this decision reach that target" and the
    question here is the stricter one: was the ruling made about *this* plan,
    run and incident. A ruling that named none of them is a ruling that would
    equally authorize a commit in the next plan's repository.

    ``operation`` defaults to ``commit`` because this module's own adapter is
    the checkpoint. It is a parameter rather than a constant so a sibling
    adapter asks this question through one implementation instead of a second
    copy that could drift: the five refusals it can return are
    :data:`AUTHORITY_REFUSALS`, which every adapter's own ``Refusal`` vocabulary
    contains.
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
            f"{decision.decision_id} says nothing about {operation}",
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
    for named_field, ruled, wanted in named:
        if not ruled or ruled != wanted:
            return (
                "authority_unscoped",
                f"the ruling's {named_field} is {ruled!r}, not {wanted!r}",
            )
    if not decision.scope.covers(scope):
        return (
            "authority_unscoped",
            f"{decision.decision_id} was not made about this scope",
        )
    return None


def _drift(error: commit_message.CommitMessageError) -> tuple[Refusal, str]:
    """Tell an edited worktree apart from a re-staged index.

    Both arrive as ``snapshot-drifted`` from
    :func:`~agent_session_loop.commit_message.assert_pathspec_snapshot`, and the
    difference matters to whoever reads the refusal: one means somebody is still
    editing, the other means somebody staged over the set this checkpoint was
    about to commit.
    """
    details = error.details if isinstance(error.details, Mapping) else {}
    if details.get("paths"):
        return ("worktree_drifted", str(error))
    return ("snapshot_mismatch", str(error))


def _key(plan_dir: str, incident_id: int, target: str) -> str:
    """The memo key one action's preparation and execution agree on."""
    return "\x00".join((plan_dir, str(incident_id), target))


def _phrase(refusal: Refusal, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


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
