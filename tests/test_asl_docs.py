"""Skill, docs, and install verification regression tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    assurance_ledger,
    assurance_models,
    assurance_report,
    authoring,
    cli,
    cli_trace,
    contracts,
    control_cli,
    control_queue,
    control_signal,
    events,
    install_verify,
    mailbox,
    mailbox_cli,
    metrics,
    pause_policy,
    plans,
    preflight,
    prepare,
    process_tree,
    qna,
    repository_registration,
    retention,
    review,
    review_continuation,
    run_inputs,
    run_persistence,
    runner,
    startup_context,
    state,
    supervisor_audit,
    task_packet,
    verification,
    workbench,
)
from tests import run_suite
from tests.support import (
    REPO_ROOT,
)


class SkillInstructionTests(unittest.TestCase):
    def test_contract_path_rules_distinguish_registered_and_legacy_plans(
        self,
    ) -> None:
        docs = (REPO_ROOT / "docs/agent-session-loop.md").read_text(encoding="utf-8")
        split_skill = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(
            encoding="utf-8"
        )
        repair_skill = (REPO_ROOT / "skills/plan-preflight-iterate/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("retain the legacy absolute-path behavior", docs)
        self.assertIn("upward-traversing `..` segment", docs)
        self.assertIn("relative to its selected", docs)
        self.assertIn("repository and cannot be absolute", docs)
        self.assertIn("repo-relative or absolute `path` / `cwd` behavior", split_skill)
        self.assertIn("free of upward `..` traversal", split_skill)
        self.assertIn("For a legacy plan", repair_skill)
        # A legacy absolute path is only tolerable inside the bound root; the
        # old blanket advice to "use an explicit absolute path" is what let a
        # multi-repository plan stay in single-root mode.
        self.assertIn(
            "keep an absolute path only when it resolves inside the bound root",
            repair_skill,
        )
        self.assertIn("For a registered plan", repair_skill)
        self.assertIn("every check needs a valid `repo`", repair_skill)
        self.assertIn("PREFLIGHT_ITERATE_PAUSE: plan-recreate", repair_skill)

    def test_session_expand_auto_marks_loop_ready_before_session_run(self) -> None:
        text = (REPO_ROOT / "skills/session-expand/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "asl preflight <plan-dir> --cwd <root> --json --mark-loop-ready",
            text,
        )
        self.assertIn("marked_loop_ready", text)
        self.assertIn(
            "Only when final preflight succeeds with `ok: true` and readiness is "
            "`loop-ready`",
            text,
        )
        self.assertIn("<skill-prefix>plan-preflight-iterate", text)
        # Contract validation and validator ingestion delegate to the `asl`
        # subcommands rather than inlining `python3 -` heredocs.
        self.assertIn(
            "asl contract-check <plan-dir> --cwd <root> --json",
            text,
        )
        self.assertIn("asl expand-ingest <plan-dir> --batch-file", text)
        self.assertNotIn("<<'PY'", text)

    def test_multi_repository_skill_contracts_stay_synchronized(self) -> None:
        surfaces = {
            name: (REPO_ROOT / path).read_text(encoding="utf-8")
            for name, path in {
                "split": "skills/session-split/SKILL.md",
                "expand": "skills/session-expand/SKILL.md",
                "run": "skills/session-run/SKILL.md",
                "resplit": "skills/session-resplit/SKILL.md",
                "preflight": "skills/plan-preflight-iterate/SKILL.md",
                "asl": "skills/asl/SKILL.md",
            }.items()
        }

        self.assertIn('put `"repo": "Alias"` on every check', surfaces["split"])
        self.assertIn("`Alias:path`", surfaces["split"])
        self.assertIn(
            f"`prepare.PREPARE_VALIDATION_VERSION` = "
            f"{prepare.PREPARE_VALIDATION_VERSION}",
            surfaces["expand"],
        )
        self.assertIn("`repository-ambiguous`", surfaces["expand"])
        self.assertIn("canonical `Alias:path` entries", surfaces["run"])
        self.assertIn("complete mutation authority", surfaces["run"])
        self.assertIn("never broadens repository authority", surfaces["resplit"])
        self.assertIn(
            "common workspace root for a registered plan", surfaces["preflight"]
        )
        self.assertIn("--rebind-workspace", surfaces["preflight"])
        for reason in (
            "workspace-write-scope-violation",
            "workspace-write-guard-unavailable",
            "commit-failed",
        ):
            self.assertIn(reason, surfaces["asl"])
        self.assertIn("one combined read-only review", surfaces["asl"])
        self.assertIn("preserve partial commit SHAs", surfaces["asl"])

        # `plans.READS_REPOSITORIES_PREFIX` puts the colon outside the bold;
        # any other spelling is prose the parser rejects.
        for name, text in surfaces.items():
            self.assertNotIn("**Reads repositories:**", text, msg=name)
            self.assertNotIn("**Writes repositories:**", text, msg=name)

        # Every skill drives `asl register-repositories` instead of asking a
        # human to hand-write the registry it derives from disk.
        self.assertIn("asl register-repositories --plan-file", surfaces["split"])
        self.assertIn("Migrate instead of asking", surfaces["split"])
        # `--cwd` is the root the plan is bound to; the workspace root is what
        # the migration derives, and only the rebind is invoked with it.
        for name in ("expand", "run", "preflight"):
            self.assertIn(
                "asl register-repositories <plan-dir> --cwd <bound-root> --json",
                surfaces[name],
                msg=name,
            )
            self.assertNotIn(
                "asl register-repositories <plan-dir> --cwd <workspace-root>",
                surfaces[name],
                msg=name,
            )
            self.assertIn(
                "asl prepare <plan-dir> --resume --rebind-workspace "
                "--cwd <workspace-root>",
                surfaces[name],
                msg=name,
            )
        self.assertNotIn(
            "No `--mark-loop-ready` or auto-repair step can clear it",
            surfaces["preflight"],
        )
        self.assertNotIn("no auto-repair step exists for them", surfaces["preflight"])
        # A refusal is the only stop condition, and it names one fact.
        for name in ("split", "preflight"):
            for fact in (
                "workspace-root",
                "repository-origin",
                "contract-path-outside-registry",
                "command-check-repo",
            ):
                self.assertIn(fact, surfaces[name], msg=name)
        # The completed-handoff refusal belongs to plan-directory mode alone: a
        # source plan carries no handoff a registry could invalidate.
        for name in ("expand", "preflight"):
            self.assertIn("completed-handoff-paths", surfaces[name], msg=name)
        self.assertNotIn("completed-handoff-paths", surfaces["split"])
        # Session 1 released non-Git targets: they are no longer a stop
        # condition anywhere, because no registry row could describe them.
        self.assertIn("never raises this finding", surfaces["split"])

    def test_docs_describe_the_automatic_registry_migration(self) -> None:
        docs = (REPO_ROOT / "docs/agent-session-loop.md").read_text(encoding="utf-8")
        readme = (REPO_ROOT / "agent_session_loop/README.md").read_text(
            encoding="utf-8"
        )

        for text in (docs, readme):
            self.assertNotIn("**Reads repositories:**", text)
            self.assertNotIn("**Writes repositories:**", text)
            self.assertNotIn("no auto-repair step can clear it", text)
            self.assertNotIn("no auto-repair step targets the category", text)

        self.assertIn(
            "asl register-repositories PLAN_DIR --cwd BOUND_ROOT --json", docs
        )
        self.assertIn(
            "asl register-repositories <plan-dir> --cwd <bound-root> --json",
            docs,
        )
        self.assertIn(
            "asl prepare <plan-dir> --resume --rebind-workspace --cwd <workspace-root>",
            docs,
        )
        self.assertIn(
            "`asl run` applies the file migration itself on that same "
            "admissibility rule",
            readme,
        )
        # One rule, three surfaces: a registry that would make a completed
        # handoff unparsable is refused rather than advertised.
        for text in (docs, readme):
            self.assertIn("completed-handoff-paths", text)

        # Session 1: an escaping target owned by no Git repository is external
        # evidence, never a refusal, because a registry row can only describe
        # a Git repository.
        self.assertIn("belongs to *no* Git worktree", docs)
        self.assertIn("`external` lists the escaping targets", docs)
        self.assertIn("belongs to no Git worktree", readme)
        self.assertIn("an escaping target that belongs to no Git worktree", readme)

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
            "agent_session_loop.authoring.parse_plan_document",
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
        text = (REPO_ROOT / "docs/agent-session-loop.md").read_text(encoding="utf-8")

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
        text = (REPO_ROOT / "docs/agent-session-loop.md").read_text(encoding="utf-8")

        categories = [
            value
            for name, value in vars(authoring).items()
            if name.startswith("LINT_") and isinstance(value, str)
        ]
        self.assertGreaterEqual(len(categories), 12)
        for category in categories:
            self.assertIn(f"`{category}`", text)

    def test_docs_name_every_preflight_category_and_no_retired_one(self) -> None:
        text = (REPO_ROOT / "docs/agent-session-loop.md").read_text(encoding="utf-8")

        for category in preflight.PREFLIGHT_CATEGORIES:
            self.assertIn(f"`{category}`", text)
        # `env-spec` was retired with the external `spec commit` dependency;
        # the finding list must not keep advertising a category no run emits.
        self.assertNotIn("env-spec", preflight.PREFLIGHT_CATEGORIES)
        self.assertNotIn("env-spec", text)

    def test_docs_document_every_asl_commit_mode(self) -> None:
        text = (REPO_ROOT / "docs/agent-session-loop.md").read_text(encoding="utf-8")

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


class PlanWorkflowDocsTests(unittest.TestCase):
    PLAN_ITERATE_PATH = REPO_ROOT / "skills/plan-iterate/SKILL.md"
    PLAN_IDEAL_PATH = REPO_ROOT / "skills/plan-ideal/SKILL.md"

    def _plan_iterate_text(self) -> str:
        return self.PLAN_ITERATE_PATH.read_text(encoding="utf-8")

    def _plan_ideal_text(self) -> str:
        return self.PLAN_IDEAL_PATH.read_text(encoding="utf-8")

    def test_plan_iterate_documents_ideal_mode_invocations(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("plan-iterate PLAN.md --ideal --cwd REPO_ROOT", text)
        self.assertIn("plan-iterate PLAN.md --ideal-only [--cwd REPO_ROOT]", text)
        self.assertIn("mutually exclusive", text)

    def test_plan_iterate_requires_single_shared_research_pass(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("One Shared Research Pass", text)
        self.assertIn(
            "shared by the ideal divergence analysis and the completeness assessment",
            text,
        )
        self.assertIn("Do not run a second research pass", text)
        self.assertIn("Do not research the codebase twice.", text)

    def test_plan_iterate_ranks_divergences_and_asks_adopt_defer_reject(
        self,
    ) -> None:
        text = self._plan_iterate_text()

        self.assertIn("Identify Divergences and Rank by Impact", text)
        self.assertIn("Ask the User to Adopt, Defer, or Reject", text)
        self.assertIn('label: "Adopt"', text)
        self.assertIn('label: "Defer"', text)
        self.assertIn('label: "Reject"', text)

    def test_plan_iterate_records_decisions_in_the_plan_file(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("## Consciously Deferred", text)
        self.assertIn(
            "Accepted ideal improvements must be present in the plan file"
            " rather than only conversation history.",
            text,
        )

    def test_plan_iterate_targets_asl_prepare_readiness(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("Ensure the Plan Is Ready for `asl prepare`", text)
        self.assertIn("needs-plan-iteration", text)
        self.assertIn("## Completion Contract", text)
        self.assertIn("### Done when", text)
        self.assertIn("### Verify", text)
        self.assertIn("contracts.parse_completion_contract", text)
        self.assertIn("baseline_expectation", text)
        self.assertIn("asl prepare <plan-path> --cwd <repo-root>", text)

    def test_plan_iterate_uses_shared_source_heading_grammar(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("case-insensitive H2-H6 ATX headings", text)
        self.assertIn("Source ID components may be zero-padded", text)
        self.assertIn("surface malformed-candidate parser errors directly", text)
        self.assertIn("without a normalization question or source rewrite", text)
        self.assertIn(
            "canonical parsing confirms has no explicit session headings",
            text,
        )
        self.assertIn("author the canonical `## Session <id>: Title` form", text)
        self.assertNotIn(
            "any `## Session N` or `### Session N` headings",
            text,
        )
        self.assertNotIn(
            "at least two explicit `## Session <id>: Title` (or `### Session <id>`) "
            "headings",
            text,
        )

    def test_plan_iterate_preserves_default_mode_without_ideal_flag(
        self,
    ) -> None:
        text = self._plan_iterate_text()

        self.assertIn(
            "behavior when `--ideal` is absent is unchanged",
            text,
        )
        self.assertIn("### Step 4: Iterative Q&A Rounds", text)
        self.assertIn("Run up to **4 rounds** of question-and-edit cycles.", text)

    def test_plan_iterate_ideal_only_mode_is_read_only(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("## Ideal-Only Mode (`--ideal-only`)", text)
        self.assertIn("**Never modify the plan file** in this mode", text)
        self.assertIn(
            "read-only alias for `plan-iterate --ideal-only`",
            text,
        )

    def test_plan_iterate_premortem_cites_category_aware_recurrence(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("### Step 3.5: Premortem", text)
        self.assertIn("asl metrics --json", text)
        self.assertIn(
            "`assurance.issues.recurrence_after_resolution_by_category` maps"
            " each category",
            text,
        )

    def test_plan_iterate_metrics_paths_exist_in_the_metrics_report(self) -> None:
        cited = set(
            re.findall(r"assurance\.issues\.([a-z_]+)", self._plan_iterate_text())
        )
        self.assertIn("recurrence_after_resolution_by_category", cited)

        with tempfile.TemporaryDirectory() as tmp:
            report = assurance_report.collect_assurance_metrics(
                state_dir=Path(tmp) / "state",
                since=datetime(2026, 8, 10, tzinfo=UTC),
            )
        self.assertLessEqual(cited, set(report["issues"]))

    def test_plan_iterate_cites_the_shared_mental_models_catalogue(self) -> None:
        text = self._plan_iterate_text()

        self.assertIn("../references/mental-models.md", text)
        self.assertIn("on demand", text)
        self.assertIn("cite at most 3 models by name per report", text)

        catalogue = (
            self.PLAN_ITERATE_PATH.parent / "../references/mental-models.md"
        ).resolve()
        self.assertTrue(catalogue.is_file(), catalogue)

        row = next(
            line
            for line in catalogue.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `plan-iterate` |")
        )
        assigned = [model.strip() for model in row.split("|")[2].split(",")]
        self.assertLessEqual(len(assigned), 3, assigned)
        lowered = text.lower()
        for model in assigned:
            self.assertIn(model.lower(), lowered)

    def test_plan_ideal_is_read_only_alias_for_ideal_only_mode(self) -> None:
        text = self._plan_ideal_text()

        self.assertIn("alias for `plan-iterate --ideal-only`", text)
        self.assertIn("**Never modify the plan file**", text)
        self.assertIn("Ideal-Only Mode (`--ideal-only`)", text)
        self.assertIn("one shared research pass", text)

    def test_plan_ideal_alias_references_existing_plan_iterate_mode(
        self,
    ) -> None:
        iterate_text = self._plan_iterate_text()
        ideal_text = self._plan_ideal_text()

        self.assertIn("../plan-iterate/SKILL.md", ideal_text)
        self.assertIn("## Ideal-Only Mode (`--ideal-only`)", iterate_text)


class InstallVerificationTests(unittest.TestCase):
    def _install_fake_home_links(self, home: Path) -> Path:
        fake_bin = home / ".local/bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "asl").symlink_to(REPO_ROOT / "bin/.local/bin/asl")

        skill_dirs = [
            path
            for path in (REPO_ROOT / "skills").iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        ]
        for install_dir in install_verify.SKILL_INSTALL_DIRS:
            install_root = home / install_dir
            install_root.mkdir(parents=True)
            for skill_dir in skill_dirs:
                (install_root / skill_dir.name).symlink_to(
                    skill_dir,
                    target_is_directory=True,
                )

        for target in startup_context.CONTEXT_TARGETS:
            context_file = home / target.installed_path
            context_file.parent.mkdir(parents=True, exist_ok=True)
            context_file.write_text(
                startup_context.render_block(target.harness) + "\n",
                encoding="utf-8",
            )
        return fake_bin

    def test_verify_install_accepts_links_to_this_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            fake_bin = self._install_fake_home_links(home)

            payload = install_verify.verify_install(
                repo_root=REPO_ROOT,
                home=home,
                path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            )

            self.assertTrue(payload["ok"], payload["failures"])
            checks = payload["checks"]
            self.assertIn("asl-path", {check["name"] for check in checks})
            self.assertIn("status-smoke", {check["name"] for check in checks})
            status_smoke = next(
                check for check in checks if check["name"] == "status-smoke"
            )
            self.assertIn("omitted parallel_validation_path", status_smoke["detail"])

    def test_verify_install_accepts_stow_folded_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            fake_bin = self._install_fake_home_links(home)
            folded = home / ".claude/skills/gitsummary"
            folded.unlink()
            folded.mkdir()
            (folded / "SKILL.md").symlink_to(REPO_ROOT / "skills/gitsummary/SKILL.md")

            payload = install_verify.verify_install(
                repo_root=REPO_ROOT,
                home=home,
                path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            )

            self.assertTrue(payload["ok"], payload["failures"])

    def test_verify_install_rejects_stale_session_run_skill_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            fake_bin = self._install_fake_home_links(home)
            stale_skill = home / ".codex/skills/session-run"
            stale_skill.unlink()
            stale_skill.mkdir()
            (stale_skill / "SKILL.md").write_text(
                "\n".join(
                    [
                        "# stale session-run",
                        "",
                        "**Explicit flag**: `<plan-dir>/ --session <ID>` runs exactly",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            payload = install_verify.verify_install(
                repo_root=REPO_ROOT,
                home=home,
                path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            )

            self.assertFalse(payload["ok"])
            self.assertTrue(
                any(
                    failure["name"] == "session-run-content"
                    for failure in payload["failures"]
                )
            )

    def test_status_smoke_marks_its_child_as_an_install_verify_invocation(
        self,
    ) -> None:
        captured: list[dict[str, str]] = []

        def _fake_run(argv, **kwargs):
            captured.append(dict(kwargs["env"]))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="stub")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            with mock.patch.object(install_verify.subprocess, "run", _fake_run):
                install_verify._check_status_smoke(
                    active_asl="/usr/bin/true",
                    home=home,
                    path_env="/usr/bin",
                )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["ASL_INVOCATION_ORIGIN"], "install-verify")
        self.assertEqual(captured[0]["PATH"], "/usr/bin")


class HarnessDocsRegressionTests(unittest.TestCase):
    """Lock harness documentation expectations."""

    def _read(self, relative: str) -> str:
        path = REPO_ROOT / relative
        return path.read_text(encoding="utf-8")

    def test_docs_describe_copilot_no_unsafe_bypass_noninteractive_expectation(
        self,
    ) -> None:
        import re as _re

        docs = self._read("docs/agent-session-loop.md")
        unsafe_copilot_argv = (
            "copilot -C <cwd> --add-dir <plan-dir> --allow-all-tools "
            "--allow-all-paths --no-ask-user --silent -p <prompt>"
        )
        safe_copilot_argv = (
            "copilot -C <cwd> --add-dir <plan-dir> --no-ask-user --silent -p <prompt>"
        )
        exact_expectation = (
            "For Copilot, `--no-unsafe-bypass` does not re-enable "
            "interactive approval prompts; it keeps `--no-ask-user --silent` "
            "and only removes `--allow-all-tools --allow-all-paths`."
        )

        self.assertIn(exact_expectation, docs)
        safety_modes = docs.split("## Safety Modes", 1)[1].split("\n## ", 1)[0]
        text_blocks = _re.findall(r"```text\n(.*?)\n```", safety_modes, _re.DOTALL)
        unsafe_block = next(
            (block for block in text_blocks if unsafe_copilot_argv in block),
            None,
        )
        safe_block = next(
            (block for block in text_blocks if safe_copilot_argv in block),
            None,
        )

        self.assertIsNotNone(unsafe_block)
        self.assertIsNotNone(safe_block)
        self.assertIn(unsafe_copilot_argv, unsafe_block)
        self.assertIn(safe_copilot_argv, safe_block)
        self.assertNotIn("--allow-all-tools", safe_block)
        self.assertNotIn("--allow-all-paths", safe_block)

    def test_docs_describe_optional_parallel_verify_recipes(self) -> None:
        docs = self._read("docs/agent-session-loop.md")

        self.assertIn("`parallel` (optional, defaults to `false`)", docs)
        self.assertIn("a parallel run is byte-identical to the\n  sequential one", docs)
        self.assertIn(
            "non-interference is the contract\n  author's responsibility", docs
        )
        self.assertIn(
            'A `"parallel": true` recipe adds that same additive key to\n'
            "`verification_started`",
            docs,
        )

    def test_docs_expose_only_role_specific_asl_model_flags(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]
        # The guard is about commands that drive turns in more than one role:
        # those must always name the role a model is forwarded to. `asl commit`
        # runs exactly one turn in one role, so a plain `--model` there names
        # nothing ambiguous and is excluded rather than weakening the rule.
        multi_role = "\n".join(
            line for line in commands.splitlines() if not line.startswith("asl commit ")
        )

        self.assertIn("[--harness-model MODEL]", commands)
        self.assertIn("[--review-harness-model MODEL]", commands)
        self.assertNotIn("[--model MODEL]", multi_role)
        self.assertIn(
            "ASL's `--harness-model` is forwarded only to turns using the "
            "implementation",
            docs,
        )
        self.assertIn(
            "`--review-harness-model` is forwarded only to\ngenerated review turns",
            docs,
        )
        self.assertIn("`--effort` remains shared", docs)
        self.assertIn("deliberately has no validator-model flag", docs)
        self.assertIn(
            "--harness-model gpt-5 --review-harness claude "
            "--review-harness-model claude-opus-4 --dry-run",
            docs,
        )


class LiveMailboxDocsRegressionTests(unittest.TestCase):
    """Lock the final mailbox/model-control public contract."""

    def _read(self, relative: str) -> str:
        return (REPO_ROOT / relative).read_text(encoding="utf-8")

    def test_command_synopsis_exposes_final_mailbox_surface(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]

        self.assertIn("[--no-mailbox]", commands)
        for command in (
            "asl inbox guidance",
            "asl inbox reply",
            "asl inbox resolve",
            "asl inbox read",
            "asl ask",
            "asl notify",
            "asl outbox wait",
        ):
            self.assertIn(command, commands)
        self.assertIn("asl control set-model", commands)
        self.assertIn("--clear-harness-model", commands)
        self.assertIn("--clear-effort", commands)

    def test_docs_expose_the_supervisor_guide_and_leaf_help_routing(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        commands = docs.split("## Commands", 1)[1].split("\n## ", 1)[0]
        autopilot = docs.split("## Autopilot Skill", 1)[1].split("\n## ", 1)[0]
        readme = self._read("README.md")
        supervision = readme.split("### Live ASL supervision", 1)[1].split("\n### ", 1)[
            0
        ]

        self.assertIn(
            "asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]",
            commands,
        )
        for marker in (
            "`selected_harness`",
            "`leaf_help`",
            "carry equivalent commands",
            "--supervision-id ... --json --detach",
            "`result_path`",
            "lazy\n`handler_ref` mechanism",
            "`asl outbox supervise --help`",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, autopilot)
        for group in (
            "`bridge`",
            "`workbench`",
            "`control`",
            "`inbox`,\n`outbox`",
            "`assurance`",
        ):
            with self.subTest(group=group):
                self.assertIn(group, autopilot)

        # The workbench parent shows the serve flags, so neither document may
        # apply the leaf-only sentence to it.
        for marker in (
            "a leaf-only group's",
            "`asl workbench` is the exception",
            "bare `asl workbench` form runs `asl workbench serve`",
        ):
            with self.subTest(workbench_marker=marker):
                self.assertIn(marker, autopilot)

        for marker in (
            "asl supervisor-guide [--harness auto|codex|claude|copilot] [--json]",
            "`asl outbox supervise --help`",
            "every supported candidate when detection is absent or ambiguous",
            "leaf-only group's help lists its leaves and none of their flags",
            "`asl workbench` instead carries the serve flags on the parent",
        ):
            with self.subTest(readme_marker=marker):
                self.assertIn(marker, supervision)
        self.assertNotIn(
            "`asl workbench`, `asl control`",
            supervision,
        )

    def test_docs_describe_activation_precedence_waits_and_fallback(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]

        for marker in (
            "`mailbox.jsonl`",
            "`mailbox-lease.json`",
            "fixed-size 512-byte supervisor",
            "fresh for 90 seconds",
            "mode `0600`",
            "existing `mailbox.jsonl` or a fresh supervisor lease",
            "`asl run --no-mailbox` always wins",
            "cannot override the",
            "runner-owned mutation rules",
            "90 seconds by default",
            "polls mailbox\n  metadata every two seconds",
            "default timeout is 300 seconds",
            "polls mailbox metadata every five seconds",
            "`acknowledged: false` plus an `acknowledgement_error`",
            "waits five minutes initially",
            "Lease freshness is checked only at an\nactive deadline",
            "extends the wait by 60 seconds",
            "30-minute ceiling bounds *unsupervised* time, not wall clock",
            "ordinary\nterminal pause",
            "next\nimplementation invocation",
            "never affect generated review turns",
        ):
            self.assertIn(
                marker,
                mailbox_docs,
                msg=f"missing mailbox contract marker: {marker!r}",
            )

    def test_docs_describe_exhausted_mailbox_reply_precedence(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("### Live reply wait and fallback", 1)[1].split(
            "\n### ", 1
        )[0]
        normalized = " ".join(mailbox_docs.split())

        self.assertIn(
            "An already-committed closure takes precedence over mailbox "
            "exhaustion and is consumed before exhaustion can end the wait.",
            normalized,
        )
        self.assertIn(
            "If no closure is available, timeout, a stale lease at an active "
            "deadline that outlasts any post-suspend grace, a full/unavailable "
            "mailbox",
            normalized,
        )
        self.assertIn("such as `mailbox-full`", normalized)
        self.assertIn(
            "an absent, stale, or otherwise non-fresh supervisor lease with "
            "no post-suspend lease grace left to cover it",
            normalized,
        )
        self.assertIn(
            "grants the supervisor a 90-second awake grace to re-prove liveness",
            normalized,
        )
        self.assertIn(
            "a non-fresh lease neither ends the wait nor extends the active "
            "deadline, and the grace never extends the ceiling",
            normalized,
        )
        self.assertIn(
            "a continuously watched wait has no terminal instant and outlives "
            "the ceiling indefinitely",
            normalized,
        )
        self.assertIn(
            "`timeout` only when the unsupervised ceiling was reached",
            normalized,
        )
        self.assertIn(
            "`suspend_lease_grace_seconds`, the awake lease grace that suspend applied",
            normalized,
        )
        self.assertIn("`suspend_skew_seconds`", normalized)
        self.assertIn(
            "`unsupervised_seconds`, how long it ran without a supervisor "
            "lease proving liveness",
            normalized,
        )

    def test_docs_describe_shared_closure_reserve_and_availability(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[1]
        normalized = " ".join(mailbox_docs.split())

        # Pin the documented reserve and minimum-closure sizes to their constants.
        self.assertEqual(512 * 1024, mailbox.MAILBOX_CLOSURE_RESERVE_BYTES)
        self.assertEqual(
            mailbox.MAILBOX_CLOSURE_RESERVE_BYTES,
            mailbox.MAILBOX_REPLY_RESERVE_BYTES,
        )
        self.assertEqual(
            mailbox.MAILBOX_CLOSURE_RESERVE_BYTES,
            mailbox.MAILBOX_QUESTION_RESERVE_BYTES,
        )
        self.assertEqual(176, mailbox.MAILBOX_MIN_REPLY_BYTES)
        self.assertEqual(200, mailbox.MAILBOX_MIN_RESOLUTION_BYTES)
        self.assertEqual(192 * 1024, mailbox.MAILBOX_WAIT_END_RESERVE_BYTES)
        self.assertEqual(96 * 1024, mailbox.MAILBOX_RECEIPT_RESERVE_BYTES)
        self.assertIn(
            "The final 512 KiB below the cap is a lifecycle reserve",
            normalized,
        )
        self.assertIn(
            "closures retain 288 KiB of nested headroom for wait termination "
            "and receipts",
            normalized,
        )
        self.assertIn(
            "ordinary exhaustion cannot strand an active question, its "
            "`wait-end`, or its consumption evidence",
            normalized,
        )
        self.assertIn(
            "`accepts_replies` and `accepts_resolutions` separately report "
            "whether capacity remains for each minimal valid closure",
            normalized,
        )
        self.assertIn(
            "`full` itself keeps its file-faithful physical-cap meaning",
            normalized,
        )

        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        status_normalized = " ".join(status_docs.split())
        self.assertIn(
            "Beside `full` it reports `accepts_questions`, `accepts_replies`, "
            "and `accepts_resolutions`",
            status_normalized,
        )
        self.assertIn(
            "the latter two independently report capacity for their "
            "differently sized minimal valid closures",
            status_normalized,
        )
        self.assertIn(
            "all three fields and the resolution/notice totals are `null` "
            "alongside `full` rather than defaulting to writable or empty",
            status_normalized,
        )
        self.assertIn(
            "`replies and resolutions still accepted`, `only replies still "
            "accepted`, or `reply and resolution capacity exhausted`",
            status_normalized,
        )
        self.assertIn(
            "never overclaims that a closure can land",
            status_normalized,
        )

    def test_docs_describe_bounded_question_body(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[1]
        normalized = " ".join(mailbox_docs.split())

        # Pin the documented character bound to the constant it describes.
        self.assertEqual(16000, runner.MAILBOX_QUESTION_TEXT_MAX_CHARS)
        self.assertIn(
            "The question body a waiting runner publishes is itself bounded: "
            "at most the first 16,000 characters of the last-message artifact",
            normalized,
        )
        self.assertIn(
            "truncated deterministically and given a footer naming the "
            "on-disk artifact path that holds the full text",
            normalized,
        )

    def test_docs_describe_resolution_marker_choices_and_notice_replay(
        self,
    ) -> None:
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        for marker in (
            "`AgentSessionLoopMailboxQuestion` Agent Mux handoff",
            "`<run-id>:mailbox-question:<question-sequence>` source event ID",
            "Closed/history-only records never emit the marker",
            "`AgentSessionLoopMailboxResolution` with `run_id`, `state_path`, "
            "`question_id`",
            "`<run-id>:mailbox-resolution:<resolution-sequence>` source event ID",
            "The marker contains no question, reply, resolution-note, or notice body",
            "offers three explicit outcomes: submit a factual answer with "
            "`asl inbox reply`",
            "`continue_command`, and `pause_command`",
            "notices from the exact live run are emitted as bounded escaped "
            "`NOTICE` progress and replayed at least once on attach within "
            "each supervisor process",
            "final structured outbox declares `include: all` and preserves "
            "complete separate `questions` and `notices` arrays",
        ):
            self.assertIn(marker, normalized)

        self.assertIn(
            "A question is closed logically by its first accepted reply or resolution",
            normalized,
        )
        self.assertIn(
            "ASL does not rotate, compact, or physically delete mailbox records",
            normalized,
        )

    def test_docs_and_skill_describe_lease_heartbeat_vs_takeover(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        skill = self._read("skills/asl/SKILL.md")
        normalized_docs = " ".join(docs.split())
        normalized_skill = " ".join(skill.split())

        self.assertNotIn("establish or renew a lease", normalized_docs)
        for marker in (
            "decide between a heartbeat and a takeover",
            "re-emits the same `supervisor_id` and question projection "
            "with only `updated_at` refreshed",
            "takes over with a new random supervisor identity and no projection",
        ):
            self.assertIn(
                marker,
                normalized_docs,
                msg=f"docs omit lease heartbeat marker: {marker!r}",
            )
        for marker in (
            "One-shot supervisor commands do not displace a live helper.",
            "heartbeat it — the helper's supervisor identity and "
            "projection survive with only the lease timestamp refreshed",
            "Only a missing, stale, or unprojected lease is taken over "
            "with a fresh supervisor identity",
        ):
            self.assertIn(
                marker,
                normalized_skill,
                msg=f"skill omits lease heartbeat marker: {marker!r}",
            )

    def test_asl_skill_ends_wait_loop_on_terminal_supervision_record(
        self,
    ) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "mint a **launch identity** — one fresh UUID",
            "--supervision-id <launch-identity>",
            "two helper lifetimes can overlap and a mailbox sequence captured "
            "at launch cannot tell a still-alive older helper's later exit "
            "from this one's",
            "Pass the same `--supervision-id` on every call",
            "keys the persisted cursor, so the command is reissued verbatim "
            "and no `--since` is ever threaded by hand",
            "The cursor starts at 0, so questions that were already open "
            "before the launch stay visible",
            "Exactly one of four tokens is printed, always with exit code 0",
            "`IDLE` means nothing actionable happened: print nothing and "
            "reissue the identical command",
            "`ACTION questions=<n>` carries `<n>` question records that no "
            "closure has answered yet",
            "`DONE outcome=<outcome> seq=<n>` carries this launch's own "
            "terminal record: stop the loop and do not reissue. Read that "
            "`outcome` before trusting stdout",
            "Every outcome except `error` means the helper wrote and flushed "
            "its complete `--json` result before appending the record",
            "`outcome: error` means the helper died abnormally",
            "its stdout may be missing, truncated mid-write, or complete but unproven",
            "Never require parseable JSON there: take the termination detail "
            "from the record's `text`, recover run state with "
            "`asl status <plan-dir> --json`",
            "Another helper's terminal record — an earlier invocation, or one "
            "whose lifetime overlapped this launch — never ends this loop",
            "It is absorbed internally: the cursor advances past it and the "
            "wait continues on the remaining budget",
            "So is a batch whose questions have all already closed. Neither "
            "ever reaches stdout",
            "Absence of `DONE` is **not** proof that the helper is alive",
            "Keep both fallbacks — the bounded stdout tail below, and the "
            "loop itself as the backstop",
            "when no terminal record can ever arrive (`--no-mailbox`, "
            "SIGKILL, machine crash)",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits supervision-end marker: {marker!r}",
            )

    def test_asl_skill_launches_the_helper_detached(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "--supervision-id <launch-identity> --json --detach",
            "`--detach` returns immediately.",
            "It prints one JSON launch handshake — `ok`, `supervision_id`, "
            "`pid`, `plan_dir`, `result_path`, `stderr_path` — and its exit "
            "code reports the **launch**, never the run",
            "The run's own verdict is only ever in the `result.json` at `result_path`",
            "reparented to init, so it outlives the tool call, the agent "
            "turn, and the harness session",
            "Never append `&`. Never rely on the harness backgrounding the "
            "command. Never invent a redirect target or a `mktemp` directory",
            "A foreground launch cannot work in this harness: a run lasts "
            "hours and a tool call is capped at 600 seconds",
            "a helper parented by a harness shell dies whenever that shell "
            "is reaped, at turn end, at session end, or at a harness restart",
            "**The first `asl outbox supervise` call is the proof of launch.**",
            "If it answers `LOST reason=helper-never-started`, the launch "
            "failed: report that to the user and do not re-enter the loop",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits detached-launch marker: {marker!r}",
            )
        self.assertNotIn(
            "--supervision-id <launch-identity> --json\n```",
            skill,
            msg="asl skill still prescribes a foreground helper launch",
        )

    def test_asl_skill_documents_the_lost_token(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "Exactly one of four tokens is printed, always with exit code 0",
            "`LOST reason=<reason>` means this launch's supervising helper "
            "is gone without a terminal record. Stop the loop, run `asl "
            "status <plan-dir> --json`, and surface to the user what died "
            "and whether the run itself is still alive. **Never "
            "auto-relaunch**",
            "`helper-exited-without-record` means the helper recorded a "
            "process identity and that process is now gone",
            "`helper-never-started` is reported only when no run is holding "
            "the plan at all, so nothing is running and the launch itself "
            "failed",
            "It waits out a 120-second grace window first, so a slow start "
            "is never called dead",
            "`LOST` never preempts `DONE` or `ACTION`",
            "A run started as a plain `asl run` with no helper writes no "
            "process identity and stays `IDLE`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits LOST marker: {marker!r}",
            )
        for token in (
            mailbox_cli.SUPERVISE_TOKEN_LOST,
            mailbox_cli.SUPERVISE_LOST_HELPER_EXITED,
            mailbox_cli.SUPERVISE_LOST_HELPER_NEVER_STARTED,
        ):
            self.assertIn(
                token,
                normalized,
                msg=f"asl skill omits supervise constant: {token!r}",
            )
        self.assertIn(
            f"{int(mailbox_cli.SUPERVISE_LIVENESS_GRACE_SECONDS)}-second grace",
            normalized,
        )

    def test_asl_skill_requires_evidence_for_every_health_claim(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        self.assertIn(
            "**absence of evidence**, the rule that a missing `DONE` record "
            "is not proof the helper is alive. Its operating rule: any claim "
            'about run health — including answering "is it still working?" '
            "— must cite a fresh `asl outbox supervise` token or a fresh "
            "`asl status <plan-dir> --json`, never the absence of output",
            normalized,
            msg="asl skill omits the health-claim rule under absence of evidence",
        )

    def test_readme_documents_the_lost_supervision_contract(self) -> None:
        readme = self._read("agent_session_loop/README.md")
        normalized = " ".join(readme.split())

        for marker in (
            "`DONE outcome=<outcome> seq=<n>`, or `LOST reason=<reason>` at "
            "exit code 0",
            "the helper records its `(pid, process birth time)` identity "
            "before starting the child",
            "`helper-exited-without-record` reports a helper whose process is gone",
            "`helper-never-started` reports one that never wrote an identity "
            "while no run holds the plan",
            "neither ever preempting `DONE` or `ACTION`",
            "exposed under `--json` as `lost_reason` and `run_evidence`",
            "Supervision is launched detached (`asl-autopilot.py --detach`)",
            "prints one JSON launch handshake carrying `pid`, `result_path`, "
            "and `stderr_path`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl README omits supervision-liveness marker: {marker!r}",
            )

    def test_asl_skill_binds_a_silent_supervision_output_contract(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "#### Output contract (binding)",
            "A healthy run is silent.",
            "you may emit text in exactly three cases: (1) a `QUESTION` record "
            "needs the user's answer — relay it and ask; (2) supervision is "
            "blocked and cannot continue without the user; (3) Step 4's final "
            "report",
            "Everything else is forbidden output: acknowledging the launch, "
            "announcing a poll, restating a command you are about to run or "
            "just ran, summarizing an `IDLE` line, reporting elapsed time, "
            'quoting the payload, describing what the run "seems" to be '
            "doing, or reassuring the user that supervision continues",
            "When `asl outbox supervise` prints `IDLE`, print nothing and "
            "reissue the identical command immediately. Hours of silence is "
            "the correct output",
            "Poll exactly one thing: `asl outbox supervise`. Never also tail "
            "the helper's background output",
            "reading both doubles the noise for no added signal",
            "Notices are informational. They never wake supervision, are "
            "never relayed, and stay readable afterwards with `asl outbox "
            "wait <plan-dir> --include notices --since 0 --timeout 0`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"asl skill omits output-contract marker: {marker!r}",
            )

    def test_docs_describe_terminal_supervision_record(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        readme = self._read("agent_session_loop/README.md")
        normalized_docs = " ".join(docs.split())
        normalized_readme = " ".join(readme.split())

        for marker in (
            "`notice`, `supervision-end`, worker `ack`",
            "A `supervision-end` record is the mailbox's terminal signal",
            "a required `outcome` from the closed enum `complete`, `paused`, "
            "`stopped`, `blocked`, `error`, an optional `run_id` validated "
            "only when present, and an optional `supervision_id`",
            "Every helper launch carries a `supervision_id`",
            "a reader treats only the record carrying its own launch identity "
            "as terminal",
            "`--supervision-id UUID` scopes that scalar to one supervising launch",
            "A record from another launch is still returned and still "
            "advances `next_since`",
            "Only the Autopilot helper writes the record, at most once per "
            "helper process",
            "On every outcome except `error`, the record is appended only "
            "after the complete `--json` result has been written and flushed",
            "`error` is the abnormal-exit outcome and carries no stdout guarantee",
            "the result may be absent, truncated, or complete but unproven",
            "must therefore decide from the record alone — the termination "
            "detail is in `text` — and recover run state from "
            "`asl status <plan-dir> --json` rather than parsing helper stdout",
            "`error` never comes from an ordinary terminal reason, so the "
            "outcome alone separates the two cases",
            "The runner deliberately does not emit one",
            "appends at the full file-cap tier alongside `receipt`",
            "`--include all` wakes on questions, notices, or the terminal "
            "`supervision-end` record",
            "returns separate `questions`, `notices`, and `supervision_ends` "
            "arrays plus the `supervision_ended` scalar",
            "A wake that carries only `supervision-end` records emits no "
            "Agent Mux handoff and writes no lease question projection",
        ):
            self.assertIn(
                marker,
                normalized_docs,
                msg=f"docs omit supervision-end marker: {marker!r}",
            )

        for marker in (
            "the Autopilot helper appends one terminal `supervision-end` message",
            "Every outcome except `error` is appended after the helper "
            "flushed its complete result",
            "`error` marks an abnormal exit whose stdout may be missing, "
            "truncated, or unproven and must be decided from the record alone",
            "`asl outbox wait --include all` wakes on it and reports it "
            "through `supervision_ends` and `supervision_ended`",
            "The runner emits no such record",
            "every launch carries a `supervision_id`",
            "reports `supervision_ended` only for that launch's own record "
            "while another launch's record still advances the cursor",
        ):
            self.assertIn(
                marker,
                normalized_readme,
                msg=f"README omits supervision-end marker: {marker!r}",
            )

    def test_docs_separate_helper_termination_from_run_termination(
        self,
    ) -> None:
        docs = self._read("docs/agent-session-loop.md")
        readme = self._read("agent_session_loop/README.md")
        skill = self._read("skills/asl/SKILL.md")
        normalized_docs = " ".join(docs.split())
        normalized_readme = " ".join(readme.split())
        normalized_skill = " ".join(skill.split())

        self.assertNotIn(
            "has exited, so no further worker questions or notices are coming",
            normalized_docs,
        )
        for marker in (
            "the mailbox's terminal signal for one supervision lifetime",
            "It ends the helper's lifetime and does not prove the supervised "
            "run is quiescent",
            "`worker_hung` deliberately stops the helper without killing or "
            "waiting on that child, which stays alive holding the plan lock "
            "and can publish questions and notices after the terminal record",
            "asks `asl status <plan-dir> --json`; the record answers only "
            "whether this supervision is over",
        ):
            self.assertIn(
                marker,
                normalized_docs,
                msg=f"docs omit supervision-lifetime marker: {marker!r}",
            )

        self.assertIn(
            "That record ends the helper's supervision lifetime and does not "
            "prove the supervised run is quiescent: a `worker_hung` stop "
            "leaves the `asl run` child alive holding the plan lock, so later "
            "worker questions and notices can still land after the terminal "
            "record",
            normalized_readme,
        )

        for marker in (
            "The record ends this supervision, not necessarily the run. It "
            "proves the helper exited, not that the `asl run` child is "
            "quiescent",
            "a `worker_hung` stop leaves that child alive holding the plan lock",
            "Check `asl status <plan-dir> --json` before treating the plan as idle",
            "Its terminal `supervision-end` record therefore marks the end of "
            "supervision only: that live child can still publish mailbox "
            "questions and notices after the record lands",
        ):
            self.assertIn(
                marker,
                normalized_skill,
                msg=f"asl skill omits supervision-lifetime marker: {marker!r}",
            )

    def test_docs_describe_guidance_rendering_bounds(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        normalized = " ".join(mailbox_docs.split())

        self.assertIn(
            f"newest {runner.MAX_RENDERED_GUIDANCE} pending guidance records",
            normalized,
        )
        self.assertIn(
            f"{runner.MAX_RENDERED_GUIDANCE_BYTES:,}-byte UTF-8 rendering budget",
            normalized,
        )
        self.assertIn(
            "records remain pending and unacknowledged",
            normalized,
        )
        self.assertIn(
            "repeats the shell-safe `asl inbox read` command",
            normalized,
        )
        self.assertIn(
            "identifies each omitted record by physical sequence and message ID",
            normalized,
        )
        self.assertIn(
            "cannot fit within the complete subsection on its own is omitted first",
            normalized,
        )
        self.assertIn(
            "evicts the oldest remaining record until the subsection fits",
            normalized,
        )
        self.assertIn(
            "labels the delimited block as intentionally empty",
            normalized,
        )
        self.assertIn(
            "text form of `asl inbox read` applies the same "
            "newest-20-record and 65,536-byte complete-section limits",
            normalized,
        )
        self.assertIn(
            "acknowledgement covers that full snapshot through the highest "
            "pending sequence",
            normalized,
        )
        self.assertIn(
            "The `--json` payload is not rendering-truncated",
            normalized,
        )

    def test_docs_separate_appending_guidance_from_delivering_it(self) -> None:
        """The prose has to carry the distinction the ledger enforces.

        Every operator surface used to say guidance was "sent" and left the
        reader to assume it arrived. What the docs owe a reader now is the
        per-harness truth: which route confirms a delivery, and what an
        unconfirmed one is called instead.
        """
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        normalized = " ".join(mailbox_docs.split())

        for marker in (
            "Appending guidance and delivering it are different events",
            "unsupported-active-steer",
            "delivered-by-read",
            "Guidance delivered live",
            "Guidance queued but not delivered",
            "enforcement remains the job of guards, not guidance",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"docs omit guidance-delivery marker: {marker!r}",
            )

    def test_operator_surfaces_never_call_an_append_a_delivery(self) -> None:
        """ "Guidance sent" is the exact claim the ledger cannot support."""
        for path in (
            "README.md",
            "agent_session_loop/README.md",
            "docs/agent-session-loop.md",
            "skills/asl/SKILL.md",
        ):
            normalized = " ".join(self._read(path).split())
            self.assertNotIn(
                "Guidance sent",
                normalized,
                msg=f"{path} still reports an append as a delivery",
            )

    def test_asl_skill_steer_route_waits_for_a_delivery_verdict(self) -> None:
        normalized = " ".join(self._read("skills/asl/SKILL.md").split())

        self.assertIn(
            "asl inbox guidance PLAN_DIR --text GUIDANCE_TEXT --wait-delivered --json",
            normalized,
        )
        for marker in (
            "Always pass `--wait-delivered`",
            "Guidance delivered live to <harness>",
            "Guidance queued but not delivered (<reason>)",
            "Delivery is never inferred from a successful write to a pipe",
            "never cancels or restarts a turn to deliver guidance",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits guidance-delivery marker: {marker!r}",
            )

    def test_docs_describe_mailbox_and_reply_text_framing(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        normalized = " ".join(mailbox_docs.split())

        self.assertIn(
            "In text mode, `asl inbox read`, `asl ask`, and `asl outbox wait`",
            normalized,
        )
        self.assertIn("`BEGIN ASL MAILBOX MESSAGE JSONL`", normalized)
        self.assertIn(
            "Every reply continuation source—mailbox, `--reply`, or "
            "`--reply-stdin`—uses the same safe prompt representation",
            normalized,
        )
        self.assertIn("`BEGIN ASL OPERATOR REPLY JSONL`", normalized)
        self.assertIn("worker consumes only its `text` value", normalized)

    def test_docs_lock_wait_timeout_and_bare_label_resolution(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        status_docs = docs.split("`status` reads durable plan files", 1)[1].split(
            "\n\n`tail` prints", 1
        )[0]

        self.assertIn(
            "`--wait-timeout` accepts 0 through 1800 seconds and requires `--wait`.",
            mailbox_docs,
        )
        self.assertIn(
            "Supplying `--wait-timeout` alone fails with `reason: invalid-timeout`.",
            mailbox_docs,
        )
        self.assertIn(
            "Bare-label resolution is available to `status`, `asl control`,",
            status_docs,
        )
        for command in (
            "`status`",
            "`asl control`",
            "`asl inbox guidance`",
            "`asl inbox reply`",
            "`asl inbox read`",
            "`asl ask`",
            "`asl notify`",
            "`asl outbox wait`",
        ):
            self.assertIn(command, status_docs)
        self.assertRegex(
            status_docs,
            r"other plan-taking\s+commands remain path-only",
        )
        self.assertNotIn(
            "Resolution is limited to `status` and `asl control`",
            status_docs,
        )

    def test_docs_enumerate_mailbox_command_error_reasons(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("### Mailbox commands", 1)[1].split("\n### ", 1)[0]
        mailbox_cli_source = self._read("agent_session_loop/mailbox_cli.py")

        def resolve_reason(node: ast.expr) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                value = getattr(mailbox_cli, node.id, None)
                return value if isinstance(value, str) else None
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "mailbox"
            ):
                value = getattr(mailbox, node.attr, None)
                return value if isinstance(value, str) else None
            return None

        cli_reasons = {
            reason
            for call in ast.walk(ast.parse(mailbox_cli_source))
            if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "reason"
            if (reason := resolve_reason(keyword.value)) is not None
        }
        operation_reasons = {
            value
            for name, value in vars(mailbox).items()
            if name.startswith("ERROR_") and isinstance(value, str)
        }
        expected_reasons = (
            cli_reasons
            | operation_reasons
            | {
                "ambiguous-plan",
                "invalid-target",
                "malformed-plan",
                "os-error",
                "question-already-answered",
                "question-not-found",
                "state-error",
            }
        )

        # Keep the most important repaired distinctions visible in this
        # regression even though their production constants are also derived.
        self.assertIn("missing-text", expected_reasons)
        self.assertIn("invalid-text-source", expected_reasons)
        self.assertIn("stdin-read-error", expected_reasons)
        for reason in sorted(expected_reasons):
            self.assertIn(
                f"`{reason}`",
                mailbox_docs,
                msg=f"mailbox command reason {reason!r} is undocumented",
            )
        self.assertRegex(
            mailbox_docs,
            r"`missing-text` when a required message body is\s+absent or blank",
        )
        self.assertRegex(
            mailbox_docs,
            r"`invalid-text-source` when both `--text` and\s+`--text-stdin` are "
            r"supplied",
        )

    def test_docs_enumerate_control_command_error_reasons(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        control_docs = docs.split("`asl control` is the programmatic equivalent", 1)[
            1
        ].split("\n`status` reads durable plan files", 1)[0]
        control_cli_source = self._read("agent_session_loop/control_cli.py")

        def resolve_reason(node: ast.expr) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "control_queue"
            ):
                value = getattr(control_queue, node.attr, None)
                return value if isinstance(value, str) else None
            return None

        expected_reasons = {
            reason
            for call in ast.walk(ast.parse(control_cli_source))
            if isinstance(call, ast.Call)
            for keyword in call.keywords
            if keyword.arg == "reason"
            if (reason := resolve_reason(keyword.value)) is not None
        }

        self.assertIn(
            control_queue.REJECT_MISSING_MODEL_CONTROL_OPERATION,
            expected_reasons,
        )
        for reason in sorted(expected_reasons):
            self.assertIn(
                f"`{reason}`",
                control_docs,
                msg=f"control command reason {reason!r} is undocumented",
            )

    def _control_docs(self) -> str:
        docs = self._read("docs/agent-session-loop.md")
        return docs.split("`asl control` is the programmatic equivalent", 1)[1].split(
            "\n`status` reads durable plan files", 1
        )[0]

    def test_docs_pin_control_abort_and_receipt_constants(self) -> None:
        normalized = " ".join(self._control_docs().split())

        # Pin the documented windows to the constants that produce them.
        self.assertEqual(120, control_queue.DEFAULT_ABORT_GRACE_SECONDS)
        self.assertEqual(1, control_queue.MIN_ABORT_GRACE_SECONDS)
        self.assertEqual(86_400, control_queue.MAX_ABORT_GRACE_SECONDS)
        self.assertEqual(0.5, control_cli.CONTROL_WAIT_POLL_INTERVAL_SECONDS)
        self.assertEqual(60.0, control_cli.DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS)
        self.assertEqual(1800.0, control_cli.MAX_CONTROL_WAIT_TIMEOUT_SECONDS)
        self.assertEqual(5.0, runner.LIVE_CONTROL_POLL_INTERVAL_SECONDS)

        self.assertIn(
            "end the in-flight turn after a bounded grace window (default "
            "`120` seconds)",
            normalized,
        )
        self.assertIn(
            "A grace outside 1–86400 seconds fails with `reason: invalid-grace`",
            normalized,
        )
        self.assertIn(
            "the handler tails `events.jsonl` every `0.5` seconds",
            normalized,
        )
        self.assertIn(
            "`--timeout` requires `--wait` and must be between 0 and 1800 seconds",
            normalized,
        )
        self.assertIn("The default wait is `60` seconds", normalized)
        self.assertIn(
            "instead of waiting out its `5.0`-second control poll",
            normalized,
        )

    def test_docs_describe_the_control_wakeup_and_its_host_limit(self) -> None:
        normalized = " ".join(self._control_docs().split())

        self.assertIn(
            "The wakeup is a single byte written to the FIFO recorded as "
            "`wakeup_endpoint` in the plan's `lock.json`",
            normalized,
        )
        # The runner opens the read end before publishing the lock that names
        # it, which is what makes an authenticated owner unnecessary.
        self.assertIn(
            "opens it for reading **before** it publishes the `lock.json` "
            "that names it",
            normalized,
        )
        for reason in sorted(
            value
            for name, value in vars(control_signal).items()
            if name.startswith("REASON_") and isinstance(value, str)
        ):
            self.assertIn(
                f"`{reason}`",
                normalized,
                msg=f"wakeup reason {reason!r} is undocumented",
            )
        self.assertIn(
            "never a delivery guarantee: a command whose wakeup did not land "
            "is still applied at the next poll",
            normalized,
        )
        self.assertIn("the wakeup is **same-host only**", normalized)
        self.assertIn(
            "a runner whose lock records another hostname reports "
            "`remote-host` and picks the command up on its ordinary poll "
            "cadence instead",
            normalized,
        )

    def test_docs_enumerate_run_control_actions(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        state_files = docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        event_reference_docs = state_files.split("`run_control` events", 1)[1].split(
            "`tail` renders", 1
        )[0]

        for action in events.RUN_CONTROL_ACTIONS:
            self.assertIn(
                f"`{action}`",
                event_reference_docs,
                msg=f"events.jsonl reference omits run-control action {action!r}",
            )

        # `control-command-observed` is a receipt, not a state transition, so it
        # is the one action that never reaches `control.last_action`.
        last_action_values = tuple(
            action
            for action in events.RUN_CONTROL_ACTIONS
            if action != events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
        )
        self.assertLessEqual(
            set(runner.RUN_CONTROL_EVENT_ACTIONS.values()),
            set(last_action_values),
        )
        self.assertNotIn(
            events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
            set(runner.RUN_CONTROL_EVENT_ACTIONS.values()),
        )
        last_action_docs = state_files.split("`last_action` is one of", 1)[1]
        for action in last_action_values:
            self.assertIn(
                f"`{action}`",
                last_action_docs,
                msg=f"state.json `last_action` omits run-control action {action!r}",
            )
        normalized_state_files = " ".join(state_files.split())
        self.assertIn(
            "`control-command-observed` is a receipt for a drained command "
            "rather than a state transition, so it is recorded in "
            "`events.jsonl` and never becomes `last_action`",
            normalized_state_files,
        )

    def test_asl_skill_registers_abort_beside_stop_after_current(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        self.assertIn(
            "asl control stop-after-current <plan-dir> [--state-dir DIR] "
            "[--wait] [--timeout SECONDS] --json",
            normalized,
        )
        self.assertIn(
            "asl control abort <plan-dir> [--grace SECONDS] [--reason TEXT] "
            "[--state-dir DIR] [--wait] [--timeout SECONDS] --json",
            normalized,
        )
        for marker in (
            "Guidance is advice, never enforcement.",
            "does **not** stop the session",
            "Only `asl control` stops a run",
            "`abort` is the enforcing mechanism when the session running "
            "*right now* must not finish",
            "Never report a run as stopped because guidance asking it to stop "
            "was sent.",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits abort-enforcement marker: {marker!r}",
            )

    def test_asl_skill_requires_wait_and_reports_its_timeout(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        for marker in (
            "Use `--wait` whenever the stop actually matters.",
            "it exits 0 whether or not any runner ever saw it, which is "
            "exactly how a stop can look sent and still be ignored",
            "A `--wait` that times out is a **failure to report, never an "
            "assumed success**",
            "do not claim the run stopped",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits control-wait marker: {marker!r}",
            )
        for reason in ("no-runner", "receipt-timeout", "abort-timeout"):
            self.assertIn(
                f"`{reason}` —",
                normalized,
                msg=f"skill omits wait failure reason {reason!r}",
            )

    def test_asl_skill_reports_the_abort_early_stop(self) -> None:
        skill = self._read("skills/asl/SKILL.md")
        normalized = " ".join(skill.split())

        self.assertNotIn(
            "honored an out-of-band `asl control stop-after-current` request",
            normalized,
        )
        for marker in (
            "`stop_kind` names which request produced that stop.",
            "`abort` means `asl control abort` ended the turn that was in flight",
            "reverted to `pending` with a Handoff note saying it holds no "
            "completion, so it is *not* counted in `completed`",
            "It is still a clean early stop: same `ok: true`, still never a "
            "pause or a failure, still never auto-resumed.",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"skill omits abort early-stop marker: {marker!r}",
            )

    def test_docs_and_skill_pin_every_abort_outcome(self) -> None:
        # The runner leaves a turn that ends inside the grace window to the
        # ordinary pause/completion path, so only the killed outcome stops the
        # run; docs and skill have to say all three or a supervisor will report
        # a stop that never happened.
        control_docs = " ".join(self._control_docs().split())
        skill = " ".join(self._read("skills/asl/SKILL.md").split())

        for marker in (
            "**Killed at the deadline.** The turn was still running when the "
            "window expired",
            "`abort-turn-honored` is recorded, and the run exits with "
            "`reason: stopped`",
            "**Self-paused inside the window.**",
            "the run exits `paused` with the worker's pause reason and "
            "`ok: false`, never `reason: stopped`",
            "**Completed inside the window.**",
            "the completion stands, and the run continues to the next session",
            "An abort observed between turns has nothing to kill and degrades "
            "to stop-after-current instead.",
            "Only `abort-turn-honored` means the run is stopping",
        ):
            self.assertIn(
                marker,
                control_docs,
                msg=f"control docs omit abort-outcome marker: {marker!r}",
            )

        for marker in (
            "**An abort has three outcomes and only one of them stops the "
            "run**, so read the run's own terminal state before reporting any "
            "of them",
            "**The turn was killed**",
            "the run exits `reason: stopped` with `stop_kind: abort`",
            "**The worker wound down and paused itself**",
            "the run comes back `paused` with the worker's pause reason and "
            "`ok: false`, *not* `reason: stopped`",
            "**The worker completed its session**",
            "the session stays `complete` and **the run continues to the next "
            "session**",
            "never report the run as stopped on the strength of the abort alone",
            "Only `abort-turn-honored` proves the run is stopping",
            "An abort the worker satisfied inside its grace window never "
            "reaches this verdict",
        ):
            self.assertIn(
                marker,
                skill,
                msg=f"skill omits abort-outcome marker: {marker!r}",
            )

    def test_docs_describe_the_autopilot_stop_kind(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        autopilot_docs = docs.split("## Autopilot Skill", 1)[1].split("\n## ", 1)[0]
        normalized = " ".join(autopilot_docs.split())

        for marker in (
            "`stop_kind` names which request it was: `stop-after-current` for "
            "the `p` key or `asl control stop-after-current`, honored at the "
            "session boundary",
            "`abort` for `asl control abort`, which ended the turn in flight "
            "and returned its session to `pending`, so that session is *not* "
            "in `completed`",
        ):
            self.assertIn(
                marker,
                normalized,
                msg=f"autopilot docs omit stop_kind marker: {marker!r}",
            )

    def test_docs_describe_the_workbench_abort_endpoint(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        normalized = " ".join(docs.split())

        self.assertIn(
            "`POST /api/control/abort` — enqueue a `request-abort-turn` command.",
            normalized,
        )
        self.assertIn(
            "so an out-of-range window returns `invalid-grace` and an empty "
            "reason returns `invalid-reason`",
            normalized,
        )
        self.assertIn(
            "it has no receipt equivalent of `asl control abort --wait`: the "
            "HTTP surface confirms the append, never the runner's observation",
            normalized,
        )

    def test_docs_enumerate_waiting_for_reply_statuses(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        pause_docs = docs.split("## Pause Semantics", 1)[1].split(
            "\n## Autopilot Skill", 1
        )[0]

        self.assertIn("`waiting_for_reply_status`", status_docs)
        for waiting_status in state.WAITING_FOR_REPLY_STATUSES:
            self.assertIn(
                f"`{waiting_status}`",
                status_docs,
                msg=(
                    f"waiting_for_reply_status value {waiting_status!r} is undocumented"
                ),
            )
        self.assertIn(f"`{qna.EXPIRED_WAIT_PAUSE_REASON}`", pause_docs)
        self.assertRegex(
            pause_docs,
            r"`wait-expired` pause reason without rewriting\s+`state\.json`",
        )
        self.assertRegex(
            pause_docs,
            r"This recovery reason is\s+never written into `state\.json`",
        )

    def test_docs_describe_the_rolling_unsupervised_ceiling(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        state_files = docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        pause_docs = docs.split("## Pause Semantics", 1)[1].split(
            "\n## Autopilot Skill", 1
        )[0]

        for section_name, section, markers in (
            (
                "state files",
                state_files,
                (
                    "`unsupervised_deadline_at` with its "
                    "`unsupervised_ceiling_seconds` span",
                    "`unsupervised_deadline_at` is the ceiling as of the last "
                    "anchor the runner persisted",
                    "a reader rolls it forward from `last_lease_activity_at` "
                    "plus the ceiling span rather than reading it as a "
                    "terminal instant",
                    "Waits persisted before the ceiling began rolling carry a "
                    "static `hard_deadline` instead of those two keys, and "
                    "every reader still renders that key for them",
                ),
            ),
            (
                "wait validity",
                status_docs,
                (
                    "the classification consults lease freshness rather than "
                    "a bare timestamp",
                    "the projected ceiling is the later of "
                    "`unsupervised_deadline_at` and `last_lease_activity_at` "
                    "plus `unsupervised_ceiling_seconds`",
                    "a wait whose supervisor keeps renewing stays `live` past "
                    "the instant persisted at its start",
                    "A record written before the ceiling began rolling "
                    "carries only `hard_deadline` and is classified from that "
                    "timestamp exactly as it was before",
                ),
            ),
            (
                "pause semantics",
                pause_docs,
                (
                    "classify it as `expired` even before its unsupervised ceiling",
                    "That lock check is what makes an abandoned wait "
                    "recoverable promptly",
                    "the ceiling itself rolls forward with lease activity, so "
                    "a record left behind by a supervised wait can carry a "
                    "projected ceiling well in the future",
                    "orphaned nonterminal wait whose unsupervised ceiling passed",
                ),
            ),
        ):
            normalized = " ".join(section.split())
            for marker in markers:
                self.assertIn(
                    marker,
                    normalized,
                    msg=(
                        f"{section_name} docs omit rolling-ceiling marker: {marker!r}"
                    ),
                )

    def test_docs_enumerate_mailbox_lease_reasons(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        status_docs = docs.split("## JSON Output", 1)[1].split(
            "\n## Pause Semantics", 1
        )[0]
        expected_reasons = {
            value
            for name, value in vars(mailbox).items()
            if name.startswith("LEASE_") and isinstance(value, str)
        }

        for key in (
            "lease_reason",
            "lease_error",
            "lease_present",
            "lease_fresh",
        ):
            self.assertIn(
                f"`{key}`",
                status_docs,
                msg=f"status mailbox projection key {key!r} is undocumented",
            )
        for reason in sorted(expected_reasons):
            self.assertIn(
                f"`{reason}`",
                status_docs,
                msg=f"mailbox lease reason {reason!r} is undocumented",
            )

    def test_docs_lock_net_surface_privacy_and_exclusions(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        mailbox_docs = docs.split("## Live Mailbox and Future-Turn Model Control", 1)[
            1
        ].split("\n## ", 1)[0]
        surface_docs = mailbox_docs.split(
            "### Surface and observability accounting", 1
        )[1].split("\n\nWorkbench", 1)[0]
        event_reference_docs = docs.split("`events.jsonl` records", 1)[1].split(
            "Verification heartbeats", 1
        )[0]

        for marker in (
            "three mailbox state files",
            "eight mailbox commands",
            "one\nmodel-control command",
            "one `mailbox` event name",
            "adds no runner or\nsupervisor threads",
            "no terminal statuses",
            "`guidance-observed` carries",
            "`notice-published` carries",
            "`reply_provided` carries",
            "do not increment pause metrics",
            "no mailbox-authoring, mailbox-reply,\nmailbox-resolution, "
            "mailbox-notice, or live model-control endpoint",
        ):
            self.assertIn(
                marker,
                mailbox_docs,
                msg=f"missing surface/privacy marker: {marker!r}",
            )
        for section_name, section in (
            ("surface accounting", surface_docs),
            ("events.jsonl reference", event_reference_docs),
        ):
            for action in events.MAILBOX_ACTIONS:
                self.assertIn(
                    f"`{action}`",
                    section,
                    msg=f"{section_name} omits mailbox action {action!r}",
                )
        normalized_surface_docs = " ".join(surface_docs.split())
        self.assertIn(
            "`wait-cleared` carries the wait identity and deadlines, "
            "the exit `reason`, `pending_outbox`, the measured count "
            "of questions open for that run when a final mailbox snapshot "
            "is available, and the `lease_supervisor_id`, `lease_updated_at`, "
            "and `lease_reason` evidence for the last lease a non-closure exit "
            "observed, plus `suspend_skew_seconds` and "
            "`suspend_lease_grace_seconds` when the wait detected a system "
            "suspend",
            normalized_surface_docs,
        )
        live_wait_docs = mailbox_docs.split("### Live reply wait and fallback", 1)[
            1
        ].split("\n### ", 1)[0]
        for reason in runner.MAILBOX_REPLY_WAIT_PREPARATION_REASONS:
            self.assertIn(
                f"`{reason}`",
                live_wait_docs,
                msg=f"live-wait docs omit preparation reason {reason!r}",
            )
        for stale_draft in (
            "set-max-sessions",
            "--mailbox-interactive",
            "append-only keepalive",
            "transient real pause",
            "model changes affect the active turn",
        ):
            self.assertNotIn(stale_draft, docs)

    def test_readme_and_autopilot_skill_match_live_contract(self) -> None:
        readme = self._read("README.md")
        skill = self._read("skills/asl/SKILL.md")

        for marker in (
            "### Live ASL supervision",
            "`asl run --no-mailbox`",
            "The eight mailbox commands",
            "Lease freshness is checked only at an active\ndeadline",
            "30-minute ceiling bounds unsupervised time rather\nthan wall clock",
            "`asl control set-model",
            "no runner/supervisor thread",
            "no Workbench",
        ):
            self.assertIn(marker, readme)
        for marker in (
            "`--no-mailbox` is accepted",
            "polls questions and notices every five seconds",
            "renews the lease every 30 seconds",
            "`QUESTION id=... preview=...`",
            "asl inbox reply <plan-dir> --to <question-id>",
            "resumes the same session",
            "Lease freshness is checked only at an active deadline",
            "awake grace to re-prove liveness before a stale lease ends the",
            "30-minute ceiling bounds unsupervised time,\nnot wall clock",
            "already-committed reply or resolution takes\nprecedence over "
            "mailbox exhaustion",
            "affect only the next implementation invocation",
        ):
            self.assertIn(marker, skill)


class WorkbenchDocsRegressionTests(unittest.TestCase):
    """Lock the documentation against regressing Workbench review findings.

    Early reviews called out four concrete gaps the docs/README must reflect:

    1. The Workbench CLI surface (`asl workbench [serve]`, `pin`, `unpin`,
       `registry --json`) — including the bare-`workbench` default to
       `serve`.
    2. Registry `plan_roots` participate in plan discovery via the queue.
    3. Launch profile nullable booleans are tri-state; blank means "leave
       the runner default alone" (no `--no-unsafe-bypass` argv).
    4. `/api/state` surfaces dotted-session `sessions`/`counts`/
       `next_pending_id`, and the frontend renders dotted IDs verbatim.
    """

    def _read(self, relative: str) -> str:
        path = REPO_ROOT / relative
        return path.read_text(encoding="utf-8")

    def test_readme_documents_workbench_cli_surface(self) -> None:
        readme = self._read("README.md")
        self.assertIn("asl workbench [serve]", readme)
        self.assertIn(
            "asl workbench pin PLAN_DIR [--cwd ROOT] [--label LABEL]",
            readme,
        )
        self.assertIn("asl workbench unpin PLAN_DIR", readme)
        self.assertIn("asl workbench registry [--json]", readme)
        # Reflects the registry plan_roots discovery wiring.
        self.assertIn("registry `plan_roots`", readme)
        # Reflects the tri-state nullable default behavior.
        self.assertIn("tri-state nullable launch profiles", readme)
        # Reflects the dotted session API/frontend rendering.
        self.assertIn("dotted session IDs", readme)
        # Reflects the Recovery Center docs and read-only API.
        self.assertIn("Recovery Center", readme)
        self.assertIn("`/api/recovery` diagnostics", readme)
        self.assertIn("copy-only repair commands", readme)
        # Reflects Workbench cleanup/archive/retention surface.
        self.assertIn("cleanup/archive/retention", readme)

    def test_docs_describe_workbench_cli_pin_unpin_registry(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("asl workbench [serve]", docs)
        self.assertIn("bare `asl workbench` invocation defaults to `serve`", docs)
        self.assertIn("asl workbench pin /path/to/plan-dir", docs)
        self.assertIn("asl workbench unpin /path/to/plan-dir", docs)
        self.assertIn("asl workbench registry --json", docs)

    def test_workspace_compatibility_claim_matches_payload_key_sets(self) -> None:
        workspace = {
            "workspace_root": "/workspace",
            "registry_digest": "digest",
            "repositories": [{"alias": "API", "path": "api"}],
            "repository_count": 1,
        }
        recovery_payload = workbench.WorkbenchRecovery(
            plan_dir=Path("/plan"),
            kind="none",
            title="No recovery needed",
            summary="Ready",
            severity="none",
            recoverable=False,
            workspace=workspace,
        ).to_payload()
        final_review_workspace_payload = workbench._workspace_payload(workspace)
        expected_keys = {
            "cwd",
            "workspace_root",
            "registry_digest",
            "repositories",
            "repository_count",
        }

        self.assertLessEqual(expected_keys, recovery_payload.keys())
        self.assertLessEqual(expected_keys, final_review_workspace_payload.keys())
        for relative in (
            "docs/agent-session-loop.md",
            "agent_session_loop/README.md",
        ):
            text = self._read(relative)
            normalized = " ".join(text.split())
            self.assertIn("final-review responses", normalized)
            self.assertTrue(
                "retain `cwd`" in normalized or "retaining `cwd`" in normalized
            )
            self.assertIn("`formatRepositoryAliases`", text)

    def test_docs_describe_workbench_cleanup_archive_workflow(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        expected_markers = (
            "Cleanup mode",
            "`sort=oldest`",
            "`archive_blocker`",
            "`POST /api/plan/archive`",
            "`POST /api/plans/archive`",
            "`POST /api/plan/delete`",
            "backward-compatible archive alias",
            '"confirm_plan_dir"',
            "typed\nfull-path confirmation",
            'reason: "active-lock"',
            "`GET /api/archive`",
            "`POST /api/archive/restore`",
            "`POST /api/archive/delete`",
            "`POST /api/archive/settings`",
            "restore-conflict",
            "`confirm_count`",
            "confirmation-mismatch",
            "`archive_retention_days`, default 30 days",
            "auto-empties entries",
            # Session 4: completed-plan cleanup workflow.
            "`completed_at`",
            "ages complete plans off their",
            "`select candidates`",
            "`select visible`",
            'review finished work in place without flipping "show',
            # Session 4: archive bulk select-all and restore-selected workflow.
            "`#wb-archive-bulk`",
            "`select all`",
            "`restore selected`",
            "`clear selection`",
            "best-effort bulk\nrestore",
            "`N of M entries selected`",
            "`restored X` or `restored X, Y failed`",
        )
        for marker in expected_markers:
            self.assertIn(marker, docs, msg=f"missing docs marker: {marker!r}")
        self.assertIn(
            "It no longer permanently deletes a live\n  plan",
            docs,
        )

    def test_docs_describe_registry_plan_roots_discovery(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        # Discovery section must say registry plan_roots feed the queue.
        self.assertIn(
            "Registry roots are scanned alongside\nCLI-supplied roots",
            docs,
        )
        self.assertIn("canonical `plan-root` discovery source label", docs)

    def test_docs_describe_tri_state_nullable_launch_profile(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn(
            "nullable booleans (`unsafe_bypass`, `allow_not_loop_ready`)",
            docs,
        )
        self.assertIn("tri-state `<select>` controls", docs)
        # Blank/default must mean "leave the runner default alone".
        self.assertIn(
            "blank/default state emits **no** `--unsafe-bypass` and **no**\n"
            "  `--no-unsafe-bypass` argv",
            docs,
        )

    def test_docs_describe_v2_role_specific_launch_profiles(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        profile_docs = docs.split("### Registry, pins, launch profiles", 1)[1].split(
            "\n### ", 1
        )[0]

        self.assertIn("Schema version `2`", profile_docs)
        self.assertIn("`harness_model`", profile_docs)
        self.assertIn("`review_harness_model`", profile_docs)
        self.assertNotIn("`harness`, `model`,", profile_docs)
        self.assertIn("legacy `profile.model` is ignored", profile_docs)
        self.assertIn("never forwarded to `asl run`", profile_docs)
        self.assertIn("next pin/profile save writes schema v2", profile_docs)

    def test_docs_workbench_smoke_checks_model_profile_migration(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        smoke = docs.split("Workbench browser workflow:", 1)[1]

        self.assertIn("implementation harness/model, review", smoke)
        self.assertIn("argv uses `--harness-model`", smoke)
        self.assertIn("schema-v1 registry pin containing `profile.model`", smoke)
        self.assertIn("global\n  warnings area", smoke)
        self.assertIn("`review_harness_model` model-routing fields", smoke)

    def test_docs_describe_state_endpoint_session_payload(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`sessions` (ordered list with", docs)
        self.assertIn("`counts` (`{pending, complete, empty,\n  unknown}`)", docs)
        self.assertIn("`next_pending_id`", docs)
        self.assertIn("schema\n  matches `asl status --json`", docs)
        self.assertIn('`reason: "malformed-plan"`', docs)

    def test_docs_describe_dotted_session_frontend_rendering(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("session table iterates `payload.sessions`", docs)
        self.assertIn("renders the dotted `id` verbatim (e.g., `2.1`, `2.2`)", docs)
        self.assertIn(
            "no integer-loop or\n  `parseInt(next_pending_session_id)` inference",
            docs,
        )

    def test_docs_reply_payload_uses_reply_text(self) -> None:
        # The server requires `reply_text` (workbench_server._handle_reply),
        # so the docs example must match. The stale `{"reply": "..."}`
        # example must not appear in the /api/reply bullet.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn('`{"plan_dir": "...", "reply_text": "..."}`', docs)
        self.assertNotIn('`{"plan_dir": "...", "reply": "..."}`', docs)

    def test_docs_describe_triage_queue_payload_fields(self) -> None:
        # Session 1 added additive triage fields to /api/queue and
        # /api/plan. The docs must enumerate every field plus the
        # urgency rank constants so external consumers know what to
        # branch on.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("### Triage queue payload", docs)
        for field in (
            "`urgency_rank`",
            "`urgency_group`",
            "`activity_label`",
            "`concise_reason`",
            "`last_activity_at`",
            "`last_activity_source`",
            # Session 4: additive completion timestamp.
            "`completed_at`",
            "`searchable_text`",
        ):
            self.assertIn(field, docs, msg=f"missing triage field: {field}")
        for field in (
            "`recovery`",
            "`primary_action`",
            "`artifact_count`",
            "`command_count`",
            "`warning_count`",
        ):
            self.assertIn(field, docs, msg=f"missing compact recovery field: {field}")
        # Urgency groups vocabulary (mirrors workbench.URGENCY_GROUPS).
        for group in (
            "`needs-action`",
            "`in-progress`",
            "`ready`",
            "`complete`",
        ):
            self.assertIn(group, docs, msg=f"missing urgency group: {group}")
        # Last-activity source vocabulary (mirrors
        # workbench.LAST_ACTIVITY_SOURCES).
        for source in (
            "`active-verification`",
            "`active-turn`",
            "`run-finished`",
            "`run-started`",
            "`lock-created`",
            "`progress-journal`",
        ):
            self.assertIn(source, docs, msg=f"missing activity source: {source}")
        # Urgency rank constants (paused-for-reply 10 .. complete 90).
        # These are documented in a table so the literal numerals appear
        # in the same lines as the primary-state names.
        for primary_state, rank in (
            ("paused-for-reply", "10"),
            ("failed", "20"),
            ("stale", "30"),
            ("invalid", "40"),
            ("verifying", "50"),
            ("running", "60"),
            ("recent", "70"),
            ("ready", "80"),
            ("complete", "90"),
        ):
            self.assertIn(
                primary_state, docs, msg=f"missing primary state: {primary_state}"
            )
            self.assertIn(rank, docs, msg=f"missing urgency rank: {rank}")
        # Exported Python constants are the source of truth — the docs
        # must reference them so downstream callers can grep their way
        # back to workbench.py.
        for constant in (
            "`workbench.URGENCY_GROUPS`",
            "`workbench.LAST_ACTIVITY_SOURCES`",
            "`workbench.URGENCY_RANK_BY_STATE`",
            "`workbench.URGENCY_GROUP_BY_STATE`",
            "`workbench.ACTIVITY_LABEL_BY_STATE`",
        ):
            self.assertIn(constant, docs, msg=f"missing exported constant: {constant}")

    def test_docs_describe_recovery_center_api_and_payload(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("### Recovery Center payload", docs)
        self.assertIn("### Recovery payload examples", docs)
        self.assertIn("`GET /api/recovery?plan_dir=<path>`", docs)
        self.assertIn("`WorkbenchRecovery.compact_payload()`", docs)
        self.assertIn("`workbench.RECOVERY_KINDS`", docs)
        example_start = docs.index("### Recovery payload examples")
        example_end = docs.index("Representative mappings:", example_start)
        examples = docs[example_start:example_end]
        for kind in workbench.RECOVERY_KINDS:
            self.assertIn(
                f"`{kind}`",
                docs,
                msg=f"missing recovery kind: {kind}",
            )
            self.assertIn(
                f'"kind": "{kind}"',
                examples,
                msg=f"missing recovery example for kind: {kind}",
            )
        for field in (
            "`plan_dir`",
            "`kind`",
            "`title`",
            "`summary`",
            "`severity`",
            "`recoverable`",
            "`run_id`",
            "`session_id`",
            "`turn_index`",
            "`commit_index`",
            "`evidence`",
            "`actions`",
            "`commands`",
            "`artifacts`",
            "`warnings`",
            "`warning_details`",
            "`compact`",
        ):
            self.assertIn(field, docs, msg=f"missing recovery payload field: {field}")
        for warning_field in ("`code`", "`message`", "`severity`"):
            self.assertIn(
                warning_field,
                docs,
                msg=f"missing warning_details field: {warning_field}",
            )
        for warning_code in (
            "`state-missing-race`",
            "`state-unreadable`",
            "`state-malformed-json`",
            "`state-invalid-json-shape`",
        ):
            self.assertIn(
                warning_code,
                docs,
                msg=f"missing warning_details code: {warning_code}",
            )
        self.assertIn("`RECOVERY_ACTION_TARGETS`", docs)
        self.assertIn("`RECOVERY_ACTION_TARGETS_BY_ID`", docs)
        for target in workbench.RECOVERY_ACTION_TARGETS:
            self.assertIn(
                f"`{target}`",
                docs,
                msg=f"missing recovery action target: {target}",
            )
            self.assertIn(
                f'"target": "{target}"',
                examples,
                msg=f"missing recovery example target: {target}",
            )
        self.assertIn("explicit but unsupported target", docs)
        self.assertIn(
            "`evidence`, `commands`, and `artifacts` are uncapped",
            docs,
        )
        self.assertIn("Artifact paths are best-effort pointers", docs)
        self.assertIn(
            "Compact payloads intentionally\n  expose only `warning_count`",
            docs,
        )

    def test_docs_describe_recovery_center_security_boundaries(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("The Recovery Center is a read-only diagnosis layer", docs)
        self.assertIn("copyable repair commands", docs)
        self.assertIn("Workbench never executes those\n  commands", docs)
        for endpoint in (
            "`/api/reply`",
            "`/api/launch`",
            "`/api/unlock`",
        ):
            self.assertIn(endpoint, docs, msg=f"missing existing endpoint: {endpoint}")
        for command in (
            "`asl preflight <plan-dir> --mark-loop-ready`",
            "`$plan-preflight-iterate <plan-dir>/`",
            "`asl verify <plan-dir>\n  --session <id> --json`",
            "`asl preflight <plan-dir> --json`",
        ):
            self.assertIn(command, docs, msg=f"missing copy-only command: {command}")

    def test_docs_describe_recovery_center_frontend_behavior(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("The Recovery Center appears near the top", docs)
        self.assertIn("renders compact queue/detail `plan.recovery`", docs)
        self.assertIn("full `/api/recovery` evidence", docs)
        self.assertIn("Recovery unavailable", docs)
        self.assertIn("Reply-needed recoveries", docs)
        self.assertIn("`/api/questions`", docs)
        self.assertIn("`/api/reply`", docs)
        self.assertIn("`/api/launch`", docs)
        self.assertIn("`/api/unlock`", docs)
        self.assertIn("inspect and navigate actions scroll/focus", docs)

    def test_docs_troubleshooting_covers_recovery_center_examples(self) -> None:
        docs = self._read("docs/agent-session-loop.md")
        for text in (
            "Recovery Center reports `not-loop-ready`",
            "Recovery Center reports `verify-failed`, `no-contract`, or\n  "
            "`malformed-contract`",
            "Recovery Center reports `commit-failed` or `commit-preflight`",
            "Recovery Center reports `stale-lock` or `stale-state`",
        ):
            self.assertIn(text, docs)

    def test_docs_describe_triage_inbox_default_behavior(self) -> None:
        # Default triage view hides completed plans and sorts by
        # urgency_rank. The docs must say so explicitly.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("triage inbox", docs)
        self.assertIn("sort ascending by\n  `urgency_rank`", docs)
        self.assertIn('"show completed" toggle', docs)
        # Search/sort/completed control bar with the five supported
        # sort modes (session 4 added `oldest`).
        self.assertIn("control bar", docs)
        self.assertIn("matches against\n  `searchable_text`", docs)
        for sort_mode in (
            "`urgency`",
            "`recent`",
            "`label`",
            "`path`",
            "`oldest`",
        ):
            self.assertIn(sort_mode, docs, msg=f"missing sort mode: {sort_mode}")
        self.assertIn("debounces at 150 ms", docs)
        # Empty-state kinds (data-state values).
        for kind in (
            "`loading`",
            "`no-plans`",
            "`no-search-results`",
            "`no-filter-matches`",
            "`completed-hidden`",
            "`no-actionable`",
        ):
            self.assertIn(kind, docs, msg=f"missing empty-state kind: {kind}")

    def test_docs_describe_two_pane_shell_and_preview(self) -> None:
        # Sessions 3 + 4 introduced the two-pane shell at the 960 px
        # breakpoint plus the plan preview panel. Document both surfaces.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("960 px", docs)
        self.assertIn("two-pane shell", docs)
        self.assertIn("preview panel", docs)
        self.assertIn("(no extra fetch)", docs)
        # Shell data attributes the CSS branches on.
        for attribute in (
            '`data-route="queue|detail"`',
            '`data-viewport="mobile|desktop"`',
            '`data-preview="empty|active"`',
        ):
            self.assertIn(attribute, docs, msg=f"missing shell attribute: {attribute}")

    def test_docs_describe_row_action_focus_contract(self) -> None:
        # Session 6 introduced #plan?dir=…&focus=<key>; document the
        # valid keys, the data-action-kind attribute, and the one-shot
        # focus semantics.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`#plan?dir=<encoded>[&focus=<key>]`", docs)
        self.assertIn("`DETAIL_FOCUS_TARGETS`", docs)
        for key in ("`reply`", "`events`", "`active`", "`launch`", "`unlock`"):
            self.assertIn(key, docs, msg=f"missing focus key: {key}")
        self.assertIn('`data-action-kind="<RecommendedAction>"`', docs)
        for action in (
            "`watch`",
            "`reply`",
            "`inspect-failure`",
            "`unlock`",
            "`view`",
            "`run`",
            "`repair-plan`",
        ):
            self.assertIn(action, docs, msg=f"missing recommended action: {action}")
        # Row buttons must remain navigation-only — no inline mutations.
        self.assertIn("navigation-only", docs)
        # Focus is one-shot so polling does not re-snap the viewport.
        self.assertIn("one-shot", docs)

    def test_docs_describe_session_storage_preferences(self) -> None:
        # Sessions 2 + 6 persist preferences under wb.* keys and
        # explicitly exclude the reply textarea. Document both the
        # persisted keys and the privacy invariant.
        docs = self._read("docs/agent-session-loop.md")
        for key in (
            "`wb.token`",
            "`wb.filter`",
            "`wb.search`",
            "`wb.sort`",
            "`wb.completed`",
            "`wb.selected`",
        ):
            self.assertIn(key, docs, msg=f"missing sessionStorage key: {key}")
        # Reply textarea privacy regression.
        self.assertIn("reply textarea is intentionally **not** persisted", docs)
        self.assertIn("`wb.reply`", docs)
        # Session 4: cleanup mode reveals completed plans without writing
        # the persisted show-completed preference.
        self.assertIn(
            "Cleanup mode reveals\n  completed plans on its own gate and "
            "never writes `wb.completed`",
            docs,
        )

    def test_docs_smoke_checklist_covers_triage_inbox(self) -> None:
        # The Workbench browser smoke checklist must include operator-
        # verifiable cases for the new triage behavior: default sort +
        # completed hidden, search/sort/completed persistence, two-pane
        # layout, focus-hint navigation, and the reply draft survival
        # invariant.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("`urgency_rank`", docs)
        self.assertIn('"show completed" toggle', docs)
        self.assertIn('`data-state="no-search-results"`', docs)
        self.assertIn("150 ms search debounce", docs)
        self.assertIn("survive via sessionStorage", docs)
        self.assertIn("two-pane shell", docs)
        self.assertIn("`#plan?dir=…&focus=reply`", docs)
        self.assertIn("reply draft", docs)
        # Session 4: completed timestamp visibility and filtered bulk
        # archive selection must be operator-verifiable.
        self.assertIn("completion time", docs)
        self.assertIn('"not recorded"', docs)
        self.assertIn("`select visible`", docs)
        self.assertIn("`select candidates`", docs)
        for recovery_case in (
            "ready (`kind: none`)",
            "running (`kind: none` with active lock)",
            "reply-needed",
            "not-loop-ready",
            "verify-failed",
            "stale-lock or\n  stale-state",
            "commit-failed",
            "complete (`kind: none`)",
        ):
            self.assertIn(
                recovery_case,
                docs,
                msg=f"missing Recovery Center smoke case: {recovery_case}",
            )
        self.assertIn("without executing arbitrary repair commands", docs)

    def test_docs_smoke_checklist_covers_archive_bulk_selection(self) -> None:
        # Session 4: the Workbench browser smoke checklist must include an
        # operator-verifiable Archive view case covering select-all, bulk
        # restore, typed-count purge, clear-selection, and partial-success
        # feedback wording.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("Open the Archive view with several archived plans", docs)
        self.assertIn("`select all`", docs)
        self.assertIn("`N of M entries\n  selected`", docs)
        self.assertIn("`restore selected`", docs)
        self.assertIn("restore best-effort through `POST /api/archive/restore`", docs)
        self.assertIn("`restored X, Y failed` on partial failure", docs)
        self.assertIn("`purge selected`", docs)
        self.assertIn("type the exact count to confirm the typed-count purge", docs)
        self.assertIn("`clear\n  selection`", docs)

    def test_docs_urgency_table_lists_activity_labels(self) -> None:
        # The per-state activity_label column and its zero-pending review
        # continuation override are both documented so external readers can
        # predict the exact UI badge text without consulting workbench.py.
        docs = self._read("docs/agent-session-loop.md")
        self.assertIn("activity_label", docs)
        for primary_state, label in workbench.ACTIVITY_LABEL_BY_STATE.items():
            self.assertIn(
                label,
                docs,
                msg=(
                    f"docs urgency table missing activity_label "
                    f"{label!r} for state {primary_state!r}"
                ),
            )
        self.assertIn(workbench.REVIEW_CONTINUATION_ACTIVITY_LABEL, docs)


class AuthoritativeDocsRegressionTests(unittest.TestCase):
    """Regression-test contract, review, state-schema, and CLI examples in the
    long-form docs against the authoritative package definitions."""

    docs: str
    readme: str
    justfile: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        cls.readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
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
        for text in (self.readme, agent_readme, self.docs):
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
        self.assertEqual(1, prepare.LEGACY_PREPARE_VALIDATION_VERSION)
        self.assertEqual(2, prepare.PREPARE_VALIDATION_VERSION)
        for text in (self.readme, self.docs):
            self.assertIn("version 1", text)
            self.assertIn("version 2", text)
            self.assertIn("workspace_root", text)
            self.assertIn("--rebind-workspace", text)
        self.assertNotIn(
            "non-dry-run `asl run` refreshes/backfills the association",
            self.readme,
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

    def test_recoverable_pause_docs_match_autopilot(self) -> None:
        module_path = REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
        spec = importlib.util.spec_from_file_location(
            "asl_autopilot_docs_regression", module_path
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        autopilot = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(autopilot)

        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        surfaces = (self.docs, skill)
        recoverable_copy = "The helper auto-retries recoverable same-session pauses"
        for surface in surfaces:
            section = surface.split(recoverable_copy, 1)[1].split("\n\n", 1)[0]
            normalized_section = " ".join(section.split())
            pause_list = re.search(r"\(([^)]*)\)", section)
            self.assertIsNotNone(pause_list)
            assert pause_list is not None
            self.assertEqual(
                autopilot.RECOVERABLE_PAUSE_REASONS,
                tuple(re.findall(r"`([^`]+)`", pause_list.group(1))),
            )
            self.assertNotIn("`nonzero-exit`", pause_list.group(1))
            self.assertIn(
                "An unclassified `nonzero-exit` pause is never retried.",
                normalized_section,
            )
            self.assertIn(
                "Only `transport-failed` backs off",
                normalized_section,
            )

    def test_inspect_pause_docs_enumerate_the_policy_set(self) -> None:
        """Every `inspect` reason is named in prose, and nothing else is.

        `user_action` is what a supervising agent branches on, so a reason that
        moves between `reply` and `inspect` without the docs moving with it
        leaves an operator reading instructions the code no longer follows.
        """
        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        marker = "Two kinds of pause are `inspect`"
        for name, surface in (
            ("docs/agent-session-loop.md", self.docs),
            ("skills/asl/SKILL.md", skill),
        ):
            with self.subTest(surface=name):
                self.assertIn(marker, surface, f"{name} never enumerates the set")
                section = surface.split(marker, 1)[1].split("\n\n", 1)[0]
                named = re.findall(r"`([^`]+)`", section)
                for reason in pause_policy.INSPECT_PAUSE_REASONS:
                    self.assertIn(
                        reason,
                        named,
                        f"{name} never names inspect reason {reason!r}",
                    )
                # A reason listed here but classified `reply` would be worse
                # than an omission: it reads as authoritative and is wrong.
                for reason in named:
                    if reason not in events.PAUSE_REASONS:
                        continue
                    self.assertIn(
                        reason,
                        pause_policy.INSPECT_PAUSE_REASONS,
                        f"{name} lists {reason!r} as inspect, but it is not",
                    )

    def test_pause_policy_version_matches_the_documented_revision(self) -> None:
        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        expected = f"`policy_version: {pause_policy.POLICY_VERSION}`"
        for name, surface in (
            ("docs/agent-session-loop.md", self.docs),
            ("skills/asl/SKILL.md", skill),
        ):
            with self.subTest(surface=name):
                self.assertIn(expected, surface)

    def test_worker_hung_docs_match_runner_and_autopilot(self) -> None:
        module_path = REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
        spec = importlib.util.spec_from_file_location(
            "asl_autopilot_hang_docs_regression", module_path
        )
        self.assertIsNotNone(spec)
        assert spec is not None
        autopilot = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(autopilot)

        # The helper's escalation classes are the runner's persisted hang
        # reasons plus the helper-only idle-ceiling class.
        self.assertEqual(
            runner.HANG_REASON_RESULT_RECEIVED,
            autopilot.HANG_CLASS_RESULT_RECEIVED,
        )
        self.assertEqual(runner.HANG_REASON_ZERO_CPU, autopilot.HANG_CLASS_ZERO_CPU)

        skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(encoding="utf-8")
        surfaces = {
            "skills/asl/SKILL.md": skill,
            "skills/asl/scripts/asl_autopilot.py": module_path.read_text(
                encoding="utf-8"
            ),
        }
        for name, text in surfaces.items():
            self.assertIn("worker_hung", text, f"{name} never mentions worker_hung")
            for token in (
                autopilot.HANG_CLASS_RESULT_RECEIVED,
                autopilot.HANG_CLASS_ZERO_CPU,
                autopilot.HANG_CLASS_IDLE_CEILING,
            ):
                self.assertIn(token, text, f"{name} never mentions {token!r}")
        # The pause subsection pins the non-destructive escalation contract.
        normalized_skill = " ".join(skill.split())
        self.assertIn("reason: worker_hung", skill)
        self.assertIn(
            "without killing or waiting on the `asl run` child", normalized_skill
        )
        self.assertIn("one automatic recovery attempt", normalized_skill)
        self.assertIn("never escalates to SIGKILL", normalized_skill)
        self.assertIn("additive `recovery` report", normalized_skill)
        self.assertIn("never the runner or the `asl run` process", skill)
        self.assertIn("re-attach", skill)

    def test_review_continuation_surfaces_share_documented_contract(self) -> None:
        self.assertIn("absolute cross-run cap", self.docs)
        self.assertIn(events.REVIEW_REPAIR_REOPENED_EVENT, self.docs)
        self.assertIn("`review_resume` projection", self.docs)
        self.assertIn("`review_reopen` record", self.docs)
        for action in (
            "initial-review",
            "materialize-repairs",
            "run-repairs",
            "rereview",
            "terminal",
        ):
            self.assertIn(f"`{action}`", self.docs)
        emitted_blockers = (
            "review-harness-required",
            "higher-cap-required",
            "artifact-invalid",
            *sorted(review_continuation.TERMINAL_REVIEW_DISPOSITIONS),
            # A standing environment-blocked pause becomes the blocker verbatim,
            # so every member of that policy set is part of the public blocker
            # vocabulary and has to be named here (see ASI-6).
            *pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS,
            "repair-sessions-missing",
            "repair-sessions-not-runnable",
            "unknown-disposition",
        )
        for blocker in emitted_blockers:
            self.assertIn(f"`{blocker}`", self.docs)
        self.assertIn(
            "An unrecognized recorded disposition is retained in\n"
            "`source_disposition` for diagnosis and maps to the stable\n"
            "`unknown-disposition` blocker.",
            self.docs,
        )
        for recovery_kind in workbench.RECOVERY_KINDS:
            self.assertIn(f"`{recovery_kind}`", self.docs)

    def test_review_metrics_additive_fields_are_documented(self) -> None:
        report = metrics.collect_metrics(
            state_dir=REPO_ROOT / ".missing-metrics-state",
            since_days=metrics.DEFAULT_SINCE_DAYS,
        )
        for field in (
            "repairs_materialized",
            "unique_materializations",
            "reopen_attempts",
            "successful_reopen_materializations",
            "reopen_failures",
            "reopen_attempts_by_source_disposition",
            "reopen_failures_by_result",
        ):
            self.assertIn(field, report.review_cycles)
            self.assertIn(field, self.docs)
        self.assertIn("Review materializations\nare timestamp-windowed too", self.docs)
        self.assertIn(
            "counted only when the event that carries one falls inside the\nwindow",
            self.docs,
        )
        self.assertIn("recovered reopen\nbackfills", self.docs)
        self.assertIn("not fresh attempts", self.docs)

    def test_operator_surfaces_do_not_restore_manual_review_plan_guidance(
        self,
    ) -> None:
        surfaces = {
            "README.md": self.readme,
            "docs/agent-session-loop.md": self.docs,
            "skills/asl/SKILL.md": (
                REPO_ROOT / "skills" / "asl" / "SKILL.md"
            ).read_text(encoding="utf-8"),
            "skills/asl/scripts/asl_autopilot.py": (
                REPO_ROOT / "skills" / "asl" / "scripts" / "asl_autopilot.py"
            ).read_text(encoding="utf-8"),
            "agent_session_loop/workbench.py": (
                REPO_ROOT / "agent_session_loop" / "workbench.py"
            ).read_text(encoding="utf-8"),
        }
        unsupported_guidance = (
            "draft a fix plan",
            "rerun the helper against the generated plan directory",
            "capped at 2 generated follow-up plans",
            "opens bounded review-fix follow-up cycles",
        )
        for name, text in surfaces.items():
            for phrase in unsupported_guidance:
                self.assertNotIn(phrase, text, f"{name} restores {phrase!r}")
        self.assertIn(
            "Do **not** draft, split, or expand a\nfollow-up fix plan",
            surfaces["skills/asl/SKILL.md"],
        )

    def test_state_schema_docs_match_authoritative_versions(self) -> None:
        self.assertEqual(3, runner.STATE_SCHEMA_VERSION)
        self.assertEqual(2, run_persistence.COMPACT_STATE_SCHEMA_VERSION)
        self.assertEqual(2, runner.RESULT_SCHEMA_VERSION)
        self.assertEqual(2, events.EVENT_SCHEMA_VERSION)
        state_files = self.docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"schema version {runner.STATE_SCHEMA_VERSION}", state_files)
        self.assertIn(f"`version: {runner.STATE_SCHEMA_VERSION}`", state_files)

    def test_json_output_pins_windowed_metrics_contract(self) -> None:
        section = self.docs.split("## JSON Output", 1)[1].split("\n## ", 1)[0]
        self.assertIn("`metrics --json`", section)
        self.assertIn("narrowing `--since-days` never\nraises a count", section)
        self.assertIn("skipped before its event log is read", section)
        for field in (
            "state_dirs_scanned",
            "state_dirs_active",
            "review_cycles.unique_materializations",
            "review_cycles.repairs_materialized",
        ):
            self.assertIn(field, section)

    def test_cli_invocation_metrics_fields_are_documented(self) -> None:
        report = metrics.collect_metrics(
            state_dir=REPO_ROOT / ".missing-metrics-state",
            since_days=metrics.DEFAULT_SINCE_DAYS,
        )
        section = self.docs.split("## JSON Output", 1)[1].split("\n## ", 1)[0]
        for field in report.cli_invocations:
            self.assertIn(f"`{field}`", section, f"`{field}` is undocumented")
        for field in metrics.RECENT_FAILURE_FIELDS:
            self.assertIn(f"`{field}`", self.docs, f"`{field}` is undocumented")
        self.assertIn("`cli_invocations`", self.docs)
        self.assertIn("CLI invocations", self.docs)
        self.assertIn("windowed by its start timestamp", section)
        self.assertIn(
            f"the {metrics.RECENT_FAILURE_LIMIT} most recent failing", self.docs
        )

    def test_invocation_origin_producers_and_precedence_are_documented(self) -> None:
        section = self.docs.split("### CLI invocation trace", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"`{cli_trace.ORIGIN_ENV_VAR}`", section)
        for origin in cli_trace.ORIGIN_NAMES:
            self.assertIn(f"`{origin}`", section)
        self.assertIn("marker is always applied **after**", section)
        self.assertIn(
            "cannot disguise\nautomation as an operator call",
            section,
        )
        for producer in (
            "autopilot helper's central child environment",
            "`workbench.launch_plan` marks",
            "verifier marks its `asl status --json` smoke call",
        ):
            self.assertIn(producer, section)

    def test_launcher_docs_name_their_own_invocation_origin(self) -> None:
        for heading, origin in (
            ("### Safety model", cli_trace.ORIGIN_AUTOPILOT),
            ("### Security model", cli_trace.ORIGIN_WORKBENCH),
        ):
            section = self.docs.split(heading, 1)[1].split("\n## ", 1)[0]
            self.assertIn(
                f"`{cli_trace.ORIGIN_ENV_VAR}={origin}`",
                section,
                f"{heading} never names its origin marker",
            )

    def test_state_file_docs_pin_trace_root_and_retention(self) -> None:
        section = self.docs.split("## State Files", 1)[1].split("\n## ", 1)[0]
        self.assertIn(
            f"retained for {cli_trace.RETENTION_DAYS} days",
            section,
        )
        self.assertIn("`cli_invocations.trace_root`", section)
        self.assertIn("`cli_invocations.retention_days`", section)
        self.assertIn("`asl gc` never touches these files", section)

    def test_metrics_docs_match_default_window(self) -> None:
        self.assertEqual(7, metrics.DEFAULT_SINCE_DAYS)
        self.assertIn(f"last {metrics.DEFAULT_SINCE_DAYS} days", self.docs)

    def test_packet_mode_and_rollback_are_documented(self) -> None:
        self.assertEqual("packet", task_packet.WORKER_MODE_PACKET)
        self.assertIn("task_packet.build_task_packet", self.docs)
        self.assertIn("ASL_WORKER_MODE=skill", self.docs)

    def test_docs_and_readme_never_reference_pytest(self) -> None:
        self.assertNotIn("pytest", self.docs)
        self.assertNotIn("pytest", self.readme)

    def test_test_recipes_exist_and_are_documented(self) -> None:
        self.assertIn("\ntest-fast:", self.justfile)
        self.assertIn("\ntest-portable:", self.justfile)
        self.assertIn("\ntest-all:", self.justfile)
        for text in (self.docs, self.readme):
            self.assertIn("just test-fast", text)
            self.assertIn("just test-portable", text)
            self.assertIn("just test-all", text)
        self.assertIn("tests/run_suite.py", self.docs)

    def test_the_portable_lane_is_a_subset_of_the_fast_lane(self) -> None:
        """A renamed or deleted module cannot survive as a silent skip.

        The portable list is hand-curated -- membership is "spawns no external
        process" -- so nothing recomputes it. Without this, a module dropped
        from ``FAST_MODULES`` would keep the Linux lane green by running a
        stale name that ``unittest`` would refuse to import.
        """
        stray = set(run_suite.PORTABLE_MODULES) - set(run_suite.FAST_MODULES)

        self.assertEqual(stray, set())
        self.assertEqual(
            len(set(run_suite.PORTABLE_MODULES)),
            len(run_suite.PORTABLE_MODULES),
        )

    def sweep_section(self) -> str:
        return self.docs.split("### Process sweep", 1)[1].split("\n## ", 1)[0]

    def test_process_sweep_docs_match_the_authoritative_definitions(self) -> None:
        section = self.sweep_section()
        self.assertIn(f"`{events.PROCESS_SWEEP_EVENT}` event", section)
        # The opt-out is documented value by value; each documented value has
        # to actually disable the sweep.
        self.assertIn(f"`{process_tree.SWEEP_DISABLED_ENV_VAR}=1`", section)
        for value in ("true", "yes", "on"):
            self.assertIn(f"`{value}`", section)
        for value in ("1", "true", "yes", "on"):
            self.assertFalse(
                process_tree.sweep_enabled(
                    {process_tree.SWEEP_DISABLED_ENV_VAR: value}
                ),
                f"docs claim {value!r} disables the sweep",
            )
        self.assertIn("The sweep is **on by default**.", section)
        self.assertTrue(process_tree.sweep_enabled({}))
        self.assertIn("including a typo, leaves it\nenabled", section)
        self.assertTrue(
            process_tree.sweep_enabled(
                {process_tree.SWEEP_DISABLED_ENV_VAR: "disbaled"}
            )
        )

    def test_process_sweep_docs_list_every_report_key(self) -> None:
        section = self.sweep_section()
        # No group to enumerate and nothing snapshotted, so this reads the
        # process table and signals nothing.
        report = process_tree.sweep_survivors(root_pid=os.getpid(), expected_pgid=None)
        self.assertEqual([], report["killed"])
        for key in report:
            self.assertIn(f"`{key}`", section, f"docs never mention report key {key!r}")
        for key in report["enumeration"]:
            self.assertIn(
                f"`{key}`",
                section,
                f"docs never mention enumeration key {key!r}",
            )

    def test_process_sweep_docs_state_the_limit_and_drop_the_stale_claims(
        self,
    ) -> None:
        section = self.sweep_section()
        self.assertIn(
            "is identified by none of the three\nroutes and survives the sweep,"
            " as does one forked after that read.",
            section,
        )
        # Superseded by the sweep: the parallel-verification contract used to
        # present an escaped descendant as something the runner never chases.
        self.assertNotIn("so the runner does not try", self.docs)
        self.assertNotIn("is **not** stopped by either", self.docs)

    def test_reader_join_bounds_match_the_documented_numbers(self) -> None:
        self.assertEqual(5.0, verification._ABANDONED_READER_JOIN_SECONDS)
        self.assertEqual(30.0, verification._EXITED_READER_JOIN_SECONDS)
        self.assertIn(
            "the pipe readers of a killed check are joined under a five-second\n"
            "  limit and those of a check that exited on its own under thirty "
            "seconds",
            self.docs,
        )


class AssuranceDocsRegressionTests(unittest.TestCase):
    """The assurance vocabulary, surface, and storage as documented."""

    docs: str
    readme: str
    package_readme: str
    asl_skill: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        cls.readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        cls.package_readme = (REPO_ROOT / "agent_session_loop" / "README.md").read_text(
            encoding="utf-8"
        )
        cls.asl_skill = (REPO_ROOT / "skills" / "asl" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    def test_every_assurance_state_is_documented_by_name(self) -> None:
        for text in (self.readme, self.docs, self.package_readme):
            for audit_state in assurance_models.AUDIT_STATES:
                self.assertIn(
                    f"`{audit_state}`",
                    text,
                    f"docs omit assurance state {audit_state!r}",
                )

    def test_every_assurance_subcommand_is_documented(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        assurance = subparsers.choices["assurance"]
        nested = next(
            action
            for action in assurance._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name in nested.choices:
            for text in (self.readme, self.docs):
                self.assertIn(
                    f"asl assurance {name}",
                    text,
                    f"docs never mention `asl assurance {name}`",
                )

    def test_every_audit_run_flag_is_documented(self) -> None:
        parser = cli.build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        run_help = subparsers.choices["run"].format_help()
        flags = (
            "--supervisor-audit",
            "--no-supervisor-audit",
            "--audit-harness",
            "--audit-harness-model",
            "--audit-effort",
            "--audit-timeout",
        )
        for flag in flags:
            self.assertIn(flag, run_help, f"`asl run` no longer offers {flag}")
            for text in (self.readme, self.docs):
                self.assertIn(flag, text, f"docs omit run flag {flag}")

    def test_opting_out_is_documented_as_the_only_way_to_run_no_checks(
        self,
    ) -> None:
        for text in (self.readme, self.docs):
            self.assertIn("--no-supervisor-audit", text)
        self.assertIn("skipped` and creates no issue", self.readme)
        self.assertIn("Opting out is the only way to run no checks at all", self.docs)

    def test_issue_references_are_documented_in_the_canonical_form(self) -> None:
        self.assertEqual("ASI-", assurance_models.ISSUE_ID_PREFIX)
        for text in (self.readme, self.docs, self.package_readme):
            self.assertIn("ASI-", text, "docs never show a canonical issue id")

    def test_storage_filenames_match_the_authoritative_constants(self) -> None:
        for name in (
            assurance_ledger.LEDGER_FILE_NAME,
            assurance_ledger.LEDGER_LOCK_FILE_NAME,
            assurance_ledger.LEDGER_INDEX_FILE_NAME,
        ):
            self.assertIn(name, self.docs, f"docs omit ledger file {name}")
        self.assertIn(state.ASSURANCE_PENDING_FILE_NAME, self.docs)
        for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
            self.assertIn(name, self.docs, f"docs omit audit artifact {name}")

    def test_the_mailbox_and_the_ledger_are_documented_as_separate_systems(
        self,
    ) -> None:
        """Conflating them is the misreading the documentation must prevent.

        Both are append-only JSONL logs an operator can be told to read, so the
        docs have to say outright that they are different files with different
        scopes, locks, and lifetimes rather than leaving it to be inferred.
        """
        for text in (self.readme, self.docs, self.package_readme):
            self.assertIn(assurance_ledger.LEDGER_FILE_NAME, text)
            self.assertIn(mailbox.MAILBOX_FILE_NAME, text)
            self.assertIn("separate system", text)

    def test_lifecycle_ordering_puts_the_audit_before_the_terminal_record(
        self,
    ) -> None:
        self.assertIn("run_finished", self.readme)
        self.assertIn(
            "before `run_finished` is appended",
            self.docs,
            "docs no longer state that auditing precedes the terminal record",
        )

    def test_pending_recovery_is_documented_as_operator_driven(self) -> None:
        for text in (self.readme, self.docs):
            self.assertIn(state.ASSURANCE_PENDING_FILE_NAME, text)
            self.assertIn("asl assurance process-pending", text)
        self.assertIn("idempotent", self.readme)

    def test_the_improvement_workflow_is_documented_as_human_gated(self) -> None:
        for text in (self.readme, self.docs):
            self.assertIn("asl assurance plan ISSUE --harness NAME --output PATH", text)
        self.assertIn("no commit, and no repository change", self.readme)

    def test_the_autopilot_skill_reports_assurance_without_driving_it(
        self,
    ) -> None:
        self.assertIn("assurance", self.asl_skill)
        self.assertIn("asl assurance list", self.asl_skill)
        self.assertIn("reports assurance and never drives it", self.asl_skill)
        for flag in ("--supervisor-audit", "--audit-harness", "--audit-timeout"):
            self.assertIn(flag, self.asl_skill)

    def test_assurance_docs_define_the_repaired_evidence_semantics(self) -> None:
        for phrase in (
            "`findings-defer-broad-failures`",
            "`resolved_root`",
            "`source: explicit` or `source: review`",
            "`requested_model`",
            "`requested_model_source`",
            "`model_projection` is authoritative",
        ):
            self.assertIn(phrase, self.docs)
        self.assertIn("containment checks use only its canonical", self.docs)
        self.assertIn("canonical `resolved_root`", self.package_readme)
        self.assertIn(
            "per-role `model_projection` is authoritative", self.package_readme
        )

    def test_the_auditor_is_told_a_null_failure_diagnostic_is_the_boundary(
        self,
    ) -> None:
        # The auditor kept refiling a null `failure_diagnostic` on an authored
        # `ci` gate as a defect. The observation is true and the fix it asked
        # for would reverse the fence that keeps application output out of
        # control-plane text, so the prompt has to explain the fence instead.
        prompt = supervisor_audit.AUDIT_PROMPT_TEMPLATE

        self.assertIn("`failure_diagnostic` of `null`", prompt)
        self.assertIn("check ASL did not generate", prompt)
        self.assertIn("retained stderr artifact", prompt)
        self.assertIn("only if a", prompt)

    def test_docs_state_why_an_authored_gate_reports_no_failure_diagnostic(
        self,
    ) -> None:
        for phrase in (
            "`failure_diagnostic`",
            "`null` for\nevery check ASL did not generate itself",
            "never a generated gate",
            "`stderr_artifact_path` retains the full text",
        ):
            self.assertIn(phrase, self.docs)


if __name__ == "__main__":
    unittest.main()
