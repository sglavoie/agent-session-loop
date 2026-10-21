# ruff: noqa: F403, F405
from .repository_registration_models import *
from .repository_registration_models import _contracts, _SessionSource
from .repository_registration_rendering import (
    _insert_registry_table,
    _join,
    _migrate_session,
    _migrate_source_plan,
    _read_text,
    _refused,
    _rewrite_contract,
    _session_scope,
)


def derive_registration(
    plan_dir: str | Path, *, cwd: str | Path
) -> RegistrationPlan | RegistrationResult:
    """Compute the registry migration for ``plan_dir`` bound to ``cwd``.

    Detection goes through
    :func:`preflight_footprint.plan_repository_intent` rather than the
    footprint scan: intent resolves a contract target no session has
    created yet, and it is the gate the runner actually enforces, so this
    derivation fires exactly when that gate would refuse.
    """
    canonical_dir = canonicalize_path(plan_dir)
    bound_root = canonicalize_filesystem_path(cwd)

    intent, report = preflight_footprint.plan_repository_intent(
        canonical_dir, bound_root
    )
    if intent is None:
        return RegistrationResult(
            status="already-registered",
            detail="manifest.md already declares a `## Repositories` table",
        )
    if report is None:
        return _not_required(intent)

    sessions: list[_SessionSource] = []
    handoffs: list[plans_models.SessionHandoff] = []
    for status in inspect_plan(canonical_dir).sessions:
        sessions.append(
            _SessionSource(
                session_id=status.session.id,
                text=_read_text(status.session.path),
                contract=read_completion_contract(
                    canonical_dir, status.session.session_id
                ),
            )
        )
        handoffs.append(
            plans_handoff.read_session_handoff(status.session, repository_registry=None)
        )

    derived = _derive(intent, sessions, bound_root=bound_root)
    if isinstance(derived, RegistrationPlan):
        refusal = _completed_handoff_refusal(handoffs, derived)
        if refusal is not None:
            return refusal
    return derived


def _completed_handoff_refusal(
    handoffs: list[plans_models.SessionHandoff], derived: RegistrationPlan
) -> RegistrationResult | None:
    """Refuse a migration that would leave a completed handoff unparsable.

    A registry makes every entry in a completed ``Files modified`` list an
    ``Alias:path`` the parser validates, so migrating a plan whose finished
    sessions recorded bare paths would stop the plan from being read at all.
    The rule is :func:`plans_registry.completed_handoff_registry_blocker`, asked with
    the registry and scopes this migration would write; every other surface
    reaches it by running this derivation rather than by repeating it.
    Source-plan mode has no handoffs to invalidate, so it never reaches this.
    """
    for handoff in handoffs:
        blocker = plans_registry.completed_handoff_registry_blocker(
            handoff,
            scope=derived.scopes.get(handoff.session.id),
            registry_aliases=derived.registry.aliases,
        )
        if blocker is not None:
            return _refused(
                "completed-handoff-paths",
                detail=(
                    f"{blocker}; qualify every completed handoff's modified "
                    "paths as `Alias:path` before migrating"
                ),
                workspace_root=derived.workspace_root,
            )
    return None


def register_source_plan(text: str, *, cwd: str | Path) -> RegistrationResult:
    """Migrate a source plan's markdown, leaving its file to the caller.

    The gate that refuses a registry-less multi-repository plan also fires in
    ``asl split --check``, where no plan directory exists yet and nothing may
    write to the operator's plan file. So the derivation is the same, and the
    result carries the migrated markdown instead of a list of written files.
    """
    bound_root = canonicalize_filesystem_path(cwd)
    parsed = authoring_parse.parse_plan_document(text)
    if parsed.registry is not None:
        return RegistrationResult(
            status="already-registered",
            detail="plan already declares a `## Repositories` table",
            markdown=text,
        )

    sessions: list[_SessionSource] = []
    for session in parsed.sessions:
        session_text = "\n".join(session.lines)
        sessions.append(
            _SessionSource(
                session_id=session.session_id.value,
                text=session_text,
                contract=_contracts.parse_completion_contract(
                    session_text, session_id=session.session_id
                ),
            )
        )

    intent = repository_intent.resolve_intent(
        _source_candidates(text, sessions, bound_root=bound_root),
        root=bound_root,
    )
    if not intent.registry_required:
        return replace(_not_required(intent), markdown=text)

    derived = _derive(intent, sessions, bound_root=bound_root)
    if isinstance(derived, RegistrationResult):
        return derived

    migrated, sessions_changed = _migrate_source_plan(text, parsed, derived)
    return RegistrationResult(
        status="applied",
        workspace_root=derived.workspace_root,
        repositories=tuple(derived.registry),
        sessions_changed=sessions_changed,
        markdown=migrated,
    )


def _source_candidates(
    text: str,
    sessions: list[_SessionSource],
    *,
    bound_root: Path,
) -> list[repository_intent.IntentCandidate]:
    """Every location a source plan declares it will reach.

    Mirrors :func:`preflight_footprint._repository_intent_candidates` minus the
    plan-directory files a source plan does not have: contract locations
    first, then the plan's own prose, then the root a run would launch from.
    """
    candidates: list[repository_intent.IntentCandidate] = []
    for session in sessions:
        if session.contract is not None:
            candidates.extend(
                repository_intent.candidates_from_checks(session.contract.verify.checks)
            )
    candidates.extend(repository_intent.candidates_from_text(text))
    candidates.extend(repository_intent.candidates_from_execution_roots([bound_root]))
    return candidates


def _not_required(intent: repository_intent.RepositoryIntent) -> RegistrationResult:
    return RegistrationResult(
        status="not-required",
        detail=(
            f"plan reaches {len(intent.git_roots)} git "
            f"{'repository' if len(intent.git_roots) == 1 else 'repositories'}"
        ),
    )


def _derive(
    intent: repository_intent.RepositoryIntent,
    sessions: list[_SessionSource],
    *,
    bound_root: Path,
) -> RegistrationPlan | RegistrationResult:
    """Turn a registry-required intent into the migration it implies."""
    workspace_root = repository_scope_registry.suggest_workspace_root(intent.git_roots)
    if workspace_root is None:
        return _refused(
            "workspace-root",
            detail=(
                "repositories have no shared parent that could serve as a "
                "workspace root: " + _join(intent.git_roots)
            ),
        )
    try:
        suggestions = repository_scope_registry.suggest_repositories(
            workspace_root, intent.git_roots
        )
    except WorkspaceError as error:
        return _refused("workspace-root", detail=str(error))

    unobservable = tuple(
        suggestion.root for suggestion in suggestions if suggestion.origin is None
    )
    if unobservable:
        return _refused(
            "repository-origin",
            detail="no usable origin remote: " + _join(unobservable),
            workspace_root=workspace_root,
        )
    try:
        registry = repository_scope_registry.suggest_registry(
            workspace_root, intent.git_roots
        )
    except WorkspaceError as error:
        return _refused(
            "workspace-root", detail=str(error), workspace_root=workspace_root
        )

    alias_by_root = {suggestion.root: suggestion.alias for suggestion in suggestions}
    git_root_cache: dict[Path, Path | None] = {}
    contracts: dict[str, CompletionContract] = {}
    scopes: dict[str, SessionScope] = {}

    for session in sessions:
        session_id = session.session_id
        writes: dict[str, None] = {}
        if session.contract is not None:
            rewritten = _rewrite_contract(
                session.contract,
                bound_root=bound_root,
                alias_by_root=alias_by_root,
                git_root_cache=git_root_cache,
                writes=writes,
            )
            if isinstance(rewritten, RegistrationResult):
                return replace(rewritten, workspace_root=workspace_root)
            contracts[session_id] = rewritten
        scopes[session_id] = _session_scope(
            writes,
            prose=repository_scope_targets.extract_path_targets(session.text),
            session_id=session_id,
            bound_root=bound_root,
            alias_by_root=alias_by_root,
            registry=registry,
        )

    return RegistrationPlan(
        workspace_root=workspace_root,
        registry=registry,
        scopes=scopes,
        contracts=contracts,
    )


def register_repositories(
    plan_dir: str | Path, *, cwd: str | Path, now: datetime | None = None
) -> RegistrationResult:
    """Apply the derived registry migration to ``plan_dir``, atomically.

    Every edited region is one the registry format defines: the manifest's
    ``## Repositories`` block, each session's scope lines, and each session's
    completion contract. Everything else stays byte-identical, and a refusal
    from :func:`derive_registration` writes nothing at all.
    """
    canonical_dir = canonicalize_path(plan_dir)
    derived = derive_registration(canonical_dir, cwd=cwd)
    if isinstance(derived, RegistrationResult):
        return derived

    previous_readiness = plans_readiness.read_readiness_metadata(canonical_dir)
    manifest_path = canonical_dir / "manifest.md"
    staged: list[tuple[str, str]] = [
        (
            "manifest.md",
            _insert_registry_table(
                manifest_path.read_text(encoding="utf-8"), derived.registry
            ),
        )
    ]
    sessions_changed: list[str] = []
    # Staging is in-memory, so a failure anywhere in this pass leaves the plan
    # directory untouched: only the single commit below writes bytes.
    for session in plans_discovery.discover_session_files(canonical_dir):
        original = session.path.read_text(encoding="utf-8")
        migrated = _migrate_session(
            original,
            scope=derived.scopes[session.id],
            contract=derived.contracts.get(session.id),
            registry=derived.registry,
        )
        if migrated != original:
            staged.append((session.filename, migrated))
            sessions_changed.append(session.id)

    transaction = authoring_transaction.PlanTransaction(canonical_dir)
    for relative_path, text in staged:
        transaction.stage(relative_path, text)
    files = transaction.commit()

    readiness = authoring_transaction.refresh_readiness_metadata(
        canonical_dir,
        state="draft" if previous_readiness is None else previous_readiness.state,
        now=now,
    )
    return RegistrationResult(
        status="applied",
        workspace_root=derived.workspace_root,
        repositories=tuple(derived.registry),
        sessions_changed=tuple(sessions_changed),
        files=files,
        readiness=readiness.readiness_sha256,
        next_commands=(
            shlex.join(
                [
                    "asl",
                    "prepare",
                    str(canonical_dir),
                    "--resume",
                    "--rebind-workspace",
                    "--cwd",
                    str(derived.workspace_root),
                ]
            ),
            shlex.join(
                [
                    "asl",
                    "run",
                    str(canonical_dir),
                    "--cwd",
                    str(derived.workspace_root),
                ]
            ),
        ),
    )
