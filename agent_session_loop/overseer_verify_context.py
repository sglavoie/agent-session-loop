# ruff: noqa: F403, F405
"""Build and prove the verification context recorded for an overseer action."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .overseer_verify_shared import *


def _context(
    *,
    plan_dir: str,
    repositories: Sequence[overseer_policy_client.Repository],
    state_dir: str | Path | None,
    build_inventory: BuildInventory,
) -> verification_models.VerificationContext | tuple[Refusal, str]:
    """Build the context this plan's checks resolve against, or say why not.

    Three questions are answered here, and all three from the record rather
    than from anything this process was configured with or the plan currently
    says: which mode the run this action was authorized about is in, which
    directory that mode's verification context is built from, and whether the
    registry it is built out of is still the one this action was permitted to
    touch. How a context is then built from a plan stays
    :mod:`agent_session_loop.verification`'s answer, so a registered plan and a
    legacy one differ here by one path and one binding and nowhere else.
    """
    try:
        registry = plans_registry.read_repository_registry(plan_dir)
    except plans_errors.PlanError as error:
        return ("workspace_unrecoverable", str(error))

    observed = _observed_run(
        plan_dir, state_dir=state_dir, build_inventory=build_inventory
    )
    if observed is None:
        return (
            "workspace_unrecoverable",
            f"discovery holds no record of {plan_dir}, so neither the root its "
            "checks resolve against nor the mode it was authorized in can be "
            "read back",
        )

    try:
        _observed_registry(observed)
    except workspaces.WorkspaceError as error:
        return (
            "registry_moved",
            f"{observed.plan_dir} has inconsistent registry evidence: {error}; "
            "this needs fresh discovery and a fresh reading of the record "
            "rather than another attempt",
        )

    if registry is None:
        # A plan with no manifest is legacy *now*. Whether it was legacy when
        # this action was authorized is the different question the record
        # answers, and asking only the plan is what would let a removed
        # manifest turn a bound registry into no binding at all.
        dropped = _registry_kept(observed)
        if dropped is not None:
            return dropped
        if len(repositories) != 1:
            return (
                "workspace_unrecoverable",
                f"{plan_dir} declares no repository registry and this action "
                f"names {len(repositories)} repositories, so a check's root is "
                "ambiguous",
            )
        cwd: str = repositories[0].path
    else:
        if not observed.workspace_root:
            return (
                "workspace_unrecoverable",
                f"discovery records no workspace root for {plan_dir}, so the "
                "registered checks have nothing to resolve against",
            )
        cwd = observed.workspace_root
        # Before the workspace is resolved, so an unpermitted participant is
        # refused without Git ever being run inside it.
        drifted = _registry_bound(
            registry,
            workspace_root=cwd,
            repositories=repositories,
            observed=observed,
        )
        if drifted is not None:
            return drifted

    try:
        context = verification_models.verification_context_from_plan(plan_dir, cwd)
    except plans_errors.PlanError as error:
        return ("workspace_unrecoverable", str(error))
    # And again once every declared path has been followed to a real root: a
    # location that only becomes another repository after symlinks is a
    # participant the declared comparison could not have seen.
    escaped = _resolved_bound(context, repositories)
    if escaped is not None:
        return escaped
    return context


def _observed_run(
    plan_dir: str,
    *,
    state_dir: str | Path | None,
    build_inventory: BuildInventory,
) -> overseer_discovery.Candidate | None:
    """The run's own persisted record, as discovery read it."""
    inventory = build_inventory(state_dir=state_dir)
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return candidate
    return None


Participants = tuple[tuple[str, Path], ...]


def _registry_kept(
    observed: overseer_discovery.Candidate,
) -> tuple[Refusal, str] | None:
    """Refuse a legacy reading of a run the record attests was registered.

    The mirror of :func:`_registry_bound`, and the case that function cannot
    see: there is no live registry to compare, because the plan no longer has
    one. A registered run that loses its manifest has moved exactly as far as
    one that grew a participant --- further, since a legacy reading drops the
    binding rather than weakening it --- so it gets the same answer and the same
    remedy.

    Nothing here consults whether the contract has *also* become legacy-valid.
    A manifest and the checks beside it are edited in the same repository by the
    same turn, so a contract that now resolves without aliases is the other half
    of the change being refused, not evidence that the change was intended.
    """
    registry = _observed_registry(observed)
    if registry is None:
        return None
    return (
        "registry_moved",
        f"{observed.plan_dir} declares no repository registry now, and "
        f"discovery observed {_render(_observed_participants(observed))} under "
        f"registry {registry.digest}; the run this action was authorized about "
        "was a registered one, so verifying it as a legacy plan would bind no "
        "registry at all, and this needs fresh discovery and a fresh reading of "
        "the record rather than another attempt",
    )


def _observed_registry(
    observed: overseer_discovery.Candidate,
) -> workspaces.RepositoryRegistry | None:
    """The registry discovery recorded for this run, if it recorded one.

    A registered run's record carries the digest and the repositories it was
    computed from, written together out of one registry, so the record attests
    to its own registry and that attestation is checkable without the plan:
    rebuild the registry from the declarations and require the recorded digest.
    Failed reconstruction or attestation raises rather than returning the
    absence that represents legacy mode. Partial registry metadata is invalid
    too; a legacy run's published workspace fields are all absent.
    """
    digest = (observed.registry_digest or "").strip()
    if not observed.workspace_root and not digest and not observed.repositories:
        return None
    if not observed.workspace_root or not digest or not observed.repositories:
        raise workspaces.WorkspaceError("incomplete registered observation")
    registry = workspaces.RepositoryRegistry(
        tuple(
            workspaces.Repository(
                alias=declaration.alias,
                path=declaration.path,
                origin=declaration.origin,
            )
            for declaration in observed.repositories
        )
    )
    if registry.digest != digest:
        raise workspaces.WorkspaceError("declarations do not match the recorded digest")
    return registry


def _registry_bound(
    registry: workspaces.RepositoryRegistry,
    *,
    workspace_root: str,
    repositories: Sequence[overseer_policy_client.Repository],
    observed: overseer_discovery.Candidate,
) -> tuple[Refusal, str] | None:
    """Require the whole live registry to be the one that was authorized.

    Two comparisons, and they answer different questions. Against the run's
    persisted record: is this still the plan discovery observed, or has its
    participating set moved since the authorization was issued about it?
    Against the action: is every repository the plan now declares one AgentMux
    permitted? Neither implies the other, and a caller that only asked about
    the selected check's repository has asked about neither.
    """
    declared = _declared_participants(registry, workspace_root)
    recorded = _observed_participants(observed)
    if registry.digest != (observed.registry_digest or "") or declared != recorded:
        return (
            "registry_moved",
            f"{observed.plan_dir} now declares {_render(declared)} under "
            f"registry {registry.digest}, and discovery observed "
            f"{_render(recorded)} under {observed.registry_digest or '(none)'}; "
            "the participating set this action was authorized against is no "
            "longer the plan's own, so this needs fresh discovery and a fresh "
            "reading of the record rather than another attempt",
        )
    authorized = _authorized_participants(repositories)
    if declared != authorized:
        return (
            "scope_unauthorized",
            f"the plan's repository registry declares {_render(declared)}, "
            f"which is not the set this action was authorized for: "
            f"{_render(authorized)}",
        )
    return None


def _resolved_bound(
    context: verification_models.VerificationContext,
    repositories: Sequence[overseer_policy_client.Repository],
) -> tuple[Refusal, str] | None:
    """Require every resolved repository to be one the action carries."""
    authorized = _authorized_participants(repositories)
    workspace = context.workspace
    if workspace is None:
        # A legacy context has one root and no aliases, so the comparison is
        # between paths: the only root an action with a single repository has
        # authority over is that repository. Stated rather than inherited from
        # how ``cwd`` was chosen, so a later caller that picks the root some
        # other way is checked rather than trusted.
        root = plans_files.canonicalize_path(context.root)
        if len(authorized) == 1 and authorized[0][1] == root:
            return None
        return (
            "scope_unauthorized",
            f"the plan's legacy verification root resolves to {root}, which is "
            f"not the single repository this action was authorized for: "
            f"{_render(authorized)}",
        )
    resolved = _resolved_participants(workspace)
    authorized = _authorized_participants(repositories)
    if resolved != authorized:
        return (
            "scope_unauthorized",
            f"the plan's repositories resolve to {_render(resolved)}, which is "
            f"not the set this action was authorized for: {_render(authorized)}",
        )
    return None


def _declared_participants(
    registry: workspaces.RepositoryRegistry, workspace_root: str
) -> Participants:
    """Where the registry says its repositories are, before Git is consulted."""
    root = plans_files.canonicalize_path(workspace_root)
    return tuple(
        (repository.alias, plans_files.canonicalize_path(root / repository.path))
        for repository in registry
    )


def _observed_participants(observed: overseer_discovery.Candidate) -> Participants:
    return tuple(
        (declaration.alias, plans_files.canonicalize_path(declaration.resolved_root))
        for declaration in observed.repositories
    )


def _authorized_participants(
    repositories: Sequence[overseer_policy_client.Repository],
) -> Participants:
    return tuple(
        (item.alias, plans_files.canonicalize_path(item.path)) for item in repositories
    )


def _resolved_participants(workspace: workspaces.ResolvedWorkspace) -> Participants:
    return tuple(
        (repository.alias, plans_files.canonicalize_path(repository.root))
        for repository in workspace.repositories
    )


def _render(participants: Participants) -> str:
    """One line naming every participant, so a refusal says which one moved."""
    return ", ".join(f"{alias}={path}" for alias, path in participants) or "(none)"


def _receipt(
    result: verification_models.CheckResult, selection: Selection
) -> overseer_dispatch.Receipt:
    """Report what the check said, in the vocabulary the dispatcher settles on.

    A check that ran and failed is ``failed`` with no refusal spelled over it in
    the dispatcher's own vocabulary: the operation happened, and the answer is
    no. The detail is the check's own reason rather than a summary of it.
    """
    locator = _locator(result, selection)
    if result.status == "pass":
        return overseer_dispatch.Receipt.executed(
            detail=f"check {result.name!r} passed in {selection.session_id}'s contract",
            locator=locator,
        )
    return overseer_dispatch.Receipt.failed(
        detail=_phrase(
            "check_failed",
            f"{result.name}: {result.detail or result.failure_reason or 'no detail'}",
        ),
        locator=locator,
    )


def _locator(result: verification_models.CheckResult, selection: Selection) -> str:
    """Where the check ran, which is the only evidence a verification leaves."""
    where = (
        result.qualified_cwd
        or result.qualified_path
        or result.cwd
        or result.path
        or result.resolved_root
        or selection.resolved_root
    )
    return f"{result.name} in {where}" if where else result.name


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
    """Whether two rulings rest on the same governing decision, unchanged."""
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
