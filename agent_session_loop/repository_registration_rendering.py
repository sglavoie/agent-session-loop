# ruff: noqa: F403, F405
from .repository_registration_models import *
from .repository_registration_models import _HORIZONTAL_RULE_RE, _contracts


def _insert_registry_table(text: str, registry: RepositoryRegistry) -> str:
    """Place ``## Repositories`` exactly where ``render_manifest`` places it."""
    lines = text.splitlines()
    index = next(
        (position for position, line in enumerate(lines) if line.startswith("## ")),
        len(lines),
    )
    return _render_lines(
        _registry_lines(
            lines,
            authoring_manifest.render_repository_registry_table(registry)
            .rstrip("\n")
            .splitlines(),
            index,
        ),
        text,
    )


def _source_registry_table(registry: RepositoryRegistry) -> list[str]:
    """Render the registry with origins spelled as URLs a source plan accepts.

    A stored origin is the transport-neutral identity ``host/path``, which
    ``workspaces.canonicalize_declared_origin`` rejects: a source plan must
    declare a real Git URL. ``https://`` spells that same identity back
    without inventing a user, and canonicalizes to the value it came from.
    """
    return [
        "| Alias | Path | Origin |",
        "| --- | --- | --- |",
        *(
            f"| {repository.alias} | {repository.path} | https://{repository.origin} |"
            for repository in registry
        ),
    ]


def _registry_lines(lines: list[str], table: list[str], index: int) -> list[str]:
    """Splice the registry block in above ``index``, spaced like a renderer."""
    block = [REPOSITORIES_HEADING, "", *table]
    if index > 0 and lines[index - 1].strip():
        block.insert(0, "")
    if index < len(lines):
        block.append("")
    return lines[:index] + block + lines[index:]


def _migrate_source_plan(
    text: str, parsed: authoring_source_models.ParsedPlan, plan: RegistrationPlan
) -> tuple[str, tuple[str, ...]]:
    """Rewrite a source plan's sessions and give it a registry."""
    lines = text.splitlines()
    bounds = _source_session_bounds(lines, parsed)
    changed: list[str] = []
    # Reverse order so an earlier session's rewrite cannot shift a later
    # session's bounds before they are used.
    for session_id, start, end in reversed(bounds):
        original = lines[start:end]
        migrated = _migrate_session_lines(
            original,
            scope=plan.scopes[session_id],
            contract=plan.contracts.get(session_id),
            registry=plan.registry,
        )
        if migrated != original:
            changed.append(session_id)
        lines[start:end] = migrated

    index = bounds[0][1] if bounds else len(lines)
    return (
        _render_lines(
            _registry_lines(lines, _source_registry_table(plan.registry), index),
            text,
        ),
        tuple(reversed(changed)),
    )


def _source_session_bounds(
    lines: list[str], parsed: authoring_source_models.ParsedPlan
) -> list[tuple[str, int, int]]:
    """Locate each session as ``(id, heading index, end)`` in the source lines.

    :func:`authoring_parse.parse_plan_document` partitions a document into a leading
    context region and one heading-plus-body chunk per session, so the chunks
    end at the last line and the first heading sits at the only offset that
    makes that true.
    """
    cursor = len(lines) - sum(len(session.lines) + 1 for session in parsed.sessions)
    bounds: list[tuple[str, int, int]] = []
    for session in parsed.sessions:
        end = cursor + 1 + len(session.lines)
        bounds.append((session.session_id.value, cursor, end))
        cursor = end
    return bounds


def _migrate_session(
    text: str,
    *,
    scope: SessionScope,
    contract: CompletionContract | None,
    registry: RepositoryRegistry,
) -> str:
    """Rewrite one session file's scope lines and completion contract."""
    return _render_lines(
        _migrate_session_lines(
            text.splitlines(),
            scope=scope,
            contract=contract,
            registry=registry,
        ),
        text,
    )


def _migrate_session_lines(
    lines: list[str],
    *,
    scope: SessionScope,
    contract: CompletionContract | None,
    registry: RepositoryRegistry,
) -> list[str]:
    """Rewrite one session, whether it is a file or a source-plan section."""
    migrated = list(plans_session_scope.strip_session_scope_lines(lines))
    if contract is not None:
        migrated = _splice_contract(migrated, contract, registry)
    return _insert_scope(migrated, scope)


def _insert_scope(lines: list[str], scope: SessionScope) -> list[str]:
    """Insert scope lines below the context and previous-session references."""
    index = 1 if lines and lines[0].startswith("#") else 0
    cursor = index
    while cursor < len(lines) and (
        not lines[cursor].strip() or lines[cursor].startswith(">")
    ):
        if lines[cursor].startswith(">"):
            index = cursor + 1
        cursor += 1
    # Stripping an existing scope block leaves the blank lines that framed it.
    while index + 1 < len(lines) and not (
        lines[index].strip() or lines[index + 1].strip()
    ):
        del lines[index]

    block = [
        "",
        *plans_session_scope.render_session_scope(scope).rstrip("\n").splitlines(),
    ]
    if index < len(lines) and lines[index].strip():
        block.append("")
    return lines[:index] + block + lines[index:]


def _splice_contract(
    lines: list[str],
    contract: CompletionContract,
    registry: RepositoryRegistry,
) -> list[str]:
    """Re-render the contract in place, using the parser's own section bounds."""
    handoff_start = _contracts._final_handoff_start(lines)
    end = handoff_start if handoff_start is not None else len(lines)
    start = _contracts._find_heading_index(
        lines, _contracts.COMPLETION_CONTRACT_HEADING, 0, end
    )
    if start is None:
        return lines
    stop = _contracts._section_end(lines, start, max_level=2, end=end)
    rendered = (
        authoring_render.render_completion_contract(
            contract, repository_registry=registry
        )
        .rstrip("\n")
        .splitlines()
    )
    return lines[:start] + rendered + _separator_tail(lines[start:stop]) + lines[stop:]


def _separator_tail(block: list[str]) -> list[str]:
    """Return the block's trailing blank lines and horizontal rules.

    The parser's section bound stops at the next heading, so it swallows the
    separator that follows a contract. Re-emitting that suffix keeps a
    migrated session file shaped like a freshly rendered one.
    """
    index = len(block)
    while index > 0 and (
        not block[index - 1].strip() or _HORIZONTAL_RULE_RE.match(block[index - 1])
    ):
        index -= 1
    return block[index:]


def _render_lines(lines: list[str], original: str) -> str:
    return "\n".join(lines) + ("\n" if original.endswith("\n") else "")


def _session_scope(
    writes: Mapping[str, None],
    *,
    prose: tuple[repository_scope_targets.PathTarget, ...],
    session_id: str,
    bound_root: Path,
    alias_by_root: Mapping[Path, str],
    registry: RepositoryRegistry,
) -> SessionScope:
    """Writes come from the contract; reads add whatever the prose names.

    A contract target is a location the session must produce, so it is the
    only evidence strong enough to widen the authoritative write scope.
    """
    suggestion = repository_scope_registry.suggest_scopes(
        {session_id: prose}, root=bound_root, alias_by_root=alias_by_root
    )[session_id]
    reads = dict.fromkeys((*writes, *suggestion.reads))
    # A session with neither a contract nor a resolvable path names no
    # repository at all. Reads are advisory, so the whole registry is the
    # honest answer; writes stay empty and keep failing closed at run time.
    if not reads:
        reads = dict.fromkeys(registry.aliases)
    order = {alias: index for index, alias in enumerate(registry.aliases)}
    return SessionScope(
        reads=tuple(sorted(reads, key=order.__getitem__)),
        writes=tuple(sorted(writes, key=order.__getitem__)),
    )


def _rewrite_contract(
    contract: CompletionContract,
    *,
    bound_root: Path,
    alias_by_root: Mapping[Path, str],
    git_root_cache: dict[Path, Path | None],
    writes: dict[str, None],
) -> CompletionContract | RegistrationResult:
    """Give every check a ``repo`` alias and a repository-relative location."""
    checks: list[VerificationCheck] = []
    unlocated: list[tuple[int, CommandCheck]] = []

    for check in contract.verify.checks:
        if isinstance(check, CommandCheck):
            if check.cwd is None:
                unlocated.append((len(checks), check))
                checks.append(check)
                continue
            location = check.cwd
        else:
            location = check.path
        located = _attribute(
            location,
            bound_root=bound_root,
            alias_by_root=alias_by_root,
            git_root_cache=git_root_cache,
        )
        if located is None:
            return _refused(
                "contract-path-outside-registry",
                detail=(
                    f"session {contract.session_id.value} check {check.name!r} "
                    "names a location outside every registered repository"
                ),
            )
        alias, relative = located
        writes[alias] = None
        checks.append(
            replace(check, repo=alias, cwd=relative)
            if isinstance(check, CommandCheck)
            else replace(check, repo=alias, path=relative)
        )

    # Snapshotted before the loop so one inferred alias cannot change what
    # "the session's sole write repository" means for the next check.
    sole_write = next(iter(writes)) if len(writes) == 1 else None
    for index, command in unlocated:
        inferred = _argv_alias(
            command.argv,
            bound_root=bound_root,
            alias_by_root=alias_by_root,
            git_root_cache=git_root_cache,
        )
        command_alias = inferred if inferred is not None else sole_write
        if command_alias is None:
            return _refused(
                "command-check-repo",
                detail=(
                    f"session {contract.session_id.value} check "
                    f"{command.name!r} names no repository: it has no cwd, no "
                    "absolute argv path in one repository, and its session "
                    "writes no single repository"
                ),
            )
        writes[command_alias] = None
        checks[index] = replace(command, repo=command_alias)

    return replace(
        contract,
        verify=VerifyRecipe(checks=tuple(checks), parallel=contract.verify.parallel),
    )


def _attribute(
    value: str,
    *,
    bound_root: Path,
    alias_by_root: Mapping[Path, str],
    git_root_cache: dict[Path, Path | None],
) -> tuple[str, str] | None:
    """Resolve one declared location to ``(alias, repository-relative path)``.

    ``nearest_git_root`` walks up from the location, so a file a session has
    still to create names its owning repository through the directory that
    already exists.
    """
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = bound_root / candidate
    canonical = canonicalize_filesystem_path(candidate)
    git_root = repository_scope.nearest_git_root(canonical, git_root_cache)
    if git_root is None:
        return None
    alias = alias_by_root.get(git_root)
    if alias is None:
        return None
    return alias, canonical.relative_to(git_root).as_posix()


def _argv_alias(
    argv: tuple[str, ...],
    *,
    bound_root: Path,
    alias_by_root: Mapping[Path, str],
    git_root_cache: dict[Path, Path | None],
) -> str | None:
    """Return the one repository this command's absolute arguments name."""
    aliases: dict[str, None] = {}
    for argument in argv:
        if not argument.startswith(("/", "~/")):
            continue
        if repository_scope.is_system_path(Path(os.path.expanduser(argument))):
            continue
        located = _attribute(
            argument,
            bound_root=bound_root,
            alias_by_root=alias_by_root,
            git_root_cache=git_root_cache,
        )
        if located is not None:
            aliases[located[0]] = None
    return next(iter(aliases)) if len(aliases) == 1 else None


def _refused(
    missing_fact: MissingFact,
    *,
    detail: str,
    workspace_root: Path | None = None,
) -> RegistrationResult:
    return RegistrationResult(
        status="refused",
        missing_fact=missing_fact,
        detail=detail,
        workspace_root=workspace_root,
    )


def _join(paths: tuple[Path, ...]) -> str:
    return ", ".join(str(path) for path in paths)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
