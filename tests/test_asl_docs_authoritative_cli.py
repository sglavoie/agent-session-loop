"""CLI, contract, and review docs checked against authoritative definitions."""

from __future__ import annotations

import argparse
import re
import unittest

from agent_session_loop import (
    cli,
    contracts,
    events,
    plans,
    preflight,
    prepare_models,
    repository_registration,
    retention,
    review,
    review_continuation,
    run_inputs,
    state,
)
from tests.asl_docs_support import ASL_GUIDE, read_asl_reference
from tests.support import (
    REPO_ROOT,
)

#: The ASL operator guide moved out of the repository README; it carries the
#: operator-facing summary these tests used to read from `README.md`.


class AuthoritativeCliDocsTests(unittest.TestCase):
    docs: str

    guide: str

    justfile: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = read_asl_reference()
        cls.guide = (REPO_ROOT / ASL_GUIDE).read_text(encoding="utf-8")
        cls.justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")

    def test_every_cli_subcommand_is_documented(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name in subparsers.choices:
            self.assertIn(
                f"asl {name}",
                self.docs,
                f"docs never mention `asl {name}`",
            )

    def test_commands_block_lists_current_flag_surfaces(self) -> None:
        self.assertIn(
            "asl metrics [--since-days N] [--state-dir DIR] [--json]", self.docs
        )
        self.assertIn("[--artifact-dir DIR]", self.docs)
        self.assertIn("asl prepare --resume PLAN_DIR", self.docs)
        self.assertIn("[--max-review-fix-cycles N]", self.docs)
        self.assertIn(
            "asl register-repositories (PLAN_DIR | --plan-file PATH | "
            "--inline-stdin) [--cwd ROOT] [--output PATH] "
            "[--state-dir DIR] [--json]",
            self.docs,
        )

    def test_registry_migration_follow_up_order_is_documented(self) -> None:
        agent_readme = (REPO_ROOT / "agent_session_loop" / "README.md").read_text(
            encoding="utf-8"
        )
        for text in (self.docs, agent_readme):
            rebind = text.index(
                "asl prepare PLAN_DIR --resume --rebind-workspace --cwd WORKSPACE_ROOT"
            )
            rerun = text.index("asl run PLAN_DIR --cwd WORKSPACE_ROOT")
            self.assertLess(rebind, rerun)
        self.assertIn("missing fact: <fact>", self.docs)
        for fact in repository_registration.MISSING_FACTS:
            self.assertIn(f"`{fact}`", self.docs)

    def test_run_help_default_matches_authoritative_review_cycle_default(
        self,
    ) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        run_help = subparsers.choices["run"].format_help()
        self.assertIn(
            f"review (default: {run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES}).",
            run_help,
        )

    def test_multi_repository_docs_include_executable_four_repo_example(
        self,
    ) -> None:
        agent_readme = (REPO_ROOT / "agent_session_loop" / "README.md").read_text(
            encoding="utf-8"
        )
        for text in (self.guide, agent_readme, self.docs):
            self.assertIn("workspace root", text.lower())
            self.assertIn("Alias:path", text)

        self.assertIn("| Alias | Path | Origin |", self.docs)
        expected_rows = (
            "| SVC | tundra_svc | git@github.com:tundra/SVC.git |",
            "| OAK | tundra_OAK | git@github.com:tundra/OAK.git |",
            "| OWL | tundra_OWL_J | git@github.com:tundra/OWL.J.git |",
            "| Kiln | tundra_Kiln | git@github.com:tundra/Kiln.git |",
        )
        for row in expected_rows:
            self.assertIn(row, self.docs)
        self.assertIn("non-Git common parent", self.docs)
        self.assertIn("does not opt it into\nworkspace behavior", self.docs)

    def test_metadata_and_validator_version_compatibility_is_documented(
        self,
    ) -> None:
        self.assertEqual(1, prepare_models.LEGACY_PREPARE_VALIDATION_VERSION)
        self.assertEqual(2, prepare_models.PREPARE_VALIDATION_VERSION)
        for text in (self.guide, self.docs):
            self.assertIn("version 1", text)
            self.assertIn("version 2", text)
            self.assertIn("workspace_root", text)
            self.assertIn("--rebind-workspace", text)
        self.assertNotIn(
            "non-dry-run `asl run` refreshes/backfills the association",
            self.guide,
        )

    def test_preflight_categories_and_pause_reasons_match_authoritative_tables(
        self,
    ) -> None:
        for category in preflight.PREFLIGHT_CATEGORIES:
            self.assertIn(
                f"`{category}`",
                self.docs,
                f"docs omit preflight category {category!r}",
            )
        for reason in events.PAUSE_REASONS:
            self.assertIn(
                f"`{reason}`",
                self.docs,
                f"docs omit pause reason {reason!r}",
            )

    def test_repository_aware_review_schema_versions_are_documented(
        self,
    ) -> None:
        self.assertIn(
            f"review session version "
            f"{review.REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION}",
            self.docs,
        )
        self.assertIn(
            f"structured artifact\nversion "
            f"{review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION}",
            self.docs,
        )

    def test_docs_pin_gc_retention_contract(self) -> None:
        self.assertIn(
            "asl gc [PLAN_DIR] [--state-dir DIR] [--max-age-days DAYS] "
            "[--dry-run|--apply] [--json]",
            self.docs,
        )
        self.assertIn(
            "reports reclaimable bytes per plan directory as well as in\naggregate",
            self.docs,
        )
        self.assertIn("A stale lock is\nrefused exactly like an active one", self.docs)
        self.assertIn("The\ndefault is a dry-run that deletes nothing", self.docs)
        self.assertIn(
            "`asl gc --dry-run` is a pure projection: it deletes nothing and "
            "writes nothing,\nlocks included.",
            self.docs,
        )
        self.assertIn("`asl gc` is the one command exempt from all\nof this", self.docs)
        for reason in (
            retention.RETAIN_PLAN_LOCKED,
            retention.RETAIN_LIVE_RUN,
            retention.RETAIN_PIN_UNKNOWN,
            retention.RETAIN_OPEN_ISSUE,
            retention.RETAIN_UNFINISHED,
            retention.RETAIN_UNDATED,
            retention.RETAIN_NEWEST_RUN,
            retention.RETAIN_WITHIN_CUTOFF,
        ):
            self.assertIn(f"`{reason}`", self.docs)

    def test_docs_pin_review_cycle_default_and_lock_reclamation(self) -> None:
        default_cycles = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES
        default_group_attempts = run_inputs.DEFAULT_MAX_REPAIR_GROUP_ATTEMPTS
        default_cycle_ceiling = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES_CEILING
        self.assertIn(
            f"unique source-review materializations (default {default_cycles};",
            self.docs,
        )
        self.assertIn(
            f"absolute cap — {default_cycles} unique source-review "
            "materializations unless the supervised",
            self.docs,
        )
        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(
            f"`asl run --max-review-fix-cycles` (default {default_cycles})",
            skill,
        )
        self.assertIn(
            f"`--max-repair-group-attempts` (default {default_group_attempts})",
            skill,
        )
        self.assertIn(
            f"`--max-review-fix-cycles-ceiling` (default {default_cycle_ceiling})",
            skill,
        )
        self.assertIn(
            "`repair-group-exhausted` or `cycle-cap-exhausted`",
            skill,
        )
        self.assertIn("recurring `repair_group`", skill)

        split_skill = (REPO_ROOT / "skills" / "session-split" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "By default `asl split` refuses a duplicate plan id before creating any "
            "directory",
            split_skill,
        )
        self.assertIn("intentional duplicate split", split_skill)
        self.assertIn("`--force-new`", split_skill)

        self.assertIn("Reading such a lock also removes it", self.docs)
        self.assertIn("reclaimed stale lock at <path>", self.docs)
        self.assertIn(
            "a legacy\nlock with no `hostname` key (its pid names no process table)",
            self.docs,
        )
        self.assertIn(
            "`asl unlock` remains the only route out for\na cross-host lock",
            self.docs,
        )
        self.assertIn(
            f"an\nexclusive `flock` on a `{state.LOCK_MUTEX_FILE_NAME}` sidecar",
            self.docs,
        )
        self.assertIn("No mutator ever\nunlinks without holding the claim", self.docs)
        self.assertIn(
            "the release at the end of a run leaves its\nlock in place",
            self.docs,
        )

    def test_contract_example_parses_with_authoritative_parser(self) -> None:
        section = self.docs.split("## Completion Contracts", 1)[1]
        match = re.search(r"```json\n(.*?)\n```", section, re.DOTALL)
        self.assertIsNotNone(match, "no fenced JSON contract example in docs")
        json_text = match.group(1).replace('"session": "<id>"', '"session": "1"')
        contract_text = (
            "## Completion Contract\n\n"
            "### Done when\n\n- Documented example criterion.\n\n"
            "### Verify\n\n```json\n" + json_text + "\n```\n"
        )
        contract = contracts.parse_completion_contract(
            contract_text, session_id=plans.parse_session_id("1")
        )
        self.assertEqual(1, len(contract.verify.checks))

    def test_contract_docs_list_baseline_expectation_vocabulary(self) -> None:
        self.assertIn("baseline_expectation", self.docs)
        for value in ("skip", "pass", "fail"):
            self.assertIn(value, self.docs)

    def test_review_artifact_docs_match_authoritative_definitions(self) -> None:
        self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, self.docs)
        self.assertIn(f"version: {review.REVIEW_ARTIFACT_VERSION}", self.docs)
        for key in review.FINDING_REQUIRED_KEYS:
            self.assertIn(
                f"`{key}`",
                self.docs,
                f"docs never mention required finding key {key!r}",
            )
        for disposition in review_continuation.REVIEW_CYCLE_DISPOSITIONS:
            self.assertIn(
                f"`{disposition}`",
                self.docs,
                f"docs never mention review-cycle disposition {disposition!r}",
            )

    def test_review_cycle_default_matches_runner_constant(self) -> None:
        self.assertIn(
            f"(default {run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES};", self.docs
        )


if __name__ == "__main__":
    unittest.main()
