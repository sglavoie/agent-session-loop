"""Commit CLI tests."""

from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_session_loop import cli, commit_message, commit_message_output


class CommitCliTests(unittest.TestCase):
    """Commit CLI tests."""

    def test_end_of_input_at_the_prompt_declines_the_commit(self) -> None:
        args = argparse.Namespace(yes=False, json=False)
        stdout = io.StringIO()
        with (
            mock.patch.object(commit_message_output, "_is_tty", return_value=True),
            mock.patch("builtins.input", side_effect=EOFError),
            redirect_stdout(stdout),
        ):
            confirmed = commit_message_output._confirm(args, "Commit?")

        self.assertFalse(confirmed)
        self.assertEqual("\n", stdout.getvalue())

    def test_commit_subparser_is_wired_with_defaults(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(["commit"])

        self.assertEqual("commit_message:commit_from_args", parsed.handler_ref)
        self.assertIs(cli.resolve_handler(parsed), commit_message.commit_from_args)
        self.assertIsNone(parsed.path)
        self.assertFalse(parsed.yes)
        # Hooks are skipped unless `--verify` asks for them, matching the skill.
        self.assertFalse(parsed.verify)
        self.assertFalse(parsed.staged)
        self.assertFalse(parsed.dry_run)
        self.assertIsNone(parsed.guidance)
        self.assertIsNone(parsed.from_id)
        self.assertIsNone(parsed.pathspec)
        self.assertIsNone(parsed.harness)
        self.assertIsNone(parsed.model)
        self.assertIsNone(parsed.effort)
        self.assertFalse(parsed.prepare_only)
        self.assertIsNone(parsed.message_file)
        self.assertFalse(parsed.json)

    def test_commit_flags_parse_into_their_handler_dests(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(
            [
                "commit",
                "/tmp/repo",
                "--yes",
                "--verify",
                "--staged",
                "--dry-run",
                "--guidance",
                "be terse",
                "--harness",
                "claude",
                "--model",
                "gpt-5",
                "--effort",
                "high",
                "--prepare-only",
                "--json",
            ]
        )

        self.assertEqual("/tmp/repo", parsed.path)
        self.assertTrue(parsed.yes)
        self.assertTrue(parsed.verify)
        self.assertTrue(parsed.staged)
        self.assertTrue(parsed.dry_run)
        self.assertEqual("be terse", parsed.guidance)
        self.assertEqual("claude", parsed.harness)
        self.assertEqual("gpt-5", parsed.model)
        self.assertEqual("high", parsed.effort)
        self.assertTrue(parsed.prepare_only)
        self.assertTrue(parsed.json)

    def test_verify_and_no_verify_share_one_dest(self) -> None:
        parser = cli.build_parser()

        self.assertTrue(parser.parse_args(["commit", "--verify"]).verify)
        self.assertFalse(
            parser.parse_args(["commit", "--verify", "--no-verify"]).verify
        )

    def test_pathspec_accumulates_and_from_id_takes_a_sha(self) -> None:
        parser = cli.build_parser()
        parsed = parser.parse_args(
            ["commit", "--pathspec", "a.py", "--pathspec", "docs/b.md"]
        )

        self.assertEqual(["a.py", "docs/b.md"], parsed.pathspec)
        self.assertEqual(
            "abc1234", parser.parse_args(["commit", "--from-id", "abc1234"]).from_id
        )

    def test_an_unknown_harness_is_rejected_by_the_parser(self) -> None:
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["commit", "--harness", "nope"])


if __name__ == "__main__":
    unittest.main()
