"""Overseer decision scope."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from . import (
    overseer_decision_models as models,
)
from . import (
    overseer_decision_vocab as vocab,
)

# -- helpers ---------------------------------------------------------------


def _supporting(
    claim: models.Claim,
    statement: models.Statement,
    known: dict[str, models.Statement],
) -> tuple[models.Statement, ...]:
    """The statements whose authorship can carry the projects a claim names.

    The cited statement, plus every corroborating one that is itself a dated
    user instruction reaching the same plan, run and incident. This is the only
    way one decision covers more than one project: a plan participating in two
    repositories needs a human statement in each, cited together, and no
    reading of the one can supply the other's authority.

    A corroborating statement that is not a dated user instruction lends
    nothing rather than rejecting the claim. Corroboration is also how a
    mailbox record showing that an instruction *arrived* is cited beside the
    transcript turn that shows who spoke, and that record has always been
    reportable without being authorizing.
    """
    supporting = [statement]
    for statement_id in claim.corroborating:
        item = known.get(statement_id)
        if item is None or not item.authorizing or item.moment is None:
            continue
        if item.scope.narrows(claim.scope):
            supporting.append(item)
    return tuple(supporting)


def _declared_roots(repositories: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Each declared root as (comparable, declared), longest comparable first.

    Repositories nest -- a checkout inside a checkout is a different project --
    so the most specific root has to be tried first or the enclosing one would
    claim a directory the nested one owns. The declared spelling is carried
    beside the comparable form because it is the identity a request names, and
    a statement scoped to a normalized variant of it would match nothing.
    """
    pairs = []
    for item in repositories:
        comparable = item.rstrip("/")
        if comparable:
            pairs.append((comparable, item))
    return tuple(sorted(pairs, key=lambda pair: len(pair[0]), reverse=True))


def _project_of(directory: str, roots: Sequence[tuple[str, str]]) -> str:
    """The declared repository a working directory belongs to, or the directory.

    Containment is measured on path boundaries, the same way AgentMux decides
    whether a conversation is in scope, so ``/srv/api-internal`` is not inside
    ``/srv/api``.
    """
    trimmed = directory.rstrip("/") or directory
    for comparable, declared in roots:
        if trimmed == comparable or trimmed.startswith(f"{comparable}/"):
            return declared
    return directory


def _applies(decision: models.Decision, scope: models.Scope) -> bool:
    """Whether one decision governs a target, which its stance decides.

    An allowance has to :meth:`Scope.covers` the target. Authority is held per
    repository, so a permission given in one of them cannot be spent on work
    that also touches another nobody spoke about, and a plan spanning two needs
    a human statement in each.

    A hold only has to :meth:`Scope.bears_on` it. A hold is a constraint on the
    repository it names rather than a permission for a set, and a target that
    still includes that repository is still work the hold was about: adding a
    second repository to an action cannot make an existing hold cease to reach
    it. Reading a hold through ``covers`` instead is exactly what let a union
    scope walk out from under one, and reading an allowance through ``bears_on``
    would let one repository's allowance authorize another.

    Both halves keep the plan, run and incident narrowing, since ``covers`` and
    ``bears_on`` share :meth:`Scope.narrows`, and both leave a decision about a
    project the target does not name a precedent.

    It is asked of a whole target and of one repository at a time -- the two
    questions :func:`_participates` and :func:`_standing_holds` ask -- and on a
    scope naming one project the two stances coincide, since a set of one is
    covered exactly when it is shared. That is why a repository's own chain can
    be read with the same predicate that decides authority over the target: the
    difference between a hold and an allowance is a difference about *sets* of
    repositories, and inside one repository there is no set to disagree about.
    """
    if decision.stance == "hold":
        return decision.scope.bears_on(scope)
    return decision.scope.covers(scope)


def _project_scopes(
    scope: models.Scope,
) -> tuple[models.Scope, ...]:
    """One target, seen one participating repository at a time.

    Everything but the project is carried through, because narrowing a target to
    a repository must not widen it in any other direction: a decision about
    another plan, run or incident is no more applicable to one of this target's
    repositories than it is to the target.
    """
    return tuple(
        models.Scope(
            project=project,
            plan_dir=scope.plan_dir,
            run_id=scope.run_id,
            incident=scope.incident,
        )
        for project in scope.named_projects
    )


def _participates(decision: models.Decision, scope: models.Scope) -> bool:
    """Whether a decision takes part in the target's history at all.

    A decision participates when it :func:`applies <_applies>` to the target or
    to any single repository the target names. The second half is what a
    per-repository release needs: an allowance naming one of two repositories
    cannot authorize the pair, but it is a decision about work the target
    includes, and one that arrived after a hold on that repository is the answer
    to whether the repository is still stopped. Excluding it -- which reading
    applicability against the whole target alone did -- left the hold governing a
    target the person had already released.

    A decision reaching none of the repositories reaches nothing here and stays
    a precedent, so the anti-transfer rule is untouched.
    """
    return any(_applies(decision, narrowed) for narrowed in _project_scopes(scope))


def _partition(
    decisions: Iterable[models.Decision],
    scope: models.Scope,
) -> tuple[
    tuple[models.Decision, ...],
    tuple[models.Decision, ...],
]:
    """Split decisions into the ones that reach ``scope`` and the ones that do not.

    Both halves come back ordered, because the applicable half is read as a
    supersession chain and the precedents are shown to an operator, and an
    unordered list of either would be a set of claims with no history.
    """
    applicable: list[models.Decision] = []
    precedents: list[models.Decision] = []
    for decision in decisions:
        target = applicable if _participates(decision, scope) else precedents
        target.append(decision)
    applicable.sort(key=lambda item: item.order)
    precedents.sort(key=lambda item: item.order)
    return tuple(applicable), tuple(precedents)


def _standing_holds(
    applicable: Sequence[models.Decision],
    scope: models.Scope,
) -> tuple[models.Decision, ...]:
    """The hold still in force over each participating repository.

    One chain per repository: the applicable decisions naming it, in order,
    which ``applicable`` already is. The repository is stopped when the last of
    them is a hold -- so a hold is lifted by a later decision naming the same
    repository and by nothing else, and a repository nobody spoke about again
    stays stopped however many others were released.
    """
    holds: list[models.Decision] = []
    for narrowed in _project_scopes(scope):
        chain = [item for item in applicable if _applies(item, narrowed)]
        if chain and chain[-1].stance == "hold":
            holds.append(chain[-1])
    return tuple(holds)


def _governing(
    applicable: Sequence[models.Decision],
    scope: models.Scope,
) -> models.Decision | None:
    """Which applicable decision is in force over the target as a whole.

    A hold standing over any participating repository governs, because the
    target includes work that repository was stopped for; the latest of them is
    reported when more than one repository is stopped, so an operator is shown
    the most recent word rather than an arbitrary one.

    Otherwise the governing decision is the last allowance
    :meth:`covering <Scope.covers>` every repository the target names. Authority
    is still held per repository and is still read over the whole target, so
    lifting a hold restores the authority that was already established and never
    manufactures one: an allowance naming a subset can end that subset's hold and
    can never be the permission an action rests on.
    """
    stopped = _standing_holds(applicable, scope)
    if stopped:
        return max(stopped, key=lambda item: item.order)
    authority: models.Decision | None = None
    for item in applicable:
        if item.stance == "allow" and item.scope.covers(scope):
            authority = item
    return authority


def _superseded(
    applicable: Sequence[models.Decision],
    governing: models.Decision | None,
) -> tuple[models.Decision, ...]:
    """Every applicable decision that is not the one in force, in order.

    Both the decisions the governing one replaced and the ones replaced under it
    -- a hold a release ended, a release that ended a hold in a repository that
    is not the one still stopping the target. They are reported rather than
    dropped for the reason supersession is reported at all: "replaced" and
    "never said" are different, and an operator reading a refusal needs to see
    the whole chain that produced it.
    """
    return tuple(item for item in applicable if item is not governing)


def _unauthorized_projects(
    applicable: Sequence[models.Decision],
    scope: models.Scope,
) -> tuple[str, ...]:
    """Which participating repositories no applicable allowance speaks for."""
    allowed = {
        project
        for item in applicable
        if item.stance == "allow"
        for project in item.scope.named_projects
    }
    return tuple(project for project in scope.named_projects if project not in allowed)


def _no_authority_detail(
    operation: str,
    *,
    precedents: Sequence[models.Decision],
    partial: Sequence[models.Decision],
    unauthorized: Sequence[str],
) -> str:
    """Why nothing authorizes an operation, in the terms that make it actionable.

    Three different absences, and telling an operator which one this is decides
    what they do next: nobody spoke, somebody spoke about another run, or
    somebody spoke about some of this run's repositories and not the rest --
    which is answered by a statement in the repository that is short, cited
    beside the one that already spoke, rather than by re-asking the repository
    that did.
    """
    if partial and unauthorized:
        return (
            f"no decision authorizes {operation} in {', '.join(unauthorized)}, "
            "which this run participates in"
        )
    if partial:
        return (
            f"{len(partial)} decision(s) allow {operation} in this run's "
            "repositories one at a time, and authority over all of them has to "
            "rest on one claim citing a statement in each"
        )
    if precedents:
        return (
            f"{len(precedents)} decision(s) elsewhere allow {operation} "
            "and none of them was made about this run"
        )
    return f"no user decision speaks to {operation} here"


def _refused(
    request: models.Request,
    refusal: vocab.Refusal,
    detail: str,
    *,
    decision: models.Decision | None = None,
    superseded: tuple[models.Decision, ...] = (),
    precedents: tuple[models.Decision, ...] = (),
    settlements: tuple[models.Settlement, ...] = (),
) -> models.Ruling:
    """A ruling that authorizes nothing, and says which refusal it is."""
    return models.Ruling(
        operation=request.operation,
        scope=request.scope,
        authorized=False,
        refusal=refusal,
        decision=decision,
        superseded=superseded,
        precedents=precedents,
        settlements=settlements,
        summary=f"{request.operation} refused ({refusal}): {detail}",
    )
