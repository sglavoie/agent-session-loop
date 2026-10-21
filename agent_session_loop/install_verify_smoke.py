"""Active-``asl`` resolution and status smoke checks for install verification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import cli_trace
from .install_verify_common import failure, readable_path, success


def _write_synthetic_plan(plan_dir: Path) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    (plan_dir / "session-1.md").write_text(
        "\n".join(
            [
                "# Session 1: Install Smoke",
                "",
                "No-op synthetic session.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _check_asl_path(
    repo_root: Path, path_env: str | None
) -> tuple[str | None, dict[str, Any]]:
    active_asl = shutil.which("asl", path=path_env)
    if active_asl is None:
        return None, failure("asl-path", "asl was not found on PATH")

    active_path = Path(active_asl)
    resolved = active_path.resolve()
    expected = (repo_root / "bin/.local/bin/agent-session-loop").resolve()
    if resolved != expected:
        return active_asl, failure(
            "asl-path",
            f"{active_path} resolves to {resolved}; expected {expected}",
        )
    return active_asl, success(
        "asl-path",
        f"{active_path} resolves to {resolved}",
    )


def _check_status_smoke(
    *,
    active_asl: str | None,
    home: Path,
    path_env: str | None,
) -> dict[str, Any]:
    if active_asl is None:
        return failure("status-smoke", "skipped because asl was not found on PATH")

    with tempfile.TemporaryDirectory(prefix="asl-install-smoke-") as tmp:
        plan_dir = Path(tmp) / "plan"
        _write_synthetic_plan(plan_dir)
        env = os.environ.copy()
        env["HOME"] = readable_path(home)
        if path_env is not None:
            env["PATH"] = path_env
        env = cli_trace.child_env(env, cli_trace.ORIGIN_INSTALL_VERIFY)
        completed = subprocess.run(
            [active_asl, "status", readable_path(plan_dir), "--json"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return failure(
            "status-smoke",
            f"asl status --json exited {completed.returncode}: {detail}",
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return failure(
            "status-smoke",
            f"asl status --json did not emit JSON: {error}",
        )

    if not payload.get("ok"):
        return failure(
            "status-smoke",
            f"asl status --json returned ok=false: {payload}",
        )

    if "parallel_validation_path" in payload:
        return failure(
            "status-smoke",
            "asl status --json still emits parallel_validation_path",
        )

    return success(
        "status-smoke",
        "asl status --json succeeded and omitted parallel_validation_path",
    )
