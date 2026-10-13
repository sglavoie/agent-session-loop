"""Derived aggregate store (``prepare-validation.json``) and validator-output ingest."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from . import (
    durability,
    plans_metadata,
    plans_registry,
    plans_session_scope,
    plans_transaction,
    workspaces,
)
from .authoring_transaction import PlanTransaction
from .plans_errors import PlanError
from .plans_files import canonicalize_path
from .plans_session_ids import parse_session_id
from .plans_snapshot import inspect_plan
from .prepare_models import (
    LEGACY_PREPARE_VALIDATION_VERSION,
    PREPARE_VALIDATION_FILENAME,
    PREPARE_VALIDATION_VERSION,
    REPOSITORY_AMBIGUITY_ISSUE,
    PrepareValidation,
    ProposedCorrection,
    SessionValidation,
    _require_legacy_repository_footprint,
    _require_legacy_repository_intent,
)
from .prepare_packet import _validator_repositories
from .prepare_parsing import _fail, parse_validator_output


def prepare_validation_path(plan_dir: str | Path) -> Path:
    return canonicalize_path(plan_dir) / PREPARE_VALIDATION_FILENAME


def load_prepare_validation(plan_dir: str | Path) -> PrepareValidation | None:
    """Load the stored aggregate, ``None`` when absent, fail-closed when malformed."""
    plans_transaction.recover_pending_plan_transaction(plan_dir)
    path = prepare_validation_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PlanError(f"cannot read {path}: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(
            f"{path} is not valid JSON: {error.msg} at line {error.lineno}"
        ) from error
    return parse_validator_output(payload)


def serialize_prepare_validation(validation: PrepareValidation) -> str:
    """Serialize the aggregate to the canonical ``prepare-validation.json`` text.

    The single source of truth for the aggregate's on-disk representation, so
    a value staged into a :class:`PlanTransaction` is byte-identical to one
    written by :func:`save_prepare_validation`.
    """
    return json.dumps(validation.to_dict(), indent=2, sort_keys=True) + "\n"


def _serialize_workspace_metadata(
    validation: PrepareValidation, registry_digest: str
) -> str:
    metadata = plans_metadata.WorkspacePlanMetadata(
        workspace_root=validation.workspace_root,
        registry_digest=registry_digest,
    )
    return json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"


def _session_attributions(
    session: SessionValidation,
) -> tuple[tuple[str, str | None, object], ...]:
    attributed: list[tuple[str, str | None, object]] = []
    for label, values in (
        ("claim", session.claims),
        ("issue", session.issues),
        ("detail", session.details),
        ("pattern", session.patterns),
        ("dependency", session.dependencies),
        ("correction", session.corrections),
    ):
        attributed.extend((label, value.repo, value) for value in values)
    return tuple(attributed)


def _validate_validation_authority(
    plan_dir: str | Path,
    validation: PrepareValidation,
    *,
    expected_session_ids: Iterable[str] | None = None,
) -> str | None:
    """Validate plan-owned identity before any validator-derived mutation.

    Returns serialized v2 workspace metadata when a registered plan is not yet
    bound. The caller stages that text in the same transaction as every other
    mutation, so a later rendering or correction failure cannot leave a partial
    first binding behind.
    """
    canonical_dir = canonicalize_path(plan_dir)
    registry = plans_registry.read_repository_registry(canonical_dir)
    snapshot = inspect_plan(canonical_dir)
    authoritative_ids = {status.id for status in snapshot.sessions}
    returned_ids = set(validation.session_ids)
    unknown_ids = sorted(returned_ids - authoritative_ids)
    if unknown_ids:
        raise _fail(
            "session ids do not match the authoritative plan: " + ", ".join(unknown_ids)
        )
    if expected_session_ids is not None:
        expected = {parse_session_id(value).value for value in expected_session_ids}
        if returned_ids != expected:
            raise _fail(
                "batch result covers sessions "
                f"{', '.join(sorted(returned_ids)) or '(none)'}; expected "
                f"{', '.join(sorted(expected))}"
            )

    if registry is None:
        if validation.version != LEGACY_PREPARE_VALIDATION_VERSION:
            raise _fail("validator v2 is only valid for a registered plan")
        _require_legacy_repository_footprint(canonical_dir, validation.codebase_root)
        _require_legacy_repository_intent(canonical_dir, validation.codebase_root)
        ambiguous = sorted(
            {
                session.session_id.value
                for session in validation.sessions
                for issue in session.issues
                if issue.issue_type == REPOSITORY_AMBIGUITY_ISSUE
            }
        )
        if ambiguous:
            raise _fail(
                "the validator reported repository ambiguity in sessions "
                f"{', '.join(ambiguous)}; declare a `## Repositories` registry "
                "before expanding this plan"
            )
        return None

    if validation.version != PREPARE_VALIDATION_VERSION:
        raise _fail(
            "registered plans require validator output version "
            f"{PREPARE_VALIDATION_VERSION}"
        )
    canonical_root = str(canonicalize_path(validation.workspace_root))
    if validation.workspace_root != canonical_root:
        raise _fail(
            "workspace_root must be a canonical absolute path: "
            f"{validation.workspace_root!r}"
        )
    try:
        workspace = workspaces.resolve_workspace(canonical_root, registry)
    except workspaces.WorkspaceError as error:
        raise _fail(
            f"workspace does not match the authoritative registry: {error}"
        ) from error
    expected_repositories = _validator_repositories(workspace)
    if validation.repositories != expected_repositories:
        raise _fail("repositories do not match the authoritative resolved registry")

    for session in validation.sessions:
        session_path = canonical_dir / session.session_id.filename
        try:
            scope = plans_session_scope.parse_session_scope(
                session_path.read_text(encoding="utf-8"), registry.aliases
            )
        except OSError as error:
            raise PlanError(f"cannot read {session_path}: {error}") from error
        unknown_dependencies = sorted(
            {
                dependency.session_id
                for dependency in session.dependencies
                if dependency.session_id not in authoritative_ids
            }
        )
        if unknown_dependencies:
            raise _fail(
                f"session {session.session_id.value} dependency session ids "
                "do not match the authoritative plan: "
                + ", ".join(unknown_dependencies)
            )
        for label, repo, value in _session_attributions(session):
            if repo not in scope.reads:
                raise _fail(
                    f"session {session.session_id.value} {label} repository "
                    f"{repo!r} is outside its declared read scope"
                )
            if isinstance(value, ProposedCorrection):
                for alias in registry.aliases:
                    if alias != repo and (
                        f"{alias}:" in value.expected_text
                        or f"{alias}:" in value.replacement_text
                    ):
                        raise _fail(
                            f"session {session.session_id.value} correction "
                            f"attributed to {repo!r} crosses repository {alias!r}"
                        )

    metadata = plans_metadata.load_plan_metadata(canonical_dir)
    if metadata is None:
        return _serialize_workspace_metadata(validation, registry.digest)
    if not isinstance(metadata, plans_metadata.WorkspacePlanMetadata):
        raise _fail("registered plan has incompatible legacy workspace metadata")
    if metadata.workspace_root != validation.workspace_root:
        raise _fail(
            f"workspace_root {validation.workspace_root!r} does not match the "
            f"bound workspace {metadata.workspace_root!r}"
        )
    if metadata.registry_digest != registry.digest:
        raise _fail("bound workspace registry digest does not match the plan")
    return None


def save_prepare_validation(
    plan_dir: str | Path, validation: PrepareValidation
) -> Path:
    """Atomically persist the aggregate as ``prepare-validation.json``."""
    path = prepare_validation_path(plan_dir)
    text = serialize_prepare_validation(validation)
    try:
        durability.replace_text(path, text)
    except OSError as error:
        raise PlanError(f"cannot write {path}: {error}") from error
    return path


def ensure_plan_metadata(
    plan_dir: str | Path,
    cwd: str | Path,
    *,
    defer_registered_binding: bool = False,
) -> Path:
    """Persist prepare's workspace without silently rebinding an existing plan."""
    canonical_cwd = canonicalize_path(cwd)
    registry = plans_registry.read_repository_registry(plan_dir)
    validation = load_prepare_validation(plan_dir)
    if validation is not None:
        validation_cwd = canonicalize_path(validation.codebase_root)
        if validation_cwd != canonical_cwd:
            raise PlanError(
                "prepare cwd does not match stored validation root: "
                f"{canonical_cwd} != {validation_cwd}"
            )

    metadata = plans_metadata.load_plan_metadata(plan_dir)
    if registry is None:
        if metadata is not None and (
            not isinstance(metadata, plans_metadata.PlanMetadata)
            or canonicalize_path(metadata.cwd) != canonical_cwd
        ):
            bound = getattr(metadata, "workspace_root", "incompatible metadata")
            raise PlanError(
                "prepare cwd does not match stored plan metadata: "
                f"{canonical_cwd} != {bound}"
            )
        return plans_metadata.save_plan_metadata(plan_dir, canonical_cwd)

    try:
        workspace = workspaces.resolve_workspace(canonical_cwd, registry)
    except workspaces.WorkspaceError as error:
        raise PlanError(f"invalid preparation workspace: {error}") from error
    if metadata is not None and (
        not isinstance(metadata, plans_metadata.WorkspacePlanMetadata)
        or metadata.workspace_root != str(workspace.root)
        or metadata.registry_digest != registry.digest
    ):
        raise PlanError("prepare workspace does not match stored plan metadata")
    if metadata is None and defer_registered_binding:
        return plans_metadata.plan_metadata_path(plan_dir)
    return plans_metadata.save_workspace_plan_metadata(
        plan_dir,
        workspace.root,
        registry.digest,
    )


def merge_validations(
    base: PrepareValidation | None, update: PrepareValidation
) -> PrepareValidation:
    """Merge a batch result into the aggregate; the update wins per session."""
    if base is None:
        return update
    if base.codebase_root != update.codebase_root:
        raise PlanError(
            "validator output codebase root "
            f"{update.codebase_root!r} does not match stored aggregate "
            f"{base.codebase_root!r}"
        )
    if base.version != update.version:
        raise PlanError(
            f"validator output version {update.version} does not match stored "
            f"aggregate version {base.version}"
        )
    if base.repositories != update.repositories:
        raise PlanError("validator output repositories do not match stored aggregate")
    merged: dict[str, SessionValidation] = {
        session.session_id.value: session for session in base.sessions
    }
    for session in update.sessions:
        merged[session.session_id.value] = session
    ordered = sorted(merged.values(), key=lambda item: item.session_id.sort_key)
    return PrepareValidation(
        codebase_root=base.codebase_root,
        sessions=tuple(ordered),
        version=base.version,
        repositories=base.repositories,
    )


def ingest_validator_output(plan_dir: str | Path, payload: object) -> PrepareValidation:
    """Parse one batch payload, merge it into the stored aggregate, and save.

    The parse happens before any disk write, so malformed or incomplete
    validator output raises :class:`PlanError` and leaves the plan directory
    untouched.
    """
    update = parse_validator_output(payload)
    metadata_text = _validate_validation_authority(plan_dir, update)
    aggregate = merge_validations(load_prepare_validation(plan_dir), update)
    transaction = PlanTransaction(canonicalize_path(plan_dir))
    if metadata_text is not None:
        transaction.stage(plans_metadata.PLAN_METADATA_FILE_NAME, metadata_text)
    transaction.stage(
        PREPARE_VALIDATION_FILENAME, serialize_prepare_validation(aggregate)
    )
    transaction.commit()
    return aggregate
