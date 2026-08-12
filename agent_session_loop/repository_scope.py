"""Deterministic repository-footprint detection for plan documents.

A plan that names locations in more than one Git repository, or whose
completion contracts point outside the root it is bound to, cannot execute
safely in legacy single-repository mode: nothing snapshots the other
repositories, so a stray write there is invisible to the workspace guard.

This module turns plan text and contract targets into that verdict, so
``asl split``, ``asl prepare``, ``asl preflight``, and the runner all reach
the same conclusion from the same evidence instead of each re-deriving it.

Two layers with deliberately different dependencies:

* :func:`extract_path_targets` and :func:`targets_from_checks` are pure. They
  find path-shaped values and classify each as ``hard`` (an absolute path, a
  ``~/`` path, a qualified ``Alias:path`` handoff entry, or a contract target)
  or ``soft`` (bare folder-shaped prose).
* :func:`resolve_footprint` needs a filesystem. It resolves each target,
  finds its owning Git worktree, and reports the verdict.

Only ``hard`` targets can force a refusal. ``soft`` prose is advisory and
feeds :attr:`RepositoryFootprint.ambiguous`, which tells a caller to ask a
human rather than guess an alias.

Escaping the bound root is not by itself a refusal. As in
:mod:`repository_intent`, a target that resolves outside any Git worktree is
classified separately, in :attr:`RepositoryFootprint.external`, and never
forces a registry: external artifacts are legitimately non-Git, and a registry
row can only describe a Git repository, so demanding one is unsatisfiable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Literal

from .contracts import (
    CommandCheck,
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
)
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    canonicalize_declared_origin,
    canonicalize_filesystem_path,
    is_descendant,
    normalize_repository_path,
    validate_repository_alias,
)


TargetKind = Literal["contract-path", "contract-cwd", "handoff", "prose"]
TargetConfidence = Literal["hard", "soft"]

#: Absolute prefixes that never denote plan-owned source, so an interpreter or
#: toolchain path cannot trip the out-of-root gate. Matched against the path a
#: plan literally declares, not its real path: on macOS the temp directories
#: that hold real workspaces resolve under ``/private``.
SYSTEM_PATH_PREFIXES: tuple[str, ...] = (
    "/Applications",
    "/Library",
    "/System",
    "/bin",
    "/dev",
    "/etc",
    "/opt",
    "/proc",
    "/run",
    "/sbin",
    "/usr",
)

#: Rendered in a suggested registry row when a repository declares no origin
#: remote. Deliberately not URL-shaped so the row cannot be pasted unedited.
UNKNOWN_ORIGIN_PLACEHOLDER = "<add-origin-remote>"

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_PATH_SEGMENT = r"[A-Za-z0-9._+@%-]+"
_ABSOLUTE_PATH_RE = re.compile(
    rf"(?<![\w./~-])(?:~/|/)(?:{_PATH_SEGMENT}/)*{_PATH_SEGMENT}/?"
)
_HANDOFF_RE = re.compile(
    rf"(?<![\w./:-])([A-Za-z][A-Za-z0-9_-]*):(?!/)((?:{_PATH_SEGMENT}/)*{_PATH_SEGMENT})"
)
_RELATIVE_PATH_RE = re.compile(
    rf"(?<![\w./~:-])(?:{_PATH_SEGMENT}/)+{_PATH_SEGMENT}/?"
)
#: Sentence punctuation the segment grammar swallows when a path ends a clause.
_TRAILING_PUNCTUATION = ".,;:!?)]}'\"`"


@dataclass(frozen=True)
class PathTarget:
    """One path-shaped value found in a plan, with where it came from."""

    value: str
    kind: TargetKind
    line: int
    confidence: TargetConfidence

    def as_dict(self) -> dict[str, object]:
        return {
            "line": self.line,
            "text": self.value,
            "kind": self.kind,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AmbiguousTarget:
    """Folder-shaped prose that exists in more than one detected repository."""

    target: PathTarget
    candidates: tuple[Path, ...]


@dataclass(frozen=True)
class RepositoryFootprint:
    """Which repositories a plan touches, judged against one bound root.

    ``out_of_root`` holds only the escaping targets a registry could describe:
    each one is owned by some Git worktree. A target that escapes the bound
    root and belongs to no Git worktree lands in ``external`` instead, matching
    :mod:`repository_intent` — it is a legitimately non-Git artifact, and no
    registry row could ever name it.
    """

    root: Path
    repo_roots: tuple[Path, ...]
    out_of_root: tuple[PathTarget, ...]
    unresolved: tuple[PathTarget, ...]
    ambiguous: tuple[AmbiguousTarget, ...] = ()
    external: tuple[PathTarget, ...] = ()

    @property
    def registry_required(self) -> bool:
        """Whether this plan may no longer stay in legacy single-repo mode."""
        return len(self.repo_roots) > 1 or bool(self.out_of_root)


@dataclass(frozen=True)
class SessionScopeSuggestion:
    """Read and write aliases derived from one session's own path targets."""

    reads: tuple[str, ...]
    writes: tuple[str, ...]


@dataclass(frozen=True)
class RepositorySuggestion:
    """One proposed registry row, with ``origin`` unset when unobservable."""

    alias: str
    path: str
    origin: str | None
    root: Path


# ---------------------------------------------------------------------------
# Layer A: pure extraction
# ---------------------------------------------------------------------------


def extract_path_targets(text: str) -> tuple[PathTarget, ...]:
    """Extract path-shaped values from plan prose, skipping fenced blocks.

    Fenced blocks hold illustrative snippets, so a path there is not evidence.
    Real contract targets never depend on this scan; they arrive through
    :func:`targets_from_checks`.
    """
    targets: list[PathTarget] = []
    fence: str | None = None
    for index, line in enumerate(text.splitlines(), start=1):
        match = _FENCE_RE.match(line)
        if match is not None:
            delimiter = match.group(1)[0] * 3
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            continue
        if fence is not None:
            continue
        targets.extend(_extract_line_targets(line, index))
    return tuple(targets)


def _extract_line_targets(line: str, line_number: int) -> list[PathTarget]:
    found: list[PathTarget] = []
    consumed: list[tuple[int, int]] = []

    for match in _ABSOLUTE_PATH_RE.finditer(line):
        value = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not _is_path_shaped(value):
            continue
        consumed.append(match.span())
        found.append(
            PathTarget(
                value=value, kind="prose", line=line_number, confidence="hard"
            )
        )

    for match in _HANDOFF_RE.finditer(line):
        if _overlaps(match.span(), consumed):
            continue
        consumed.append(match.span())
        found.append(
            PathTarget(
                value=match.group(0).rstrip(_TRAILING_PUNCTUATION),
                kind="handoff",
                line=line_number,
                confidence="hard",
            )
        )

    for match in _RELATIVE_PATH_RE.finditer(line):
        if _overlaps(match.span(), consumed):
            continue
        value = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if "/" not in value:
            continue
        found.append(
            PathTarget(
                value=value,
                kind="prose",
                line=line_number,
                confidence="soft",
            )
        )

    return found


def _overlaps(span: tuple[int, int], consumed: Sequence[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in consumed)


def _is_path_shaped(value: str) -> bool:
    """Reject a bare root-level token such as ``/opt`` or a lone ``~/``."""
    trimmed = value.rstrip("/")
    if trimmed.startswith("~"):
        return len(trimmed) > 2
    return trimmed.count("/") >= 2


def targets_from_checks(
    checks: Iterable[VerificationCheck], *, line: int = 0
) -> tuple[PathTarget, ...]:
    """Turn contract check locations into hard targets.

    A contract target is the strongest available evidence: it is a location
    the session is required to produce or inspect, not a passing mention.
    """
    targets: list[PathTarget] = []
    for check in checks:
        if isinstance(check, CommandCheck):
            if check.cwd is not None:
                targets.append(
                    PathTarget(
                        value=check.cwd,
                        kind="contract-cwd",
                        line=line,
                        confidence="hard",
                    )
                )
        elif isinstance(check, (FileExistsCheck, RegexInFileCheck)):
            targets.append(
                PathTarget(
                    value=check.path,
                    kind="contract-path",
                    line=line,
                    confidence="hard",
                )
            )
    return tuple(targets)


def targets_from_contract(contract: CompletionContract) -> tuple[PathTarget, ...]:
    return targets_from_checks(contract.verify.checks)


# ---------------------------------------------------------------------------
# Layer B: filesystem resolution
# ---------------------------------------------------------------------------


def resolve_footprint(
    targets: Iterable[PathTarget],
    root: str | Path,
    *,
    plan_dir: str | Path | None = None,
) -> RepositoryFootprint:
    """Judge ``targets`` against ``root``, the single root the plan is bound to.

    ``plan_dir`` names the plan's own directory. ASL's generated review
    contracts verify plan artifacts that live there, so a target under it
    describes the plan, not a second repository.
    """
    bound_root = canonicalize_filesystem_path(root)
    plan_root = (
        canonicalize_filesystem_path(plan_dir) if plan_dir is not None else None
    )
    git_root_cache: dict[Path, Path | None] = {}
    repo_roots: dict[Path, None] = {}
    out_of_root: list[PathTarget] = []
    external: list[PathTarget] = []
    unresolved: list[PathTarget] = []
    soft_targets: list[tuple[PathTarget, Path]] = []

    for target in targets:
        resolved = _resolve_target(target, bound_root)
        if resolved is None:
            continue
        if plan_root is not None and is_descendant(
            resolved, plan_root, allow_equal=True
        ):
            continue
        if target.confidence == "soft":
            soft_targets.append((target, resolved))
            continue
        if not resolved.exists():
            unresolved.append(target)
            continue
        git_root = nearest_git_root(resolved, git_root_cache)
        if not is_descendant(resolved, bound_root, allow_equal=True):
            if git_root is None:
                external.append(target)
            else:
                out_of_root.append(target)
        if git_root is not None:
            repo_roots[git_root] = None

    ordered_roots = tuple(sorted(repo_roots, key=str))
    ambiguous = _ambiguous_targets(soft_targets, ordered_roots)
    return RepositoryFootprint(
        root=bound_root,
        repo_roots=ordered_roots,
        out_of_root=tuple(out_of_root),
        unresolved=tuple(unresolved),
        ambiguous=ambiguous,
        external=tuple(external),
    )


def _ambiguous_targets(
    soft_targets: Sequence[tuple[PathTarget, Path]],
    repo_roots: Sequence[Path],
) -> tuple[AmbiguousTarget, ...]:
    """Report folder-shaped prose that resolves inside two or more repositories."""
    if len(repo_roots) < 2:
        return ()
    ambiguous: list[AmbiguousTarget] = []
    for target, _resolved in soft_targets:
        relative = target.value.strip("/")
        if not relative:
            continue
        candidates = tuple(
            repo_root
            for repo_root in repo_roots
            if (repo_root / relative).exists()
        )
        if len(candidates) > 1:
            ambiguous.append(AmbiguousTarget(target=target, candidates=candidates))
    return tuple(ambiguous)


def _resolve_target(target: PathTarget, root: Path) -> Path | None:
    """Resolve one target, or ``None`` when it names a system location."""
    value = target.value
    if target.kind == "handoff":
        _, _, value = value.partition(":")
    if not value:
        return None
    candidate = Path(os.path.expanduser(value))
    if not candidate.is_absolute():
        candidate = root / candidate
    if is_system_path(candidate):
        return None
    return canonicalize_filesystem_path(candidate)


def is_system_path(path: Path) -> bool:
    """Return whether ``path`` names an interpreter or toolchain location."""
    text = Path(os.path.abspath(path)).as_posix()
    return any(
        text == prefix or text.startswith(f"{prefix}/")
        for prefix in SYSTEM_PATH_PREFIXES
    )


def nearest_git_root(
    path: Path, cache: dict[Path, Path | None] | None = None
) -> Path | None:
    """Walk up to the nearest ``.git`` marker without invoking Git."""
    if cache is None:
        cache = {}
    start = path if path.is_dir() else path.parent
    chain: list[Path] = []
    current = start
    found: Path | None = None
    while True:
        if current in cache:
            found = cache[current]
            break
        chain.append(current)
        marker = current / ".git"
        try:
            mode = marker.lstat().st_mode
        except OSError:
            mode = None
        if mode is not None and (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            found = current
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    for entry in chain:
        cache[entry] = found
    return found


# ---------------------------------------------------------------------------
# Layer C: registry suggestion
# ---------------------------------------------------------------------------


def suggest_workspace_root(repo_roots: Sequence[Path]) -> Path | None:
    """Return the shallowest directory containing every detected repository.

    ``None`` when the repositories have no usable common ancestor, or when one
    contains another: a registry forbids overlapping roots, so that topology
    needs a human decision rather than a generated table.
    """
    roots = tuple(
        dict.fromkeys(canonicalize_filesystem_path(root) for root in repo_roots)
    )
    if not roots:
        return None
    if len(roots) == 1:
        parent = roots[0].parent
        return parent if parent != roots[0] else None
    common = Path(os.path.commonpath([str(root) for root in roots]))
    if common in roots or common == common.parent:
        return None
    return common


def suggest_repositories(
    workspace_root: str | Path, repo_roots: Sequence[Path]
) -> tuple[RepositorySuggestion, ...]:
    """Describe each detected root as a registry row, inventing nothing.

    Aliases come from directory names, paths from the workspace-relative
    location, and origins from each repository's own ``origin`` remote. A
    repository with no usable origin keeps its row with ``origin=None``: the
    alias and path are still derived from disk, and only the one field ASL
    cannot observe is left for a human to fill in.
    """
    root = canonicalize_filesystem_path(workspace_root)
    suggestions: list[RepositorySuggestion] = []
    used: set[str] = set()
    for repo_root in sorted(dict.fromkeys(repo_roots), key=str):
        canonical = canonicalize_filesystem_path(repo_root)
        if not is_descendant(canonical, root):
            raise WorkspaceError(
                f"repository {canonical} is not inside the workspace root {root}"
            )
        suggestions.append(
            RepositorySuggestion(
                alias=_unique_alias(canonical, used),
                path=normalize_repository_path(
                    canonical.relative_to(root).as_posix()
                ),
                origin=_origin_of(canonical),
                root=canonical,
            )
        )
    return tuple(suggestions)


def suggest_registry(
    workspace_root: str | Path, repo_roots: Sequence[Path]
) -> RepositoryRegistry:
    """Build a validated registry from detected roots.

    Raises :class:`WorkspaceError` when a repository has no usable origin
    remote, because a registry entry without one cannot be validated.
    """
    repositories: list[Repository] = []
    for suggestion in suggest_repositories(workspace_root, repo_roots):
        if suggestion.origin is None:
            raise WorkspaceError(
                f"repository {suggestion.root} has no usable origin remote"
            )
        repositories.append(
            Repository(
                alias=suggestion.alias,
                path=suggestion.path,
                origin=suggestion.origin,
            )
        )
    return RepositoryRegistry(tuple(repositories))


def _unique_alias(repo_root: Path, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]", "_", repo_root.name).lstrip("_-")
    if not base or not base[0].isalpha():
        base = f"repo_{base}" if base else "repo"
    alias = validate_repository_alias(base)
    suffix = 2
    while alias in used:
        alias = f"{base}_{suffix}"
        suffix += 1
    used.add(alias)
    return alias


def _origin_of(repo_root: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        return canonicalize_declared_origin(completed.stdout.strip())
    except WorkspaceError:
        return None


def registry_table_lines(
    registry: Iterable[Repository | RepositorySuggestion],
) -> tuple[str, ...]:
    """Render an ordered registry as Markdown table lines.

    A suggested row whose origin could not be observed renders
    :data:`UNKNOWN_ORIGIN_PLACEHOLDER`, which is visibly not a URL so the table
    cannot be pasted without the human supplying the missing remote.
    """
    return (
        "| Alias | Path | Origin |",
        "| --- | --- | --- |",
        *(
            f"| {repository.alias} | {repository.path} | "
            f"{repository.origin or UNKNOWN_ORIGIN_PLACEHOLDER} |"
            for repository in registry
        ),
    )


def suggest_scopes(
    targets_by_session: Mapping[str, Sequence[PathTarget]],
    *,
    root: str | Path,
    alias_by_root: Mapping[Path, str],
) -> dict[str, SessionScopeSuggestion]:
    """Derive per-session read and write aliases from that session's targets.

    Reads cover every repository the session mentions. Writes cover only the
    repositories its completion contract targets, because a contract target is
    a location the session must produce. Under-declaring a write fails closed
    at run time, which is the safe direction to err in.
    """
    bound_root = canonicalize_filesystem_path(root)
    git_root_cache: dict[Path, Path | None] = {}
    suggestions: dict[str, SessionScopeSuggestion] = {}
    for session_id, targets in targets_by_session.items():
        reads: dict[str, None] = {}
        writes: dict[str, None] = {}
        for target in targets:
            if target.confidence != "hard":
                continue
            resolved = _resolve_target(target, bound_root)
            if resolved is None or not resolved.exists():
                continue
            git_root = nearest_git_root(resolved, git_root_cache)
            alias = alias_by_root.get(git_root) if git_root is not None else None
            if alias is None:
                continue
            reads[alias] = None
            if target.kind in {"contract-path", "contract-cwd"}:
                writes[alias] = None
        suggestions[session_id] = SessionScopeSuggestion(
            reads=tuple(reads), writes=tuple(writes)
        )
    return suggestions


def footprint_payload(
    targets_by_session: Mapping[str, Sequence[PathTarget]],
    footprint: RepositoryFootprint,
    *,
    registry_present: bool,
) -> dict[str, object] | None:
    """Serialize the footprint verdict, or ``None`` when there is nothing to say.

    This is the shared machine-readable contract behind ``asl split --check``
    and ``asl preflight``, so a skill pastes a table ASL derived from disk
    instead of inventing aliases, paths, or origins of its own.
    """
    if not footprint.registry_required:
        return None
    suggested_root = suggest_workspace_root(footprint.repo_roots)
    workspace_root = suggested_root or footprint.root
    try:
        suggestions = (
            suggest_repositories(suggested_root, footprint.repo_roots)
            if suggested_root is not None
            else ()
        )
    except WorkspaceError:
        suggestions = ()
    alias_by_root = {
        suggestion.root: suggestion.alias for suggestion in suggestions
    }
    evidence = _evidence_by_root(targets_by_session, footprint.root)
    detected = [
        {
            "alias": suggestion.alias,
            "path": suggestion.path,
            "origin": suggestion.origin,
            "root": str(suggestion.root),
            "evidence": [
                target.as_dict() for target in evidence.get(suggestion.root, ())
            ],
        }
        for suggestion in suggestions
    ]
    scopes = suggest_scopes(
        targets_by_session, root=footprint.root, alias_by_root=alias_by_root
    )
    return {
        "workspace_root": str(workspace_root),
        "bound_root": str(footprint.root),
        "registry_present": registry_present,
        "registry_required": True,
        "detected": detected,
        "out_of_root": [target.as_dict() for target in footprint.out_of_root],
        "external": [target.as_dict() for target in footprint.external],
        "unattributed": [
            {
                "line": entry.target.line,
                "text": entry.target.value,
                "candidates": [
                    alias_by_root[candidate]
                    for candidate in entry.candidates
                    if candidate in alias_by_root
                ],
            }
            for entry in footprint.ambiguous
        ],
        "suggested_registry_table": (
            list(registry_table_lines(suggestions)) if suggestions else []
        ),
        "suggested_scopes": {
            session_id: {
                "reads": list(scope.reads),
                "writes": list(scope.writes),
            }
            for session_id, scope in sorted(scopes.items())
        },
    }


def _evidence_by_root(
    targets_by_session: Mapping[str, Sequence[PathTarget]], root: Path
) -> dict[Path, tuple[PathTarget, ...]]:
    git_root_cache: dict[Path, Path | None] = {}
    evidence: dict[Path, list[PathTarget]] = {}
    for targets in targets_by_session.values():
        for target in targets:
            if target.confidence != "hard":
                continue
            resolved = _resolve_target(target, root)
            if resolved is None or not resolved.exists():
                continue
            git_root = nearest_git_root(resolved, git_root_cache)
            if git_root is None:
                continue
            evidence.setdefault(git_root, []).append(target)
    return {
        git_root: tuple(targets) for git_root, targets in evidence.items()
    }
