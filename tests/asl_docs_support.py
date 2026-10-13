"""Helpers for reading the ASL reference, which is split across several files."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASL_REFERENCE = "docs/agent-session-loop.md"
#: The ASL operator guide, which carries the operator-facing summary.
ASL_GUIDE = "docs/asl-guide.md"


def read_asl_reference() -> str:
    """Return the index and every part file, in reading order, as one text."""
    parts = sorted((REPO_ROOT / "docs" / "agent-session-loop").glob("*.md"))
    texts = [(REPO_ROOT / ASL_REFERENCE).read_text(encoding="utf-8")]
    texts.extend(part.read_text(encoding="utf-8") for part in parts)
    return "\n".join(texts)


def read_repo_text(relative: str) -> str:
    if relative == ASL_REFERENCE:
        return read_asl_reference()
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def autopilot_module_sources() -> str:
    """Return the text of every helper module that makes up the autopilot."""
    scripts = REPO_ROOT / "skills" / "asl" / "scripts"
    modules = sorted(
        path
        for path in scripts.glob("asl_autopilot*.py")
        if not path.name.startswith("asl_autopilot_test_")
    )
    return "\n".join(path.read_text(encoding="utf-8") for path in modules)
