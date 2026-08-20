"""Verify that the active ASL install points at this checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import cli_trace, startup_context

SKILL_INSTALL_DIRS = (
    ".claude/skills",
    ".claude-personal/skills",
    ".codex/skills",
    ".copilot/skills",
)

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _readable_path(path: Path) -> str:
    return os.fspath(path)


def _success(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": True, "detail": detail}


def _failure(name: str, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": False, "detail": detail}


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


def _expected_skill_dirs(repo_root: Path) -> list[Path]:
    skills_root = repo_root / "skills"
    return sorted(
        (
            path
            for path in skills_root.iterdir()
            if path.is_dir() and (path / "SKILL.md").is_file()
        ),
        key=lambda path: path.name,
    )


def _check_asl_path(
    repo_root: Path, path_env: str | None
) -> tuple[str | None, dict[str, Any]]:
    active_asl = shutil.which("asl", path=path_env)
    if active_asl is None:
        return None, _failure("asl-path", "asl was not found on PATH")

    active_path = Path(active_asl)
    resolved = active_path.resolve()
    expected = (repo_root / "bin/.local/bin/agent-session-loop").resolve()
    if resolved != expected:
        return active_asl, _failure(
            "asl-path",
            f"{active_path} resolves to {resolved}; expected {expected}",
        )
    return active_asl, _success(
        "asl-path",
        f"{active_path} resolves to {resolved}",
    )


def _check_skill_links(repo_root: Path, home: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for install_dir in SKILL_INSTALL_DIRS:
        install_root = home / install_dir
        for skill_dir in _expected_skill_dirs(repo_root):
            installed = install_root / skill_dir.name
            expected = skill_dir.resolve()
            if not installed.exists():
                checks.append(
                    _failure(
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
                        _success(
                            "skill-link",
                            f"{installed_skill} resolves to {expected_skill}",
                        )
                    )
                    continue
                checks.append(
                    _failure(
                        "skill-link",
                        f"{installed} resolves to {resolved}; expected {expected}",
                    )
                )
                continue
            checks.append(
                _success(
                    "skill-link",
                    f"{installed} resolves to {resolved}",
                )
            )
    return checks


def _check_session_run_content(home: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for install_dir in SKILL_INSTALL_DIRS:
        skill_path = home / install_dir / "session-run" / "SKILL.md"
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError as error:
            checks.append(
                _failure(
                    "session-run-content",
                    f"cannot read {skill_path}: {error}",
                )
            )
            continue

        if SESSION_RUN_REQUIRED_TEXT not in text:
            checks.append(
                _failure(
                    "session-run-content",
                    f"{skill_path} is missing the serial-only explicit-selector "
                    f"rejection text",
                )
            )
            continue

        forbidden = [needle for needle in SESSION_RUN_FORBIDDEN_TEXT if needle in text]
        if forbidden:
            checks.append(
                _failure(
                    "session-run-content",
                    f"{skill_path} still contains stale explicit-session text: "
                    f"{forbidden[0]!r}",
                )
            )
            continue

        checks.append(
            _success(
                "session-run-content",
                f"{skill_path} documents serial-only session-run input",
            )
        )
    return checks


def _check_startup_context(home: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for target in startup_context.CONTEXT_TARGETS:
        path = home / target.installed_path
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            checks.append(
                _failure("startup-context", f"cannot read {path}: {error}"),
            )
            continue

        expected = startup_context.render_block(target.harness)
        problem = startup_context.block_problem(text, expected)
        if problem is None:
            marker = expected.split("\n", 1)[0]
            checks.append(
                _success("startup-context", f"{path} carries {marker}"),
            )
            continue

        checks.append(_failure("startup-context", f"{path} {problem}"))
    return checks


def _check_asl_skill(repo_root: Path, home: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    expected_helper = (repo_root / "skills" / ASL_HELPER_RELATIVE_PATH).resolve()
    for install_dir in SKILL_INSTALL_DIRS:
        install_root = home / install_dir

        skill_path = install_root / "asl/SKILL.md"
        try:
            text = skill_path.read_text(encoding="utf-8")
        except OSError as error:
            checks.append(_failure("asl-skill", f"cannot read {skill_path}: {error}"))
        else:
            if ASL_SKILL_REQUIRED_TEXT in text:
                checks.append(
                    _success("asl-skill", f"{skill_path} front-loads the reference"),
                )
            else:
                checks.append(
                    _failure(
                        "asl-skill",
                        f"{skill_path} is missing the reference-loading instruction",
                    ),
                )

        reference = install_root / startup_context.INSTALLED_REFERENCE_PATH
        if not reference.is_file():
            checks.append(
                _failure("asl-reference", f"{reference} is missing"),
            )
        else:
            expected_block = startup_context.render_block(
                startup_context.REFERENCE_HARNESS
            )
            reference_text = reference.read_text(encoding="utf-8")
            problem = startup_context.block_problem(reference_text, expected_block)
            if problem is not None:
                checks.append(_failure("asl-reference", f"{reference} {problem}"))
            elif reference_text != startup_context.render_reference():
                checks.append(
                    _failure(
                        "asl-reference",
                        f"{reference} does not match the generated reference",
                    ),
                )
            else:
                marker = expected_block.split("\n", 1)[0]
                checks.append(
                    _success("asl-reference", f"{reference} carries {marker}"),
                )

        helper = install_root / ASL_HELPER_RELATIVE_PATH
        if not helper.is_file():
            checks.append(_failure("asl-helper", f"{helper} is missing"))
        elif helper.resolve() != expected_helper.resolve():
            checks.append(
                _failure(
                    "asl-helper",
                    f"{helper} resolves to {helper.resolve()}; "
                    f"expected {expected_helper.resolve()}",
                ),
            )
        else:
            checks.append(
                _success("asl-helper", f"{helper} resolves to {expected_helper}"),
            )
    return checks


def _check_status_smoke(
    *,
    active_asl: str | None,
    home: Path,
    path_env: str | None,
) -> dict[str, Any]:
    if active_asl is None:
        return _failure("status-smoke", "skipped because asl was not found on PATH")

    with tempfile.TemporaryDirectory(prefix="asl-install-smoke-") as tmp:
        plan_dir = Path(tmp) / "plan"
        _write_synthetic_plan(plan_dir)
        env = os.environ.copy()
        env["HOME"] = _readable_path(home)
        if path_env is not None:
            env["PATH"] = path_env
        env = cli_trace.child_env(env, cli_trace.ORIGIN_INSTALL_VERIFY)
        completed = subprocess.run(
            [active_asl, "status", _readable_path(plan_dir), "--json"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return _failure(
            "status-smoke",
            f"asl status --json exited {completed.returncode}: {detail}",
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return _failure(
            "status-smoke",
            f"asl status --json did not emit JSON: {error}",
        )

    if not payload.get("ok"):
        return _failure(
            "status-smoke",
            f"asl status --json returned ok=false: {payload}",
        )

    if "parallel_validation_path" in payload:
        return _failure(
            "status-smoke",
            "asl status --json still emits parallel_validation_path",
        )

    return _success(
        "status-smoke",
        "asl status --json succeeded and omitted parallel_validation_path",
    )


def verify_install(
    *,
    repo_root: str | Path | None = None,
    home: str | Path | None = None,
    path_env: str | None = None,
) -> dict[str, Any]:
    """Return a structured active-install verification payload."""

    resolved_repo = Path(repo_root).resolve() if repo_root is not None else _repo_root()
    resolved_home = (
        Path(home).expanduser().resolve() if home is not None else Path.home()
    )
    active_asl, asl_check = _check_asl_path(resolved_repo, path_env)
    checks = [
        asl_check,
        *_check_skill_links(resolved_repo, resolved_home),
        *_check_session_run_content(resolved_home),
        *_check_startup_context(resolved_home),
        *_check_asl_skill(resolved_repo, resolved_home),
        _check_status_smoke(
            active_asl=active_asl,
            home=resolved_home,
            path_env=path_env,
        ),
    ]
    failures = [check for check in checks if not check["ok"]]
    return {
        "ok": not failures,
        "repo_root": _readable_path(resolved_repo),
        "home": _readable_path(resolved_home),
        "active_asl": active_asl,
        "checks": checks,
        "failures": failures,
    }


def _print_text(payload: dict[str, Any]) -> None:
    status = "ok" if payload["ok"] else "FAIL"
    print(f"ASL install verification: {status}")
    print(f"Repo: {payload['repo_root']}")
    print(f"Home: {payload['home']}")
    print(f"Active asl: {payload['active_asl'] or 'not found'}")
    for check in payload["checks"]:
        prefix = "ok" if check["ok"] else "FAIL"
        print(f"{prefix} {check['name']}: {check['detail']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that active ASL wrappers and skills resolve to this "
        "checkout.",
    )
    parser.add_argument(
        "--repo-root",
        help="Checkout root to verify against (default: inferred from this module)",
    )
    parser.add_argument(
        "--home",
        help="Home directory whose installed links should be checked (default: $HOME)",
    )
    parser.add_argument(
        "--path",
        dest="path_env",
        help="PATH to search for asl (default: current process PATH)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = verify_install(
        repo_root=args.repo_root,
        home=args.home,
        path_env=args.path_env,
    )
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        _print_text(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
