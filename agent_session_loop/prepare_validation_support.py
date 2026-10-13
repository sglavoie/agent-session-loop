"""Validation identity, cache reuse, and progress helpers."""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

from . import plans, repository_evidence, workspaces
from .plans import PlanError, canonicalize_path, parse_session_id
from .prepare_models import (
    PREPARE_VALIDATION_CACHE_VERSION,
    PrepareValidation,
    PrepareValidationCache,
    PrepareValidationCacheEntry,
    SessionValidation,
)
from .prepare_packet import _validation_prompt_digest
from .prepare_parsing import _fail
from .prepare_validation_cache import _canonical_json_digest


def _positive_validator_control(value: object, *, name: str) -> int:
    """Return a positive integer validator control or fail before work starts."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanError(f"{name} must be a positive integer")
    return value


def _read_validation_text(path: Path, *, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise PlanError(f"cannot read {label} {path}: {error}") from error


def _validation_session_hashes(
    plan_dir: Path,
    session_ids: tuple[str, ...],
) -> dict[str, str]:
    """Hash current session text without recapturing shared worker inputs."""
    hashes: dict[str, str] = {}
    for session_id in session_ids:
        canonical_session_id = parse_session_id(session_id)
        session_text = _read_validation_text(
            plan_dir / canonical_session_id.filename,
            label="session",
        )
        hashes[canonical_session_id.value] = hashlib.sha256(
            session_text.encode("utf-8")
        ).hexdigest()
    return hashes


def _validation_repository_identity(
    plan_dir: Path,
    codebase_root: str,
) -> tuple[dict[str, object], bool]:
    """Capture the repository portion shared by every session cache key.

    A capture that Git cannot establish is retained for diagnostics but never
    used as reusable evidence.  This is deliberately stricter than an empty
    fingerprint: a probe failure must cause a fresh validator run, not make
    two unknown repository states compare equal.
    """
    registry = plans.read_repository_registry(plan_dir)
    canonical_root = canonicalize_path(codebase_root)
    if registry is None:
        fingerprint = repository_evidence.capture_repository_fingerprint(
            canonical_root, plan_dir
        )
        reusable = bool(fingerprint.get("git")) and (
            repository_evidence.repository_evidence_unavailable(fingerprint) is None
        )
        return (
            {
                "mode": "legacy",
                "codebase_root": str(canonical_root),
                "repository_fingerprint": _canonical_json_digest(fingerprint),
            },
            reusable,
        )

    try:
        workspace = workspaces.resolve_workspace(canonical_root, registry)
    except workspaces.WorkspaceError as error:
        raise PlanError(f"invalid validation workspace: {error}") from error
    repositories: list[dict[str, str]] = []
    reusable = True
    for repository in workspace.repositories:
        fingerprint = repository_evidence.capture_repository_fingerprint(
            repository.root, plan_dir
        )
        if (
            not fingerprint.get("git")
            or repository_evidence.repository_evidence_unavailable(fingerprint)
            is not None
        ):
            reusable = False
        repositories.append(
            {
                "alias": repository.alias,
                "path": repository.repository.path,
                "origin": repository.origin,
                "root": str(repository.root),
                "fingerprint": _canonical_json_digest(fingerprint),
            }
        )
    return (
        {
            "mode": "registered",
            "workspace_root": str(workspace.root),
            "registry_digest": registry.digest,
            "repositories": repositories,
        },
        reusable,
    )


def _validation_cache_identities(
    plan_dir: Path,
    session_ids: tuple[str, ...],
    *,
    codebase_root: str,
    validator_harness: str,
    validator_model: str | None,
    validator_effort: str | None,
    validator_schema_version: int,
) -> tuple[dict[str, dict[str, object]], bool]:
    """Snapshot all per-session cache identities before workers launch."""
    context_text = _read_validation_text(plan_dir / "context.md", label="context")
    registry = plans.read_repository_registry(plan_dir)
    repository_identity, reusable = _validation_repository_identity(
        plan_dir, codebase_root
    )
    prompt_sha256 = _validation_prompt_digest(validator_schema_version)
    identities: dict[str, dict[str, object]] = {}
    for session_id in session_ids:
        canonical_session_id = parse_session_id(session_id)
        session_path = plan_dir / canonical_session_id.filename
        session_text = _read_validation_text(session_path, label="session")
        scope: dict[str, object] | None = None
        if registry is not None:
            parsed_scope = plans.parse_session_scope(session_text, registry.aliases)
            scope = {
                "reads": list(parsed_scope.reads),
                "writes": list(parsed_scope.writes),
            }
        identities[canonical_session_id.value] = {
            "cache_version": PREPARE_VALIDATION_CACHE_VERSION,
            "validator_schema_version": validator_schema_version,
            "prompt_sha256": prompt_sha256,
            "repository": repository_identity,
            "context_sha256": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
            "session_sha256": hashlib.sha256(session_text.encode("utf-8")).hexdigest(),
            "scope": scope,
            "validator": {
                "harness": validator_harness,
                "model": validator_model,
                "effort": validator_effort,
            },
        }
    return identities, reusable


def _single_session_validation(
    validation: PrepareValidation,
    session: SessionValidation,
) -> PrepareValidation:
    return PrepareValidation(
        codebase_root=validation.codebase_root,
        sessions=(session,),
        version=validation.version,
        repositories=validation.repositories,
    )


def _cache_entry_for_validation(
    validation: PrepareValidation,
    *,
    identity: dict[str, object],
) -> PrepareValidationCacheEntry:
    if len(validation.sessions) != 1:
        raise AssertionError("cache entries require exactly one session validation")
    return PrepareValidationCacheEntry(
        session_id=validation.sessions[0].session_id,
        key=_canonical_json_digest(identity),
        identity=identity,
        validation=validation,
    )


def _cache_hit_validation(
    cache: PrepareValidationCache,
    session_id: str,
    *,
    identity: dict[str, object],
    reusable: bool,
    codebase_root: str,
    validator_schema_version: int,
) -> PrepareValidation | None:
    """Return a usable cached result only when every identity field matches."""
    if not reusable:
        return None
    entry = cache.entry(session_id)
    if entry is None or entry.key != _canonical_json_digest(identity):
        return None
    if entry.identity != identity:
        return None
    validation = entry.validation
    if (
        validation.codebase_root != codebase_root
        or validation.version != validator_schema_version
        or validation.session_ids != (parse_session_id(session_id).value,)
    ):
        return None
    return validation


def _merge_batch_validation_results(
    session_ids: tuple[str, ...],
    results: dict[str, PrepareValidation],
) -> PrepareValidation:
    """Reassemble one original batch in deterministic session order."""
    try:
        first = results[session_ids[0]]
    except KeyError as error:
        raise _fail(
            f"validator result is missing session {session_ids[0]!r}"
        ) from error
    sessions: list[SessionValidation] = []
    for session_id in session_ids:
        try:
            validation = results[session_id]
        except KeyError as error:
            raise _fail(
                f"validator result is missing session {session_id!r}"
            ) from error
        if (
            validation.codebase_root != first.codebase_root
            or validation.version != first.version
            or validation.repositories != first.repositories
        ):
            raise _fail(
                "validator batch results disagree on root or repository identity"
            )
        if len(validation.sessions) != 1:
            raise _fail("validator cache result must contain exactly one session")
        sessions.append(validation.sessions[0])
    return PrepareValidation(
        codebase_root=first.codebase_root,
        sessions=tuple(sessions),
        version=first.version,
        repositories=first.repositories,
    )


def _validation_elapsed(started_at: float) -> str:
    return f"{max(0.0, time.monotonic() - started_at):.1f}s"


def _validation_progress(
    message: str,
    *,
    completed: int,
    total: int,
    in_flight: int,
    started_at: float,
) -> None:
    """Write human progress to stderr without contaminating JSON stdout."""
    print(
        (
            "asl prepare: validator "
            f"{message} ({completed}/{total} batches complete; "
            f"{in_flight} in-flight; total elapsed {_validation_elapsed(started_at)})"
        ),
        file=sys.stderr,
        flush=True,
    )
