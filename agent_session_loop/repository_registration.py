"""Derive the registry migration a legacy multi-repository plan needs.

A plan that reaches two Git repositories without a ``## Repositories`` table
cannot execute: nothing snapshots the second repository. ASL already knows
every fact needed to write that table — the repositories, their origins, the
workspace root above them, and which repository owns each contract target —
so the migration is derivable rather than a manual repair.

Deciding and writing are separate. :func:`derive_registration` touches no
file, so the fail-closed taxonomy is directly testable; it returns one of two
outcomes, never a third:

* :class:`RegistrationPlan` — everything a writer needs: the workspace root,
  the ordered :class:`~.workspaces.RepositoryRegistry`, one
  :class:`~.plans.SessionScope` per session, and each session's contract
  rewritten so every check names its owning repository with a
  repository-relative location.
* :class:`RegistrationResult` — the migration is unnecessary
  (``already-registered``, ``not-required``) or impossible (``refused``,
  naming the one fact ASL could not observe).

:func:`register_repositories` applies a :class:`RegistrationPlan` to disk in
one :class:`~.authoring.PlanTransaction`, editing only the regions the
registry format defines and leaving every other byte — prose, handoffs,
``plan-metadata.json`` — verbatim.

The same gate fires in ``asl split --check``, before any plan directory
exists. :func:`register_source_plan` runs the identical derivation over a
source plan's text and returns the migrated markdown on the result, because
nothing may write to an operator's original plan file.

A refusal writes zero bytes and never invents a placeholder. A registry row
without an origin fails validation, so a plan carrying one could not run
anyway: guessing would only move the failure later. A part-way plan refuses
for the same reason: a registry makes a completed handoff's bare
``Files modified`` paths illegal, and applying it would leave a plan nothing
can parse.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import (
    authoring,
    plans,
    preflight_footprint,
    repository_intent,
    repository_scope,
    responses,
    state,
)
from . import (
    contracts as _contracts,
)
from .contracts import (
    CommandCheck,
    CompletionContract,
    VerificationCheck,
    VerifyRecipe,
    read_completion_contract,
)
from .plans import SessionScope, canonicalize_path, inspect_plan
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    canonicalize_filesystem_path,
)

REPOSITORIES_HEADING = "## Repositories"

_HORIZONTAL_RULE_RE = re.compile(r"^ {0,3}([-*_])(?: *\1){2,} *$")


RegistrationStatus = Literal["applied", "already-registered", "not-required", "refused"]

MissingFact = Literal[
    "workspace-root",
    "repository-origin",
    "contract-path-outside-registry",
    "command-check-repo",
    "completed-handoff-paths",
]

#: The complete status vocabulary, in a stable order for reports and tests.
REGISTRATION_STATUSES: tuple[str, ...] = (
    "applied",
    "already-registered",
    "not-required",
    "refused",
)

#: Every fact whose absence refuses the migration, in a stable order.
MISSING_FACTS: tuple[str, ...] = (
    "workspace-root",
    "repository-origin",
    "contract-path-outside-registry",
    "command-check-repo",
    "completed-handoff-paths",
)


@dataclass(frozen=True)
class RegistrationPlan:
    """The computed migration, derived entirely from disk."""

    workspace_root: Path
    registry: RepositoryRegistry
    scopes: Mapping[str, SessionScope]
    contracts: Mapping[str, CompletionContract]


@dataclass(frozen=True)
class RegistrationResult:
    """The outcome of a migration that was refused, skipped, or applied.

    ``markdown`` carries source-plan mode's output: the migrated plan when the
    registry was applied, the input verbatim when no migration was needed, and
    ``None`` for a refusal or for plan-directory mode, which writes files.
    """

    status: RegistrationStatus
    missing_fact: MissingFact | None = None
    detail: str = ""
    workspace_root: Path | None = None
    repositories: tuple[Repository, ...] = ()
    sessions_changed: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()
    readiness: str | None = None
    next_commands: tuple[str, ...] = ()
    markdown: str | None = None


@dataclass(frozen=True)
class _SessionSource:
    """One session's text and parsed contract, whatever holds them."""

    session_id: str
    text: str
    contract: CompletionContract | None


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
    handoffs: list[plans.SessionHandoff] = []
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
            plans.read_session_handoff(status.session, repository_registry=None)
        )

    derived = _derive(intent, sessions, bound_root=bound_root)
    if isinstance(derived, RegistrationPlan):
        refusal = _completed_handoff_refusal(handoffs, derived)
        if refusal is not None:
            return refusal
    return derived


def _completed_handoff_refusal(
    handoffs: list[plans.SessionHandoff], derived: RegistrationPlan
) -> RegistrationResult | None:
    """Refuse a migration that would leave a completed handoff unparsable.

    A registry makes every entry in a completed ``Files modified`` list an
    ``Alias:path`` the parser validates, so migrating a plan whose finished
    sessions recorded bare paths would stop the plan from being read at all.
    The rule is :func:`plans.completed_handoff_registry_blocker`, asked with
    the registry and scopes this migration would write; every other surface
    reaches it by running this derivation rather than by repeating it.
    Source-plan mode has no handoffs to invalidate, so it never reaches this.
    """
    for handoff in handoffs:
        blocker = plans.completed_handoff_registry_blocker(
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
    parsed = authoring.parse_plan_document(text)
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
    workspace_root = repository_scope.suggest_workspace_root(intent.git_roots)
    if workspace_root is None:
        return _refused(
            "workspace-root",
            detail=(
                "repositories have no shared parent that could serve as a "
                "workspace root: " + _join(intent.git_roots)
            ),
        )
    try:
        suggestions = repository_scope.suggest_repositories(
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
        registry = repository_scope.suggest_registry(workspace_root, intent.git_roots)
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
            prose=repository_scope.extract_path_targets(session.text),
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

    previous_readiness = plans.read_readiness_metadata(canonical_dir)
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
    for session in plans.discover_session_files(canonical_dir):
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

    transaction = authoring.PlanTransaction(canonical_dir)
    for relative_path, text in staged:
        transaction.stage(relative_path, text)
    files = transaction.commit()

    readiness = authoring.refresh_readiness_metadata(
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
            authoring.render_repository_registry_table(registry)
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
    text: str, parsed: authoring.ParsedPlan, plan: RegistrationPlan
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
    lines: list[str], parsed: authoring.ParsedPlan
) -> list[tuple[str, int, int]]:
    """Locate each session as ``(id, heading index, end)`` in the source lines.

    :func:`authoring.parse_plan_document` partitions a document into a leading
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
    migrated = list(plans.strip_session_scope_lines(lines))
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

    block = ["", *plans.render_session_scope(scope).rstrip("\n").splitlines()]
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
        authoring.render_completion_contract(contract, repository_registry=registry)
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
    prose: tuple[repository_scope.PathTarget, ...],
    session_id: str,
    bound_root: Path,
    alias_by_root: Mapping[Path, str],
    registry: RepositoryRegistry,
) -> SessionScope:
    """Writes come from the contract; reads add whatever the prose names.

    A contract target is a location the session must produce, so it is the
    only evidence strong enough to widen the authoritative write scope.
    """
    suggestion = repository_scope.suggest_scopes(
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
        alias = _argv_alias(
            command.argv,
            bound_root=bound_root,
            alias_by_root=alias_by_root,
            git_root_cache=git_root_cache,
        )
        if alias is None:
            alias = sole_write
        if alias is None:
            return _refused(
                "command-check-repo",
                detail=(
                    f"session {contract.session_id.value} check "
                    f"{command.name!r} names no repository: it has no cwd, no "
                    "absolute argv path in one repository, and its session "
                    "writes no single repository"
                ),
            )
        writes[alias] = None
        checks[index] = replace(command, repo=alias)

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


REGISTER_REASON_OK = "ok"
REGISTER_REASON_REFUSED = "refused"
REGISTER_REASON_PLAN_STRUCTURE = "plan-structure"
REGISTER_REASON_LOCK_CONTENTION = "lock-contention"
REGISTER_REASON_INPUT_ERROR = "input-error"


def result_payload(result: RegistrationResult) -> dict[str, Any]:
    """Serialize a :class:`RegistrationResult` for the ``--json`` surface."""
    return {
        "status": result.status,
        "missing_fact": result.missing_fact,
        "detail": result.detail,
        "workspace_root": (
            None if result.workspace_root is None else str(result.workspace_root)
        ),
        "repositories": [repository.to_dict() for repository in result.repositories],
        "sessions_changed": list(result.sessions_changed),
        "files": [str(path) for path in result.files],
        "readiness": result.readiness,
        "next_commands": list(result.next_commands),
        "markdown": result.markdown,
    }


def _emit_register_error(
    reason: str, error: str, *, want_json: bool, **fields: Any
) -> int:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "register-repositories",
                reason=reason,
                error=error,
                **fields,
            )
        )
    else:
        print(
            f"agent-session-loop register-repositories: {error}",
            file=sys.stderr,
        )
    return 1


def register_repositories_from_args(args: Any) -> int:
    """Run the ``register-repositories`` subcommand from CLI arguments."""
    source_mode = bool(args.plan_file) or bool(args.inline_stdin)
    if source_mode == (args.plan_dir is not None):
        return _emit_register_error(
            REGISTER_REASON_INPUT_ERROR,
            "give exactly one of PLAN_DIR, --plan-file, or --inline-stdin",
            want_json=bool(args.json),
        )
    if source_mode:
        return _register_source_plan_from_args(args)
    return _register_plan_dir_from_args(args)


def _register_plan_dir_from_args(args: Any) -> int:
    """Migrate a split plan directory in place."""
    want_json = bool(args.json)
    plan_dir = str(args.plan_dir)

    def _emit_error(reason: str, error: str, **fields: Any) -> int:
        return _emit_register_error(
            reason, error, want_json=want_json, plan_dir=plan_dir, **fields
        )

    if args.output is not None:
        return _emit_error(
            REGISTER_REASON_INPUT_ERROR,
            "--output belongs to source-plan mode; a plan directory is "
            "migrated in place",
        )

    # The migration rewrites every session file, and PlanTransaction leaves
    # serializing writers to its caller, so a live runner owns the plan first.
    lock_info = state.inspect_lock_info(plan_dir, args.state_dir)
    if lock_info is not None and lock_info.active:
        return _emit_error(
            REGISTER_REASON_LOCK_CONTENTION,
            f"plan is locked by pid {lock_info.pid} at {lock_info.path}; "
            "register the registry once that run has finished",
        )

    try:
        result = register_repositories(plan_dir, cwd=args.cwd)
    except plans.PlanError as error:
        return _emit_error(REGISTER_REASON_PLAN_STRUCTURE, str(error))

    payload = result_payload(result)
    if result.status == "refused":
        return _emit_error(
            REGISTER_REASON_REFUSED,
            f"missing fact: {result.missing_fact}: {result.detail}",
            **payload,
        )

    if want_json:
        responses.print_json(
            responses.success_payload(
                "register-repositories",
                reason=REGISTER_REASON_OK,
                plan_dir=plan_dir,
                **payload,
            )
        )
        return 0

    print(f"Plan directory: {plan_dir}")
    print(f"Status: {result.status}")
    if result.status != "applied":
        print(result.detail)
        return 0
    print(f"Workspace root: {result.workspace_root}")
    for line in repository_scope.registry_table_lines(result.repositories):
        print(line)
    print("Changed files:")
    for path in result.files:
        print(f"  {path}")
    print("Run next, in this order:")
    for command in result.next_commands:
        print(f"  {command}")
    return 0


def _register_source_plan_from_args(args: Any) -> int:
    """Emit a migrated source plan, never writing over its input."""
    want_json = bool(args.json)
    output = None if args.output is None else os.path.expanduser(str(args.output))

    def _emit_error(reason: str, error: str, **fields: Any) -> int:
        return _emit_register_error(
            reason, error, want_json=want_json, source_path=source_path, **fields
        )

    if args.inline_stdin:
        source_path = "<stdin>"
        try:
            text = authoring.extract_inline_plan(sys.stdin.read())
        except ValueError as error:
            return _emit_error(REGISTER_REASON_INPUT_ERROR, str(error))
    else:
        source_path = os.path.expanduser(str(args.plan_file))
        if not Path(source_path).is_file():
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                f"source plan file does not exist: {args.plan_file}",
            )
        if output is not None and os.path.realpath(output) == os.path.realpath(
            source_path
        ):
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                "--output names the input plan; the migrated markdown never "
                "replaces its source",
            )
        try:
            text = Path(source_path).read_text(encoding="utf-8")
        except OSError as error:
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                f"cannot read source plan {source_path}: {error}",
            )

    try:
        result = register_source_plan(text, cwd=args.cwd)
    except plans.PlanError as error:
        return _emit_error(REGISTER_REASON_PLAN_STRUCTURE, str(error))

    payload = result_payload(result)
    if result.status == "refused":
        return _emit_error(
            REGISTER_REASON_REFUSED,
            f"missing fact: {result.missing_fact}: {result.detail}",
            **payload,
        )

    if output is not None:
        try:
            Path(output).write_text(result.markdown or "", encoding="utf-8")
        except OSError as error:
            return _emit_error(
                REGISTER_REASON_INPUT_ERROR,
                f"cannot write migrated plan {output}: {error}",
            )

    if want_json:
        responses.print_json(
            responses.success_payload(
                "register-repositories",
                reason=REGISTER_REASON_OK,
                source_path=source_path,
                output_path=output,
                **payload,
            )
        )
        return 0

    # Without --output the markdown is the whole of stdout, so a caller may
    # pipe it straight into `asl split`.
    if output is None:
        print(result.markdown or "", end="")
        return 0

    print(f"Source plan: {source_path}")
    print(f"Status: {result.status}")
    print(f"Migrated plan: {output}")
    if result.status != "applied":
        print(result.detail)
        return 0
    print(f"Workspace root: {result.workspace_root}")
    for line in repository_scope.registry_table_lines(result.repositories):
        print(line)
    return 0
