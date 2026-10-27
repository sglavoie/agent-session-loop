"""Plan metadata (`plan-metadata.json`) loading and saving."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .plans_errors import (
    PlanError,
)
from .plans_files import (
    _atomic_write_text,
    _raise_read_error,
    canonicalize_path,
)
from .plans_transaction import (
    recover_pending_plan_transaction,
)

PLAN_METADATA_FILE_NAME = "plan-metadata.json"


PLAN_METADATA_SCHEMA_VERSION = 1


PLAN_METADATA_V2_SCHEMA_VERSION = 2


PLAN_METADATA_V3_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class PlanMetadata:
    """Stable machine-readable workspace association for one plan."""

    cwd: str
    version: int = PLAN_METADATA_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "cwd": self.cwd}


@dataclass(frozen=True)
class WorkspacePlanMetadata:
    """Durable binding for a registered workspace."""

    workspace_root: str
    registry_digest: str
    version: int = PLAN_METADATA_V3_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "workspace_root": self.workspace_root,
            "registry_digest": self.registry_digest,
        }


PlanMetadataV2 = WorkspacePlanMetadata


PlanMetadataValue = PlanMetadata | WorkspacePlanMetadata


def plan_metadata_path(plan_dir: str | Path) -> Path:
    """Return the stable workspace-metadata path for ``plan_dir``."""
    return canonicalize_path(plan_dir) / PLAN_METADATA_FILE_NAME


def load_plan_metadata(plan_dir: str | Path) -> PlanMetadataValue | None:
    """Load workspace metadata, returning ``None`` when it has not been written."""
    recover_pending_plan_transaction(plan_dir)
    path = plan_metadata_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        _raise_read_error(path, error)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(
            f"{path} is not valid JSON: {error.msg} at line {error.lineno}"
        ) from error
    if not isinstance(payload, dict):
        raise PlanError(f"{path} must contain a JSON object")

    version = payload.get("version")
    # An unhashable or boolean version falls through to the legacy parser's
    # unsupported-version refusal instead of a set-membership TypeError.
    if type(version) is int and version in {
        PLAN_METADATA_V2_SCHEMA_VERSION,
        PLAN_METADATA_V3_SCHEMA_VERSION,
    }:
        return _parse_workspace_plan_metadata(path, payload)
    return _parse_legacy_plan_metadata(path, payload)


def _parse_legacy_plan_metadata(path: Path, payload: dict[str, object]) -> PlanMetadata:
    """Parse v1 metadata without changing its strict compatibility contract."""
    unknown = sorted(set(payload) - {"version", "cwd"})
    if unknown:
        raise PlanError(f"{path} contains unknown keys: {', '.join(unknown)}")
    version = payload.get("version")
    if type(version) is not int or version != PLAN_METADATA_SCHEMA_VERSION:
        raise PlanError(
            f"{path} uses unsupported version {version!r}; "
            f"expected {PLAN_METADATA_SCHEMA_VERSION}"
        )
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise PlanError(f"{path} field 'cwd' must be a non-empty string")
    if cwd != cwd.strip() or not Path(cwd).is_absolute():
        raise PlanError(f"{path} field 'cwd' must be a canonical absolute path")
    canonical_cwd = str(canonicalize_path(cwd))
    if cwd != canonical_cwd:
        raise PlanError(
            f"{path} field 'cwd' is not canonical: {cwd!r}; expected {canonical_cwd!r}"
        )
    return PlanMetadata(cwd=cwd, version=version)


def _parse_workspace_plan_metadata(
    path: Path, payload: dict[str, object]
) -> WorkspacePlanMetadata:
    version = payload.get("version")
    allowed = {"version", "workspace_root", "registry_digest"}
    if version == PLAN_METADATA_V3_SCHEMA_VERSION:
        allowed.add("workspace_inventory_excludes")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise PlanError(f"{path} contains unknown keys: {', '.join(unknown)}")
    if type(version) is not int or version not in {
        PLAN_METADATA_V2_SCHEMA_VERSION,
        PLAN_METADATA_V3_SCHEMA_VERSION,
    }:
        raise PlanError(
            f"{path} uses unsupported version {version!r}; "
            f"expected {PLAN_METADATA_V2_SCHEMA_VERSION} or "
            f"{PLAN_METADATA_V3_SCHEMA_VERSION}"
        )
    workspace_root = payload.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        raise PlanError(f"{path} field 'workspace_root' must be a non-empty string")
    if (
        workspace_root != workspace_root.strip()
        or not Path(workspace_root).is_absolute()
    ):
        raise PlanError(
            f"{path} field 'workspace_root' must be a canonical absolute path"
        )
    canonical_root = str(canonicalize_path(workspace_root))
    if workspace_root != canonical_root:
        raise PlanError(
            f"{path} field 'workspace_root' is not canonical: "
            f"{workspace_root!r}; expected {canonical_root!r}"
        )
    registry_digest = payload.get("registry_digest")
    if (
        not isinstance(registry_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", registry_digest) is None
    ):
        raise PlanError(
            f"{path} field 'registry_digest' must be a lowercase SHA-256 digest"
        )
    # Version 3 files written before the policy was retired still carry this
    # key; accept a well-formed value and discard it rather than churn versions.
    raw_excludes = payload.get("workspace_inventory_excludes", [])
    if not isinstance(raw_excludes, list) or not all(
        isinstance(value, str) for value in raw_excludes
    ):
        raise PlanError(
            f"{path} field 'workspace_inventory_excludes' must be a string array"
        )
    return WorkspacePlanMetadata(
        workspace_root=workspace_root,
        registry_digest=registry_digest,
        version=version,
    )


def save_plan_metadata(plan_dir: str | Path, cwd: str | Path) -> Path:
    """Atomically persist the canonical workspace associated with a plan."""
    path = plan_metadata_path(plan_dir)
    canonical_cwd = canonicalize_path(cwd)
    metadata = PlanMetadata(cwd=str(canonical_cwd))
    text = json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)
    return path


def save_workspace_plan_metadata(
    plan_dir: str | Path,
    workspace_root: str | Path,
    registry_digest: str,
) -> Path:
    """Atomically persist a registered workspace binding."""
    path = plan_metadata_path(plan_dir)
    canonical_root = canonicalize_path(workspace_root)
    metadata = WorkspacePlanMetadata(
        workspace_root=str(canonical_root),
        registry_digest=registry_digest,
    )
    # Validate the digest through the same strict parser used for disk reads.
    _parse_workspace_plan_metadata(path, metadata.to_dict())
    text = json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)
    return path


save_plan_metadata_v2 = save_workspace_plan_metadata
