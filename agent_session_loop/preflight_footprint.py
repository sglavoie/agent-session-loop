"""Path reference, repository footprint, and repository intent checks."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import (
    repository_intent,
    repository_scope,
    repository_scope_payload,
    repository_scope_targets,
    workspaces,
)
from .contracts import (
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
)
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_metadata import PlanMetadata, load_plan_metadata
from .plans_models import PlanSnapshot
from .plans_readiness import read_split_from
from .plans_registry import read_repository_registry
from .plans_snapshot import inspect_plan
from .preflight_models import PreflightFinding, _finding
from .preflight_session_checks import _check_session_contracts, _read_session_text
from .repository_intent import RepositoryIntent


def _check_path_references(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            path_value: str | None = None
            if isinstance(check, (FileExistsCheck, RegexInFileCheck)):
                path_value = check.path
            else:
                continue

            candidate = Path(path_value)
            if candidate.is_absolute():
                continue

            normalized_parts = tuple(candidate.parts)
            if ".." in normalized_parts:
                findings.append(
                    _finding(
                        "yellow",
                        "path-reference",
                        (
                            f"session {session_id} check {check.name!r} escapes "
                            "the repo root with '..'"
                        ),
                        session_id=session_id,
                        detail=f"path: {path_value}",
                    )
                )

    return tuple(findings)


def _footprint_targets_by_session(
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
) -> dict[str, tuple[repository_scope_targets.PathTarget, ...]]:
    """Collect contract and prose path evidence, keyed by session id."""
    targets: dict[str, list[repository_scope_targets.PathTarget]] = {}
    for session_id, contract in contracts_by_session.items():
        targets.setdefault(session_id, []).extend(
            repository_scope_targets.targets_from_checks(contract.verify.checks)
        )
    if snapshot is not None:
        for status in snapshot.sessions:
            text = _read_session_text(status.session)
            if text is None:
                continue
            targets.setdefault(status.session.id, []).extend(
                repository_scope_targets.extract_path_targets(text)
            )
    return {
        session_id: tuple(session_targets)
        for session_id, session_targets in targets.items()
    }


def plan_repository_footprint(
    plan_dir: str | Path, root: str | Path
) -> tuple[repository_scope.RepositoryFootprint | None, dict[str, Any] | None]:
    """Judge a split plan directory against ``root``, contracts included.

    Returns ``(None, None)`` for a registered plan, whose registry already
    governs where it may reach. The second element is the serialized verdict,
    non-``None`` exactly when the plan may no longer stay in legacy mode.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry = read_repository_registry(canonical_dir)
    if registry is not None:
        return None, None
    snapshot = inspect_plan(canonical_dir)
    _, contracts_by_session = _check_session_contracts(snapshot, registry)
    targets_by_session = _footprint_targets_by_session(contracts_by_session, snapshot)
    footprint = repository_scope.resolve_footprint(
        [target for targets in targets_by_session.values() for target in targets],
        root,
        plan_dir=canonical_dir,
        provenance_roots=plan_provenance_exempt_roots(canonical_dir),
    )
    payload = repository_scope_payload.footprint_payload(
        targets_by_session, footprint, registry_present=False
    )
    return footprint, payload


def _check_repository_footprint(
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    root: str | Path,
    registry: workspaces.RepositoryRegistry | None,
    plan_dir: str | Path | None = None,
) -> tuple[tuple[PreflightFinding, ...], repository_scope.RepositoryFootprint | None]:
    """Refuse a legacy plan whose evidence reaches outside its bound root.

    A registered plan is already governed by the registry, so it is skipped
    here. Absolute targets that stay inside ``root`` remain green: the
    judgement is about reach, not about path style.
    """
    if registry is not None:
        return (), None
    targets_by_session = _footprint_targets_by_session(contracts_by_session, snapshot)
    provenance_roots = plan_provenance_exempt_roots(plan_dir)
    footprint = repository_scope.resolve_footprint(
        [target for targets in targets_by_session.values() for target in targets],
        root,
        plan_dir=plan_dir,
        provenance_roots=provenance_roots,
    )
    findings: list[PreflightFinding] = []
    for session_id in sorted(targets_by_session):
        session_footprint = repository_scope.resolve_footprint(
            targets_by_session[session_id],
            footprint.root,
            plan_dir=plan_dir,
            provenance_roots=provenance_roots,
        )
        for target in session_footprint.out_of_root:
            findings.append(
                _finding(
                    "red",
                    "repository-footprint",
                    (
                        f"session {session_id} {target.kind} target escapes the "
                        "bound root"
                    ),
                    session_id=session_id,
                    line=target.line or None,
                    detail=f"{target.value} is outside {footprint.root}",
                )
            )
    if len(footprint.repo_roots) > 1:
        findings.append(
            _finding(
                "red",
                "repository-footprint",
                (
                    f"plan spans {len(footprint.repo_roots)} git repositories "
                    "but manifest.md declares no `## Repositories` registry"
                ),
                detail=", ".join(str(repo) for repo in footprint.repo_roots),
            )
        )
    return tuple(findings), footprint


def _repository_intent_candidates(
    plan_dir: Path | None,
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    root: str | Path,
) -> tuple[repository_intent.IntentCandidate, ...]:
    """Collect every location a legacy plan declares it will reach.

    Unlike footprint targets, these include locations that do not exist yet:
    a contract ``path`` naming a file a session has still to create resolves
    through its nearest existing ancestor, so the repository owning it is
    nameable before the session runs.
    """
    candidates: list[repository_intent.IntentCandidate] = []
    for session_id in sorted(contracts_by_session):
        candidates.extend(
            repository_intent.candidates_from_checks(
                contracts_by_session[session_id].verify.checks
            )
        )
    if snapshot is not None:
        for status in snapshot.sessions:
            text = _read_session_text(status.session)
            if text is not None:
                candidates.extend(repository_intent.candidates_from_plan_document(text))
    if plan_dir is not None:
        for name in ("context.md", "manifest.md"):
            try:
                text = (plan_dir / name).read_text(encoding="utf-8")
            except OSError:
                continue
            candidates.extend(repository_intent.candidates_from_plan_document(text))
        try:
            metadata = load_plan_metadata(plan_dir)
        except PlanError:
            metadata = None
        if metadata is not None:
            candidates.extend(
                repository_intent.candidates_from_plan_metadata(metadata.to_dict())
            )
    candidates.extend(repository_intent.candidates_from_execution_roots([root]))
    return tuple(candidates)


def plan_provenance_exempt_roots(plan_dir: str | Path | None) -> tuple[Path, ...]:
    """Trees a plan's declared source pointer exempts from the intent gate.

    Public because the runner's per-turn packet gate needs the same answer the
    plan-level gate reaches, and both must derive it from the manifest rather
    than from a filter applied to one line shape.
    """
    if plan_dir is None:
        return ()
    return repository_intent.provenance_exempt_roots(read_split_from(plan_dir))


def _check_repository_intent(
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    root: str | Path,
    registry: workspaces.RepositoryRegistry | None,
    plan_dir: Path | None = None,
) -> tuple[tuple[PreflightFinding, ...], RepositoryIntent | None]:
    """Refuse a legacy plan that intends to reach several repositories.

    A registered plan already names its repositories, so it is skipped here.
    The verdict is a property of the whole plan rather than of one session:
    the registry that is missing is a plan-level declaration.
    """
    if registry is not None:
        return (), None
    intent = repository_intent.resolve_intent(
        _repository_intent_candidates(plan_dir, contracts_by_session, snapshot, root),
        root=root,
        exempt_roots=() if plan_dir is None else (plan_dir,),
        provenance_roots=plan_provenance_exempt_roots(plan_dir),
    )
    if not intent.registry_required:
        return (), intent
    finding = _finding(
        "red",
        "repository-registry-required",
        (
            f"plan intends to reach {len(intent.git_roots)} git repositories "
            "but manifest.md declares no `## Repositories` table"
        ),
        detail=", ".join(
            repository_intent.redact_credentials(str(git_root))
            for git_root in intent.git_roots
        ),
    )
    return (finding,), intent


def plan_repository_intent(
    plan_dir: str | Path,
    root: str | Path,
    *,
    extra_exempt_roots: Iterable[str | Path] = (),
) -> tuple[RepositoryIntent | None, dict[str, Any] | None]:
    """Judge a split plan directory's repository intent against ``root``.

    Returns ``(None, None)`` for a registered plan, whose registry already
    names every repository it may reach. The second element is the serialized
    intent report, non-``None`` exactly when a registry is required.

    ``extra_exempt_roots`` names further trees that are ASL's own artifacts
    rather than plan-owned source — the runner passes its state and run
    directories, which an operator may legitimately keep inside an unrelated
    checkout.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry = read_repository_registry(canonical_dir)
    if registry is not None:
        return None, None
    snapshot = inspect_plan(canonical_dir)
    _, contracts_by_session = _check_session_contracts(snapshot, registry)
    intent = repository_intent.resolve_intent(
        _repository_intent_candidates(
            canonical_dir, contracts_by_session, snapshot, root
        ),
        root=root,
        exempt_roots=(canonical_dir, *extra_exempt_roots),
        provenance_roots=plan_provenance_exempt_roots(canonical_dir),
    )
    if not intent.registry_required:
        return intent, None
    return intent, repository_intent.intent_report(intent)


def _check_bound_repository_footprint(
    plan_dir: Path,
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    registry: workspaces.RepositoryRegistry | None,
) -> tuple[tuple[PreflightFinding, ...], repository_scope.RepositoryFootprint | None]:
    """Judge the footprint against the root the plan already recorded.

    The stored v1 ``cwd`` is the root an out-of-root target contradicts, so
    ``asl preflight <dir>`` catches the escape with no ``--cwd`` argument. With
    no v1 binding there is no root to judge against, and the check stays
    silent rather than guessing one.
    """
    if registry is not None:
        return (), None
    try:
        metadata = load_plan_metadata(plan_dir)
    except PlanError:
        return (), None
    if not isinstance(metadata, PlanMetadata):
        return (), None
    return _check_repository_footprint(
        contracts_by_session, snapshot, metadata.cwd, registry, plan_dir
    )


def _check_bound_repository_intent(
    plan_dir: Path,
    contracts_by_session: dict[str, CompletionContract],
    snapshot: PlanSnapshot | None,
    registry: workspaces.RepositoryRegistry | None,
) -> tuple[tuple[PreflightFinding, ...], RepositoryIntent | None]:
    """Judge repository intent against the root the plan already recorded.

    With no v1 binding there is no root to anchor relative declarations
    against, and the check stays silent rather than guessing one.
    """
    if registry is not None:
        return (), None
    try:
        metadata = load_plan_metadata(plan_dir)
    except PlanError:
        return (), None
    if not isinstance(metadata, PlanMetadata):
        return (), None
    return _check_repository_intent(
        contracts_by_session, snapshot, metadata.cwd, registry, plan_dir
    )
