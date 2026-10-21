"""Reusable repository-intent extraction for plans, contracts, and packets.

:mod:`repository_scope` answers one narrow question — does a plan's prose and
contract targets stay inside the single root it is bound to? That question is
only asked where a plan document is available, and it only counts a path that
already exists on disk. A plan whose sessions create their files, or whose
locations arrive through plan metadata or a runner task packet, slipped past
it and executed in legacy single-repository mode against two repositories.

This module generalizes that evidence into a *repository intent*: every
location a plan or run intends to touch, wherever it was declared, resolved to
the Git top-level that would actually own the write.

Two layers with deliberately different dependencies:

* the ``candidates_from_*`` functions are pure. Each turns one declaration
  surface — plan prose, inline backtick spans, contract ``path``/``cwd``
  fields, plan metadata, execution roots, runner task-packet paths — into
  :class:`IntentCandidate` values that remember where they came from.
* :func:`resolve_intent` needs a filesystem. It resolves symlinks, walks to
  the nearest *existing* ancestor so a path a session has yet to create still
  names its owning repository, and classifies each candidate.

Two rules keep the verdict honest:

* a candidate that resolves outside any Git worktree is classified separately
  and never forces a registry. External artifacts are legitimately non-Git.
* repositories are identified by resolved root, never by origin. Two clones of
  one upstream are two repositories, and a write to the wrong one is exactly
  the accident this gate exists to stop.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import repository_scope, repository_scope_targets
from .workspaces import (
    bounded_repository_rows,
    canonicalize_filesystem_path,
    is_descendant,
)

#: Structured refusal reason shared by every surface that enforces this gate.
REASON_REPOSITORY_REGISTRY_REQUIRED = "repository-registry-required"

#: Where a candidate was declared. Kept in the report so an operator can see
#: which surface produced the evidence rather than only its resolved path.
SourceSurface = Literal[
    "absolute-path",
    "backtick-path",
    "contract-path",
    "contract-cwd",
    "plan-metadata",
    "execution-root",
    "task-packet",
]

#: Why a candidate cannot count toward the repository verdict. ``plan-artifact``
#: and ``plan-provenance`` are kept apart on purpose: the bounded report is an
#: operator's only window into a verdict, and conflating "ASL owns this tree"
#: with "this is where the plan came from" would make a wrongly exempted
#: candidate indistinguishable from runner bookkeeping during exactly the
#: incident where the two need telling apart.
Exemption = Literal[
    "system-path",
    "plan-artifact",
    "plan-provenance",
    "unresolved",
    "non-git",
]

#: Upper bound on the candidates, roots, and non-Git paths one report renders.
#: A broad plan can name hundreds of locations; the verdict never depends on
#: seeing all of them, so the report stays readable and bounded.
INTENT_REPORT_LIMIT = 40

#: Replacement for URL userinfo that carries a secret. A bare ``user@`` SCP
#: prefix carries none and is preserved.
REDACTED_CREDENTIAL = "***@"

#: Plan-document metadata fields whose value says where a plan *came from*
#: rather than naming anything it will touch. The document a plan was split
#: from is read-only by construction — the split and iterate skills refuse to
#: modify it — so the repository that happens to hold it is not evidence the
#: plan reaches two repositories. Operators routinely keep their source plans
#: in a versioned home directory, which would otherwise force a registry onto
#: every single-repository plan.
PROVENANCE_METADATA_FIELDS: tuple[str, ...] = ("Split from",)

_CREDENTIAL_RE = re.compile(r"[^\s/@:]+:[^\s/@]+@")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_PROVENANCE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*\*(?:"
    + "|".join(re.escape(field) for field in PROVENANCE_METADATA_FIELDS)
    + r")\*\*\s*:",
    re.IGNORECASE,
)


def redact_credentials(text: str) -> str:
    """Strip URL userinfo secrets from a value before it is reported."""
    return _CREDENTIAL_RE.sub(REDACTED_CREDENTIAL, text)


@dataclass(frozen=True)
class IntentCandidate:
    """One declared location, with the surface that declared it."""

    value: str
    surface: SourceSurface
    line: int = 0


@dataclass(frozen=True)
class ResolvedCandidate:
    """One candidate judged against the filesystem."""

    candidate: IntentCandidate
    resolved: Path | None
    git_root: Path | None
    exemption: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": redact_credentials(self.candidate.value),
            "surface": self.candidate.surface,
            "line": self.candidate.line,
            "resolved": (
                redact_credentials(str(self.resolved))
                if self.resolved is not None
                else None
            ),
            "git_root": (
                redact_credentials(str(self.git_root))
                if self.git_root is not None
                else None
            ),
            "exemption": self.exemption,
        }


@dataclass(frozen=True)
class RepositoryIntent:
    """Every repository a set of declarations intends to reach."""

    root: Path
    bound_git_root: Path | None
    candidates: tuple[ResolvedCandidate, ...]
    git_roots: tuple[Path, ...]
    non_git_paths: tuple[Path, ...]

    @property
    def registry_required(self) -> bool:
        """Whether intent exceeds the authority implied by the bound root.

        A legacy plan may omit a registry only when every discovered Git target
        belongs to the Git repository containing its bound execution root.  In
        particular, one Git target below an unrelated non-Git root is not a
        single-repository plan: the root grants no authority over that target.
        """
        return any(git_root != self.bound_git_root for git_root in self.git_roots)


# ---------------------------------------------------------------------------
# Layer A: pure extraction, one function per declaration surface
# ---------------------------------------------------------------------------


def candidates_from_text(text: str) -> tuple[IntentCandidate, ...]:
    """Extract absolute and inline-backtick locations from plan prose.

    Fenced blocks hold illustrative snippets, so both scans skip them for the
    same reason :func:`repository_scope_targets.extract_path_targets` does.
    """
    candidates: list[IntentCandidate] = []
    for target in repository_scope_targets.extract_path_targets(text):
        if target.confidence != "hard" or target.kind != "prose":
            continue
        candidates.append(
            IntentCandidate(
                value=target.value, surface="absolute-path", line=target.line
            )
        )
    candidates.extend(_backtick_candidates(text))
    return _deduplicate(candidates)


def candidates_from_plan_document(text: str) -> tuple[IntentCandidate, ...]:
    """Extract locations from a plan document, ignoring provenance metadata.

    ``manifest.md`` records the document a plan was split from. That pointer
    is where the plan came from, not somewhere it will write, so a plan whose
    source lives in an unrelated checkout must not read as reaching two
    repositories. Every other line is scanned exactly as
    :func:`candidates_from_text` scans it.
    """
    return candidates_from_text(_without_provenance_metadata(text))


def _without_provenance_metadata(text: str) -> str:
    """Blank provenance metadata lines, keeping every other line's number."""
    lines = text.splitlines()
    if not any(_PROVENANCE_LINE_RE.match(line) for line in lines):
        return text
    return "\n".join("" if _PROVENANCE_LINE_RE.match(line) else line for line in lines)


def _backtick_candidates(text: str) -> list[IntentCandidate]:
    candidates: list[IntentCandidate] = []
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
        for span in _BACKTICK_RE.finditer(line):
            value = span.group(1).strip()
            # A span holding whitespace is a command or a prose phrase, not a
            # path. Everything else is decided by whether it resolves.
            if not value or any(character.isspace() for character in value):
                continue
            candidates.append(
                IntentCandidate(value=value, surface="backtick-path", line=index)
            )
    return candidates


def candidates_from_checks(
    checks: Iterable[Any], *, line: int = 0
) -> tuple[IntentCandidate, ...]:
    """Extract ``path`` and ``cwd`` locations from parsed contract checks."""
    candidates: list[IntentCandidate] = []
    for target in repository_scope_targets.targets_from_checks(checks, line=line):
        surface: SourceSurface = (
            "contract-cwd" if target.kind == "contract-cwd" else "contract-path"
        )
        candidates.append(
            IntentCandidate(value=target.value, surface=surface, line=target.line)
        )
    return _deduplicate(candidates)


#: Which check field maps to which surface. Written down once, and annotated,
#: so the surface stays the `SourceSurface` the record declares instead of
#: widening to `str` on its way into `IntentCandidate`.
_CONTRACT_LOCATION_SURFACES: tuple[tuple[str, SourceSurface], ...] = (
    ("path", "contract-path"),
    ("cwd", "contract-cwd"),
)


def candidates_from_contract_payloads(
    contracts_by_id: Any,
) -> tuple[IntentCandidate, ...]:
    """Extract locations from a raw ``--contracts-file`` object.

    Contract targets never appear in plan prose, so a source-only scan cannot
    see them. They are also the strongest evidence available: a check ``path``
    is a location the session is required to produce.
    """
    if not isinstance(contracts_by_id, Mapping):
        return ()
    candidates: list[IntentCandidate] = []
    for contract in contracts_by_id.values():
        if not isinstance(contract, Mapping):
            continue
        verify = contract.get("verify")
        checks = (
            verify.get("checks")
            if isinstance(verify, Mapping)
            else contract.get("checks")
        )
        if not isinstance(checks, Sequence) or isinstance(checks, (str, bytes)):
            continue
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            for field, surface in _CONTRACT_LOCATION_SURFACES:
                value = check.get(field)
                if isinstance(value, str) and value:
                    candidates.append(IntentCandidate(value=value, surface=surface))
    return _deduplicate(candidates)


def candidates_from_plan_metadata(
    metadata: Any,
) -> tuple[IntentCandidate, ...]:
    """Extract the roots a prepared plan recorded in ``plan-metadata.json``."""
    if not isinstance(metadata, Mapping):
        return ()
    candidates: list[IntentCandidate] = []
    for field in ("cwd", "codebase_root", "workspace_root"):
        value = metadata.get(field)
        if isinstance(value, str) and value:
            candidates.append(IntentCandidate(value=value, surface="plan-metadata"))
    repositories = metadata.get("repositories")
    if isinstance(repositories, Sequence) and not isinstance(
        repositories, (str, bytes)
    ):
        for repository in repositories:
            if not isinstance(repository, Mapping):
                continue
            value = repository.get("root") or repository.get("path")
            if isinstance(value, str) and value:
                candidates.append(IntentCandidate(value=value, surface="plan-metadata"))
    return _deduplicate(candidates)


def candidates_from_execution_roots(
    roots: Iterable[str | os.PathLike[str]],
) -> tuple[IntentCandidate, ...]:
    """Treat each root a run would launch from as a declared location."""
    return _deduplicate(
        IntentCandidate(value=os.fspath(root), surface="execution-root")
        for root in roots
        if os.fspath(root)
    )


def candidates_from_task_packet(
    paths: Iterable[str | os.PathLike[str]],
) -> tuple[IntentCandidate, ...]:
    """Treat the artifact paths a task packet hands a worker as locations.

    These are normally runner-owned plan artifacts, which :func:`resolve_intent`
    exempts through ``exempt_roots``. They are extracted anyway so a packet
    that points a worker at a second repository is visible rather than
    invisible.
    """
    return _deduplicate(
        IntentCandidate(value=os.fspath(path), surface="task-packet")
        for path in paths
        if os.fspath(path)
    )


def _deduplicate(
    candidates: Iterable[IntentCandidate],
) -> tuple[IntentCandidate, ...]:
    seen: dict[tuple[str, str], IntentCandidate] = {}
    for candidate in candidates:
        seen.setdefault((candidate.surface, candidate.value), candidate)
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# Layer B: filesystem resolution
# ---------------------------------------------------------------------------


def provenance_exempt_roots(
    *values: str | os.PathLike[str] | None,
) -> tuple[Path, ...]:
    """Resolve declared plan sources into trees this gate must not count.

    A plan that says where it came from is not a plan that reaches there. The
    line-shaped filter in :func:`candidates_from_plan_document` only catches
    the metadata field; resolving the pointer to a path instead exempts it
    wherever it is named — session prose, an expansion note, a handoff, a
    regenerated artifact.

    Scope is the source file *and its parent directory*, so sibling plans in
    the same store — routine in multi-plan groups — stop tripping the gate too.
    Two guards keep that bounded:

    * a value that is itself a directory exempts nothing. Its parent is
      meaningless and exempting the directory would hand out an unbounded
      subtree.
    * the parent is exempted only when it sits *strictly inside* a Git
      repository — never when it is that repository's root, ``$HOME``, the
      filesystem root, or a directory in no repository at all. A source plan
      at a repository root would otherwise disable the gate for the whole
      repository, and one in a plain directory would exempt every repository
      that happens to live beneath it. A store outside any repository needs no
      widening anyway: nothing under it resolves to a Git root, so it cannot
      force a registry in the first place.

    A source path that no longer exists is exempted deliberately: resolution
    walks a deleted file up to its nearest existing ancestor and would name
    that ancestor's repository, which is the case that most needs the
    exemption. ``realpath`` does not require existence, so a declared value and
    a candidate naming it canonicalize identically either way.
    """
    home = canonicalize_filesystem_path(Path.home())
    git_root_cache: dict[Path, Path | None] = {}
    roots: dict[Path, None] = {}
    for value in values:
        if not value:
            continue
        # Expansion comes first: most manifests write `~/.claude/…`, which
        # `Path.is_absolute` calls relative.
        expanded = os.path.expanduser(os.fspath(value))
        if not os.path.isabs(expanded):
            continue
        source = canonicalize_filesystem_path(expanded)
        if source.is_dir():
            continue
        roots[source] = None
        parent = source.parent
        parent_git_root = repository_scope.nearest_git_root(parent, git_root_cache)
        if (
            parent in (source, home)
            or parent_git_root is None
            or parent_git_root == parent
        ):
            continue
        roots[parent] = None
    return tuple(roots)


def resolve_intent(
    candidates: Iterable[IntentCandidate],
    *,
    root: str | Path,
    exempt_roots: Iterable[str | Path] = (),
    provenance_roots: Iterable[str | Path] = (),
) -> RepositoryIntent:
    """Resolve every candidate and report which repositories it reaches.

    ``root`` anchors relative candidates. ``exempt_roots`` names trees that are
    not plan-owned source — the plan directory, the state directory, a run
    directory — so a runner-owned artifact never counts as a second repository.
    ``provenance_roots`` names the document a plan was split from and the
    directory holding it, already bounded by :func:`provenance_exempt_roots`;
    it is exempted under its own label so the report keeps the two reasons
    apart.
    """
    bound_root = canonicalize_filesystem_path(root)
    # Plan artifacts are listed first so a source plan that lives inside the
    # plan directory keeps the stronger label.
    exempt: tuple[tuple[Path, Exemption], ...] = tuple(
        [
            (canonicalize_filesystem_path(entry), "plan-artifact")
            for entry in exempt_roots
        ]
        + [
            (canonicalize_filesystem_path(entry), "plan-provenance")
            for entry in provenance_roots
        ]
    )
    git_root_cache: dict[Path, Path | None] = {}
    bound_git_root = repository_scope.nearest_git_root(bound_root, git_root_cache)
    resolved_candidates: list[ResolvedCandidate] = []
    git_roots: dict[Path, None] = {}
    non_git_paths: dict[Path, None] = {}

    for candidate in _deduplicate(candidates):
        resolved = _absolute(candidate.value, bound_root)
        if resolved is None:
            continue
        if repository_scope.is_system_path(resolved):
            resolved_candidates.append(
                ResolvedCandidate(
                    candidate=candidate,
                    resolved=canonicalize_filesystem_path(resolved),
                    git_root=None,
                    exemption="system-path",
                )
            )
            continue
        canonical = canonicalize_filesystem_path(resolved)
        # A workspace-relative virtualenv executable can be a symlink into a
        # system toolchain (notably Homebrew under /opt).  Judge both the path
        # the plan spelled and its resolved destination so that following the
        # symlink cannot turn an interpreter installation into plan-owned
        # repository intent.
        if repository_scope.is_system_path(canonical):
            resolved_candidates.append(
                ResolvedCandidate(
                    candidate=candidate,
                    resolved=canonical,
                    git_root=None,
                    exemption="system-path",
                )
            )
            continue
        exemption = next(
            (
                label
                for entry, label in exempt
                if is_descendant(canonical, entry, allow_equal=True)
            ),
            None,
        )
        if exemption is not None:
            resolved_candidates.append(
                ResolvedCandidate(
                    candidate=candidate,
                    resolved=canonical,
                    git_root=None,
                    exemption=exemption,
                )
            )
            continue
        ancestor = _nearest_existing(canonical)
        # A backtick span is only a location when it really names one; prose
        # such as `verify.parallel` must not become repository evidence.
        if ancestor is None or (
            candidate.surface == "backtick-path" and not canonical.exists()
        ):
            resolved_candidates.append(
                ResolvedCandidate(
                    candidate=candidate,
                    resolved=canonical,
                    git_root=None,
                    exemption="unresolved",
                )
            )
            continue
        git_root = repository_scope.nearest_git_root(ancestor, git_root_cache)
        if git_root is None:
            non_git_paths[ancestor] = None
            resolved_candidates.append(
                ResolvedCandidate(
                    candidate=candidate,
                    resolved=canonical,
                    git_root=None,
                    exemption="non-git",
                )
            )
            continue
        git_roots[git_root] = None
        resolved_candidates.append(
            ResolvedCandidate(
                candidate=candidate,
                resolved=canonical,
                git_root=git_root,
                exemption=None,
            )
        )

    return RepositoryIntent(
        root=bound_root,
        bound_git_root=bound_git_root,
        candidates=tuple(resolved_candidates),
        git_roots=tuple(sorted(git_roots, key=str)),
        non_git_paths=tuple(sorted(non_git_paths, key=str)),
    )


def _absolute(value: str, root: Path) -> Path | None:
    expanded = os.path.expanduser(value)
    if not expanded:
        return None
    candidate = Path(expanded)
    return candidate if candidate.is_absolute() else root / candidate


def _nearest_existing(path: Path) -> Path | None:
    """Return the closest ancestor that exists, or ``path`` when it does.

    A session's contract routinely names a file it has yet to create. The
    repository that would own that write is still knowable from the deepest
    directory that already exists.
    """
    current = path
    while True:
        if current.exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


# ---------------------------------------------------------------------------
# Layer C: bounded reporting
# ---------------------------------------------------------------------------


def intent_report(
    intent: RepositoryIntent, *, limit: int = INTENT_REPORT_LIMIT
) -> dict[str, Any]:
    """Render a bounded, credential-redacted projection of an intent."""
    candidates, omitted_candidates = bounded_repository_rows(
        intent.candidates, limit=limit
    )
    git_roots, omitted_git_roots = bounded_repository_rows(
        intent.git_roots, limit=limit
    )
    non_git, omitted_non_git = bounded_repository_rows(
        intent.non_git_paths, limit=limit
    )
    return {
        "root": redact_credentials(str(intent.root)),
        "bound_git_root": (
            redact_credentials(str(intent.bound_git_root))
            if intent.bound_git_root is not None
            else None
        ),
        "registry_required": intent.registry_required,
        "reason": (
            REASON_REPOSITORY_REGISTRY_REQUIRED if intent.registry_required else None
        ),
        "git_roots": [redact_credentials(str(root)) for root in git_roots],
        "git_root_count": len(intent.git_roots),
        "omitted_git_roots": omitted_git_roots,
        "non_git_paths": [redact_credentials(str(path)) for path in non_git],
        "omitted_non_git_paths": omitted_non_git,
        "candidates": [candidate.as_dict() for candidate in candidates],
        "candidate_count": len(intent.candidates),
        "omitted_candidates": omitted_candidates,
    }


def plan_intent(
    text: str,
    *,
    root: str | Path,
    contracts_by_id: Any = None,
    exempt_roots: Iterable[str | Path] = (),
    provenance_roots: Iterable[str | Path] = (),
) -> RepositoryIntent:
    """Resolve a source plan's intent, optionally including its contracts.

    Omitting ``contracts_by_id`` yields the source-only verdict ``asl split
    --check`` can reach without a contracts file; supplying it yields the
    contract-aware verdict ``asl split`` enforces.
    """
    candidates = list(candidates_from_text(text))
    candidates.extend(candidates_from_contract_payloads(contracts_by_id))
    return resolve_intent(
        candidates,
        root=root,
        exempt_roots=exempt_roots,
        provenance_roots=provenance_roots,
    )
