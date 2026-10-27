"""Tests for test asl commit message modes."""

from __future__ import annotations

import json
import unittest

from agent_session_loop import commit_message
from tests import support_asl_commit_message as fixtures
from tests.support import (
    git_stdout,
)


class CommitHandlerTests(fixtures.CommitHandlerCase):
    def test_from_id_json_reports_that_nothing_was_committed(self) -> None:
        sha = git_stdout(self.repo, "rev-parse", "HEAD")

        code, out, err = self.run_commit(str(self.repo), "--from-id", sha, "--json")

        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual("from-id", payload["mode"])
        self.assertFalse(payload["committed"])
        self.assertEqual(sha, payload["from_id"])

    def test_from_id_rejects_an_unresolvable_sha(self) -> None:
        code, _out, err = self.run_commit(str(self.repo), "--from-id", "deadbee")

        self.assertEqual(1, code)
        self.assertIn("commit not found", err)

    def test_prepare_only_emits_the_header_then_body_contract(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--guidance", "be terse"
        )

        self.assertEqual(0, code, err)
        self.assertEqual(before, self.commit_count())
        # The exact parse the retired `gitsummary-prepare.py` consumers did.
        header_line, _blank, body = out.partition("\n\n")
        header = json.loads(header_line)
        self.assertEqual(
            [
                "mode",
                "yes",
                "no_verify",
                "guidance",
                "is_truncated",
                "truncation_notes",
                "from_id",
                "snapshot",
            ],
            list(header),
        )
        self.assertEqual("commit", header["mode"])
        self.assertFalse(header["yes"])
        self.assertTrue(header["no_verify"])
        self.assertEqual("be terse", header["guidance"])
        self.assertFalse(header["is_truncated"])
        self.assertEqual("", header["truncation_notes"])
        self.assertIsNone(header["from_id"])
        # An unscoped invocation commits the index it is handed, so it has no
        # named-path snapshot to carry forward.
        self.assertIsNone(header["snapshot"])
        self.assertIn("diff --git a/src/alpha.py b/src/alpha.py", body)
        # No harness turn is needed to prepare.
        self.assertFalse(self.record_path.exists())

    def test_prepare_only_stages_the_worktree_like_the_skill_does(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(str(self.repo), "--prepare-only")

        self.assertEqual(0, code, err)
        self.assertTrue(commit_message.has_staged_changes(self.repo))

    def test_prepare_only_reports_truncation_in_its_header(self) -> None:
        (self.repo / "assets").mkdir()
        (self.repo / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(str(self.repo), "--prepare-only")

        self.assertEqual(0, code, err)
        header = json.loads(out.partition("\n\n")[0])
        self.assertTrue(header["is_truncated"])
        self.assertIn("1 binary file(s) summarized", header["truncation_notes"])

    def test_prepare_only_from_id_reports_the_from_id_mode(self) -> None:
        sha = git_stdout(self.repo, "rev-parse", "HEAD")

        code, out, err = self.run_commit(
            str(self.repo), "--prepare-only", "--from-id", sha
        )

        self.assertEqual(0, code, err)
        header = json.loads(out.partition("\n\n")[0])
        self.assertEqual("from-id", header["mode"])
        self.assertEqual(sha, header["from_id"])

    def test_prepare_only_json_carries_the_same_header_plus_the_diff(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(str(self.repo), "--prepare-only", "--json")

        self.assertEqual(0, code, err)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual("prepare-only", payload["mode"])
        self.assertEqual("commit", payload["header"]["mode"])
        self.assertIn("diff --git a/src/alpha.py", payload["diff"])

    def test_message_file_commits_without_a_harness(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        message_path = self.tmp_path / "message.txt"
        message_path.write_text(
            "fix: correct the alpha greeting\n\nThe module printed the wrong "
            "word, which every caller then repeated downstream.\n",
            encoding="utf-8",
        )
        before = self.commit_count()

        # No harness is configured at all, proving this mode never needs one.
        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--message-file",
            str(message_path),
            env={"XDG_CONFIG_HOME": str(self.tmp_path / "absent")},
        )

        self.assertEqual(0, code, err)
        self.assertEqual(before + 1, self.commit_count())
        self.assertEqual(
            "fix: correct the alpha greeting", self.head_message().splitlines()[0]
        )
        self.assertFalse(self.record_path.exists())

    def test_message_file_dash_reads_stdin_and_formats_the_message(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--message-file",
            "-",
            stdin=(
                "feat: add alpha\n\n"
                "This body is deliberately written as one very long single line "
                "so that the formatter has to rewrap it at eighty columns before "
                "git ever sees the text.\n"
            ),
        )

        self.assertEqual(0, code, err)
        body_lines = self.head_message().splitlines()[2:]
        self.assertTrue(body_lines)
        self.assertTrue(all(len(line) <= 80 for line in body_lines), body_lines)
        self.assertIn("feat: add alpha", out)

    def test_message_file_strips_a_disallowed_trailer_before_committing(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo),
            "--yes",
            "--message-file",
            "-",
            stdin="feat: add alpha\n\nA body.\n\nCo-authored-by: Someone <a@b.c>\n",
        )

        self.assertEqual(0, code, err)
        self.assertNotIn("Co-authored-by", self.head_message())

    def test_message_file_refuses_an_empty_message(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        before = self.commit_count()

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--message-file", "-", stdin="   \n\n"
        )

        self.assertEqual(1, code)
        self.assertEqual(before, self.commit_count())
        self.assertIn("empty", err)

    def test_message_file_refuses_a_missing_file(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")

        code, _out, err = self.run_commit(
            str(self.repo), "--yes", "--message-file", str(self.tmp_path / "absent.txt")
        )

        self.assertEqual(1, code)
        self.assertIn("cannot read message file", err)
        # The OS reason alone, so the path is printed once, not twice.
        self.assertIn("absent.txt: No such file or directory", err)
        self.assertNotIn("Errno", err)
        self.assertEqual(1, err.count("absent.txt"))

    def test_contradictory_mode_flags_are_refused(self) -> None:
        combinations = (
            ("--from-id", "HEAD", "--staged"),
            ("--from-id", "HEAD", "--pathspec", "src/alpha.py"),
            ("--from-id", "HEAD", "--message-file", "-"),
            ("--prepare-only", "--message-file", "-"),
            ("--staged", "--pathspec", "src/alpha.py"),
        )
        for combination in combinations:
            with self.subTest(combination=combination):
                code, out, err = self.run_commit(str(self.repo), "--json", *combination)
                self.assertEqual(1, code)
                payload = json.loads(out)
                self.assertEqual(
                    commit_message.REASON_INVALID_ARGUMENTS, payload["reason"]
                )
                self.assertIn("mutually exclusive", payload["error"])

    def test_from_id_prints_a_message_without_staging_or_committing(self) -> None:
        self.write("src/alpha.py", "print('alpha')\n")
        commit_message.stage_all(self.repo)
        git_stdout(self.repo, "commit", "-m", "add alpha", "--no-verify")
        sha = git_stdout(self.repo, "rev-parse", "--short", "HEAD")
        self.write("src/beta.py", "print('beta')\n")
        before = self.commit_count()

        code, out, err = self.run_commit(str(self.repo), "--from-id", sha)

        self.assertEqual(0, code, err)
        self.assertEqual(before, self.commit_count())
        self.assertIn("feat: add the fake codex commit branch", out)
        self.assertIn("src/alpha.py", self.recorded_turn()["prompt"])
        # The unrelated worktree change was never staged.
        self.assertIn("?? src/beta.py", self.porcelain())


if __name__ == "__main__":
    unittest.main()
