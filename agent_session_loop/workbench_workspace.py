"""The workspace binding a Workbench payload reports for one plan.

Every operator-facing Workbench answer -- a summary, a pin, a launch result, a
recovery card, a final review -- reports the same credential-free workspace
projection: the root, the registry digest, and the ordered repository rows.
This module computes that projection once, from runtime state when a run has
recorded one and from prepared plan metadata before the first run.

It is the lowest Workbench layer: it imports only ``plans``, so ``workbench``
and ``workbench_recovery`` can both read it without importing each other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import plans


def _repository_rows(value: Any) -> tuple[dict[str, Any], ...]:
    """Return well-formed repository rows without changing their order."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(dict(row) for row in value if isinstance(row, Mapping))


def _workspace_projection(
    plan_dir: str | Path,
    *,
    runtime: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a plan's exact workspace binding for operator-facing APIs.

    Runtime state wins because it records the resolved vector used by the run.
    Before the first run, prepared workspace metadata plus the manifest
    registry gives the same ordered, credential-free projection without probing
    Git. Legacy plans intentionally return an empty mapping so their payloads
    stay additive.
    """
    runtime_payload = runtime or {}
    workspace_root = runtime_payload.get("workspace_root")
    repositories = _repository_rows(runtime_payload.get("repositories"))
    if isinstance(workspace_root, str) and workspace_root and repositories:
        return {
            "workspace_root": workspace_root,
            "registry_digest": runtime_payload.get("registry_digest"),
            "repositories": [dict(row) for row in repositories],
            "repository_count": len(repositories),
        }

    registry = plans.read_repository_registry(plan_dir)
    metadata = plans.load_plan_metadata(plan_dir)
    if registry is None or not isinstance(metadata, plans.WorkspacePlanMetadata):
        return {}
    root = plans.canonicalize_path(metadata.workspace_root)
    rows = [
        {
            "alias": repository.alias,
            "path": repository.path,
            "origin": repository.origin,
            "resolved_root": str(plans.canonicalize_path(root / repository.path)),
        }
        for repository in registry.repositories
    ]
    return {
        "workspace_root": str(root),
        "registry_digest": metadata.registry_digest,
        "repositories": rows,
        "repository_count": len(rows),
    }


def _workspace_payload(
    workspace: Mapping[str, Any],
    *,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Return the compatibility and structured workspace API fields."""
    workspace_root = workspace.get("workspace_root")
    return {
        "cwd": cwd if cwd is not None else workspace_root,
        "workspace_root": workspace_root,
        "registry_digest": workspace.get("registry_digest"),
        "repositories": [
            dict(row) for row in _repository_rows(workspace.get("repositories"))
        ],
        "repository_count": int(workspace.get("repository_count") or 0),
    }
