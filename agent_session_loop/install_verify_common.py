"""Shared check-result constructors and path helpers for install verification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SKILL_INSTALL_DIRS = (
    ".claude/skills",
    ".claude-personal/skills",
    ".codex/skills",
    ".copilot/skills",
)


def readable_path(path: Path) -> str:
    return os.fspath(path)


def success(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": True, "detail": detail, "advisory": False}


def failure(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": False, "detail": detail, "advisory": False}


def advisory(name: str, detail: str, *, ok: bool = False) -> dict[str, Any]:
    """A check whose failure is reported but does not fail the verification.

    ASL is the sole run authority and must install, verify, and run with no
    cockpit present at all. Reporting paired health is useful; making it a
    precondition would invert that. ``--require-agent-mux`` is the deliberate
    opt-in for an operator who has decided the pairing is load-bearing on their
    machine.
    """
    return {"name": name, "ok": ok, "detail": detail, "advisory": True}


def same_file(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.realpath(left) == os.path.realpath(right)
