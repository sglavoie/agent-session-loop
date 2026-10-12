#!/usr/bin/env python3
"""Tests for the asl autopilot argument parser, main entry, and command parsing."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    run_inputs,
    setUpModule,
    tearDownModule,
)


class ArgParserTest(unittest.TestCase):
    def test_parser_exposes_stable_option_surface(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(
            [
                "--command",
                "asl run /tmp/plan",
                "--json",
                "--reply-stdin",
                "--wait-quota-reset",
                "--detach",
                "--supervision-log-dir",
                "/tmp/logs",
            ]
        )
        self.assertEqual(args.command, "asl run /tmp/plan")
        self.assertTrue(args.json)
        self.assertTrue(args.reply_stdin)
        self.assertTrue(args.wait_quota_reset)
        self.assertTrue(args.detach)
        self.assertEqual(args.supervision_log_dir, "/tmp/logs")

    def test_helper_no_longer_accepts_review_fix_plan_flag(self) -> None:
        # The runner owns repair cycles (Session 18); the helper-only
        # --max-review-fix-plans option is gone. It survives only inside the
        # `asl run ...` command string as a deprecated alias.
        parser = autopilot.build_arg_parser()
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(
                [
                    "--command",
                    "asl run /tmp/plan",
                    "--max-review-fix-plans",
                    "4",
                ]
            )

    def test_command_is_required(self) -> None:
        parser = autopilot.build_arg_parser()
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args([])

    def test_json_and_reply_stdin_default_false(self) -> None:
        parser = autopilot.build_arg_parser()
        args = parser.parse_args(["--command", "asl run /tmp/plan"])
        self.assertFalse(args.json)
        self.assertFalse(args.reply_stdin)
        self.assertFalse(args.wait_quota_reset)
        self.assertFalse(args.detach)
        self.assertIsNone(args.supervision_log_dir)


class MainTest(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        with (
            self.assertRaises(SystemExit) as caught,
            redirect_stdout(io.StringIO()),
        ):
            autopilot.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_json_output_is_valid_parse_failure_payload(self) -> None:
        # A parse failure never reaches the supervisor, so main() stays
        # hermetic (no subprocess) while still exercising the JSON + exit path.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(
                ["--command", "asl run /tmp/plan | cat", "--json"]
            )
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["stage"], autopilot.PARSE_STAGE)
        self.assertEqual(payload["reason"], "shell-syntax")
        self.assertFalse(payload["ok"])
        self.assertEqual(exit_code, 1)


class ParseCommandTest(unittest.TestCase):
    def test_review_cycle_default_matches_run_inputs(self) -> None:
        self.assertEqual(
            run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            autopilot.DEFAULT_MAX_REVIEW_FIX_CYCLES,
        )

    def test_parses_minimal_run_and_appends_json(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], autopilot.PARSE_STAGE)
        self.assertEqual(result["executable"], "asl")
        self.assertEqual(result["env"], {})
        self.assertEqual(result["run_args"], ["run", "/tmp/plan", "--json"])
        self.assertEqual(result["argv"], ["asl", "run", "/tmp/plan", "--json"])
        self.assertTrue(result["json_appended"])
        self.assertEqual(
            result["max_review_fix_cycles"],
            autopilot.DEFAULT_MAX_REVIEW_FIX_CYCLES,
        )

    def test_existing_json_is_preserved_not_duplicated(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --json")
        self.assertTrue(result["ok"])
        self.assertFalse(result["json_appended"])
        self.assertEqual(result["run_args"].count("--json"), 1)

    def test_spark_opt_in_and_opt_out_are_forwarded(self) -> None:
        enabled = autopilot.parse_command("asl run /tmp/plan --spark")
        disabled = autopilot.parse_command("asl run /tmp/plan --no-spark")

        self.assertTrue(enabled["ok"])
        self.assertIn("--spark", enabled["run_args"])
        self.assertTrue(disabled["ok"])
        self.assertIn("--no-spark", disabled["run_args"])

    def test_quoted_args_are_preserved(self) -> None:
        result = autopilot.parse_command(
            'asl run /tmp/plan --note "fix the parser bug"'
        )
        self.assertTrue(result["ok"])
        self.assertIn("--note", result["run_args"])
        note_index = result["run_args"].index("--note")
        self.assertEqual(result["run_args"][note_index + 1], "fix the parser bug")

    def test_env_assignments_are_captured_and_tilde_expanded(self) -> None:
        home = os.path.expanduser("~")
        result = autopilot.parse_command(
            "CLAUDE_CONFIG_DIR=~/.claude-personal asl run /tmp/plan"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["env"], {"CLAUDE_CONFIG_DIR": os.path.join(home, ".claude-personal")}
        )
        # The executable and subcommand are found after the env assignment.
        self.assertEqual(result["executable"], "asl")

    def test_env_value_does_not_shell_expand_variables(self) -> None:
        result = autopilot.parse_command("FOO=$BAR asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertEqual(result["env"], {"FOO": "$BAR"})

    def test_local_asl_path_is_supported(self) -> None:
        local = "/Users/dev/scripts-asl-interactive/bin/.local/bin/asl"
        result = autopilot.parse_command(f"{local} run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertEqual(result["executable"], local)
        self.assertEqual(result["argv"][0], local)

    def test_plan_dir_tilde_is_rewritten(self) -> None:
        home = os.path.expanduser("~")
        result = autopilot.parse_command("asl run ~/plans/demo")
        self.assertTrue(result["ok"])
        self.assertEqual(result["plan_dir"], os.path.join(home, "plans", "demo"))
        self.assertEqual(result["run_args"][1], os.path.join(home, "plans", "demo"))

    def test_cwd_and_state_dir_tilde_rewritten(self) -> None:
        home = os.path.expanduser("~")
        result = autopilot.parse_command(
            "asl run /tmp/plan --cwd ~/repo --state-dir ~/state"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["cwd"], os.path.join(home, "repo"))
        self.assertEqual(result["state_dir"], os.path.join(home, "state"))

    def test_status_workspace_is_added_when_registered_command_omits_cwd(self) -> None:
        parsed = autopilot.parse_command("asl run /tmp/plan")
        status = {
            "ok": True,
            "workspace_root": "/tmp/workspace",
            "registry_digest": "a" * 64,
            "repositories": [
                {"alias": "SVC", "resolved_root": "/tmp/workspace/svc"},
                {"alias": "OAK", "resolved_root": "/tmp/workspace/oak"},
            ],
        }

        bound = autopilot._parsed_with_status_workspace(parsed, status)

        self.assertEqual("/tmp/workspace", bound["cwd"])
        self.assertEqual(
            "/tmp/workspace", bound["argv"][bound["argv"].index("--cwd") + 1]
        )
        self.assertEqual(
            ["SVC", "OAK"], [row["alias"] for row in bound["repositories"]]
        )

    def test_status_workspace_mismatch_requires_explicit_rebind(self) -> None:
        parsed = autopilot.parse_command("asl run /tmp/plan --cwd /tmp/workspace/svc")
        status = {
            "ok": True,
            "workspace_root": "/tmp/workspace",
            "repositories": [{"alias": "SVC", "resolved_root": "/tmp/workspace/svc"}],
        }

        error = autopilot._workspace_launch_error(parsed, status)

        self.assertIsNotNone(error)
        self.assertIn("--rebind-workspace", error)

    def test_supported_flags_are_preserved(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --harness claude --review-harness codex "
            "--harness-model opus --review-harness-model gpt-5 "
            "--effort high --timeout 600 --max-sessions 3 "
            "--max-pending-sessions 30 --commit-at-end --no-unsafe-bypass "
            "--no-mailbox --allow-not-loop-ready"
        )
        self.assertTrue(result["ok"])
        args = result["run_args"]
        self.assertIn("--harness", args)
        self.assertIn("claude", args)
        self.assertIn("--review-harness", args)
        self.assertEqual(args[args.index("--harness-model") + 1], "opus")
        self.assertEqual(args[args.index("--review-harness-model") + 1], "gpt-5")
        self.assertIn("--commit-at-end", args)
        self.assertIn("--no-unsafe-bypass", args)
        self.assertIn("--no-mailbox", args)
        self.assertTrue(result["no_mailbox"])
        self.assertIn("--allow-not-loop-ready", args)
        self.assertEqual(args[args.index("--timeout") + 1], "600")

    def test_rejects_legacy_model_flag(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --model opus")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_review_harness_model_requires_review_harness(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness-model gpt-5"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")
        self.assertEqual(
            result["error"],
            "--review-harness-model requires --review-harness",
        )

    def test_resume_defers_review_harness_model_cross_field(self) -> None:
        # On a --resume command the review harness may be rehydrated from the
        # persisted run_config, so a model-only override must not be rejected at
        # parse time; the cross-field check is deferred to the child, which the
        # runner re-runs after applying the resume overlay. This mirrors runner
        # behavior (`_apply_resume_config` runs before
        # `run_inputs._validate_run_inputs`).
        result = autopilot.parse_command(
            "asl run /tmp/plan --resume --review-harness-model gpt-5"
        )
        self.assertTrue(result["ok"], result)
        args = result["run_args"]
        self.assertIn("--resume", args)
        self.assertEqual(args[args.index("--review-harness-model") + 1], "gpt-5")

    def test_non_resume_still_rejects_model_only_override(self) -> None:
        # The deferral is scoped to --resume: without it, a model-only override
        # is still a parse-time invalid-argument.
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness-model gpt-5"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")
        self.assertEqual(
            result["error"], "--review-harness-model requires --review-harness"
        )

    def test_review_fix_cycles_forwarded_to_child(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness codex --max-review-fix-cycles 3"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_review_fix_cycles"], 3)
        args = result["run_args"]
        self.assertEqual(args[args.index("--max-review-fix-cycles") + 1], "3")

    def test_deprecated_review_fix_plans_alias_translates_to_cycles(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-harness codex --max-review-fix-plans 5"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_review_fix_cycles"], 5)
        args = result["run_args"]
        self.assertNotIn("--max-review-fix-plans", args)
        self.assertEqual(args[args.index("--max-review-fix-cycles") + 1], "5")

    def test_zero_review_fix_cycles_is_accepted(self) -> None:
        # 0 is the review-only mode: the runner reviews but creates no repairs.
        result = autopilot.parse_command("asl run /tmp/plan --max-review-fix-cycles 0")
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_review_fix_cycles"], 0)

    def test_rejects_negative_review_fix_cycles(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --max-review-fix-cycles -1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_both_review_fix_flags_together(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --max-review-fix-cycles 2 --max-review-fix-plans 2"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")
        self.assertEqual(result["reason"], "invalid-argument")

    def test_review_guard_exclude_round_trips_repeated_values(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-guard-exclude claude/.claude-personal "
            "--review-guard-exclude notes"
        )
        self.assertTrue(result["ok"], result)
        args = result["run_args"]
        self.assertEqual(
            ["claude/.claude-personal", "notes"],
            [
                args[index + 1]
                for index, token in enumerate(args)
                if token == "--review-guard-exclude"
            ],
        )

    def test_review_guard_exclude_absent_by_default(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertNotIn("--review-guard-exclude", result["run_args"])

    def test_review_guard_include_ignored_round_trips(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --review-guard-include-ignored"
        )
        self.assertTrue(result["ok"], result)
        self.assertIn("--review-guard-include-ignored", result["run_args"])

    def test_allow_external_changes_round_trips(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --allow-external-changes scripts"
        )
        self.assertTrue(result["ok"], result)
        index = result["run_args"].index("--allow-external-changes")
        self.assertEqual("scripts", result["run_args"][index + 1])

    def test_review_guard_include_ignored_absent_by_default(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertNotIn("--review-guard-include-ignored", result["run_args"])

    def test_rejects_shell_pipe(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan | tee log")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_rejects_redirect(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan > out.txt")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_rejects_command_substitution(self) -> None:
        result = autopilot.parse_command("asl run $(echo /tmp/plan)")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "shell-syntax")

    def test_resume_parsed_and_forwarded_to_child(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --resume")
        self.assertTrue(result["ok"])
        self.assertIn("--resume", result["run_args"])

    def test_resume_absent_by_default(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan")
        self.assertTrue(result["ok"])
        self.assertNotIn("--resume", result["run_args"])

    def test_rejects_dry_run(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --dry-run")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-mode")

    def test_rejects_user_supplied_reply(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --reply hello")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-mode")

    def test_rejects_user_supplied_reply_stdin(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --reply-stdin")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-mode")

    def test_rejects_non_asl_executable(self) -> None:
        result = autopilot.parse_command("rm run /tmp/plan")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not-asl")

    def test_rejects_non_run_subcommand(self) -> None:
        result = autopilot.parse_command("asl status /tmp/plan")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported-subcommand")

    def test_rejects_unknown_flag(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --bogus")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_non_positive_timeout(self) -> None:
        result = autopilot.parse_command("asl run /tmp/plan --timeout 0")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_review_harness_equal_to_harness(self) -> None:
        result = autopilot.parse_command(
            "asl run /tmp/plan --harness claude --review-harness claude"
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "invalid-argument")

    def test_rejects_empty_command(self) -> None:
        result = autopilot.parse_command("   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "empty-command")


if __name__ == "__main__":
    unittest.main()
