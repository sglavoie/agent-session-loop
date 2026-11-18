"""Conservative recognition of durable suites and pre-commit cleanliness gates."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePath


def durable_suite_command(argv: Sequence[str]) -> bool:
    """Recognize conventional suite entry points; arbitrary commands stay opt-in."""
    if not argv:
        return False
    executable = PurePath(argv[0]).name
    if executable in ("pytest", "mypy", "pyright", "eslint", "tsc", "shellcheck"):
        return True
    if executable in ("python", "python3"):
        return (
            len(argv) > 2
            and argv[1] == "-m"
            and argv[2]
            in (
                "pytest",
                "unittest",
                "mypy",
                "ruff",
            )
        )
    if executable == "ruff":
        return len(argv) > 1 and argv[1] in ("check", "format")
    if executable in ("go", "cargo", "swift"):
        return len(argv) > 1 and argv[1] in ("test", "check", "build", "clippy")
    if executable in ("just", "make", "npm", "pnpm", "yarn", "bun", "operator"):
        return any(
            re.fullmatch(r"(?:test|lint|check|build|ci)(?:[-_:].+|[A-Z].*)?", arg)
            for arg in argv[1:]
        )
    return False


def clean_tree_gate(argv: Sequence[str]) -> bool:
    """Recognize assertions about an empty Git worktree, without running code."""
    if not argv:
        return False
    if PurePath(argv[0]).name == "git" and "diff" in argv:
        # Path-limited diffs can deliberately guard files a session never edits.
        return set(argv[1:]) <= {
            "diff",
            "--quiet",
            "--exit-code",
            "--cached",
            "--staged",
        } and any(arg in ("--quiet", "--exit-code") for arg in argv)
    script = " ".join(argv)
    return re.search(
        r"git.{0,100}status.{0,100}--porcelain", script, re.DOTALL
    ) is not None and any(
        token in script
        for token in (
            "assert",
            "test -z",
            "[ -z",
            "exit(",
            "exit ",
            "==",
            "!=",
        )
    )
