"""Shared fixtures for the ``test_asl_workspace_evidence_*`` modules."""

from __future__ import annotations

from pathlib import Path

from agent_session_loop import workspaces


def _workspace(*aliases: str) -> workspaces.ResolvedWorkspace:
    """A resolved workspace built from names, with no checkout behind it."""
    root = Path("/tmp/workspace")
    return workspaces.ResolvedWorkspace(
        root=root,
        repositories=tuple(
            workspaces.ResolvedRepository(
                repository=workspaces.Repository(
                    alias,
                    alias.lower(),
                    f"git@github.com:example/{alias.lower()}.git",
                ),
                root=root / alias.lower(),
                origin=f"git@github.com:example/{alias.lower()}.git",
            )
            for alias in aliases
        ),
    )
