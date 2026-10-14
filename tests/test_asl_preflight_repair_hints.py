"""Preflight repair hint resolution and substitution tests."""

from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    preflight,
    preflight_auto_repair,
    preflight_hint_resolution,
    preflight_models,
    preflight_repair_hints,
    preflight_runtime_hints,
)
from tests.support import (
    _FIX_CATEGORIES,
    _write_loop_ready_plan,
)
from tests.support_asl_preflight import _backtick_commands, _extract_backtick_command


class PreflightRepairHintTests(unittest.TestCase):
    def test_every_preflight_category_has_a_default_hint(self) -> None:
        for category in preflight_models.PREFLIGHT_CATEGORIES:
            with self.subTest(category=category):
                hint = preflight_repair_hints.REPAIR_HINTS[category]
                self.assertEqual(category, hint.category)
                self.assertTrue(hint.check_id)
                self.assertTrue(hint.message)
                self.assertTrue(hint.question_template)
                self.assertIn(hint.fix_category, _FIX_CATEGORIES)

    def test_all_repair_hint_check_ids_are_unique(self) -> None:
        check_ids: list[str] = []
        for hint in preflight_repair_hints.REPAIR_HINTS.values():
            check_ids.append(hint.check_id)
        for hint in preflight_hint_resolution._CATEGORY_HINT_OVERRIDES.values():
            check_ids.append(hint.check_id)
        for hint in preflight_runtime_hints.RUNTIME_REPAIR_HINTS.values():
            check_ids.append(hint.check_id)
        self.assertEqual(
            len(check_ids),
            len(set(check_ids)),
            msg=f"duplicate check_ids in repair-hint tables: {check_ids}",
        )

    def test_missing_contract_finding_resolves_to_add_hint(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="red",
            category="missing-contract",
            message="session 1 is missing a ## Completion Contract section",
            session_id="1",
            path="session-1.md",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("missing-contract.add", hint.check_id)
        self.assertEqual("missing-contract", hint.category)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_manifest_readiness_missing_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="red",
            category="manifest-readiness",
            message="manifest is missing readiness metadata",
            path="manifest.md",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.missing", hint.check_id)
        self.assertEqual("manifest-update", hint.fix_category)

    def test_manifest_readiness_sha_mismatch_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="red",
            category="manifest-readiness",
            message="manifest readiness SHA-256 does not match current plan content",
            path="manifest.md",
            detail="recorded 00; expected aa",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.sha-mismatch", hint.check_id)

    def test_manifest_readiness_state_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="yellow",
            category="manifest-readiness",
            message="plan readiness state is 'draft'",
            path="manifest.md",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.state-not-loop-ready", hint.check_id)
        self.assertEqual("yellow", hint.severity_hint)

    def test_env_git_outside_worktree_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="red",
            category="env-git",
            message="cwd is not inside a git repository: /tmp/x",
            path="/tmp/x",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("env-git.outside-worktree", hint.check_id)
        self.assertEqual("invocation-fix", hint.fix_category)

    def test_env_git_dirty_worktree_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="yellow",
            category="env-git",
            message="git worktree is dirty; commit-mode runs require a clean tree",
            path="/tmp/repo",
            detail="commit-mode: per-session",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("env-git.dirty-worktree", hint.check_id)
        self.assertEqual("env-state", hint.fix_category)

    def test_env_lock_stale_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="yellow",
            category="env-lock",
            message="stale lock present at /tmp/lock",
            path="/tmp/lock",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("env-lock.stale", hint.check_id)

    def test_env_lock_active_resolves_to_override(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="red",
            category="env-lock",
            message="active lock held by pid 1234 at /tmp/lock",
            path="/tmp/lock",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("env-lock.active", hint.check_id)

    def test_env_amx_green_finding_resolves_to_optional(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="green",
            category="env-amx",
            message="amx hook indicator not on PATH",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("env-amx.optional", hint.check_id)
        self.assertEqual("informational", hint.fix_category)

    def test_env_turn_budget_yellow_resolves_to_split(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="yellow",
            category="env-turn-budget",
            message="30 pending sessions exceeds yellow threshold 25",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        # The id still says `split` because it feeds an issue fingerprint and
        # renaming it would split the issue's history in the inbox.
        self.assertEqual("env-turn-budget.split", hint.check_id)
        self.assertEqual("env-state", hint.fix_category)

    def test_the_turn_budget_hint_names_no_remedy_that_raises_the_count(self) -> None:
        # A resplit replaces one pending parent with several pending children,
        # so the remedy the hint used to lead with raised the very count the
        # finding is about.
        hint = preflight_hint_resolution._CATEGORY_HINT_OVERRIDES[
            "env-turn-budget.split"
        ]

        for text in (hint.message, hint.question_template):
            self.assertNotIn("resplit", text.lower())
        self.assertIn("--max-sessions", hint.message)
        self.assertIn("--max-pending-sessions", hint.message)

    def test_env_turn_budget_green_resolves_to_healthy(self) -> None:
        finding = preflight_models.PreflightFinding(
            severity="green",
            category="env-turn-budget",
            message="5 session(s) pending; rough turn budget ~15 turns",
        )
        hint = preflight_hint_resolution.repair_hint_for_finding(finding)
        self.assertEqual("env-turn-budget.healthy", hint.check_id)
        self.assertEqual("informational", hint.fix_category)

    def test_baseline_categories_resolve_to_hints(self) -> None:
        expected = {
            "baseline-missing": ("baseline-missing.refresh", "manifest-update"),
            "baseline-stale": ("baseline-stale.refresh", "manifest-update"),
            "baseline-unexpected": (
                "baseline-unexpected.fix",
                "session-recipe-edit",
            ),
            "baseline-timeout": ("baseline-timeout.fix", "session-recipe-edit"),
            "baseline-near-timeout": (
                "baseline-near-timeout.split",
                "session-recipe-edit",
            ),
        }
        for category, (check_id, fix_category) in expected.items():
            with self.subTest(category=category):
                finding = preflight_models.PreflightFinding(
                    severity="red",
                    category=category,
                    message="baseline finding",
                    session_id="1",
                    detail="run `asl baseline`",
                )
                hint = preflight_hint_resolution.repair_hint_for_finding(finding)
                self.assertEqual(check_id, hint.check_id)
                self.assertEqual(category, hint.category)
                self.assertEqual(fix_category, hint.fix_category)

    def test_report_repair_hints_align_with_findings_one_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",), state="draft")

            report = preflight.run_core_preflight(plan_dir)

            self.assertEqual(len(report.findings), len(report.repair_hints))
            for finding, hint in zip(
                report.findings, report.repair_hints, strict=False
            ):
                self.assertEqual(finding.category, hint.category)

    def test_runtime_no_contract_resolves(self) -> None:
        hint = preflight_runtime_hints.runtime_repair_hint("no-contract")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.no-contract", hint.check_id)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_runtime_malformed_contract_resolves(self) -> None:
        hint = preflight_runtime_hints.runtime_repair_hint("malformed-contract")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.malformed-contract", hint.check_id)

    def test_runtime_verify_failed_without_failure_reason_returns_none(
        self,
    ) -> None:
        self.assertIsNone(preflight_runtime_hints.runtime_repair_hint("verify-failed"))

    def test_runtime_verify_failed_command_not_found_resolves(self) -> None:
        hint = preflight_runtime_hints.runtime_repair_hint(
            "verify-failed", failure_reason="command-not-found"
        )
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.verify-failed.command-not-found", hint.check_id)
        self.assertEqual("env-install", hint.fix_category)

    def test_runtime_verify_failed_every_failure_reason_resolves(self) -> None:
        # Every failure_reason emitted by the verification modules must resolve so the
        # guided skill can surface a fix for any per-check failure.
        for failure_reason in (
            "command-failed",
            "command-timeout",
            "command-not-found",
            "command-error",
            "command-overbroad",
            "file-missing",
            "file-error",
            "regex-not-matched",
        ):
            with self.subTest(failure_reason=failure_reason):
                hint = preflight_runtime_hints.runtime_repair_hint(
                    "verify-failed", failure_reason=failure_reason
                )
                self.assertIsNotNone(hint)
                assert hint is not None
                self.assertEqual(
                    f"runtime.verify-failed.{failure_reason}", hint.check_id
                )
                self.assertEqual("verify-failed", hint.category)

    def test_unknown_runtime_reason_returns_none(self) -> None:
        self.assertIsNone(preflight_runtime_hints.runtime_repair_hint("nonexistent"))


class RepairHintSubstitutionTests(unittest.TestCase):
    def test_plan_dir_and_repo_root_substituted_into_command(self) -> None:
        hint = preflight_repair_hints.REPAIR_HINTS["env-lock"]
        payload = preflight_auto_repair.repair_hint_payload(
            hint, plan_dir="/plans/demo", cwd="/repo"
        )
        self.assertIn("asl unlock /plans/demo --json", payload["message"])
        self.assertNotIn("<plan-dir>", payload["message"])
        self.assertNotIn("<plan-dir>", payload["question_template"])

    def test_repo_root_left_as_placeholder_when_cwd_none(self) -> None:
        hint = preflight_repair_hints.REPAIR_HINTS["baseline-missing"]
        payload = preflight_auto_repair.repair_hint_payload(
            hint, plan_dir="/plans/demo", cwd=None
        )
        self.assertIn("asl baseline /plans/demo --cwd <repo-root>", payload["message"])

    def test_every_hint_command_carries_plan_dir_placeholder(self) -> None:
        # Audit: any hint whose message embeds an `asl <plandir-cmd>` command
        # must use the `<plan-dir>` placeholder (never a bare `<plan>` or an
        # unsubstituted literal) so the payload can inject the real directory.
        plan_dir_commands = ("asl unlock", "asl backfill-progress", "asl baseline")
        tables = (
            preflight_repair_hints.REPAIR_HINTS.values(),
            preflight_hint_resolution._CATEGORY_HINT_OVERRIDES.values(),
        )
        for table in tables:
            for hint in table:
                with self.subTest(check_id=hint.check_id):
                    self.assertNotIn("<plan>", hint.message)
                    for command in plan_dir_commands:
                        if command in hint.message:
                            self.assertIn("<plan-dir>", hint.message)
                    if (
                        "asl preflight" in hint.message
                        and "--mark-loop-ready" in hint.message
                    ):
                        self.assertIn(
                            "asl preflight <plan-dir> --json --mark-loop-ready",
                            hint.message,
                        )

    def test_baseline_command_survives_whitespace_and_metacharacters(self) -> None:
        # The baseline remediation command embeds both the plan directory and
        # the repo root; a path with whitespace or shell metacharacters must
        # round-trip as one argument each.
        weird_plan = "/plans/de mo; rm -rf $(evil)"
        weird_repo = "/re po & 'quote'"
        payload = preflight_auto_repair.repair_hint_payload(
            preflight_repair_hints.REPAIR_HINTS["baseline-missing"],
            plan_dir=weird_plan,
            cwd=weird_repo,
        )
        command = _extract_backtick_command(payload["message"], "asl baseline")
        self.assertEqual(
            ["asl", "baseline", weird_plan, "--cwd", weird_repo],
            shlex.split(command),
        )

    def test_every_substituted_hint_embeds_only_quoted_paths(self) -> None:
        # Audit: after substitution, each weird path appears in the payload
        # only in its shell-quoted form (never raw), so every embedded command
        # round-trips through shlex.split without splitting a path.
        weird_plan = "/plans/de mo; rm -rf $(evil)"
        weird_repo = "/re po & 'quote'"
        tables = (
            preflight_repair_hints.REPAIR_HINTS.values(),
            preflight_hint_resolution._CATEGORY_HINT_OVERRIDES.values(),
        )
        for table in tables:
            for hint in table:
                payload = preflight_auto_repair.repair_hint_payload(
                    hint, plan_dir=weird_plan, cwd=weird_repo
                )
                for field in ("message", "question_template"):
                    text = payload[field]
                    with self.subTest(check_id=hint.check_id, field=field):
                        # Strip the quoted forms; the raw path must not survive.
                        residue = text.replace(shlex.quote(weird_plan), "").replace(
                            shlex.quote(weird_repo), ""
                        )
                        self.assertNotIn(weird_plan, residue)
                        self.assertNotIn(weird_repo, residue)
                    for command in _backtick_commands(text):
                        if command.startswith("asl "):
                            # Well-formed shell: parses without raising.
                            shlex.split(command)
