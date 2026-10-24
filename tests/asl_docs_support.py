"""Helpers for reading the ASL reference, which is split across several files."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASL_REFERENCE = "docs/agent-session-loop.md"
#: The ASL operator guide, which carries the operator-facing summary.
ASL_GUIDE = "docs/asl-guide.md"
#: The `asl` skill keeps its supervise loop and report contract in `SKILL.md`
#: and moves the rarely needed procedures into on-demand reference files.
ASL_SKILL = "skills/asl/SKILL.md"
ASL_SKILL_MID_RUN_CONTROLS = "skills/asl/references/mid-run-controls.md"
ASL_SKILL_QUESTIONS_AND_PAUSES = "skills/asl/references/questions-and-pauses.md"
ASL_SKILL_QUOTA_AND_HANGS = "skills/asl/references/quota-and-hangs.md"
ASL_SKILL_REVIEW_FINDINGS = "skills/asl/references/review-findings.md"
ASL_SKILL_SUPERVISOR_AUDIT = "skills/asl/references/supervisor-audit.md"
ASL_SKILL_HELPER_INTERNALS = "skills/asl/references/helper-internals.md"
ASL_SKILL_SUPERVISE_TOKENS = "skills/asl/references/supervise-tokens.md"
#: Every on-demand reference `SKILL.md` must point at.
ASL_SKILL_ON_DEMAND_REFERENCES = (
    ASL_SKILL_MID_RUN_CONTROLS,
    ASL_SKILL_QUESTIONS_AND_PAUSES,
    ASL_SKILL_QUOTA_AND_HANGS,
    ASL_SKILL_REVIEW_FINDINGS,
    ASL_SKILL_SUPERVISOR_AUDIT,
    ASL_SKILL_HELPER_INTERNALS,
    ASL_SKILL_SUPERVISE_TOKENS,
)


def read_asl_reference() -> str:
    """Return the index and every part file, in reading order, as one text."""
    parts = sorted((REPO_ROOT / "docs" / "agent-session-loop").glob("*.md"))
    texts = [(REPO_ROOT / ASL_REFERENCE).read_text(encoding="utf-8")]
    texts.extend(part.read_text(encoding="utf-8") for part in parts)
    return "\n".join(texts)


def read_asl_skill_bundle() -> str:
    """Return `SKILL.md` followed by every on-demand reference it points at.

    For assertions about what the skill as a whole tells a supervisor, whether
    the agent reads it on every invocation or loads it when the case arises.
    """
    paths = (ASL_SKILL, *ASL_SKILL_ON_DEMAND_REFERENCES)
    return "\n".join((REPO_ROOT / path).read_text(encoding="utf-8") for path in paths)


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
