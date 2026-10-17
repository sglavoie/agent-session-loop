"""Tests for test asl guards hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    claude_stream,
    control_queue,
    events,
    guards,
)
from tests import support_asl_guards as fixtures
from tests.asl_docs_support import read_asl_reference


class GuardPreToolHookTests(unittest.TestCase):
    """The early lane: a denial the worker is told about, before it runs.

    Exercised as a real subprocess, the way the harness invokes it, because
    what is being proved is the contract at the process boundary — the JSON in,
    the decision out, and an exit status that never stalls a turn.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = self.root / "app"
        (self.app / "docs").mkdir(parents=True)
        self.bin_dir = self.root / "bin"
        fixtures.install_fake_git(self.bin_dir)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.root / "runs" / "run-1",
            path_value=str(self.bin_dir),
            workspace_aliases={"App": str(self.app)},
        )
        self.controller.add(
            [("git", "push")],
            paths=[guards.parse_protected_path_rule("App:docs/**/*.md")],
            reason="operator instruction",
        )
        self.hook = claude_stream.guard_hook_script_path()
        self.policy = str(self.controller.policy_file)

    def call(self, event: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        completed = subprocess.run(
            [sys.executable, str(self.hook), self.policy],
            input=json.dumps(event),
            capture_output=True,
            text=True,
        )
        output = completed.stdout.strip()
        return completed.returncode, json.loads(output) if output else None

    def assertDenied(self, event: dict[str, Any]) -> str:
        code, decision = self.call(event)
        self.assertEqual(0, code)
        self.assertIsNotNone(decision, f"expected a denial for {event}")
        hook_output = decision["hookSpecificOutput"]
        self.assertEqual("PreToolUse", hook_output["hookEventName"])
        self.assertEqual("deny", hook_output["permissionDecision"])
        return hook_output["permissionDecisionReason"]

    def assertAllowed(self, event: dict[str, Any]) -> None:
        code, decision = self.call(event)
        self.assertEqual(0, code)
        self.assertIsNone(decision, f"expected {event} to pass through")

    def test_a_write_to_a_protected_path_is_denied_with_the_reason(self) -> None:
        message = self.assertDenied(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.app / "docs" / "guide.md")},
            }
        )
        self.assertIn("App:docs/**/*.md", message)
        self.assertIn("operator instruction", message)

    def test_reading_a_protected_path_is_not_a_change(self) -> None:
        """A guard freezes content, not attention."""
        self.assertAllowed(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(self.app / "docs" / "guide.md")},
            }
        )

    def test_a_write_outside_the_rule_passes_through(self) -> None:
        for path in ("docs/guide.txt", "src/main.py"):
            with self.subTest(path=path):
                self.assertAllowed(
                    {
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(self.app / path)},
                    }
                )

    def test_a_path_outside_every_repository_is_not_this_guard_s_business(self) -> None:
        self.assertAllowed(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.root / "elsewhere" / "guide.md")},
            }
        )

    def test_a_denied_command_is_refused_before_it_runs(self) -> None:
        message = self.assertDenied(
            {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}
        )
        self.assertIn("git push", message)

    def test_an_absolute_path_bypass_is_caught_by_basename(self) -> None:
        """``/usr/bin/git push`` never resolves through PATH, so the shim
        cannot see it — but the basename is right there in the tool call."""
        self.assertDenied(
            {"tool_name": "Bash", "tool_input": {"command": "/usr/bin/git push"}}
        )

    def test_an_unguarded_command_passes_through(self) -> None:
        self.assertAllowed(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}}
        )

    def test_a_shell_script_is_declined_rather_than_guessed_at(self) -> None:
        """Predicting a compound command's effects would produce confident
        wrong answers; the PATH shim is what catches these."""
        for command in (
            "echo hi | git push",
            "true && git push",
            "sh -c 'git push'",
            "$(echo git) push",
        ):
            with self.subTest(command=command):
                self.assertAllowed(
                    {"tool_name": "Bash", "tool_input": {"command": command}}
                )

    def test_every_failure_allows_the_call_rather_than_stalling_the_turn(self) -> None:
        """The guarantees live in the shim and the snapshot comparison, so a
        hook that cannot answer must not become a wedged run."""
        cases: list[tuple[str, str]] = [
            ("missing policy", str(self.root / "absent.json")),
            ("unreadable policy", str(self.root)),
        ]
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        cases.append(("malformed policy", str(broken)))
        event = {
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.app / "docs" / "guide.md")},
        }
        for label, policy in cases:
            with self.subTest(case=label):
                completed = subprocess.run(
                    [sys.executable, str(self.hook), policy],
                    input=json.dumps(event),
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode)
                self.assertEqual("", completed.stdout.strip())
        for label, payload in (
            ("not json", "{"),
            ("not an object", "[]"),
            ("no tool input", '{"tool_name": "Write"}'),
        ):
            with self.subTest(case=label):
                completed = subprocess.run(
                    [sys.executable, str(self.hook), self.policy],
                    input=payload,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode)
                self.assertEqual("", completed.stdout.strip())

    def test_the_policy_path_can_arrive_through_the_environment(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(self.hook)],
            input=json.dumps(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(self.app / "docs" / "guide.md")},
                }
            ),
            capture_output=True,
            text=True,
            env={**os.environ, guards.GUARD_POLICY_ENV_VAR: self.policy},
        )
        self.assertEqual(0, completed.returncode)
        self.assertIn("deny", completed.stdout)

    def test_the_claude_overlay_arms_the_hook_ahead_of_guidance(self) -> None:
        overlay = self.root / "overlay"
        overlay.mkdir()
        spool = claude_stream.ClaudeGuidanceSpool(overlay / "spool")
        settings = json.loads(
            claude_stream.write_settings_overlay(
                overlay, spool, guard_policy_path=self.policy
            ).read_text(encoding="utf-8")
        )
        commands = [
            entry["command"]
            for entry in settings["hooks"][claude_stream.HOOK_EVENT][0]["hooks"]
        ]
        self.assertEqual(2, len(commands))
        self.assertIn(str(self.hook), commands[0])
        self.assertIn(self.policy, commands[0])

    def test_a_run_without_guards_arms_no_guard_hook(self) -> None:
        overlay = self.root / "overlay-plain"
        overlay.mkdir()
        spool = claude_stream.ClaudeGuidanceSpool(overlay / "spool")
        settings = json.loads(
            claude_stream.write_settings_overlay(overlay, spool).read_text(
                encoding="utf-8"
            )
        )
        commands = [
            entry["command"]
            for entry in settings["hooks"][claude_stream.HOOK_EVENT][0]["hooks"]
        ]
        self.assertEqual(1, len(commands))
        self.assertNotIn(str(self.hook), commands[0])


class ExternalReadOnlyAliasTests(unittest.TestCase):
    """``--allow-external-changes``: edits refused before they run, per turn.

    The postflight comparison records rather than convicts a change in such an
    alias, so the hook is the lane that still keeps the worker's own edit tools
    out of it. It must work with no operator guard active at all.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = self.root / "app"
        self.scripts = self.root / "scripts"
        self.app.mkdir()
        self.scripts.mkdir()
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.root / "runs" / "run-1",
            workspace_aliases={"app": str(self.app), "scripts": str(self.scripts)},
        )
        self.hook = claude_stream.guard_hook_script_path()

    def call(self, tool_name: str, path: Path) -> dict[str, Any] | None:
        completed = subprocess.run(
            [sys.executable, str(self.hook), str(self.controller.policy_file)],
            input=json.dumps(
                {"tool_name": tool_name, "tool_input": {"file_path": str(path)}}
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode)
        output = completed.stdout.strip()
        return json.loads(output) if output else None

    def test_policy_exists_without_guards_and_refuses_only_edit_tools(self) -> None:
        self.controller.set_external_read_only_aliases(["scripts"])
        self.controller.prepare_turn(harness="claude", pretool_armed=True)
        policy = json.loads(self.controller.policy_file.read_text(encoding="utf-8"))
        self.assertEqual(["scripts"], policy["external_read_only_aliases"])
        self.assertEqual([], policy["guards"])

        decision = self.call("Edit", self.scripts / "tool.py")
        self.assertIsNotNone(decision)
        reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("scripts:tool.py", reason)
        self.assertIsNone(self.call("Read", self.scripts / "tool.py"))
        self.assertIsNone(self.call("Write", self.app / "main.py"))

    def test_a_turn_that_writes_the_alias_drops_the_policy(self) -> None:
        self.controller.set_external_read_only_aliases(["scripts"])
        self.controller.prepare_turn(harness="claude", pretool_armed=True)
        self.assertTrue(self.controller.policy_file.exists())
        self.controller.set_external_read_only_aliases([])
        self.controller.prepare_turn(harness="claude", pretool_armed=True)
        self.assertFalse(self.controller.policy_file.exists())


class GuardDocumentationTests(unittest.TestCase):
    """Every stable reason a guard can produce is documented."""

    def test_guard_reasons_are_documented(self) -> None:
        text = read_asl_reference()
        reasons = [
            guards.REJECT_NO_ACTIVE_RUN,
            guards.REJECT_RUN_CHANGED,
            guards.REJECT_UNKNOWN_GUARD,
            guards.REJECT_EXECUTABLE_NOT_FOUND,
            guards.REJECT_ACTIVATION_FAILED,
            guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            guards.REJECT_SHELL_OPERATOR,
            guards.REJECT_GLOB,
            guards.REJECT_ENVIRONMENT_ASSIGNMENT,
            guards.REJECT_EMPTY_GUARD_RULE,
            guards.REJECT_EMPTY_PATH_RULE,
            guards.REJECT_PATH_MISSING_QUALIFIER,
            guards.REJECT_PATH_UNKNOWN_ALIAS,
            guards.REJECT_PATH_ABSOLUTE,
            guards.REJECT_PATH_BACKSLASH,
            guards.REJECT_PATH_TRAVERSAL,
            guards.REJECT_PATH_EMPTY_SEGMENT,
            guards.REJECT_PATH_PATTERN,
            guards.REJECT_PROTECTED_PATH_EVIDENCE,
            control_queue.REJECT_MISSING_EXPECTED_RUN_ID,
            control_queue.REJECT_MISSING_GUARD_RULE,
            control_queue.REJECT_MISSING_GUARD_ID,
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(f"`{reason}`", text)

    def test_every_coverage_reason_is_documented(self) -> None:
        """A lane verdict an operator can read has to be one they can look up.

        The coverage block is the honest part of this feature -- it is where a
        harness says it cannot enforce something. A reason with no
        documentation turns that honesty back into a mystery string.
        """
        text = read_asl_reference()
        reasons = [
            value
            for name, value in vars(guards).items()
            if name.startswith("COVERAGE_REASON_") and isinstance(value, str)
        ]
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(f"`{reason}`", text)

    def test_the_pause_reasons_a_path_guard_raises_are_documented(self) -> None:
        text = read_asl_reference()
        for reason in (
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
        ):
            with self.subTest(reason=reason):
                self.assertIn(f"`{reason}`", text)

    def test_protected_path_coverage_is_documented(self) -> None:
        text = read_asl_reference()
        self.assertIn("asl control guard add", text)
        self.assertIn("--protect-path", text)
        for claim in (
            *guards.PROTECTED_PATH_COVERAGE["covers"],
            *guards.PROTECTED_PATH_COVERAGE["does_not_cover"],
        ):
            with self.subTest(claim=claim):
                self.assertIn(claim, text)

    def test_bypasses_are_documented(self) -> None:
        text = read_asl_reference()
        self.assertIn("asl control guard add", text)
        for bypass in guards.COMMAND_GATE_COVERAGE["does_not_cover"]:
            with self.subTest(bypass=bypass):
                self.assertIn(bypass, text)


if __name__ == "__main__":
    unittest.main()
