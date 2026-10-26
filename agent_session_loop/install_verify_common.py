"""Shared check-result constructors and path helpers for install verification.

This module also owns harness-root discovery, the one answer to "which harness
directories under this home does ASL manage". ``install.py`` (skill links),
``startup_context`` (context files) and ``install_verify`` (the checks) all ask
it, so the three can never disagree about what an install touched. It imports
nothing from the package, which is what lets the standalone ``install.py`` use
it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Extra Claude Code configuration roots, ``os.pathsep``-separated. A second
#: Claude profile (``CLAUDE_CONFIG_DIR=~/.claude-work``) is managed only
#: when it is named here or in ``CLAUDE_CONFIG_DIR`` and the directory exists.
CLAUDE_CONFIG_DIRS_ENV = "ASL_CLAUDE_CONFIG_DIRS"

#: Claude Code's own override for its configuration root.
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

#: Harness roots that are always candidates, as (path under home, harness).
DEFAULT_HARNESS_ROOTS: tuple[tuple[str, str], ...] = (
    (".claude", "claude"),
    (".codex", "codex"),
    (".copilot", "copilot"),
)

#: Skill-directory entries that carry no ``SKILL.md`` but are linked like a
#: skill, so a skill's relative ``../references/<file>`` read resolves in the
#: installed layout. ``install.py`` links them and verification checks them.
SUPPORT_DIRS: tuple[str, ...] = ("references",)


#: The startup-context file each harness loads at session start. Copilot CLI
#: reads ``~/.copilot/copilot-instructions.md`` as its user-level instructions
#: in every repository.
CONTEXT_FILES: Mapping[str, str] = {
    "claude": "CLAUDE.md",
    "codex": "AGENTS.md",
    "copilot": "copilot-instructions.md",
}


@dataclass(frozen=True)
class HarnessRoot:
    """One harness configuration directory, addressed relative to a home."""

    harness: str
    relative: str
    source: str

    @property
    def skills_dir(self) -> str:
        return f"{self.relative}/skills"

    @property
    def context_file(self) -> str | None:
        name = CONTEXT_FILES.get(self.harness)
        return None if name is None else f"{self.relative}/{name}"


@dataclass(frozen=True)
class HarnessDiscovery:
    """Harness roots found under a home, and the candidates that were not."""

    home: Path
    found: tuple[HarnessRoot, ...]
    skipped: tuple[tuple[str, str], ...]

    def note(self) -> str:
        """Name what is managed and what was skipped, one line each."""
        found = ", ".join(f"~/{root.relative} ({root.harness})" for root in self.found)
        lines = [f"harness roots: {found or 'none found'}"]
        if self.skipped:
            skipped = ", ".join(f"{path} ({reason})" for path, reason in self.skipped)
            lines.append(f"skipped harness roots: {skipped}")
        return "\n".join(lines)

    def payload(self) -> dict[str, Any]:
        return {
            "found": [
                {
                    "path": root.relative,
                    "harness": root.harness,
                    "source": root.source,
                }
                for root in self.found
            ],
            "skipped": [
                {"path": path, "reason": reason} for path, reason in self.skipped
            ],
        }


def _relative_to_home(value: str, home: Path) -> str | None:
    """Return ``value`` as a path under ``home``, or None when it lies outside.

    ``~`` expands against ``home`` rather than the ambient ``$HOME`` and a
    relative value is read under ``home``, so a scratch ``--home`` never
    reaches into the real one. An absolute value is accepted only when it lies
    inside ``home``.
    """
    home_text = os.fspath(home)
    if value == "~" or value.startswith("~/"):
        candidate = os.path.join(home_text, value[2:])
    elif os.path.isabs(value):
        candidate = value
    else:
        candidate = os.path.join(home_text, value)
    for base, path in (
        (os.path.normpath(home_text), os.path.normpath(candidate)),
        (os.path.realpath(home_text), os.path.realpath(candidate)),
    ):
        relative = os.path.relpath(path, base)
        if relative != "." and not relative.startswith(".."):
            return Path(relative).as_posix()
    return None


def discover_harness_roots(
    home: Path, env: Mapping[str, str] | None = None
) -> HarnessDiscovery:
    """Return the harness roots under ``home`` that ASL should manage.

    A root is managed only when its directory already exists: installing ASL
    never invents a harness configuration directory. Candidates are
    ``~/.claude``, ``$CLAUDE_CONFIG_DIR``, every entry of
    ``$ASL_CLAUDE_CONFIG_DIRS``, ``~/.codex`` and ``~/.copilot``.
    """
    environ = os.environ if env is None else env
    candidates: list[tuple[str, str, str]] = [(".claude", "claude", "default")]
    claude_config_dir = environ.get(CLAUDE_CONFIG_DIR_ENV, "").strip()
    if claude_config_dir:
        candidates.append((claude_config_dir, "claude", CLAUDE_CONFIG_DIR_ENV))
    for entry in environ.get(CLAUDE_CONFIG_DIRS_ENV, "").split(os.pathsep):
        if entry.strip():
            candidates.append((entry.strip(), "claude", CLAUDE_CONFIG_DIRS_ENV))
    candidates.extend(
        (relative, harness, "default")
        for relative, harness in DEFAULT_HARNESS_ROOTS
        if relative != ".claude"
    )

    found: list[HarnessRoot] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value, harness, source in candidates:
        relative = _relative_to_home(value, home)
        if relative is None:
            skipped.append((value, f"{source} is outside {home}"))
            continue
        if relative in seen:
            continue
        seen.add(relative)
        if not (home / relative).is_dir():
            skipped.append((f"~/{relative}", "not present"))
            continue
        found.append(HarnessRoot(harness=harness, relative=relative, source=source))
    return HarnessDiscovery(home=home, found=tuple(found), skipped=tuple(skipped))


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
