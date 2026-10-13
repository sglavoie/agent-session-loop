"""Nested command help, mailbox activation, review-guard/resume overlay tests."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    events,
    mailbox_leases,
    review_guard,
    run_entry,
    run_selection,
    runner,
    state,
)
from tests.support import (
    REPO_ROOT,
)


class ParserReviewGuardAndResumeOverlayTests(unittest.TestCase):
    """Nested command help, mailbox activation, review-guard/resume overlay tests."""

    def test_nested_command_groups_route_agents_to_leaf_help(self) -> None:
        parser = cli.build_parser()
        groups = {
            "bridge": "asl bridge serve --help",
            "workbench": "asl workbench serve --help",
            "control": "asl control abort --help",
            "inbox": "asl inbox reply --help",
            "outbox": "asl outbox supervise --help",
            "assurance": "asl assurance list --help",
        }

        for group, leaf_example in groups.items():
            with self.subTest(group=group):
                group_help = io.StringIO()
                with (
                    redirect_stdout(group_help),
                    self.assertRaises(SystemExit) as printed,
                ):
                    parser.parse_args([group, "--help"])
                self.assertEqual(0, printed.exception.code)
                help_text = group_help.getvalue()

                if group == "workbench":
                    # The workbench parent installs the serve flags and runs
                    # them bare, so its help must not deny the flags it prints.
                    self.assertIn("--host HOST", help_text)
                    self.assertNotIn(
                        "This help lists the `asl workbench` leaf commands only.",
                        help_text,
                    )
                    self.assertIn(
                        "This help lists the `asl workbench` leaf commands plus "
                        "the flags the bare `asl workbench` form accepts.",
                        help_text,
                    )
                    self.assertIn(
                        "Bare `asl workbench` runs `asl workbench serve` with "
                        "those flags; every other leaf has its own, so read "
                        f"`{leaf_example}` or that leaf's help.",
                        help_text,
                    )
                else:
                    self.assertNotIn("--host HOST", help_text)
                    self.assertIn(
                        f"This help lists the `asl {group}` leaf commands only.",
                        help_text,
                    )
                    self.assertIn(
                        "Flags belong to a leaf, so read "
                        f"`{leaf_example}` for its flags.",
                        help_text,
                    )
                self.assertEqual(
                    group in ("control", "inbox", "outbox"),
                    "Run `asl supervisor-guide` for the whole supervision workflow."
                    in help_text,
                )

        bare = parser.parse_args(["workbench", "--host", "10.0.0.2"])
        self.assertIsNone(bare.workbench_command)
        self.assertEqual("10.0.0.2", bare.host)

    def test_building_the_parser_does_not_import_the_supervisor_guide(self) -> None:
        # The guide is registered through `handler_ref`, so neither importing
        # nor building the CLI may load it. Run in a clean interpreter because
        # sys.modules is process-global and this module imports the guide.
        probe = (
            "import sys\n"
            "from agent_session_loop import cli\n"
            "cli.build_parser()\n"
            "print('agent_session_loop.supervisor_guide' in sys.modules)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("False", completed.stdout.strip(), completed.stdout)

    def test_mailbox_activation_accepts_fresh_lease_and_honors_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            plan_dir.mkdir()
            mailbox_leases.write_lease(
                plan_dir,
                "11111111-1111-4111-8111-111111111111",
                state_dir=state_dir,
            )

            enabled = run_selection._mailbox_activation_payload(
                argparse.Namespace(state_dir=str(state_dir), no_mailbox=False),
                plan_dir,
            )
            disabled = run_selection._mailbox_activation_payload(
                argparse.Namespace(state_dir=str(state_dir), no_mailbox=True),
                plan_dir,
            )

            self.assertTrue(enabled["enabled"])
            self.assertEqual("fresh-lease", enabled["reason"])
            self.assertTrue(enabled["lease_fresh"])
            self.assertFalse(disabled["enabled"])
            self.assertEqual("disabled-by-flag", disabled["reason"])
            self.assertFalse(disabled["lease_fresh"])

    def test_review_guard_exclude_is_repeatable_and_defaults_unset(self) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        repeated_args = parser.parse_args(
            [
                "run",
                "/tmp/plan",
                "--review-guard-exclude",
                "claude/.claude-personal",
                "--review-guard-exclude",
                "notes",
            ]
        )

        self.assertIsNone(default_args.review_guard_exclude)
        self.assertEqual(
            ["claude/.claude-personal", "notes"],
            repeated_args.review_guard_exclude,
        )
        self.assertIn("review_guard_exclude", run_entry._RESUME_OVERLAY_DESTS)

    def test_review_guard_include_ignored_defaults_off_and_round_trips(
        self,
    ) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        enabled_args = parser.parse_args(
            ["run", "/tmp/plan", "--review-guard-include-ignored"]
        )

        self.assertFalse(default_args.review_guard_include_ignored)
        self.assertFalse(review_guard._review_guard_include_ignored(default_args))
        self.assertTrue(enabled_args.review_guard_include_ignored)
        self.assertTrue(review_guard._review_guard_include_ignored(enabled_args))
        self.assertIn("review_guard_include_ignored", run_entry._RESUME_OVERLAY_DESTS)

    def test_allow_external_changes_repeats_and_round_trips(self) -> None:
        parser = cli.build_parser()

        default_args = parser.parse_args(["run", "/tmp/plan"])
        repeated_args = parser.parse_args(
            [
                "run",
                "/tmp/plan",
                "--allow-external-changes",
                "scripts",
                "--allow-external-changes",
                "dotfiles",
            ]
        )

        self.assertIsNone(default_args.allow_external_changes)
        self.assertEqual(["scripts", "dotfiles"], repeated_args.allow_external_changes)
        self.assertIn("allow_external_changes", run_entry._RESUME_OVERLAY_DESTS)

    def test_review_guard_exclude_merges_env_and_flags(self) -> None:
        args = cli.build_parser().parse_args(
            [
                "run",
                "/tmp/plan",
                "--review-guard-exclude",
                "claude/",
                "--review-guard-exclude",
                "./nested/dir",
            ]
        )

        with mock.patch.dict(
            os.environ,
            {
                review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR: os.pathsep.join(
                    ["notes", "claude"]
                )
            },
        ):
            resolved = review_guard._review_guard_excludes(args)

        self.assertEqual(("claude", "nested/dir", "notes"), resolved)

    def test_review_guard_exclude_rejects_absolute_and_parent_paths(self) -> None:
        parser = cli.build_parser()

        for value in ("/etc", "../outside", "a/../../b"):
            with self.subTest(value=value):
                args = parser.parse_args(
                    ["run", "/tmp/plan", "--review-guard-exclude", value]
                )
                with self.assertRaises(runner.RunError):
                    review_guard._review_guard_excludes(args)

    def test_review_guard_exclude_invalid_value_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(Path(tmp) / "plan"),
                    "--cwd",
                    tmp,
                    "--review-guard-exclude",
                    "../escape",
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual("input-error", payload["reason"])
            self.assertIn("--review-guard-exclude", payload["error"])

    def test_resume_overlay_rehydrates_review_guard_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            plan_dir.mkdir()
            plan_state_dir = state.plan_state_dir(plan_dir, state_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / events.STATE_FILE_NAME).write_text(
                json.dumps({"run_config": {"review_guard_exclude": ["claude"]}}),
                encoding="utf-8",
            )
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(state_dir),
            ]
            args = cli.build_parser().parse_args(tokens)

            overlaid = run_entry._apply_resume_config(args, tokens)

            self.assertEqual(["claude"], overlaid.review_guard_exclude)

    def test_resume_overlay_rehydrates_no_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            plan_dir.mkdir()
            plan_state_dir = state.plan_state_dir(plan_dir, state_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / events.STATE_FILE_NAME).write_text(
                json.dumps({"run_config": {"no_mailbox": True}}),
                encoding="utf-8",
            )
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(state_dir),
            ]
            args = cli.build_parser().parse_args(tokens)

            overlaid = run_entry._apply_resume_config(args, tokens)

            self.assertTrue(overlaid.no_mailbox)


if __name__ == "__main__":
    unittest.main()
