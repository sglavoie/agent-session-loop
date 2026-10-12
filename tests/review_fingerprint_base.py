"""Shared fixtures for the review mutation guard fingerprint tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    review_guard,
)
from tests.support import (
    init_git_repo,
)


class ReviewFingerprintBase(unittest.TestCase):
    """Shared fixtures for the review mutation guard fingerprint tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.plan_dir.mkdir()
        init_git_repo(self.repo_root)

    def _fingerprint(
        self, *exclude: str, include_ignored: bool = False
    ) -> dict[str, Any]:
        return review_guard._capture_review_repo_fingerprint(
            self.repo_root,
            self.plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )

    def _write(self, rel: str, text: str) -> None:
        path = self.repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _commit_all(self, message: str) -> None:
        for argv in (["git", "add", "-A"], ["git", "commit", "-m", message]):
            subprocess.run(argv, cwd=self.repo_root, capture_output=True, check=True)

    def _stage(self, rel: str) -> None:
        subprocess.run(
            ["git", "add", rel],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def _info_exclude(self) -> Path:
        return self.repo_root / ".git" / "info" / "exclude"

    def _write_info_exclude(self, text: str) -> None:
        path = self._info_exclude()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _ignore_config(self, fingerprint: dict[str, Any]) -> dict[str, list[str]]:
        entries = fingerprint["ignore_config"]
        assert isinstance(entries, list)
        return {str(rel): [str(kind), str(sha)] for _role, rel, kind, sha in entries}

    def _symlink_info_exclude(self, target: str) -> Path:
        link = self._info_exclude()
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        return link.parent / target

    def _set_excludes_file(self, value: str) -> None:
        subprocess.run(
            ["git", "config", "core.excludesFile", value],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def _symlink_selected_excludes_file(self, target: str) -> Path:
        link = self.repo_root / ".git" / "review-excludes"
        link.symlink_to(target)
        self._set_excludes_file(".git/review-excludes")
        return link.parent / target

    def _git_config(self, key: str, value: str) -> None:
        subprocess.run(
            ["git", "config", key, value],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def _commit_constant_textconv_driver(self) -> None:
        """Commit a diff driver that renders every content identically."""
        self._write(".gitattributes", "*.txt diff=const\n")
        self._write("masked.txt", "one\n")
        self._commit_all("add a constant text converter")
        self._git_config("diff.const.textconv", "printf CONST")

    def _flag_index_entry(self, rel: str, flag: str) -> None:
        subprocess.run(
            ["git", "update-index", flag, rel],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def _git_status_porcelain(self) -> str:
        return self._git("status", "--porcelain").strip()
