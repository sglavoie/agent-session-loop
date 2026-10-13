"""Session-split, session-run, and lint-category docs regression tests."""

from __future__ import annotations

import unittest

from agent_session_loop import authoring_source_models, preflight_models
from tests.asl_docs_support import read_asl_reference
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class SessionSkillDocsTests(unittest.TestCase):
    def test_session_split_names_current_contract_categories(self) -> None:
        text = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("missing-contract", text)
        self.assertNotIn("session-contract", text)

    def test_session_split_documents_optional_parallel_verify_flag(self) -> None:
        text = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("`verify.parallel` is optional and defaults to `false`", text)
        self.assertIn(
            "two or more `command` checks that cannot interfere with each other",
            text,
        )
        self.assertIn("reporting results in declared order", text)

    def test_session_split_delegates_heading_grammar_to_parser(self) -> None:
        text = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(encoding="utf-8")

        # The full source-heading grammar restatement is gone; the parser is
        # authoritative and malformed headings surface as PlanError verbatim.
        self.assertIn(
            "agent_session_loop.authoring_parse.parse_plan_document",
            text,
        )
        self.assertIn("without asking for normalization approval", text)
        self.assertIn("line-specific `PlanError`", text)
        self.assertIn("reason `malformed-plan`", text)
        self.assertIn(
            "only when canonical parsing succeeds with zero explicit session headings",
            text,
        )
        self.assertIn("canonical `## Session N: <title>` headings", text)
        self.assertNotIn("zero to three leading spaces", text)
        self.assertNotIn(
            "using the `## Session <id>` / `### Session <id>` headings",
            text,
        )

    def test_session_split_repairs_malformed_headings_before_stopping(self) -> None:
        text = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(encoding="utf-8")

        # A malformed heading is a repairable typo, not a dead end: the skill
        # lints first, repairs, re-checks, and only then stops.
        self.assertIn("### Step 2: Validate and Repair the Source Plan", text)
        self.assertIn("asl split --check --plan-file", text)
        # --cwd is the root the footprint check measures against, so both
        # invocations carry it.
        self.assertIn('asl split --check --inline-stdin --cwd "$PWD" --json', text)
        self.assertIn("**Repair loop** (at most 3 rounds):", text)
        self.assertIn("non-null `suggested_line`", text)
        self.assertIn("`suggested_line: null`", text)
        self.assertIn("ask the user to approve it", text)
        self.assertIn("never write to the original plan file", text)
        self.assertIn("If findings remain after 3 rounds", text)
        self.assertIn(
            "keyed by the canonical session ids reported by the final clean "
            "`asl split --check`",
            text,
        )
        self.assertIn("one `line <N>: <category>` line per repair", text)

    def test_docs_document_split_check_lint_contract(self) -> None:
        text = read_asl_reference()

        self.assertIn("`split --check` lints a source plan's session headings", text)
        self.assertIn("It needs no `--contracts-file`", text)
        self.assertIn("`lint_plan_document`", text)
        self.assertIn("`findings` array", text)
        self.assertIn("`suggested_line` is non-null exactly when the damage has", text)
        self.assertIn("`reason: malformed-plan`", text)
        self.assertIn("for at most three", text)
        # The fail-fast parser's verbatim-message promise is unchanged.
        self.assertIn(
            "`malformed-plan` (the parser's line-specific `PlanError` verbatim)",
            text,
        )

    def test_docs_name_every_lint_category(self) -> None:
        text = read_asl_reference()

        categories = [
            value
            for name, value in vars(authoring_source_models).items()
            if name.startswith("LINT_") and isinstance(value, str)
        ]
        self.assertGreaterEqual(len(categories), 12)
        for category in categories:
            self.assertIn(f"`{category}`", text)

    def test_docs_name_every_preflight_category_and_no_retired_one(self) -> None:
        text = read_asl_reference()

        for category in preflight_models.PREFLIGHT_CATEGORIES:
            self.assertIn(f"`{category}`", text)
        # `env-spec` was retired with the external `spec commit` dependency;
        # the finding list must not keep advertising a category no run emits.
        self.assertNotIn("env-spec", preflight_models.PREFLIGHT_CATEGORIES)
        self.assertNotIn("env-spec", text)

    def test_docs_document_every_asl_commit_mode(self) -> None:
        text = read_asl_reference()

        self.assertIn(
            "asl commit [PATH] [--yes] [--verify|--no-verify] [--staged] [--dry-run]",
            text,
        )
        self.assertIn("It is usable standalone, outside any loop or plan.", text)
        # One command, four modes: the synopsis alone does not say which flag
        # selects which behavior, so each is named in prose.
        for mode in (
            "**default**",
            "**`--from-id SHA`**",
            "**`--prepare-only`**",
            "**`--message-file PATH`**",
        ):
            self.assertIn(mode, text)
        self.assertIn("`--staged` trusts the index and\nnever touches it", text)
        self.assertIn("budgeted to 150 KB", text)
        self.assertIn("with `-` reading stdin", text)
        # The generation bound is a behavior an operator has to be able to find
        # before a stall makes them go looking for it.
        self.assertIn("`--timeout SECONDS` (default: 900)", text)
        self.assertIn("`generation-timeout`", text)
        # The retired external binary must not reappear in either spelling.
        self.assertNotIn("spec commit", text)

    def test_session_run_documents_bounded_manual_auto_repair(self) -> None:
        text = (REPO_ROOT / "skills/session-run/SKILL.md").read_text(encoding="utf-8")

        # The manual readiness gate delegates to the machine-readable preflight
        # payload rather than re-encoding the repair ordering in prose.
        self.assertIn("`auto_repair_plan`", text)
        self.assertIn("`suggested_pause`", text)
        self.assertIn("asl unlock <plan-dir> --json", text)
        self.assertIn("Never run `asl unlock --force`", text)
        self.assertIn("asl backfill-progress --force <plan-dir>", text)
        self.assertIn(
            "asl preflight <plan-dir> --json --mark-loop-ready",
            text,
        )
        self.assertIn(
            "The only `progress.md` mutation this skill may trigger",
            text,
        )
        self.assertIn("When `runner_managed=true`:", text)

    def test_session_run_delegates_to_shared_payload_surfaces(self) -> None:
        text = (REPO_ROOT / "skills/session-run/SKILL.md").read_text(encoding="utf-8")

        # Single shared execution path plus one compact mode-deviation table —
        # the only place `--runner-managed` differs after input parsing.
        self.assertIn("## Shared execution path", text)
        self.assertIn("## Mode deviations", text)
        # Next-command derivation and verify remediation defer to the tooling.
        self.assertIn("`next_command`", text)
        self.assertIn("`repair_hint`", text)
        self.assertIn("`--runner-managed`", text)

    def test_session_run_bounds_repeated_checking_without_capping_it(self) -> None:
        """The skill must not read as "rerun every contract check yourself"."""
        text = (REPO_ROOT / "skills/session-run/SKILL.md").read_text(encoding="utf-8")

        # Ownership: focused checks while implementing, one authoritative run.
        self.assertIn(
            "Do not rerun the full Completion Contract solely as a final gate", text
        )
        self.assertIn("manual mode's single authoritative verification step", text)
        self.assertIn(
            '"Leave the contract\'s checks passing" is a statement about the '
            "state you hand over, not an instruction to rerun every check "
            "yourself.",
            text,
        )

        # The three cases that justify running a check again or a broader one.
        self.assertIn(
            "Running a check again, or running a broader one, is justified", text
        )
        self.assertIn("invalidated it", text)
        self.assertIn("a failure needs diagnosis", text)
        self.assertIn("concrete correctness concern none of your checks covered", text)
        self.assertIn(
            "Nothing here caps how many checks you run or how long they take", text
        )
        self.assertIn("record the brief reason in the handoff `Notes`", text)

        # The policy is shared, so it must not appear as a mode deviation.
        deviations = text.split("## Mode deviations", 1)[1]
        self.assertNotIn("Running a check again", deviations)

    def test_session_resplit_delegates_mechanics_to_helper(self) -> None:
        text = (REPO_ROOT / "skills/session-resplit/SKILL.md").read_text(
            encoding="utf-8"
        )

        # Compact single-line prefix rule shared with the other session-* skills
        # (the old multi-line "Determine <skill-prefix> once" block is gone).
        self.assertIn("**Skill-invocation prefix**", text)
        self.assertNotIn("Determine `<skill-prefix>` once", text)

        # Mechanics (parsing, inference, locks, archival, ledgers) belong to the
        # helper; the skill keeps only the semantic contract-allocation work.
        self.assertIn("resplit-session.py", text)
        self.assertIn(
            "input parsing, target/count inference",
            text,
        )
        # Handoff status inspection delegates to `asl status --json` rather than
        # re-deriving the final-handoff parsing rules in prose.
        self.assertIn("asl status <plan-dir> --json", text)
        self.assertNotIn(
            "Find the last line whose trimmed text is exactly `## Handoff`",
            text,
        )
        # The compressed lock step no longer restates the acquire_lock API call.
        self.assertNotIn(
            'acquire_lock(plan_dir, state_dir=None, owner="session-resplit")',
            text,
        )
        # Active-lock recovery policy: the helper self-clears stale locks, so
        # `lock-contention` is always an active lock — wait for or investigate
        # the live runner, never force-remove/unlock it (F6).
        self.assertIn("self-clears stale locks", text)
        self.assertIn("always means an *active* lock", text)
        self.assertIn("wait for that runner to finish", text)
        self.assertIn("never force-remove an active lock or run a manual unlock", text)
        self.assertNotIn("add --force if it", text)

        # Child-spec schema and contract-allocation reasons stay in the skill.
        self.assertIn("--child-spec", text)
        self.assertIn("`no-contract`", text)
        self.assertIn("`ambiguous-contract`", text)
        # Runner signal vocabulary is preserved for the supervising loop.
        self.assertIn("RESPLIT_STATUS: complete", text)
        self.assertIn("RESPLIT_PAUSE: <reason> - <detail>", text)


if __name__ == "__main__":
    unittest.main()
