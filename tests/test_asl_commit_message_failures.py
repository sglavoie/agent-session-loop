"""Tests for test asl commit message failures."""

from __future__ import annotations

import inspect
import json
import time
import unittest
from unittest import mock

from agent_session_loop import cli, commit_message, config
from tests import support_asl_commit_message as fixtures
from tests.support import (
    GENERATION_TIMEOUT_PROBE_SECONDS,
    git_stdout,
    wait_for_process_exit,
)


class CommitHandlerTests(fixtures.CommitHandlerCase):
    def test_an_empty_commit_is_refused_as_an_empty_diff(self) -> None:
        git_stdout(self.repo, "commit", "--allow-empty", "-m", "nothing", "--no-verify")

        code, out, _err = self.run_commit(str(self.repo), "--from-id", "HEAD", "--json")

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_EMPTY_DIFF, payload["reason"])
        self.assertFalse(self.record_path.exists())

    def test_an_all_binary_change_is_named_as_such(self) -> None:
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
        commit_message.stage_all(self.repo)
        self.assertTrue(commit_message.changes_are_all_binary(self.repo))

        # Git still emits a header and a `[Binary file]` summary for a binary
        # change, so the processed diff is only ever empty when git itself
        # produced no patch text; the probe above is what tells that case apart
        # from having nothing staged at all.
        empty = commit_message.DiffResult(diff="", is_empty=True)
        with mock.patch.object(
            commit_message, "capture_staged_diff", return_value=empty
        ):
            code, out, _err = self.run_commit(str(self.repo), "--yes", "--json")

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(commit_message.REASON_ALL_BINARY, payload["reason"])
        self.assertIn("all changed files are binary", payload["error"])

    def test_a_mixed_change_is_not_reported_as_all_binary(self) -> None:
        (self.repo / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
        self.write("src/alpha.py", "print('alpha')\n")
        commit_message.stage_all(self.repo)

        self.assertFalse(commit_message.changes_are_all_binary(self.repo))

    def test_not_a_git_worktree_is_refused(self) -> None:
        outside = self.tmp_path / "outside"
        outside.mkdir()

        code, _out, err = self.run_commit(str(outside), "--yes")

        self.assertEqual(1, code)
        self.assertIn("not a git worktree", err)

    def test_json_reports_the_commit_and_the_resolved_generation_settings(
        self,
    ) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(str(self.repo), "--yes", "--json")

        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual("commit", payload["command"])
        self.assertEqual("commit", payload["mode"])
        self.assertTrue(payload["committed"])
        self.assertTrue(payload["generated"])
        self.assertEqual(git_stdout(self.repo, "rev-parse", "HEAD"), payload["commit"])
        self.assertEqual("codex", payload["harness"])
        self.assertEqual(str(self.fake_codex), payload["harness_executable"])
        self.assertEqual(config.MODEL_SOURCE_UNRESOLVED, payload["model_source"])
        self.assertIsNone(payload["effort"])
        self.assertIsNone(payload["effort_source"])

    def test_a_nonzero_harness_exit_is_classified_not_reported_raw(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            env={"FAKE_CODEX_COMMIT_MODE": "quota"},
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_GENERATION_FAILED, payload["reason"])
        classification = payload["details"]["classification"]
        self.assertEqual("quota-exhausted", classification["reason"])
        self.assertEqual(7, classification["returncode"])
        # `retry_hints` resolves the provider hint to an absolute instant.
        self.assertIsNotNone(payload["details"]["retry_at"])
        # The staged change survives, so the operator can retry or commit by hand.
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_an_unrecognized_failure_falls_closed_to_nonzero_exit(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            env={"FAKE_CODEX_COMMIT_MODE": "failure"},
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual("nonzero-exit", payload["details"]["classification"]["reason"])
        self.assertIsNone(payload["details"]["retry_at"])

    def test_generation_timeout_is_bounded(self) -> None:
        # The bound is the command's own, not the caller's: `asl commit` with
        # no `--timeout` still refuses to wait forever, which is what keeps a
        # stalled message harness from hanging a standalone commit.
        parser_default = cli.build_parser().parse_args(["commit"]).timeout
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS, parser_default
        )
        self.assertIsNotNone(parser_default)
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS,
            inspect.signature(commit_message.generate_commit_message)
            .parameters["timeout"]
            .default,
        )

        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()
        pid_path = self.tmp_path / "hang-child.pid"

        started = time.monotonic()
        code, out, _err = self.run_commit(
            str(self.repo),
            "--yes",
            "--json",
            "--timeout",
            str(GENERATION_TIMEOUT_PROBE_SECONDS),
            env={
                "FAKE_CODEX_COMMIT_MODE": "hang",
                "FAKE_CODEX_COMMIT_HANG_CHILD_PID": str(pid_path),
            },
        )
        elapsed = time.monotonic() - started

        self.assertEqual(1, code)
        # The bound is what ended the turn, not the fixture's own 600s sleep.
        self.assertLess(elapsed, 120, "the harness turn outlived its bound")
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_GENERATION_TIMEOUT, payload["reason"])
        self.assertEqual(
            GENERATION_TIMEOUT_PROBE_SECONDS, payload["details"]["timeout_seconds"]
        )
        self.assertIn(
            "did not produce a commit message within "
            f"{GENERATION_TIMEOUT_PROBE_SECONDS}s",
            payload["error"],
        )
        # Nothing was committed and the staged change survives for a retry.
        self.assertEqual(before, self.commit_count())
        self.assertTrue(commit_message.has_staged_changes(self.repo))
        # The whole process tree went down, not just the harness process: the
        # grandchild inherited the turn's process group and is killed with it.
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        self.assertFalse(
            wait_for_process_exit(child_pid),
            "a harness descendant outlived the generation timeout",
        )

    def test_a_non_positive_timeout_is_refused_before_staging(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, _err = self.run_commit(
            str(self.repo), "--yes", "--json", "--timeout", "0"
        )

        self.assertEqual(1, code)
        payload = json.loads(out)
        self.assertEqual(commit_message.REASON_INVALID_ARGUMENTS, payload["reason"])
        # Refused ahead of staging, so the index is untouched.
        self.assertFalse(commit_message.has_staged_changes(self.repo))
        self.assertFalse(self.record_path.exists())

    def test_an_empty_response_is_rejected(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", env={"FAKE_CODEX_COMMIT_MODE": "empty"}
        )

        self.assertEqual(1, code)
        self.assertIn("returned an empty commit message", err)

    def test_an_api_error_body_is_never_committed(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", env={"FAKE_CODEX_COMMIT_MODE": "api-error"}
        )

        self.assertEqual(1, code)
        self.assertEqual(before, self.commit_count())
        self.assertIn("returned an API error", err)

    def test_a_clean_worktree_is_refused_before_any_harness_turn(self) -> None:
        code, _out, err = self.run_commit(str(self.repo), "--yes")

        self.assertEqual(1, code)
        self.assertIn("no changes to commit", err)
        self.assertFalse(self.record_path.exists())


if __name__ == "__main__":
    unittest.main()
