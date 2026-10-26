"""Tests for test asl commit message git."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import commit_message, config
from tests.support import (
    git_stdout,
    init_git_repo,
    install_fake_codex,
    patched_env,
)


class GitSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        init_git_repo(self.repo)

    def write(self, name: str, text: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def staged_names(self) -> list[str]:
        listing = git_stdout(self.repo, "diff", "--cached", "--name-only")
        return sorted(listing.splitlines()) if listing else []

    def test_find_git_root_walks_upward_from_a_nested_path(self) -> None:
        nested = self.repo / "a" / "b" / "c"
        nested.mkdir(parents=True)

        self.assertEqual(commit_message.find_git_root(nested), self.repo.resolve())
        self.assertEqual(commit_message.find_git_root(self.repo), self.repo.resolve())

    def test_find_git_root_returns_none_outside_a_worktree(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()

        # The temp dir itself is not a worktree; only ``repo`` is.
        self.assertIsNone(commit_message.find_git_root(outside))

    def test_change_predicates_track_the_worktree_and_the_index(self) -> None:
        self.assertFalse(commit_message.has_changes(self.repo))
        self.assertFalse(commit_message.has_staged_changes(self.repo))

        self.write("new.txt", "hello\n")
        self.assertTrue(commit_message.has_changes(self.repo))
        self.assertFalse(commit_message.has_staged_changes(self.repo))

        commit_message.stage_all(self.repo)
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_stage_all_stages_every_change(self) -> None:
        self.write("one.txt", "one\n")
        self.write("nested/two.txt", "two\n")

        result = commit_message.stage_all(self.repo)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.staged_names(), ["nested/two.txt", "one.txt"])

    def test_stage_paths_stages_only_the_named_files(self) -> None:
        self.write("keep.txt", "keep\n")
        self.write("also.txt", "also\n")
        self.write("untouched.txt", "untouched\n")

        result = commit_message.stage_paths(self.repo, ["keep.txt", "also.txt"])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.staged_names(), ["also.txt", "keep.txt"])
        self.assertIn("untouched.txt", git_stdout(self.repo, "status", "--porcelain"))

    def test_stage_paths_accepts_a_deletion_already_staged(self) -> None:
        self.write("gone.txt", "gone\n")
        self.write("worktree_gone.txt", "gone too\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-qm", "seed")
        git_stdout(self.repo, "rm", "-q", "gone.txt")
        (self.repo / "worktree_gone.txt").unlink()
        self.write("changed.txt", "changed\n")

        result = commit_message.stage_paths(
            self.repo, ["gone.txt", "worktree_gone.txt", "changed.txt"]
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.staged_names(), ["changed.txt", "gone.txt", "worktree_gone.txt"]
        )

    def test_scoped_paths_are_literal_rather_than_pathspec_patterns(self) -> None:
        # `[id]` would glob-match `i`, and a leading colon would parse as magic.
        self.write("seed.txt", "seed\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-qm", "seed")
        self.write("app/[id]/page.tsx", "dynamic\n")
        self.write("app/i/page.tsx", "static\n")
        self.write(":notes.md", "notes\n")
        owned = ["app/[id]/page.tsx", ":notes.md"]

        result = commit_message.stage_paths(self.repo, owned)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.staged_names(), [":notes.md", "app/[id]/page.tsx"])
        self.assertTrue(commit_message.has_staged_changes(self.repo, owned))
        commit_message.pathspec_snapshot(self.repo, owned)
        self.assertEqual([], commit_message.worktree_drifted_paths(self.repo, owned))
        committed = commit_message.commit_staged(self.repo, "scoped", pathspec=owned)
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assertEqual(
            sorted(
                git_stdout(
                    self.repo, "show", "--name-only", "--format=", "HEAD"
                ).splitlines()
            ),
            [":notes.md", "app/[id]/page.tsx"],
        )

    def test_stage_paths_still_rejects_an_unknown_path(self) -> None:
        result = commit_message.stage_paths(self.repo, ["never-existed.txt"])

        self.assertNotEqual(result.returncode, 0)

    def test_stage_paths_with_no_paths_stages_nothing(self) -> None:
        self.write("keep.txt", "keep\n")

        result = commit_message.stage_paths(self.repo, [])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.staged_names(), [])

    def test_capture_staged_diff_reads_the_index(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        commit_message.stage_all(self.repo)

        result = commit_message.capture_staged_diff(self.repo)

        self.assertFalse(result.is_empty)
        self.assertEqual(result.files_included, ["src/alpha.py"])
        self.assertIn("+print('alpha')", result.diff)
        self.assertEqual(result.total_size, len(result.diff.encode("utf-8")))

    def test_capture_ignores_diff_configuration_and_quoted_names(self) -> None:
        # Each of these settings used to leave no header the parser matched,
        # so a real staged change was reported as an empty diff.
        for key, value in (
            ("diff.noprefix", "true"),
            ("diff.mnemonicPrefix", "true"),
            ("color.diff", "always"),
            ("color.ui", "always"),
            ("core.quotePath", "true"),
        ):
            git_stdout(self.repo, "config", key, value)
        self.write("plain.txt", "plain\n")
        self.write("ünï.txt", "umlaut\n")
        self.write('qu"ote.txt', "quote\n")
        self.write("tab\there.txt", "tab\n")
        commit_message.stage_all(self.repo)

        result = commit_message.capture_staged_diff(self.repo)

        self.assertFalse(result.is_empty)
        self.assertEqual(
            sorted(result.files_included),
            sorted(["plain.txt", "ünï.txt", 'qu"ote.txt', "tab\there.txt"]),
        )
        self.assertNotIn("\x1b[", result.diff)
        self.assertIn("+umlaut", result.diff)
        git_stdout(self.repo, "commit", "-qm", "seed", "--no-verify")
        committed = commit_message.capture_commit_diff("HEAD", self.repo)
        self.assertEqual(
            sorted(committed.files_included), sorted(result.files_included)
        )

    def test_capture_staged_diff_on_a_clean_index_is_empty(self) -> None:
        result = commit_message.capture_staged_diff(self.repo)

        self.assertTrue(result.is_empty)
        self.assertEqual(result.diff, "")

    def test_capture_commit_diff_resolves_a_short_sha(self) -> None:
        self.write("src/beta.py", "print('beta')\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-m", "add beta", "--no-verify")
        short_sha = git_stdout(self.repo, "rev-parse", "--short", "HEAD")

        result = commit_message.capture_commit_diff(short_sha, self.repo)

        self.assertEqual(result.files_included, ["src/beta.py"])
        self.assertIn("+print('beta')", result.diff)

    def test_capture_commit_diff_rejects_an_unresolvable_sha(self) -> None:
        with self.assertRaises(commit_message.CommitMessageError) as caught:
            commit_message.capture_commit_diff("deadbee", self.repo)

        self.assertIn("commit not found", str(caught.exception))

    def test_git_helper_reports_a_missing_executable_without_raising(self) -> None:
        result = commit_message._git(self.repo, ["definitely-not-a-git-command"])

        self.assertNotEqual(result.returncode, 0)


class HarnessResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.config_home = self.tmp_path / "config"
        self.config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def test_an_explicit_harness_wins_over_configuration(self) -> None:
        self.config_path.write_text(
            json.dumps({"codex": {"executable": "codex"}}), encoding="utf-8"
        )
        with patched_env(XDG_CONFIG_HOME=str(self.config_home)):
            self.assertEqual("claude", commit_message.resolve_harness("claude"))

    def test_a_configured_installed_harness_is_preferred(self) -> None:
        fake = install_fake_codex(self.tmp_path)
        self.config_path.write_text(
            json.dumps({"copilot": {"executable": str(fake)}}), encoding="utf-8"
        )
        with patched_env(XDG_CONFIG_HOME=str(self.config_home)):
            self.assertEqual("copilot", commit_message.resolve_harness())

    def test_no_installed_harness_is_reported_rather_than_launched(self) -> None:
        self.config_path.write_text(
            json.dumps(
                {
                    name: {"executable": str(self.tmp_path / f"absent-{name}")}
                    for name in ("codex", "claude", "copilot")
                }
            ),
            encoding="utf-8",
        )
        with (
            patched_env(XDG_CONFIG_HOME=str(self.config_home)),
            self.assertRaises(commit_message.CommitMessageError) as caught,
        ):
            commit_message.resolve_harness()

        self.assertEqual(commit_message.REASON_NO_HARNESS, caught.exception.reason)
        self.assertIn("no harness executable found", str(caught.exception))

    def test_settings_report_a_flag_model_and_effort(self) -> None:
        fake = install_fake_codex(self.tmp_path)
        self.config_path.write_text(
            json.dumps({"codex": {"executable": str(fake)}}), encoding="utf-8"
        )
        with patched_env(XDG_CONFIG_HOME=str(self.config_home)):
            settings = commit_message.generation_settings("codex", "gpt-5", "high")

        self.assertEqual("codex", settings["harness"])
        self.assertEqual(str(fake), settings["harness_executable"])
        self.assertEqual("gpt-5", settings["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, settings["model_source"])
        self.assertEqual("high", settings["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, settings["effort_source"])


if __name__ == "__main__":
    unittest.main()
