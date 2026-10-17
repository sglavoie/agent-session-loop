"""Shared fixtures for support asl commit message."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_session_loop import cli, commit_message, config
from tests.support import (
    git_stdout,
    init_git_repo,
    install_fake_codex,
    patched_env,
)


def build_hunk(index: int, *, lines: int = 6) -> str:
    """One unified-diff hunk with a stable, greppable body."""
    body = "".join(
        f"+hunk {index} line {line} {'x' * 40}\n" for line in range(1, lines + 1)
    )
    return f"@@ -{index * 10},3 +{index * 10},{lines} @@ context {index}\n{body}"


def build_file_diff(path: str, *, hunks: int = 1, lines: int = 6) -> str:
    """A single file's diff section with ``hunks`` hunks."""
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
    )
    return header + "".join(build_hunk(i, lines=lines) for i in range(1, hunks + 1))


def build_binary_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"Binary files a/{path} and b/{path} differ\n"
    )


class CommitHandlerCase(unittest.TestCase):
    """The four `asl commit` modes, driven by the fake codex harness fixture.

    Generation runs the real adapter against the real fixture executable, so
    these exercise the actual audit-profile argv rather than a patched
    ``run_harness``; the fixture records the argv and prompt it was handed.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)

        self.repo = self.tmp_path / "repo"
        init_git_repo(self.repo)

        # `native_model_payload` reads `~/.codex/config.toml`, so the reported
        # model source would otherwise depend on the developer's own machine.
        self.home = self.tmp_path / "home"
        self.home.mkdir()

        self.fake_codex = install_fake_codex(self.tmp_path)
        self.config_home = self.tmp_path / "config"
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )
        self.record_path = self.tmp_path / "commit-turn.json"

    def run_commit(
        self,
        *argv: str,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["commit", *argv])
        overrides: dict[str, str | None] = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "FAKE_CODEX_COMMIT_RECORD": str(self.record_path),
        }
        overrides.update(env or {})

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patched_env(**overrides), redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is None:
                code = commit_message.commit_from_args(args)
            else:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = commit_message.commit_from_args(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def write(self, relative: str, text: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def head_message(self) -> str:
        return git_stdout(self.repo, "log", "-1", "--format=%B").strip()

    def porcelain(self) -> str:
        # `-uall` so an untracked file inside a new directory is named rather
        # than collapsed into its parent.
        return git_stdout(self.repo, "status", "--porcelain", "-uall")

    def commit_count(self) -> int:
        return int(git_stdout(self.repo, "rev-list", "--count", "HEAD"))

    def recorded_turn(self) -> dict[str, Any]:
        """The argv and prompt the fake codex was handed for its one turn."""
        return cast(
            dict[str, Any], json.loads(self.record_path.read_text(encoding="utf-8"))
        )
