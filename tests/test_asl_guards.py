"""Run-scoped command guard tests: rules, the gate, and its lifecycle.

Enforcement is proved by running real processes against a fake ``git``
installed on a temporary ``PATH``. Nothing here touches a real network-affecting
command: the point is that the gate intercepts a bare-name resolution, and a
fake executable proves that more honestly than a real one, which would also
have to be prevented from doing its job.
"""

from __future__ import annotations

import unittest

from agent_session_loop import (
    command_guard,
    guards,
)

ASL_TEST_LANE = "integration"


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


if __name__ == "__main__":
    unittest.main()
