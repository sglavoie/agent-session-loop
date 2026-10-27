"""Installed-skill and startup-context checks for install verification."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import startup_context
from .install_verify_common import SUPPORT_DIRS, HarnessRoot, failure, success

ASL_SKILL_REQUIRED_TEXT = (
    "Read `references/supervisor-context.md` in this skill directory before"
)

ASL_HELPER_RELATIVE_PATH = "asl/scripts/asl-autopilot.py"

SESSION_RUN_REQUIRED_TEXT = (
    "Explicit session selectors (`--session <ID>`) and direct session file paths "
    "(`<plan-dir>/session-<ID>.md`) are not accepted by this skill."
)
SESSION_RUN_FORBIDDEN_TEXT = (
    "**Explicit flag**: `<plan-dir>/ --session <ID>` runs exactly",
    "**Direct session file**: `<plan-dir>/session-<ID>.md` runs exactly",
    "In explicit mode, if another pending session remains",
)


def _expected_skill_dirs(repo_root: Path) -> list[Path]:
    """Every linked skill-directory entry: each skill, and each support dir.

    A missing ``references`` link breaks every skill's relative reference
    read, so it is verified like a skill rather than skipped for lacking a
    ``SKILL.md``.
    """
    skills_root = repo_root / "skills"
    return sorted(
        (
            path
            for path in skills_root.iterdir()
            if path.is_dir()
            and ((path / "SKILL.md").is_file() or path.name in SUPPORT_DIRS)
        ),
        key=lambda path: path.name,
    )


def _check_harness_roots(home: Path, roots: Sequence[HarnessRoot]) -> dict[str, Any]:
    """Report which harness roots verification covers, failing when none exist."""
    if not roots:
        return failure(
            "harness-roots",
            f"no harness root (~/.claude, ~/.codex, ~/.copilot, $CLAUDE_CONFIG_DIR "
            f"or $ASL_CLAUDE_CONFIG_DIRS) exists under {home}",
        )
    names = ", ".join(f"~/{root.relative} ({root.harness})" for root in roots)
    return success("harness-roots", f"verifying {names}")


def _check_skill_links(
    repo_root: Path, home: Path, roots: Sequence[HarnessRoot]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = [_check_harness_roots(home, roots)]
    for root in roots:
        install_root = home / root.skills_dir
        for skill_dir in _expected_skill_dirs(repo_root):
            installed = install_root / skill_dir.name
            expected = skill_dir.resolve()
            if not installed.exists():
                checks.append(
                    failure(
                        "skill-link",
                        f"{installed} is missing; expected it to resolve to {expected}",
                    )
                )
                continue

            resolved = installed.resolve()
            if resolved != expected:
                installed_skill = installed / "SKILL.md"
                expected_skill = expected / "SKILL.md"
                if (
                    installed_skill.exists()
                    and installed_skill.resolve() == expected_skill
                ):
                    checks.append(
                        success(
                            "skill-link",
                            f"{installed_skill} resolves to {expected_skill}",
                        )
                    )
                    continue
                checks.append(
                    failure(
                        "skill-link",
                        f"{installed} resolves to {resolved}; expected {expected}",
                    )
                )
                continue
            checks.append(
                success(
                    "skill-link",
                    f"{installed} resolves to {resolved}",
                )
            )
    return checks


def _check_session_run_content(
    home: Path, roots: Sequence[HarnessRoot]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for root in roots:
        skill_path = home / root.skills_dir / "session-run" / "SKILL.md"
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError as error:
            checks.append(
                failure(
                    "session-run-content",
                    f"cannot read {skill_path}: {error}",
                )
            )
            continue

        if SESSION_RUN_REQUIRED_TEXT not in text:
            checks.append(
                failure(
                    "session-run-content",
                    f"{skill_path} is missing the serial-only explicit-selector "
                    f"rejection text",
                )
            )
            continue

        forbidden = [needle for needle in SESSION_RUN_FORBIDDEN_TEXT if needle in text]
        if forbidden:
            checks.append(
                failure(
                    "session-run-content",
                    f"{skill_path} still contains stale explicit-session text: "
                    f"{forbidden[0]!r}",
                )
            )
            continue

        checks.append(
            success(
                "session-run-content",
                f"{skill_path} documents serial-only session-run input",
            )
        )
    return checks


def _check_startup_context(
    home: Path, roots: Sequence[HarnessRoot]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for target in startup_context.context_targets(roots):
        path = home / target.installed_path
        remedy = f"run `{startup_context.SYNC_REMEDY}` to render the startup card"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            checks.append(
                failure("startup-context", f"{path} is missing; {remedy}"),
            )
            continue
        except (OSError, UnicodeDecodeError) as error:
            checks.append(
                failure("startup-context", f"cannot read {path}: {error}"),
            )
            continue

        expected = startup_context.render_block(target.harness)
        problem = startup_context.block_problem(text, expected)
        if problem is None:
            marker = expected.split("\n", 1)[0]
            checks.append(
                success("startup-context", f"{path} carries {marker}"),
            )
            continue

        checks.append(failure("startup-context", f"{path} {problem}; {remedy}"))
    return checks


def _check_asl_skill(
    repo_root: Path, home: Path, roots: Sequence[HarnessRoot]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    expected_helper = (repo_root / "skills" / ASL_HELPER_RELATIVE_PATH).resolve()
    for root in roots:
        install_root = home / root.skills_dir

        skill_path = install_root / "asl/SKILL.md"
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError as error:
            checks.append(failure("asl-skill", f"cannot read {skill_path}: {error}"))
        else:
            if ASL_SKILL_REQUIRED_TEXT in text:
                checks.append(
                    success("asl-skill", f"{skill_path} front-loads the reference"),
                )
            else:
                checks.append(
                    failure(
                        "asl-skill",
                        f"{skill_path} is missing the reference-loading instruction",
                    ),
                )

        reference = install_root / startup_context.INSTALLED_REFERENCE_PATH
        if not reference.is_file():
            checks.append(
                failure("asl-reference", f"{reference} is missing"),
            )
        else:
            expected_block = startup_context.render_block(
                startup_context.REFERENCE_HARNESS
            )
            reference_text = reference.read_text(encoding="utf-8")
            problem = startup_context.block_problem(reference_text, expected_block)
            if problem is not None:
                checks.append(failure("asl-reference", f"{reference} {problem}"))
            elif reference_text != startup_context.render_reference():
                checks.append(
                    failure(
                        "asl-reference",
                        f"{reference} does not match the generated reference",
                    ),
                )
            else:
                marker = expected_block.split("\n", 1)[0]
                checks.append(
                    success("asl-reference", f"{reference} carries {marker}"),
                )

        helper = install_root / ASL_HELPER_RELATIVE_PATH
        if not helper.is_file():
            checks.append(failure("asl-helper", f"{helper} is missing"))
        elif helper.resolve() != expected_helper.resolve():
            checks.append(
                failure(
                    "asl-helper",
                    f"{helper} resolves to {helper.resolve()}; "
                    f"expected {expected_helper.resolve()}",
                ),
            )
        else:
            checks.append(
                success("asl-helper", f"{helper} resolves to {expected_helper}"),
            )
    return checks
