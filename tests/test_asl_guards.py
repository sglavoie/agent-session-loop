"""Run-scoped command guard tests: rules, the gate, and its lifecycle.

Enforcement is proved by running real processes against a fake ``git``
installed on a temporary ``PATH``. Nothing here touches a real network-affecting
command: the point is that the gate intercepts a bare-name resolution, and a
fake executable proves that more honestly than a real one, which would also
have to be prevented from doing its job.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    claude_stream,
    command_guard,
    control_cli,
    control_queue,
    events,
    guards,
    harnesses,
    intervention,
    run_coordination,
    state,
)
from tests.support import REPO_ROOT, _write_lock_file, _write_state_payload, write_plan

FAKE_GIT_SOURCE = """#!/bin/sh
echo "fake git $*"
exit 0
"""


def install_fake_git(bin_dir: Path, name: str = "git") -> Path:
    """Install a harmless stand-in for a command an operator would guard."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / name
    fake.write_text(FAKE_GIT_SOURCE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return fake


class CommandRuleParsingTests(unittest.TestCase):
    """A rule is an argv prefix, and everything else is refused by name."""

    def test_posix_tokenization_splits_and_respects_quotes(self) -> None:
        self.assertEqual(("git", "push"), guards.parse_command_rule("git push"))
        self.assertEqual(
            ("git", "push", "origin", "main"),
            guards.parse_command_rule("  git   push origin main  "),
        )
        self.assertEqual(
            ("git", "commit", "-m", "two words"),
            guards.parse_command_rule('git commit -m "two words"'),
        )

    def test_single_token_rule_is_accepted(self) -> None:
        self.assertEqual(("git",), guards.parse_command_rule("git"))

    def test_rejects_every_non_argv_construct_with_a_stable_reason(self) -> None:
        cases = {
            "": guards.REJECT_EMPTY_COMMAND_RULE,
            "   ": guards.REJECT_EMPTY_COMMAND_RULE,
            'git "unterminated': guards.REJECT_UNPARSABLE_COMMAND_RULE,
            "/usr/bin/git push": guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            "./git push": guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            "../git push": guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            "git push && rm -rf /": guards.REJECT_SHELL_OPERATOR,
            "git push | tee log": guards.REJECT_SHELL_OPERATOR,
            "git push origin $BRANCH": guards.REJECT_SHELL_OPERATOR,
            "git push origin *": guards.REJECT_GLOB,
            "git push origin main?": guards.REJECT_GLOB,
            "GIT_DIR=/tmp git push": guards.REJECT_ENVIRONMENT_ASSIGNMENT,
        }
        for rule, reason in cases.items():
            with self.subTest(rule=rule):
                with self.assertRaises(guards.GuardRuleError) as caught:
                    guards.parse_command_rule(rule)
                self.assertEqual(reason, caught.exception.reason)

    def test_empty_token_is_rejected(self) -> None:
        with self.assertRaises(guards.GuardRuleError) as caught:
            guards.parse_command_rule("git '' push")
        self.assertEqual(guards.REJECT_EMPTY_TOKEN, caught.exception.reason)

    def test_matching_is_case_sensitive_and_prefix_only(self) -> None:
        policy = {
            "version": guards.POLICY_VERSION,
            "guards": [{"id": "g-1", "command": ["git", "push", "origin"]}],
        }
        self.assertIsNotNone(
            command_guard.matched_guard(policy, "git", ["push", "origin", "main"])
        )
        self.assertIsNotNone(
            command_guard.matched_guard(policy, "git", ["push", "origin"])
        )
        self.assertIsNone(command_guard.matched_guard(policy, "git", ["push"]))
        self.assertIsNone(
            command_guard.matched_guard(policy, "git", ["Push", "origin"])
        )
        self.assertIsNone(
            command_guard.matched_guard(policy, "git", ["fetch", "origin"])
        )

    def test_one_token_rule_blocks_every_invocation(self) -> None:
        policy = {
            "version": guards.POLICY_VERSION,
            "guards": [{"id": "g-1", "command": ["git"]}],
        }
        for argv in ([], ["status"], ["push", "origin", "main"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(command_guard.matched_guard(policy, "git", argv))


class ProtectedPathRuleParsingTests(unittest.TestCase):
    """A path rule is ``Alias:glob``, and every refusal names its own defect."""

    ALIASES = ("App", "Docs", ".")

    def parse(self, raw: str) -> guards.ProtectedPath:
        return guards.parse_protected_path_rule(raw, aliases=self.ALIASES)

    def test_qualifier_and_pattern_are_kept_verbatim(self) -> None:
        rule = self.parse("App:docs/guide.md")
        self.assertEqual("App", rule.qualifier)
        self.assertEqual("docs/guide.md", rule.pattern)
        self.assertEqual("App:docs/guide.md", rule.rendered)

    def test_leading_dot_slash_is_the_one_dot_segment_accepted(self) -> None:
        """``./x`` is how people type "in this repository", not a mistake."""
        self.assertEqual("docs/guide.md", self.parse("App:./docs/guide.md").pattern)

    def test_legacy_plans_qualify_with_a_bare_dot(self) -> None:
        rule = guards.parse_protected_path_rule(
            ".:README.md", aliases=(guards.LEGACY_PATH_QUALIFIER,)
        )
        self.assertEqual(guards.LEGACY_PATH_QUALIFIER, rule.qualifier)

    def test_rejects_every_unsupported_form_with_a_stable_reason(self) -> None:
        cases = {
            "": guards.REJECT_EMPTY_PATH_RULE,
            "   ": guards.REJECT_EMPTY_PATH_RULE,
            "docs/guide.md": guards.REJECT_PATH_MISSING_QUALIFIER,
            "App:": guards.REJECT_EMPTY_PATH_RULE,
            ":docs": guards.REJECT_EMPTY_PATH_RULE,
            "App:/etc/passwd": guards.REJECT_PATH_ABSOLUTE,
            "App:docs\\guide.md": guards.REJECT_PATH_BACKSLASH,
            "App:../other/file": guards.REJECT_PATH_TRAVERSAL,
            "App:docs/../secrets": guards.REJECT_PATH_TRAVERSAL,
            "App:docs//guide.md": guards.REJECT_PATH_EMPTY_SEGMENT,
            "App:docs/": guards.REJECT_PATH_EMPTY_SEGMENT,
            "App:docs/[abc].md": guards.REJECT_PATH_PATTERN,
            "App:docs/{a,b}.md": guards.REJECT_PATH_PATTERN,
            "App:docs/**x/guide.md": guards.REJECT_PATH_PATTERN,
            "Other:docs/guide.md": guards.REJECT_PATH_UNKNOWN_ALIAS,
        }
        for rule, reason in cases.items():
            with self.subTest(rule=rule):
                with self.assertRaises(guards.GuardRuleError) as caught:
                    self.parse(rule)
                self.assertEqual(reason, caught.exception.reason)

    def test_syntax_is_checked_without_a_plan_and_the_alias_is_deferred(self) -> None:
        """No plan context validates the shape and leaves the alias alone.

        The queue is written by producers that do not hold the run's registry,
        so refusing an unknown alias there would refuse rules the run itself
        would have accepted.
        """
        rule = guards.parse_protected_path_rule("Unknown:docs/guide.md")
        self.assertEqual("Unknown", rule.qualifier)
        with self.assertRaises(guards.GuardRuleError):
            guards.parse_protected_path_rule("no-qualifier")

    def test_star_matches_within_one_segment_only(self) -> None:
        rule = self.parse("App:docs/*.md")
        self.assertTrue(rule.matches("App", "docs/guide.md"))
        self.assertFalse(rule.matches("App", "docs/nested/guide.md"))
        self.assertFalse(rule.matches("App", "guide.md"))

    def test_double_star_spans_segments(self) -> None:
        """``**/`` spans *zero* or more segments, as every other globstar does.

        Requiring at least one would make ``docs/**/*.md`` silently skip the
        file at the top of ``docs/``, which is the opposite of what an operator
        freezing a directory tree asks for.
        """
        rule = self.parse("App:docs/**/*.md")
        self.assertTrue(rule.matches("App", "docs/a/guide.md"))
        self.assertTrue(rule.matches("App", "docs/a/b/guide.md"))
        self.assertTrue(rule.matches("App", "docs/guide.md"))
        self.assertFalse(rule.matches("App", "docs/guide.txt"))
        self.assertFalse(rule.matches("App", "src/guide.md"))
        trailing = self.parse("App:docs/**")
        self.assertTrue(trailing.matches("App", "docs/a/guide.md"))
        self.assertTrue(trailing.matches("App", "docs/guide.md"))
        self.assertFalse(trailing.matches("App", "docs"))

    def test_question_mark_matches_one_non_separator_character(self) -> None:
        rule = self.parse("App:docs/v?.md")
        self.assertTrue(rule.matches("App", "docs/v1.md"))
        self.assertFalse(rule.matches("App", "docs/v10.md"))
        self.assertFalse(rule.matches("App", "docs/v/.md"))

    def test_matching_is_case_sensitive_and_repository_scoped(self) -> None:
        rule = self.parse("App:docs/Guide.md")
        self.assertTrue(rule.matches("App", "docs/Guide.md"))
        self.assertFalse(rule.matches("App", "docs/guide.md"))
        self.assertFalse(rule.matches("Docs", "docs/Guide.md"))

    def test_qualified_matching_reads_the_evidence_form(self) -> None:
        rule = self.parse("App:docs/*.md")
        self.assertTrue(rule.matches_qualified("App:docs/guide.md"))
        self.assertFalse(rule.matches_qualified("Docs:docs/guide.md"))
        self.assertFalse(rule.matches_qualified("docs/guide.md"))

    def test_match_reports_every_guard_and_the_paths_it_caught(self) -> None:
        stamp = "2026-01-01T00:00:00+00:00"
        docs = guards.GuardRule(
            id="g-docs", reason=None, created_at=stamp, path=self.parse("App:docs/**")
        )
        readme = guards.GuardRule(
            id="g-readme",
            reason=None,
            created_at=stamp,
            path=self.parse("Docs:README.md"),
        )
        matched = guards.match_protected_paths(
            (docs, readme),
            (
                "App:docs/a/guide.md",
                "App:src/main.py",
                "Docs:README.md",
                "App:README.md",
            ),
        )
        self.assertEqual(
            {
                "g-docs": ("App:docs/a/guide.md",),
                "g-readme": ("Docs:README.md",),
            },
            dict(matched),
        )

    def test_no_match_is_an_empty_result_rather_than_a_guess(self) -> None:
        stamp = "2026-01-01T00:00:00+00:00"
        rule = guards.GuardRule(
            id="g-1", reason=None, created_at=stamp, path=self.parse("App:docs/**")
        )
        self.assertEqual({}, dict(guards.match_protected_paths((rule,), ())))
        self.assertEqual(
            {}, dict(guards.match_protected_paths((rule,), ("App:src/main.py",)))
        )


class GuardCoverageTests(unittest.TestCase):
    """Per-lane coverage says what is armed, and names what is not."""

    def rule(self, *, command: tuple[str, ...] = (), path: str | None = None):
        return guards.GuardRule(
            id="g-1",
            reason=None,
            created_at="2026-01-01T00:00:00+00:00",
            command=command,
            path=(guards.parse_protected_path_rule(path) if path is not None else None),
        )

    def test_claude_denies_both_kinds_before_execution(self) -> None:
        for guard in (self.rule(command=("git", "push")), self.rule(path="App:a.md")):
            with self.subTest(kind=guard.kind):
                lanes = guards.guard_coverage(
                    guard, harness="claude", pretool_armed=True
                )
                self.assertEqual(guards.COVERAGE_ACTIVE, lanes["pretool"]["status"])
                self.assertEqual(
                    guards.COVERAGE_REASON_HOOK_DENIES, lanes["pretool"]["reason"]
                )

    def test_codex_reports_the_hook_trust_it_will_not_take(self) -> None:
        lanes = guards.guard_coverage(
            self.rule(command=("git", "push")), harness="codex"
        )
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, lanes["pretool"]["status"])
        self.assertEqual(
            guards.COVERAGE_REASON_CODEX_HOOK_TRUST, lanes["pretool"]["reason"]
        )

    def test_copilot_path_rules_are_postflight_only(self) -> None:
        lanes = guards.guard_coverage(
            self.rule(path="App:docs/**"), harness="copilot", process_start=True
        )
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, lanes["pretool"]["status"])
        self.assertEqual(
            guards.COVERAGE_REASON_COPILOT_NO_PATH_RULE, lanes["pretool"]["reason"]
        )
        self.assertEqual(guards.COVERAGE_ACTIVE, lanes["postflight"]["status"])

    def test_copilot_command_rules_cover_only_the_process_start_cohort(self) -> None:
        guard = self.rule(command=("git", "push"))
        armed = guards.guard_coverage(guard, harness="copilot", process_start=True)
        self.assertEqual(guards.COVERAGE_DEGRADED, armed["pretool"]["status"])
        self.assertEqual(
            guards.COVERAGE_REASON_COPILOT_PROCESS_START, armed["pretool"]["reason"]
        )
        late = guards.guard_coverage(guard, harness="copilot", process_start=False)
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, late["pretool"]["status"])
        self.assertEqual(
            guards.COVERAGE_REASON_ADDED_AFTER_PROCESS_START,
            late["pretool"]["reason"],
        )
        # The command gate is unaffected either way: the shim is what actually
        # holds a command rule, and it is armed for the whole run.
        self.assertEqual(guards.COVERAGE_ACTIVE, late["command-gate"]["status"])

    def test_a_rule_copilot_cannot_state_says_so_rather_than_claiming_it(self) -> None:
        guard = self.rule(command=("git", "commit", "-m", "two words"))
        lanes = guards.guard_coverage(guard, harness="copilot", process_start=True)
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, lanes["pretool"]["status"])
        self.assertEqual(
            guards.COVERAGE_REASON_COPILOT_UNEXPRESSIBLE, lanes["pretool"]["reason"]
        )
        self.assertEqual((), guards.copilot_deny_tool_rules((guard,)))

    def test_kinds_never_claim_the_other_kind_s_lane(self) -> None:
        command = guards.guard_coverage(
            self.rule(command=("git", "push")), harness="claude", pretool_armed=True
        )
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, command["postflight"]["status"])
        path = guards.guard_coverage(
            self.rule(path="App:a.md"), harness="claude", pretool_armed=True
        )
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, path["command-gate"]["status"])
        self.assertEqual(
            guards.COVERAGE_REASON_NOT_A_COMMAND_RULE, path["command-gate"]["reason"]
        )

    def test_deny_tool_compilation_is_ordered_and_deduplicated(self) -> None:
        rules = guards.copilot_deny_tool_rules(
            (
                self.rule(command=("git", "push")),
                self.rule(path="App:docs/**"),
                self.rule(command=("git", "push")),
                self.rule(command=("rm",)),
            )
        )
        self.assertEqual(("shell(git push)", "shell(rm)"), rules)

    def test_policy_compilation_matches_the_object_compilation(self) -> None:
        """Argv is built from the policy file, so the two must not drift."""
        objects = (
            self.rule(command=("git", "push")),
            self.rule(path="App:docs/**"),
        )
        document = {"guards": [guard.to_payload() for guard in objects]}
        self.assertEqual(
            guards.copilot_deny_tool_rules(objects),
            guards.copilot_deny_tool_rules_from_policy(document),
        )

    def test_a_policy_that_cannot_be_read_compiles_to_nothing(self) -> None:
        for document in (None, {}, {"guards": "not-a-list"}, {"guards": [1, "x"]}):
            with self.subTest(document=document):
                self.assertEqual(
                    (), guards.copilot_deny_tool_rules_from_policy(document)
                )


class GuardControllerTests(unittest.TestCase):
    """Activation order, atomic materialization, and the active-set contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.run_dir = self.root / "runs" / "run-1"
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.run_dir,
            path_value=str(self.bin_dir),
        )

    def read_policy(self) -> dict[str, Any]:
        return json.loads(self.controller.policy_file.read_text(encoding="utf-8"))

    def test_add_materializes_policy_and_shim_and_reports_applied(self) -> None:
        outcome = self.controller.add(
            [("git", "push", "origin", "main")], reason="protect main"
        )
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertTrue(outcome.ok)
        self.assertEqual(1, len(outcome.guard_ids))

        policy = self.read_policy()
        self.assertEqual(guards.POLICY_VERSION, policy["version"])
        self.assertEqual("run-1", policy["run_id"])
        self.assertEqual(
            [["git", "push", "origin", "main"]],
            [guard["command"] for guard in policy["guards"]],
        )
        # The original was resolved once, at activation, outside guard-bin.
        self.assertEqual(str(self.bin_dir / "git"), policy["executables"]["git"])

        shim = self.controller.guard_bin / "git"
        self.assertTrue(shim.exists())
        self.assertTrue(os.access(shim, os.X_OK))

    def test_add_is_all_or_nothing_across_rules(self) -> None:
        outcome = self.controller.add(
            [("git", "push"), ("definitely-not-installed", "run")]
        )
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EXECUTABLE_NOT_FOUND, outcome.reason)
        self.assertEqual((), self.controller.active())
        self.assertFalse(self.controller.policy_file.exists())

    def test_add_rejects_an_empty_rule_set(self) -> None:
        """No command rules *and* no path rules is the one empty ``add``.

        The reason names the whole mutation rather than the command half: an
        operator who passed only ``--protect-path`` and had every path
        rejected must not be told the command list was the problem.
        """
        outcome = self.controller.add([])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EMPTY_GUARD_RULE, outcome.reason)

    def test_remove_updates_policy_before_reporting_success(self) -> None:
        applied = self.controller.add([("git", "push")])
        guard_id = applied.guard_ids[0]
        removed = self.controller.remove(guard_id)
        self.assertEqual(guards.ACTION_REMOVED, removed.action)
        self.assertEqual([], self.read_policy()["guards"])
        self.assertEqual((), self.controller.active())
        # The shim stays; it is now a plain delegator.
        self.assertTrue((self.controller.guard_bin / "git").exists())

    def test_remove_of_an_unknown_guard_is_rejected(self) -> None:
        outcome = self.controller.remove("g-nope")
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_UNKNOWN_GUARD, outcome.reason)

    def test_expire_clears_the_policy_and_refuses_later_mutations(self) -> None:
        self.controller.add([("git", "push")])
        expired = self.controller.expire()
        self.assertEqual(1, len(expired))
        self.assertFalse(self.controller.policy_file.exists())
        self.assertEqual((), self.controller.active())

        outcome = self.controller.add([("git", "push")])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, outcome.reason)
        self.assertEqual(guards.ACTION_REJECTED, self.controller.remove("g-1").action)

    def test_expire_is_idempotent(self) -> None:
        self.controller.add([("git", "push")])
        self.assertEqual(1, len(self.controller.expire()))
        self.assertEqual((), self.controller.expire())

    def test_prepare_turn_creates_guard_bin_and_prepends_path(self) -> None:
        env = self.controller.prepare_turn({"PATH": str(self.bin_dir), "KEEP": "1"})
        self.assertTrue(self.controller.guard_bin.is_dir())
        self.assertEqual(
            str(self.controller.guard_bin), env["PATH"].split(os.pathsep)[0]
        )
        # Nothing else about the environment is disturbed.
        self.assertEqual("1", env["KEEP"])
        self.assertIn(str(self.bin_dir), env["PATH"].split(os.pathsep))
        # No guard is active, so the empty directory is the whole overlay.
        self.assertFalse(self.controller.policy_file.exists())
        self.assertEqual([], list(self.controller.guard_bin.iterdir()))

    def test_prepare_turn_rearms_a_guard_added_during_an_earlier_turn(self) -> None:
        self.controller.add([("git", "push")])
        self.controller.policy_file.unlink()
        self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertTrue(self.controller.policy_file.exists())

    def test_prepare_turn_does_not_duplicate_the_guard_bin_entry(self) -> None:
        first = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        second = self.controller.prepare_turn(first)
        self.assertEqual(
            1,
            second["PATH"].split(os.pathsep).count(str(self.controller.guard_bin)),
        )

    def gate_lane(self) -> dict[str, str]:
        return self.controller.to_payload()[0]["coverage"]["command-gate"]

    def test_prepare_turn_failure_reports_command_gate_unarmed(self) -> None:
        """A turn that never received the overlay is not reported as gated.

        ``prepare_turn`` fails open on purpose — an overlay that cannot be
        written must not cost the turn — but the environment it hands back is
        then the untouched parent one: no ``guard-bin`` on ``PATH`` and no
        policy path for a hook to read. Claiming ``path-shim-armed`` for that
        turn would tell an operator the one thing they would act on and the one
        thing that is false.
        """
        self.controller.add([("git", "push")])
        self.assertEqual(guards.COVERAGE_ACTIVE, self.gate_lane()["status"])

        base = {"PATH": str(self.bin_dir), "KEEP": "1"}
        with mock.patch.object(
            guards, "write_policy", side_effect=OSError("read-only run directory")
        ):
            env = self.controller.prepare_turn(
                base, harness="claude", pretool_armed=True
            )

        # Fails open: the turn still runs, with the environment untouched.
        self.assertEqual(base, env)
        self.assertNotIn(guards.GUARD_POLICY_ENV_VAR, env)
        self.assertNotIn(str(self.controller.guard_bin), env["PATH"].split(os.pathsep))
        # The guard is still active, and still reported — as unenforced.
        self.assertEqual(1, len(self.controller.active()))
        self.assertFalse(self.controller.overlay_armed)
        lane = self.gate_lane()
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, lane["status"])
        self.assertEqual(guards.COVERAGE_REASON_OVERLAY_UNARMED, lane["reason"])
        self.assertNotEqual(guards.COVERAGE_REASON_SHIM_ARMED, lane["reason"])
        # The pretool lane reads the policy path out of the same environment,
        # so it cannot be armed either, whatever the adapter can do in general.
        pretool = self.controller.to_payload()[0]["coverage"]["pretool"]
        self.assertEqual(guards.COVERAGE_UNSUPPORTED, pretool["status"])
        self.assertEqual(guards.COVERAGE_REASON_OVERLAY_UNARMED, pretool["reason"])

    def test_a_later_successful_prepare_returns_the_lane_to_active(self) -> None:
        """The failure is the last turn's verdict, not a permanent state."""
        self.controller.add([("git", "push")])
        with mock.patch.object(guards, "write_policy", side_effect=OSError("boom")):
            self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertEqual(
            guards.COVERAGE_REASON_OVERLAY_UNARMED, self.gate_lane()["reason"]
        )

        env = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertTrue(self.controller.overlay_armed)
        self.assertEqual(
            str(self.controller.guard_bin), env["PATH"].split(os.pathsep)[0]
        )
        self.assertEqual(
            str(self.controller.policy_file), env[guards.GUARD_POLICY_ENV_VAR]
        )
        lane = self.gate_lane()
        self.assertEqual(guards.COVERAGE_ACTIVE, lane["status"])
        self.assertEqual(guards.COVERAGE_REASON_SHIM_ARMED, lane["reason"])

    def test_a_guard_added_after_a_failed_prepare_is_reported_unarmed_too(self) -> None:
        """The verdict belongs to the turn, not to the guard that survived it.

        A guard added mid-turn normally installs into a ``PATH`` entry the
        running worker already has. When that entry was never injected, the
        mid-turn addition has nowhere to install into either, even though its
        own ``add`` materialized cleanly.
        """
        self.controller.add([("git", "push")])
        with mock.patch.object(guards, "write_policy", side_effect=OSError("boom")):
            self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.controller.mark_turn_snapshot(True)
        self.assertEqual(
            guards.ACTION_APPLIED, self.controller.add([("git", "fetch")]).action
        )
        self.assertEqual(
            {guards.COVERAGE_REASON_OVERLAY_UNARMED},
            {
                entry["coverage"]["command-gate"]["reason"]
                for entry in self.controller.to_payload()
            },
        )

    def test_an_expired_run_still_reports_the_shim_reason_not_the_overlay_one(
        self,
    ) -> None:
        """Two different failures keep two different reasons.

        Expiry is the ordinary end of every guard and the operator needs no
        action; a failed overlay is an anomaly during a live turn. Collapsing
        them would make the anomaly invisible.
        """
        guard = guards.GuardRule(
            id="g-1",
            reason=None,
            created_at="2026-01-01T00:00:00+00:00",
            command=("git", "push"),
        )
        lanes = guards.guard_coverage(
            guard, harness="claude", gate_armed=False, overlay_armed=False
        )
        self.assertEqual(
            guards.COVERAGE_REASON_SHIM_UNAVAILABLE, lanes["command-gate"]["reason"]
        )

    def test_active_payload_describes_the_rule_for_status(self) -> None:
        self.controller.add([("git", "push", "origin", "main")], reason="why")
        payload = self.controller.to_payload()[0]
        self.assertEqual(["git", "push", "origin", "main"], payload["command"])
        self.assertEqual("git push origin main", payload["rendered_command"])
        self.assertEqual("git", payload["executable"])
        self.assertEqual("why", payload["reason"])


class ProtectedPathControllerTests(unittest.TestCase):
    """A path guard joins the same active set without needing a shim."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.root / "runs" / "run-1",
            path_value=str(self.bin_dir),
            workspace_aliases={"App": str(self.root / "app")},
        )

    def path(self, raw: str) -> guards.ProtectedPath:
        return guards.parse_protected_path_rule(raw)

    def read_policy(self) -> dict[str, Any]:
        return json.loads(self.controller.policy_file.read_text(encoding="utf-8"))

    def test_add_arms_a_path_guard_and_installs_no_shim(self) -> None:
        outcome = self.controller.add(paths=[self.path("App:docs/**")], reason="frozen")
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertEqual(1, len(self.controller.active_paths()))
        # A path rule shadows no executable, so no shim overlay is built at all.
        self.assertFalse(self.controller.guard_bin.exists())
        entry = self.read_policy()["guards"][0]
        self.assertEqual(guards.GUARD_KIND_PROTECTED_PATH, entry["kind"])
        self.assertEqual(
            {"qualifier": "App", "pattern": "docs/**"}, entry["protected_path"]
        )

    def test_the_policy_carries_the_repository_binding_for_the_hook(self) -> None:
        """The hook resolves an absolute tool-call path, so it needs the roots."""
        self.controller.add(paths=[self.path("App:docs/**")])
        self.assertEqual(
            {"App": str(self.root / "app")},
            self.read_policy()["workspace"]["aliases"],
        )

    def test_both_kinds_activate_together_or_not_at_all(self) -> None:
        outcome = self.controller.add(
            [("git", "push")], paths=[self.path("App:docs/**")], reason="one intent"
        )
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertEqual(2, len(outcome.guard_ids))
        self.assertEqual(1, len(self.controller.active_paths()))

    def test_an_add_with_neither_kind_is_refused(self) -> None:
        outcome = self.controller.add([], paths=[])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EMPTY_GUARD_RULE, outcome.reason)

    def test_a_turn_without_a_baseline_refuses_a_new_path_guard(self) -> None:
        """Fail closed: a guard that could never be compared is not armed.

        Refusing tells the operator now, while they can still act. Accepting
        would arm something that silently convicts or clears nothing.
        """
        self.controller.mark_turn_snapshot(False)
        outcome = self.controller.add(paths=[self.path("App:docs/**")])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_PROTECTED_PATH_EVIDENCE, outcome.reason)
        self.assertEqual((), self.controller.active_paths())
        # A command rule is unaffected: it needs no snapshot at all.
        self.assertEqual(
            guards.ACTION_APPLIED, self.controller.add([("git", "push")]).action
        )

    def test_a_turn_with_a_baseline_accepts_a_mid_turn_path_guard(self) -> None:
        self.controller.mark_turn_snapshot(True)
        self.assertEqual(
            guards.ACTION_APPLIED,
            self.controller.add(paths=[self.path("App:docs/**")]).action,
        )

    def test_between_turns_a_path_guard_is_accepted_for_the_next_baseline(self) -> None:
        self.controller.mark_turn_snapshot(None)
        self.assertEqual(
            guards.ACTION_APPLIED,
            self.controller.add(paths=[self.path("App:docs/**")]).action,
        )

    def test_removing_a_path_guard_drops_it_from_the_active_set(self) -> None:
        applied = self.controller.add(paths=[self.path("App:docs/**")])
        removed = self.controller.remove(applied.guard_ids[0])
        self.assertEqual(guards.ACTION_REMOVED, removed.action)
        self.assertEqual((), self.controller.active_paths())
        self.assertEqual([], self.read_policy()["guards"])

    def test_prepare_turn_exports_the_policy_path_for_the_hook(self) -> None:
        self.controller.add(paths=[self.path("App:docs/**")])
        env = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertEqual(
            str(self.controller.policy_file), env[guards.GUARD_POLICY_ENV_VAR]
        )

    def test_the_process_start_cohort_is_the_set_prepare_turn_saw(self) -> None:
        first = self.controller.add([("git", "push")])
        self.controller.prepare_turn(
            {"PATH": str(self.bin_dir)}, harness="copilot", pretool_armed=False
        )
        second = self.controller.add([("git", "fetch")])
        coverage = {
            entry["id"]: entry["coverage"]["pretool"]["reason"]
            for entry in self.controller.to_payload()
        }
        self.assertEqual(
            guards.COVERAGE_REASON_COPILOT_PROCESS_START,
            coverage[first.guard_ids[0]],
        )
        self.assertEqual(
            guards.COVERAGE_REASON_ADDED_AFTER_PROCESS_START,
            coverage[second.guard_ids[0]],
        )


class CommandGateEnforcementTests(unittest.TestCase):
    """The gate, exercised by real processes resolving a bare name on PATH."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.run_dir = self.root / "runs" / "run-1"
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.run_dir,
            path_value=str(self.bin_dir),
        )
        base = dict(os.environ)
        base["PATH"] = os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"])
        self.env = self.controller.prepare_turn(base)

    def arm(self, *rules: tuple[str, ...]) -> str:
        outcome = self.controller.add(
            list(rules) or [("git", "push", "origin", "main")]
        )
        self.assertTrue(outcome.ok, outcome.to_payload())
        # A guard added mid-turn installs into a directory the running turn's
        # PATH already names, so the environment is not rebuilt here.
        return outcome.guard_ids[0]

    def run_argv(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            env=self.env,
            capture_output=True,
            text=True,
            cwd=self.root,
            check=False,
        )

    def deny_result(self) -> subprocess.CompletedProcess[str]:
        """The invocation the default rule denies."""
        return self.run_argv(["git", "push", "origin", "main"])

    def test_unguarded_invocation_delegates_to_the_real_executable(self) -> None:
        self.arm()
        result = self.run_argv(["git", "status"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git status", result.stdout)

    def test_guarded_invocation_is_denied_with_a_stable_message(self) -> None:
        guard_id = self.arm()
        result = self.run_argv(["git", "push", "origin", "main"])
        self.assertEqual(guards.DENIED_EXIT_CODE, result.returncode)
        self.assertEqual(126, result.returncode)
        self.assertIn(guard_id, result.stderr)
        self.assertIn("git push origin main", result.stderr)
        self.assertEqual("", result.stdout)

    def test_denial_covers_longer_argv_that_starts_with_the_prefix(self) -> None:
        self.arm()
        result = self.run_argv(["git", "push", "origin", "main", "--force"])
        self.assertEqual(126, result.returncode)

    def test_gate_covers_shell_env_and_python_subprocess_resolution(self) -> None:
        self.arm()
        denied = "git push origin main"
        allowed = "git status"
        cases = {
            "sh -c": (["sh", "-c", denied], ["sh", "-c", allowed]),
            "env": (
                ["env", "GUARD_TEST=1", "git", "push", "origin", "main"],
                ["env", "GUARD_TEST=1", "git", "status"],
            ),
            "python subprocess": (
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys;"
                    "sys.exit(subprocess.run("
                    "['git','push','origin','main']).returncode)",
                ],
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys;"
                    "sys.exit(subprocess.run(['git','status']).returncode)",
                ],
            ),
        }
        for label, (denied_argv, allowed_argv) in cases.items():
            with self.subTest(path=label):
                self.assertEqual(126, self.run_argv(denied_argv).returncode)
                self.assertEqual(0, self.run_argv(allowed_argv).returncode)

    def test_documented_bypasses_are_real(self) -> None:
        """The coverage statement is verified, not merely asserted."""
        self.arm()
        # An absolute path never consults PATH, so the shim is not reached.
        absolute = self.run_argv([str(self.bin_dir / "git"), "push", "origin", "main"])
        self.assertEqual(0, absolute.returncode)
        self.assertIn("fake git push", absolute.stdout)
        # A process that rewrites PATH out from under the overlay is likewise
        # outside the gate. This is why the guarantee is operational.
        rewritten = subprocess.run(
            ["sh", "-c", "git push origin main"],
            env={
                **self.env,
                "PATH": os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"]),
            },
            capture_output=True,
            text=True,
            cwd=self.root,
            check=False,
        )
        self.assertEqual(0, rewritten.returncode)
        self.assertIn(
            "an absolute or relative path to the real executable",
            guards.COMMAND_GATE_COVERAGE["does_not_cover"],
        )

    def test_removal_disables_enforcement_within_the_same_turn(self) -> None:
        guard_id = self.arm()
        self.assertEqual(126, self.deny_result().returncode)
        self.assertEqual(guards.ACTION_REMOVED, self.controller.remove(guard_id).action)
        result = self.run_argv(["git", "push", "origin", "main"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git push", result.stdout)

    def test_expiry_disables_enforcement_and_leaves_an_inert_shim(self) -> None:
        self.arm()
        self.assertEqual(126, self.deny_result().returncode)
        self.controller.expire()
        result = self.run_argv(["git", "push", "origin", "main"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git push", result.stdout)
        self.assertTrue((self.controller.guard_bin / "git").exists())

    def test_shim_never_resolves_to_itself(self) -> None:
        """Delegation excludes guard-bin, so a lost policy cannot loop."""
        self.arm()
        self.controller.policy_file.unlink()
        result = self.run_argv(["git", "status"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git status", result.stdout)

    def test_multiple_rules_share_one_shim(self) -> None:
        outcome = self.controller.add(
            [("git", "push", "origin", "main"), ("git", "tag", "-d")]
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(126, self.deny_result().returncode)
        self.assertEqual(126, self.run_argv(["git", "tag", "-d", "v1"]).returncode)
        self.assertEqual(0, self.run_argv(["git", "tag"]).returncode)


class GuardHarnessEnvironmentTests(unittest.TestCase):
    """Every harness launches its turn inside the run's overlay."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.root / "runs" / "run-1",
            path_value=str(self.bin_dir),
        )
        self.controller.add([("git", "push", "origin", "main")])
        base = dict(os.environ)
        base["PATH"] = os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"])
        self.env = self.controller.prepare_turn(base)

    def spawn_env(self, name: str, env: Any) -> dict[str, str] | None:
        """Return the environment the adapter would hand to ``Popen``."""
        recorded: dict[str, Any] = {}

        class _Recorder:
            def __init__(self, argv: Any, **kwargs: Any) -> None:
                recorded["argv"] = list(argv)
                recorded["env"] = kwargs.get("env")
                raise FileNotFoundError("stop after spawn arguments")

        invocation = harnesses.HarnessInvocation(
            name=name,
            argv=[name, "--version"],
            cwd=self.root,
            transport=harnesses.TRANSPORT_EXEC,
        )
        with (
            mock.patch.object(harnesses.subprocess, "Popen", _Recorder),
            self.assertRaises(harnesses.HarnessError),
        ):
            harnesses.get_adapter(name).run(
                cwd=self.root,
                plan_dir=None,
                prompt="hello",
                invocation=invocation,
                env=env,
            )
        return recorded["env"]

    def test_every_harness_spawns_with_the_guarded_path(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                child_env = self.spawn_env(name, self.env)
                self.assertIsNotNone(child_env)
                self.assertEqual(
                    str(self.controller.guard_bin),
                    child_env["PATH"].split(os.pathsep)[0],
                )

    def test_a_child_of_any_harness_environment_is_denied(self) -> None:
        """The environment each harness receives is itself enforcing."""
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                result = subprocess.run(
                    ["sh", "-c", "git push origin main"],
                    env={**self.env, "ASL_HARNESS": name},
                    capture_output=True,
                    text=True,
                    cwd=self.root,
                    check=False,
                )
                self.assertEqual(126, result.returncode)

    def test_run_harness_forwards_the_turn_environment_to_the_adapter(self) -> None:
        """The seam the runner actually calls, not just the adapter beneath it."""
        seen: dict[str, Any] = {}

        def record(_self: Any = None, **kwargs: Any) -> str:
            seen["env"] = kwargs.get("env")
            return "result"

        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                seen.clear()
                with mock.patch.object(
                    type(harnesses.get_adapter(name)), "run", record
                ):
                    harnesses.run_harness(
                        name,
                        cwd=self.root,
                        plan_dir=None,
                        prompt="hello",
                        env=self.env,
                    )
                self.assertEqual(self.env, seen["env"])

    def test_omitting_env_still_inherits_the_parent_environment(self) -> None:
        self.assertIsNone(self.spawn_env("codex", None))


class GuardControlQueueTests(unittest.TestCase):
    """Guard mutations are durable, validated commands like every other."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = self.root / "state"

    def test_add_guard_round_trips_through_the_queue(self) -> None:
        queued = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_ADD_GUARD,
            state_dir=self.state_dir,
            deny_commands=["git push origin main"],
            expected_run_id="run-1",
            reason="protect main",
        )
        self.assertEqual((("git", "push", "origin", "main"),), queued.deny_commands)
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.rejected)
        command = drained.accepted[0]
        self.assertEqual(control_queue.COMMAND_ADD_GUARD, command.command)
        self.assertEqual((("git", "push", "origin", "main"),), command.deny_commands)
        self.assertEqual("run-1", command.expected_run_id)
        self.assertEqual("protect main", command.reason)

    def test_remove_guard_round_trips_through_the_queue(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REMOVE_GUARD,
            state_dir=self.state_dir,
            guard_id="g-abc",
            expected_run_id="run-1",
        )
        command = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        ).accepted[0]
        self.assertEqual("g-abc", command.guard_id)
        self.assertEqual("run-1", command.expected_run_id)

    def test_append_rejects_malformed_guard_commands(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            (
                {"deny_commands": ["git push"]},
                control_queue.REJECT_MISSING_EXPECTED_RUN_ID,
            ),
            ({"expected_run_id": "run-1"}, control_queue.REJECT_MISSING_GUARD_RULE),
            (
                {"deny_commands": [], "expected_run_id": "run-1"},
                control_queue.REJECT_MISSING_GUARD_RULE,
            ),
            (
                {"deny_commands": ["/bin/git push"], "expected_run_id": "run-1"},
                guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            ),
            (
                {"deny_commands": ["git push && rm"], "expected_run_id": "run-1"},
                guards.REJECT_SHELL_OPERATOR,
            ),
        ]
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(control_queue.ControlQueueError) as caught:
                    control_queue.append_command(
                        self.plan_dir,
                        control_queue.COMMAND_ADD_GUARD,
                        state_dir=self.state_dir,
                        **kwargs,
                    )
                self.assertEqual(reason, str(caught.exception))

    def test_remove_without_a_guard_id_is_rejected(self) -> None:
        with self.assertRaises(control_queue.ControlQueueError) as caught:
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REMOVE_GUARD,
                state_dir=self.state_dir,
                expected_run_id="run-1",
            )
        self.assertEqual(control_queue.REJECT_MISSING_GUARD_ID, str(caught.exception))

    def test_drain_rejects_a_hand_written_line_with_the_same_reason(self) -> None:
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps(
                {
                    "id": "c-1",
                    "command": control_queue.COMMAND_ADD_GUARD,
                    "deny_commands": ["git push"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.accepted)
        self.assertEqual(
            control_queue.REJECT_MISSING_EXPECTED_RUN_ID, drained.rejected[0].reason
        )


class _RecordingPersistence:
    """The coordination persistence seam, keeping events and dropping state."""

    def __init__(self, append_event) -> None:
        self.lock = threading.RLock()
        self.append_event = append_event
        self.states: list[dict[str, Any]] = []

    def write_state(self, payload: dict[str, Any]) -> None:
        self.states.append(dict(payload))


class GuardDrainTests(unittest.TestCase):
    """The runner applies guard commands only to the run they name."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = self.root / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True, exist_ok=True)
        self.event_path = self.plan_state / events.EVENTS_FILE_NAME
        self.payload: dict[str, Any] = {"run_id": "run-1"}
        self.control_payload = run_coordination.initial_control_payload()
        self.persistence = _RecordingPersistence(self.append_event)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.plan_state / "runs" / "run-1",
            path_value=str(self.bin_dir),
        )

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def guard_events(self) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [
            record["payload"]
            for record in records
            if record["payload"].get("action") in events.GUARD_TERMINAL_ACTIONS
        ]

    def drain(self) -> None:
        run_coordination.drain_external_control_queue(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=run_coordination.run_control.RunControlState(),
            seen_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            context=None,
            persistence=self.persistence,
            run_control_lock=mock.MagicMock(),
            guard_controller=self.controller,
        )

    def queue(self, **kwargs: Any) -> None:
        control_queue.append_command(
            self.plan_dir,
            kwargs.pop("command"),
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_guard_add_is_applied_and_recorded(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push origin main"],
            expected_run_id="run-1",
        )
        self.drain()
        recorded = self.guard_events()
        self.assertEqual(1, len(recorded))
        self.assertEqual(events.RUN_CONTROL_ACTION_GUARD_APPLIED, recorded[0]["action"])
        self.assertEqual(1, len(self.controller.active()))
        self.assertEqual(1, len(self.control_payload["guards"]))
        self.assertEqual(
            self.control_payload["guards"], self.payload["control"]["guards"]
        )

    def test_guard_for_a_different_run_is_rejected_at_drain(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push"],
            expected_run_id="run-0",
        )
        self.drain()
        recorded = self.guard_events()
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_REJECTED, recorded[0]["action"]
        )
        self.assertEqual(guards.REJECT_RUN_CHANGED, recorded[0]["reason"])
        self.assertEqual((), self.controller.active())

    def test_guard_command_without_a_controller_is_rejected(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push"],
            expected_run_id="run-1",
        )
        self.controller = None  # type: ignore[assignment]
        self.drain()
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, self.guard_events()[0]["reason"])

    def test_guard_remove_is_applied_and_recorded(self) -> None:
        applied = self.controller.add([("git", "push")])
        self.queue(
            command=control_queue.COMMAND_REMOVE_GUARD,
            guard_id=applied.guard_ids[0],
            expected_run_id="run-1",
        )
        self.drain()
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_REMOVED, self.guard_events()[0]["action"]
        )
        self.assertEqual((), self.controller.active())
        self.assertEqual([], self.control_payload["guards"])

    def test_removing_an_unknown_guard_is_rejected(self) -> None:
        self.queue(
            command=control_queue.COMMAND_REMOVE_GUARD,
            guard_id="g-nope",
            expected_run_id="run-1",
        )
        self.drain()
        self.assertEqual(guards.REJECT_UNKNOWN_GUARD, self.guard_events()[0]["reason"])

    def test_expiry_records_the_guards_it_ended(self) -> None:
        self.controller.add([("git", "push")])
        run_coordination.expire_run_guards(
            controller=self.controller,
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            persistence=self.persistence,
        )
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        expired = [
            record["payload"]
            for record in records
            if record["payload"].get("action")
            == events.RUN_CONTROL_ACTION_GUARDS_EXPIRED
        ]
        self.assertEqual(1, len(expired))
        self.assertEqual(1, len(expired[0]["guard_ids"]))
        self.assertEqual([], self.control_payload["guards"])

    def test_expiry_of_an_empty_gate_records_nothing(self) -> None:
        run_coordination.expire_run_guards(
            controller=self.controller,
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            persistence=self.persistence,
        )
        self.assertFalse(self.event_path.exists())


class GuardCliTests(unittest.TestCase):
    """The operator surface: refuses without a run, waits for enforcement."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.root / "state"

    def args(self, **overrides: Any) -> Any:
        namespace = {
            "plan_dir": str(self.plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
            "wait": False,
            "wait_timeout": None,
            "deny_command": None,
            "protect_path": None,
            "reason": None,
            "id": None,
        }
        namespace.update(overrides)
        return type("Args", (), namespace)()

    def activate_run(self, run_id: str = "run-1") -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": run_id, "started_at": "2026-01-01T00:00:00+00:00"},
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())

    def capture(self, handler, args) -> tuple[int, dict[str, Any]]:
        import contextlib
        import io as _io

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = handler(args)
        return code, json.loads(buffer.getvalue())

    def test_add_without_a_rule_is_refused_before_anything_is_written(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args, self.args()
        )
        self.assertEqual(1, code)
        self.assertEqual(control_queue.REJECT_MISSING_GUARD_RULE, payload["reason"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_add_names_the_rule_that_is_malformed(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["/bin/git push"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_EXECUTABLE_NOT_BARE_NAME, payload["reason"])

    def test_add_without_an_active_run_is_refused(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["git push"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, payload["reason"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_remove_without_an_active_run_is_refused(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_remove_from_args,
            self.args(id="g-abc"),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, payload["reason"])

    def test_add_enqueues_with_the_active_run_id(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["git push origin main"], reason="protect"),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual("run-1", payload["run_id"])
        self.assertEqual([["git", "push", "origin", "main"]], payload["deny_commands"])
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("run-1", drained.accepted[0].expected_run_id)

    def test_add_enqueues_protected_paths_alongside_command_rules(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(
                deny_command=["git push"],
                protect_path=[".:docs/**/*.md", ".:README.md"],
                reason="frozen",
            ),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual([["git", "push"]], payload["deny_commands"])
        self.assertEqual([".:docs/**/*.md", ".:README.md"], payload["protect_paths"])
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        queued = drained.accepted[0]
        self.assertEqual(
            [".:docs/**/*.md", ".:README.md"],
            [path.rendered for path in queued.protect_paths],
        )

    def test_add_with_only_a_protected_path_is_accepted(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(protect_path=[".:README.md"]),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual([], payload["deny_commands"])

    def test_add_names_the_protected_path_that_is_malformed(self) -> None:
        cases = {
            ".:/etc/passwd": guards.REJECT_PATH_ABSOLUTE,
            # No qualifier at all is the more specific complaint, even when the
            # path is also absolute: the operator has to name a repository
            # before the path can mean anything.
            "/etc/passwd": guards.REJECT_PATH_MISSING_QUALIFIER,
            "docs/api.md": guards.REJECT_PATH_MISSING_QUALIFIER,
            ".:../escape": guards.REJECT_PATH_TRAVERSAL,
            ".:docs\\api.md": guards.REJECT_PATH_BACKSLASH,
        }
        for rule, reason in cases.items():
            with self.subTest(rule=rule):
                code, payload = self.capture(
                    control_cli.control_guard_add_from_args,
                    self.args(protect_path=[rule]),
                )
                self.assertEqual(1, code)
                self.assertEqual(reason, payload["reason"])
                self.assertEqual(rule, payload["details"]["protect_path"])

    def test_a_legacy_plan_refuses_an_alias_it_does_not_have(self) -> None:
        """The alias half is checked against the plan the operator named."""
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(protect_path=["App:docs/api.md"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_PATH_UNKNOWN_ALIAS, payload["reason"])
        self.assertEqual(["."], payload["details"]["known_qualifiers"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_a_registered_plan_takes_its_declared_aliases(self) -> None:
        self.assertEqual(
            ("App", "Docs"),
            control_cli.plan_path_qualifiers(
                {"repositories": [{"alias": "App"}, {"alias": "Docs"}]}
            ),
        )
        self.assertEqual(
            (guards.LEGACY_PATH_QUALIFIER,),
            control_cli.plan_path_qualifiers({"repositories": []}),
        )

    def test_list_reports_the_active_set_and_the_coverage_statement(self) -> None:
        self.activate_run()
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "control": {
                    "guards": [{"id": "g-1", "command": ["git", "push"]}],
                },
            },
        )
        code, payload = self.capture(
            control_cli.control_guard_list_from_args, self.args()
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["run_active"])
        self.assertEqual("g-1", payload["guards"][0]["id"])
        self.assertEqual(
            guards.COMMAND_GATE_COVERAGE["denied_exit_code"],
            payload["command_gate"]["denied_exit_code"],
        )

    def test_list_without_a_run_reports_no_enforcement(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_list_from_args, self.args()
        )
        self.assertEqual(0, code)
        self.assertFalse(payload["run_active"])
        self.assertEqual([], payload["guards"])

    def test_wait_resolves_on_the_matching_guard_outcome(self) -> None:
        self.activate_run()
        events_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

        def fake_dispatch(*_args: Any, **kwargs: Any) -> Any:
            command = control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_ADD_GUARD,
                state_dir=self.state_dir,
                deny_commands=list(kwargs["deny_commands"]),
                expected_run_id=kwargs["expected_run_id"],
            )
            with open(events_path, "a", encoding="utf-8") as handle:
                for action, command_id in (
                    (
                        events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                        command.id,
                    ),
                    # A concurrent operator's guard must not answer this wait.
                    (events.RUN_CONTROL_ACTION_GUARD_APPLIED, "other-command"),
                    (events.RUN_CONTROL_ACTION_GUARD_APPLIED, command.id),
                ):
                    handle.write(
                        json.dumps(
                            {
                                "name": events.RUN_CONTROL_EVENT,
                                "payload": {
                                    "action": action,
                                    "command_id": command_id,
                                    "guard_ids": ["g-1"],
                                },
                            }
                        )
                        + "\n"
                    )
            return intervention.ControlDispatch(
                command=command,
                queue_path=control_queue.queue_path_for_plan(
                    self.plan_dir, self.state_dir
                ),
                signal=control_cli.control_signal.NotifyResult(
                    delivered=False, reason="no-runner"
                ),
            )

        with mock.patch.object(
            control_cli.intervention, "dispatch_control_command", fake_dispatch
        ):
            code, payload = self.capture(
                control_cli.control_guard_add_from_args,
                self.args(deny_command=["git push"], wait=True, wait_timeout=5.0),
            )
        self.assertEqual(0, code, payload)
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_APPLIED, payload["wait"]["terminal_action"]
        )
        # A guard outcome is final on its own; it must not be reported as a
        # run that is still finalizing.
        self.assertNotIn("note", payload["wait"])

    def test_wait_reports_a_refusal_as_a_failure(self) -> None:
        self.activate_run()
        events_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

        def fake_dispatch(*_args: Any, **kwargs: Any) -> Any:
            command = control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_ADD_GUARD,
                state_dir=self.state_dir,
                deny_commands=list(kwargs["deny_commands"]),
                expected_run_id=kwargs["expected_run_id"],
            )
            with open(events_path, "a", encoding="utf-8") as handle:
                for action, extra in (
                    (events.RUN_CONTROL_ACTION_COMMAND_OBSERVED, {}),
                    (
                        events.RUN_CONTROL_ACTION_GUARD_REJECTED,
                        {"reason": guards.REJECT_RUN_CHANGED, "detail": "run-0"},
                    ),
                ):
                    handle.write(
                        json.dumps(
                            {
                                "name": events.RUN_CONTROL_EVENT,
                                "payload": {
                                    "action": action,
                                    "command_id": command.id,
                                    **extra,
                                },
                            }
                        )
                        + "\n"
                    )
            return intervention.ControlDispatch(
                command=command,
                queue_path=control_queue.queue_path_for_plan(
                    self.plan_dir, self.state_dir
                ),
                signal=control_cli.control_signal.NotifyResult(
                    delivered=False, reason="no-runner"
                ),
            )

        with mock.patch.object(
            control_cli.intervention, "dispatch_control_command", fake_dispatch
        ):
            code, payload = self.capture(
                control_cli.control_guard_add_from_args,
                self.args(deny_command=["git push"], wait=True, wait_timeout=5.0),
            )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_RUN_CHANGED, payload["reason"])


class GuardStatusTests(unittest.TestCase):
    """Status reports the gate a live run is enforcing, and nothing more."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.root / "state"

    def status(self) -> dict[str, Any]:
        from agent_session_loop import plans

        return state.status_payload(plans.inspect_plan(self.plan_dir), self.state_dir)

    def test_no_run_reports_an_empty_gate_with_its_coverage(self) -> None:
        payload = self.status()
        self.assertEqual([], payload["command_guards"]["active"])
        self.assertEqual(0, payload["command_guards"]["count"])
        self.assertEqual(
            guards.COMMAND_GATE_COVERAGE["mechanism"],
            payload["command_guards"]["coverage"]["mechanism"],
        )

    def test_active_run_reports_its_guards(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "control": {
                    "guards": [
                        {
                            "id": "g-1",
                            "command": ["git", "push"],
                            "rendered_command": "git push",
                        }
                    ]
                },
            },
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        payload = self.status()
        self.assertEqual(1, payload["command_guards"]["count"])
        self.assertEqual("g-1", payload["command_guards"]["active"][0]["id"])

    def test_a_finished_run_reports_no_enforcement(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "run_finished_at": "2026-01-01T01:00:00+00:00",
                "control": {"guards": [{"id": "g-1", "command": ["git", "push"]}]},
            },
        )
        payload = self.status()
        self.assertEqual([], payload["command_guards"]["active"])


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
        install_fake_git(self.bin_dir)
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


class GuardDocumentationTests(unittest.TestCase):
    """Every stable reason a guard can produce is documented."""

    def test_guard_reasons_are_documented(self) -> None:
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
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
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
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
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        for reason in (
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED,
            events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE,
        ):
            with self.subTest(reason=reason):
                self.assertIn(f"`{reason}`", text)

    def test_protected_path_coverage_is_documented(self) -> None:
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("asl control guard add", text)
        self.assertIn("--protect-path", text)
        for claim in (
            *guards.PROTECTED_PATH_COVERAGE["covers"],
            *guards.PROTECTED_PATH_COVERAGE["does_not_cover"],
        ):
            with self.subTest(claim=claim):
                self.assertIn(claim, text)

    def test_bypasses_are_documented(self) -> None:
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("asl control guard add", text)
        for bypass in guards.COMMAND_GATE_COVERAGE["does_not_cover"]:
            with self.subTest(bypass=bypass):
                self.assertIn(bypass, text)


if __name__ == "__main__":
    unittest.main()
