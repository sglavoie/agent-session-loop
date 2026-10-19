"""Derivation and application of the multi-repository registry migration."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from agent_session_loop import (
    authoring_lint,
    authoring_manifest,
    authoring_parse,
    authoring_render,
    plans_content_hash,
    plans_models,
    plans_readiness,
    plans_registry,
    plans_session_ids,
    plans_session_scope,
    plans_snapshot,
    repository_registration,
)
from agent_session_loop.contracts import parse_completion_contract
from agent_session_loop.workspaces import (
    Repository,
    RepositoryRegistry,
    canonicalize_filesystem_path,
)
from tests.support import (
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


TWO_REPO_SPECS: tuple[tuple[str, str, str], ...] = (
    ("SVC", "tundra_svc", "git@github.com:tundra/SVC.git"),
    ("OAK", "tundra_OAK", "git@github.com:tundra/OAK.git"),
)


def _contract_block(session: str, checks: list[dict]) -> str:
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- the session lands",
            "",
            "### Verify",
            "",
            "```json",
            json.dumps({"session": session, "checks": checks}),
            "```",
        ]
    )


def _verify_payload(contract) -> dict:
    """Round-trip a rewritten contract back through the registry validators."""
    checks = []
    for check in contract.verify.checks:
        payload = {"name": check.name, "type": check.kind, "repo": check.repo}
        if check.kind == "command":
            payload["argv"] = list(check.argv)
            if check.cwd is not None:
                payload["cwd"] = check.cwd
        else:
            payload["path"] = check.path
        if check.kind == "regex_in_file":
            payload["pattern"] = check.pattern
        checks.append(payload)
    return {"session": contract.session_id.value, "checks": checks}


def _tree_state(root: Path) -> dict[str, tuple[int, int]]:
    """Every file beneath ``root`` with its size and modification time."""
    return {
        str(path.relative_to(root)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


__all__ = ["json", "subprocess", "tempfile", "unittest", "replace", "UTC", "datetime", "Path", "get_args", "authoring_lint", "authoring_manifest", "authoring_parse", "authoring_render", "plans_content_hash", "plans_models", "plans_readiness", "plans_registry", "plans_session_ids", "plans_session_scope", "plans_snapshot", "repository_registration", "parse_completion_contract", "Repository", "RepositoryRegistry", "canonicalize_filesystem_path", "init_git_repo", "init_multi_repo_workspace", "write_multi_repo_plan", "write_plan", "FIXED_NOW", "TWO_REPO_SPECS", "_contract_block", "_verify_payload", "_tree_state"]  # fmt: skip
