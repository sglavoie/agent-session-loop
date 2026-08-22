"""Audit run configuration, independent role resolution, and persistence."""

from __future__ import annotations

import argparse
import ast
import dataclasses
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    artifacts,
    assurance_ledger,
    assurance_models,
    assurance_projection,
    cli,
    contracts,
    events,
    harness_failures,
    harnesses,
    mailbox,
    model_availability,
    pause_policy,
    plans,
    repository_evidence,
    review,
    run_flags,
    runner,
    state,
    supervisor_audit,
    verification,
)
from agent_session_loop.assurance_models import (
    AUDIT_MARKDOWN_FILENAME,
    AUDIT_REPORT_FILENAME,
    MAX_AUDIT_ATTEMPT_RECORDS,
    MAX_AUDIT_ATTEMPTS,
    MAX_AUDIT_DELIVERY_ATTEMPTS,
    RUBRIC_LANES,
    AssuranceError,
    AuditAttempt,
    AuditRole,
    DeterministicResult,
    EvidenceRef,
    Finding,
    IssueObservation,
    LaneEvidence,
    SupervisorAudit,
    audit_artifact_name,
    canonical_json_bytes,
    issue_id_number,
)
from agent_session_loop.assurance_projection import CatalogEntry, IdentityCatalog
from tests.support import git_stdout, init_git_repo, write_plan

RUN_ID = "20260810T101112123456Z-4242"


def _parse(tokens: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(tokens)


def _namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, Any] = {
        "harness": "codex",
        "review_harness": None,
        "review_harness_model": None,
        "supervisor_audit": True,
        "audit_harness": None,
        "audit_harness_model": None,
        "audit_effort": None,
        "audit_timeout": supervisor_audit.DEFAULT_AUDIT_TIMEOUT_SECONDS,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class AuditFlagSurfaceTests(unittest.TestCase):
    """The audit flags reach the CLI, supervisor, and resume parsers."""

    def test_cli_defaults_enable_audit_with_the_default_timeout(self) -> None:
        args = _parse(["run", "/tmp/plan"])

        self.assertTrue(args.supervisor_audit)
        self.assertEqual(600, args.audit_timeout)
        self.assertIsNone(args.audit_harness)
        self.assertIsNone(args.audit_harness_model)
        self.assertIsNone(args.audit_effort)

    def test_cli_accepts_every_audit_flag(self) -> None:
        args = _parse(
            [
                "run",
                "/tmp/plan",
                "--audit-harness",
                "claude",
                "--audit-harness-model",
                "claude-opus-4",
                "--audit-effort",
                "high",
                "--audit-timeout",
                "900",
                "--no-supervisor-audit",
            ]
        )

        self.assertEqual("claude", args.audit_harness)
        self.assertEqual("claude-opus-4", args.audit_harness_model)
        self.assertEqual("high", args.audit_effort)
        self.assertEqual(900, args.audit_timeout)
        self.assertFalse(args.supervisor_audit)

    def test_enable_and_disable_are_mutually_exclusive(self) -> None:
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            _parse(["run", "/tmp/plan", "--supervisor-audit", "--no-supervisor-audit"])

    def test_supervisor_mode_strips_audit_defaults(self) -> None:
        # The helper must be able to tell an omitted flag from an explicit one,
        # so no audit flag may carry a meaningful default there.
        parser = argparse.ArgumentParser(prog="asl run", add_help=False)
        run_flags.add_run_arguments(parser, mode="supervisor")

        args = parser.parse_args(["/tmp/plan"])

        self.assertIsNone(args.supervisor_audit)
        self.assertIsNone(args.audit_timeout)
        self.assertIsNone(args.audit_harness)

    def test_resume_mode_suppresses_unset_audit_flags(self) -> None:
        parser = argparse.ArgumentParser(prog="asl run", add_help=False)
        run_flags.add_run_arguments(parser, mode="resume")

        args = parser.parse_args(["/tmp/plan", "--audit-effort", "low"])

        self.assertEqual({"plan_dir", "audit_effort"}, set(vars(args)))


class AuditValidationTests(unittest.TestCase):
    """Cross-flag rules shared by the runner and the supervisor helper."""

    def test_audit_harness_equal_to_implementation_is_rejected(self) -> None:
        errors = run_flags.validate_run_namespace(
            _namespace(harness="codex", audit_harness="codex")
        )

        self.assertEqual(
            ["--audit-harness must differ from --harness (both set to 'codex')"],
            [error.message for error in errors],
        )

    def test_audit_harness_may_equal_review_harness(self) -> None:
        errors = run_flags.validate_run_namespace(
            _namespace(harness="codex", review_harness="claude", audit_harness="claude")
        )

        self.assertEqual([], errors)

    def test_audit_harness_model_does_not_require_audit_harness(self) -> None:
        # An explicit audit model also overrides the model an inherited review
        # role would have supplied, so it is meaningful on its own.
        errors = run_flags.validate_run_namespace(
            _namespace(
                harness="codex",
                review_harness="claude",
                audit_harness_model="claude-opus-4",
            )
        )

        self.assertEqual([], errors)

    def test_audit_timeout_must_be_positive(self) -> None:
        errors = run_flags.validate_run_namespace(_namespace(audit_timeout=0))

        self.assertEqual(
            ["--audit-timeout must be a positive integer"],
            [error.message for error in errors],
        )

    def test_partial_namespace_validates_cleanly(self) -> None:
        self.assertEqual(
            [], run_flags.validate_run_namespace(argparse.Namespace(plan_dir="/tmp/p"))
        )


class AuditRoleResolutionTests(unittest.TestCase):
    """Resolving the independent role from the effective run configuration."""

    def _role(self, **overrides: object):
        return supervisor_audit.plan_audit(_namespace(**overrides)).role

    def test_explicit_audit_harness_wins_over_the_review_harness(self) -> None:
        role = self._role(
            harness="codex", review_harness="claude", audit_harness="copilot"
        )

        self.assertEqual("copilot", role.harness)
        self.assertEqual("explicit", role.source)
        self.assertEqual("codex", role.implementation_harness)

    def test_inherited_review_role_inherits_its_model(self) -> None:
        role = self._role(
            harness="codex",
            review_harness="claude",
            review_harness_model="claude-opus-4",
        )

        self.assertEqual("claude", role.harness)
        self.assertEqual("review", role.source)
        self.assertEqual("claude-opus-4", role.model)

    def test_explicit_audit_model_wins_over_the_inherited_model(self) -> None:
        role = self._role(
            harness="codex",
            review_harness="claude",
            review_harness_model="claude-opus-4",
            audit_harness_model="claude-sonnet-4",
        )

        self.assertEqual("claude", role.harness)
        self.assertEqual("review", role.source)
        self.assertEqual("claude-sonnet-4", role.model)

    def test_explicit_audit_model_wins_over_the_harness_native_model(self) -> None:
        explicit = self._role(harness="codex", audit_harness="claude")
        overridden = self._role(
            harness="codex",
            audit_harness="claude",
            audit_harness_model="claude-opus-4",
        )

        # No model request means the audit harness native default.
        self.assertIsNone(explicit.model)
        self.assertEqual("claude-opus-4", overridden.model)

    def test_audit_effort_is_independent_of_the_implementation_effort(self) -> None:
        inherited = self._role(harness="codex", review_harness="claude", effort="high")
        explicit = self._role(
            harness="codex", review_harness="claude", audit_effort="low"
        )

        self.assertIsNone(inherited.effort)
        self.assertEqual("low", explicit.effort)

    def test_no_review_and_no_audit_harness_yields_no_role(self) -> None:
        self.assertIsNone(self._role(harness="codex"))

    def test_a_role_equal_to_the_implementation_harness_is_never_resolved(
        self,
    ) -> None:
        # Validation rejects the flag combination, and resolution refuses it a
        # second time so no path can reach a self-auditing role.
        config = supervisor_audit.AuditConfig(harness="codex")

        self.assertIsNone(
            supervisor_audit.resolve_role(config, implementation_harness="codex")
        )

    def test_the_role_model_rejects_a_self_audit(self) -> None:
        with self.assertRaises(AssuranceError):
            supervisor_audit.AuditRole(
                harness="codex",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=600.0,
            )

    def test_the_timeout_applies_to_each_attempt_independently(self) -> None:
        role = self._role(harness="codex", review_harness="claude", audit_timeout=900)

        self.assertEqual(900.0, role.timeout_seconds)
        self.assertEqual(
            (900.0,) * MAX_AUDIT_ATTEMPT_RECORDS,
            supervisor_audit.attempt_timeouts(role),
        )

    def test_answer_and_delivery_budgets_are_separate(self) -> None:
        # A bad answer is the agent's doing and stays cheap; an undelivered
        # request is the provider's and gets its own, longer budget.
        self.assertEqual(2, MAX_AUDIT_ATTEMPTS)
        self.assertEqual(4, MAX_AUDIT_DELIVERY_ATTEMPTS)
        self.assertEqual(
            MAX_AUDIT_ATTEMPTS + MAX_AUDIT_DELIVERY_ATTEMPTS,
            MAX_AUDIT_ATTEMPT_RECORDS,
        )


class AuditPlanTests(unittest.TestCase):
    """The enable/skip decision and the assurance object a run starts with."""

    def test_enabled_run_with_a_role_is_pending(self) -> None:
        plan = supervisor_audit.plan_audit(
            _namespace(harness="codex", review_harness="claude")
        )

        self.assertEqual("pending", plan.state)
        self.assertIsNone(plan.reason)
        self.assertEqual("claude", plan.role.harness)

    def test_opting_out_skips_without_resolving_a_role(self) -> None:
        plan = supervisor_audit.plan_audit(
            _namespace(harness="codex", review_harness="claude", supervisor_audit=False)
        )

        self.assertEqual("skipped", plan.state)
        self.assertEqual(supervisor_audit.REASON_OPTED_OUT, plan.reason)
        self.assertIsNone(plan.role)

    def test_no_independent_role_stays_enabled_and_ends_unavailable(self) -> None:
        plan = supervisor_audit.plan_audit(_namespace(harness="codex"))

        self.assertTrue(plan.config.enabled)
        self.assertEqual("unavailable", plan.state)
        self.assertEqual(supervisor_audit.REASON_NO_INDEPENDENT_ROLE, plan.reason)
        self.assertIsNone(plan.role)

    def test_pending_is_persisted_only_for_an_eligible_run(self) -> None:
        plan = supervisor_audit.plan_audit(
            _namespace(harness="codex", review_harness="claude")
        )

        eligible = supervisor_audit.initial_summary(plan, run_id=RUN_ID, eligible=True)
        ineligible = supervisor_audit.initial_summary(
            plan, run_id=RUN_ID, eligible=False
        )

        self.assertEqual("pending", eligible.state)
        self.assertTrue(eligible.eligible)
        self.assertEqual((), eligible.attempts)
        self.assertEqual("skipped", ineligible.state)
        self.assertFalse(ineligible.eligible)
        self.assertEqual(supervisor_audit.REASON_DRY_RUN, ineligible.reason)

    def test_opting_out_is_persisted_as_skipped_immediately(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                _namespace(harness="codex", supervisor_audit=False)
            ),
            run_id=RUN_ID,
            eligible=True,
        )

        self.assertEqual("skipped", summary.state)
        self.assertTrue(summary.eligible)
        self.assertEqual(supervisor_audit.REASON_OPTED_OUT, summary.reason)
        self.assertEqual((), summary.issue_ids)

    def test_status_lines_name_the_role_or_the_reason(self) -> None:
        pending = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                _namespace(harness="codex", review_harness="claude")
            ),
            run_id=RUN_ID,
            eligible=True,
        )
        unavailable = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(_namespace(harness="codex")),
            run_id=RUN_ID,
            eligible=True,
        )

        self.assertEqual(
            "Supervisor audit: pending (claude via review role, 600s per attempt)",
            supervisor_audit.status_line(pending.to_dict()),
        )
        self.assertEqual(
            "Supervisor audit: unavailable (no-independent-role)",
            supervisor_audit.status_line(unavailable.to_dict()),
        )
        self.assertIsNone(supervisor_audit.status_line(None))


class AuditRunConfigPersistenceTests(unittest.TestCase):
    """Persisted run_config, the resume overlay, and command reconstruction."""

    def _run_config(self, tokens: list[str]) -> dict:
        args = _parse(["run", "/tmp/plan", *tokens])
        return runner._run_config_payload(args, cwd=Path("/tmp"), commit_mode=None)

    def test_new_runs_persist_audit_enabled_with_the_default_timeout(self) -> None:
        config = self._run_config([])

        self.assertTrue(config["supervisor_audit"])
        self.assertEqual(600, config["audit_timeout"])
        self.assertIsNone(config["audit_harness"])
        self.assertIsNone(config["audit_harness_model"])
        self.assertIsNone(config["audit_effort"])

    def test_explicit_audit_settings_are_persisted(self) -> None:
        config = self._run_config(
            [
                "--audit-harness",
                "claude",
                "--audit-harness-model",
                "claude-opus-4",
                "--audit-effort",
                "high",
                "--audit-timeout",
                "900",
                "--no-supervisor-audit",
            ]
        )

        self.assertFalse(config["supervisor_audit"])
        self.assertEqual("claude", config["audit_harness"])
        self.assertEqual("claude-opus-4", config["audit_harness_model"])
        self.assertEqual("high", config["audit_effort"])
        self.assertEqual(900, config["audit_timeout"])

    def _seed_state(self, tmp_dir: Path, run_config: dict) -> Path:
        plan_dir = tmp_dir / "plan"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_state = state.plan_state_dir(plan_dir, tmp_dir / "state")
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps({"run_id": RUN_ID, "run_config": run_config}),
            encoding="utf-8",
        )
        return plan_dir

    def test_resume_restores_persisted_audit_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = self._seed_state(
                tmp_dir,
                {
                    "supervisor_audit": True,
                    "audit_harness": "claude",
                    "audit_harness_model": "claude-opus-4",
                    "audit_effort": "high",
                    "audit_timeout": 900,
                },
            )
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(tmp_dir / "state"),
            ]

            args = runner._apply_resume_config(_parse(tokens), tokens[1:])

            self.assertEqual("claude", args.audit_harness)
            self.assertEqual("claude-opus-4", args.audit_harness_model)
            self.assertEqual("high", args.audit_effort)
            self.assertEqual(900, args.audit_timeout)
            self.assertTrue(args.supervisor_audit)

    def test_explicit_resume_flags_override_persisted_audit_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = self._seed_state(
                tmp_dir,
                {
                    "supervisor_audit": True,
                    "audit_harness": "claude",
                    "audit_timeout": 900,
                },
            )
            tokens = [
                "run",
                str(plan_dir),
                "--resume",
                "--state-dir",
                str(tmp_dir / "state"),
                "--no-supervisor-audit",
                "--audit-timeout",
                "120",
            ]

            args = runner._apply_resume_config(_parse(tokens), tokens[1:])

            self.assertFalse(args.supervisor_audit)
            self.assertEqual(120, args.audit_timeout)
            # Unpassed flags still rehydrate.
            self.assertEqual("claude", args.audit_harness)

    def test_resume_command_reconstruction_restates_an_explicit_role(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                _namespace(
                    harness="codex",
                    audit_harness="claude",
                    audit_harness_model="claude-opus-4",
                    audit_effort="high",
                    audit_timeout=900,
                )
            ),
            run_id=RUN_ID,
            eligible=True,
        )

        command = runner._reconstruct_resume_command(
            "/tmp/plan", None, {"assurance": summary.to_dict()}
        )

        self.assertIn("--audit-harness claude", command)
        self.assertIn("--audit-harness-model claude-opus-4", command)
        self.assertIn("--audit-effort high", command)
        self.assertIn("--audit-timeout 900", command)

    def test_resume_command_reconstruction_restates_an_opt_out(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                _namespace(harness="codex", supervisor_audit=False)
            ),
            run_id=RUN_ID,
            eligible=True,
        )

        command = runner._reconstruct_resume_command(
            "/tmp/plan", None, {"assurance": summary.to_dict()}
        )

        self.assertIn("--no-supervisor-audit", command)
        self.assertNotIn("--audit-harness", command)

    def test_reconstruction_leaves_an_inherited_role_to_the_review_flag(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                _namespace(
                    harness="codex",
                    review_harness="claude",
                    review_harness_model="claude-opus-4",
                )
            ),
            run_id=RUN_ID,
            eligible=True,
        )

        command = runner._reconstruct_resume_command(
            "/tmp/plan",
            None,
            {"review_harness": "claude", "assurance": summary.to_dict()},
        )

        self.assertIn("--review-harness claude", command)
        self.assertNotIn("--audit-harness ", command)
        # The effective model is restated so the rebuilt role is identical.
        self.assertIn("--audit-harness-model claude-opus-4", command)
        self.assertNotIn("--audit-timeout", command)


class AuditRunOutputTests(unittest.TestCase):
    """Fresh, no-role, opted-out, and dry-run behavior in text and JSON."""

    def _initial_payload(self, tmp_dir: Path, tokens: list[str]) -> dict:
        plan_dir = tmp_dir / "plan"
        write_plan(plan_dir, ("pending",))
        args = _parse(["run", str(plan_dir), *tokens])
        plan_state = state.plan_state_dir(plan_dir, tmp_dir / "state")
        lock_info = state.LockInfo(
            path=plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans.canonicalize_path(plan_dir)),
            created_at="2026-08-10T10:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        return runner._initial_payload(
            args=args,
            snapshot=plans.inspect_plan(plan_dir),
            state_dir=plan_state,
            run_dir=plan_state / "runs" / RUN_ID,
            event_path=plan_state / events.EVENTS_FILE_NAME,
            state_path=plan_state / events.STATE_FILE_NAME,
            lock_info=lock_info,
            cwd=tmp_dir,
            run_id=RUN_ID,
            commit_mode=None,
            mailbox_activation=runner._mailbox_activation_payload(args, plan_dir),
        )

    def test_a_fresh_eligible_run_starts_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._initial_payload(Path(tmp), ["--review-harness", "claude"])

            assurance = payload["assurance"]
            self.assertEqual("pending", assurance["state"])
            self.assertTrue(assurance["eligible"])
            self.assertEqual(RUN_ID, assurance["run_id"])
            self.assertEqual("claude", assurance["role"]["harness"])
            self.assertEqual(600.0, assurance["role"]["timeout_seconds"])

    def test_a_run_without_an_independent_role_starts_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._initial_payload(Path(tmp), [])

            assurance = payload["assurance"]
            self.assertEqual("unavailable", assurance["state"])
            self.assertTrue(assurance["eligible"])
            self.assertTrue(payload["run_config"]["supervisor_audit"])
            self.assertIsNone(assurance["role"])

    def test_a_dry_run_is_never_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._initial_payload(
                Path(tmp), ["--review-harness", "claude", "--dry-run"]
            )

            assurance = payload["assurance"]
            self.assertFalse(assurance["eligible"])
            self.assertEqual("skipped", assurance["state"])
            self.assertEqual(supervisor_audit.REASON_DRY_RUN, assurance["reason"])

    def _dry_run(self, tmp_dir: Path, tokens: list[str]) -> tuple[int, str]:
        plan_dir = tmp_dir / "plan"
        write_plan(plan_dir, ("pending",))
        argv = [
            "run",
            str(plan_dir),
            "--cwd",
            str(tmp_dir),
            "--state-dir",
            str(tmp_dir / "state"),
            "--allow-not-loop-ready",
            "--dry-run",
            *tokens,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = runner.run_from_args(_parse(argv), run_argv=argv)
        return code, out.getvalue()

    def test_dry_run_json_reports_the_resolved_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._dry_run(
                Path(tmp), ["--json", "--review-harness", "claude"]
            )

            self.assertEqual(0, code, out)
            audit = json.loads(out)["planned"]["audit"]
            self.assertTrue(audit["enabled"])
            self.assertEqual("pending", audit["state"])
            self.assertEqual("claude", audit["role"]["harness"])
            self.assertEqual("review", audit["role"]["source"])
            self.assertEqual(
                [600.0] * MAX_AUDIT_ATTEMPT_RECORDS, audit["attempt_timeouts"]
            )

    def test_dry_run_json_reports_an_unavailable_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._dry_run(Path(tmp), ["--json"])

            self.assertEqual(0, code, out)
            payload = json.loads(out)
            audit = payload["planned"]["audit"]
            self.assertTrue(audit["enabled"])
            self.assertEqual("unavailable", audit["state"])
            self.assertEqual(
                supervisor_audit.REASON_NO_INDEPENDENT_ROLE, audit["reason"]
            )
            self.assertIsNone(audit["role"])
            self.assertEqual([], audit["attempt_timeouts"])

    def test_dry_run_json_reports_an_opt_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._dry_run(
                Path(tmp),
                ["--json", "--review-harness", "claude", "--no-supervisor-audit"],
            )

            self.assertEqual(0, code, out)
            payload = json.loads(out)
            self.assertEqual("skipped", payload["planned"]["audit"]["state"])
            self.assertFalse(payload["run_config"]["supervisor_audit"])

    def test_dry_run_text_names_the_planned_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._dry_run(Path(tmp), ["--review-harness", "claude"])

            self.assertEqual(0, code, out)
            self.assertIn(
                "Supervisor audit: pending (claude via review role, 600s per attempt)",
                out,
            )

    def test_dry_run_text_names_an_unavailable_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._dry_run(Path(tmp), [])

            self.assertEqual(0, code, out)
            self.assertIn("Supervisor audit: unavailable (no-independent-role)", out)

    def test_a_dry_run_launches_no_auditor(self) -> None:
        # Planning an audit is not running one: a rehearsal must produce no
        # harness invocation at all, even though this module can now launch.
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self._dry_run(Path(tmp), ["--json"])

            self.assertEqual(0, code, out)
            self.assertEqual([], json.loads(out)["turns"])


class AuditStateProjectionTests(unittest.TestCase):
    """`state` exposes the persisted audit decision to status readers."""

    def test_runner_state_payload_exposes_the_assurance_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            summary = supervisor_audit.initial_summary(
                supervisor_audit.plan_audit(
                    _namespace(harness="codex", review_harness="claude")
                ),
                run_id=RUN_ID,
                eligible=True,
            )
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": RUN_ID,
                        "harness": "codex",
                        "assurance": summary.to_dict(),
                        "run_config": {"supervisor_audit": True},
                    }
                ),
                encoding="utf-8",
            )

            payload = state._runner_state_payload(state_path, lock_info=None)

            self.assertTrue(payload["supervisor_audit"])
            self.assertEqual("pending", payload["assurance"]["state"])

    def test_state_without_an_assurance_object_reports_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            state_path.write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")

            payload = state._runner_state_payload(state_path, lock_info=None)

            self.assertIsNone(payload["assurance"])
            self.assertIsNone(payload["supervisor_audit"])

    def _audited_status(self, tmp: str) -> tuple[dict, Path]:
        plan_dir = Path(tmp) / "plan"
        state_dir = Path(tmp) / "state"
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "harness": "codex",
                    "assurance": {
                        "run_id": RUN_ID,
                        "state": "findings",
                        "eligible": True,
                        "issue_ids": ["ASI-1", "ASI-2"],
                        "new_issue_count": 1,
                        "recurring_issue_count": 1,
                        "artifacts": [
                            {"kind": "artifact", "locator": "run/supervisor-audit.json"}
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        return (
            state.status_payload(plans.inspect_plan(plan_dir), state_dir),
            plan_state,
        )

    def test_status_projects_the_audit_without_launching_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload, plan_state = self._audited_status(tmp)

            report = payload["assurance_report"]
            self.assertEqual("findings", payload["assurance"]["state"])
            self.assertEqual(["ASI-1", "ASI-2"], report["issue_ids"])
            self.assertEqual(1, report["new_issue_count"])
            self.assertEqual(1, report["recurring_issue_count"])
            self.assertFalse(report["partial"])
            self.assertEqual(0, report["open_issue_count"])
            self.assertEqual("asl assurance list", report["list_command"])
            self.assertEqual(
                str(plan_state / "runs" / RUN_ID / "supervisor-audit.json"),
                report["report_path"],
            )
            json.dumps(payload)

    def test_status_text_prints_the_audit_and_the_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload, _ = self._audited_status(tmp)

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()

            self.assertIn("Supervisor audit: findings", output)
            self.assertIn("Assurance issues: 1 new, 1 recurring; ASI-1, ASI-2", output)
            self.assertIn("Assurance inbox: 0 open; `asl assurance list`", output)
            self.assertIn("Assurance report: ", output)

    def _partial_status(self, tmp: str) -> dict:
        plan_dir = Path(tmp) / "plan"
        state_dir = Path(tmp) / "state"
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        run_dir = plan_state / "runs" / RUN_ID
        run_dir.mkdir(parents=True, exist_ok=True)
        unjudged = ("resources-stalls", "unknown-anomalies")
        (run_dir / AUDIT_REPORT_FILENAME).write_text(
            json.dumps(
                {
                    "lanes": [
                        {
                            "lane": lane,
                            "status": "unavailable" if lane in unjudged else "pass",
                        }
                        for lane in RUBRIC_LANES
                    ]
                }
            ),
            encoding="utf-8",
        )
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "harness": "codex",
                    "assurance": {
                        "run_id": RUN_ID,
                        "state": "partial",
                        "eligible": True,
                        "reason": supervisor_audit.REASON_LANE_EVIDENCE_ABSENT,
                        "artifacts": [
                            {"kind": "artifact", "locator": "run/supervisor-audit.json"}
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        return state.status_payload(plans.inspect_plan(plan_dir), state_dir)

    def test_a_partial_audit_reports_which_lanes_had_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._partial_status(tmp)

            report = payload["assurance_report"]
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)

            self.assertEqual(6, report["lanes_judged"])
            self.assertEqual(8, report["lanes_total"])
            self.assertEqual(
                ["resources-stalls", "unknown-anomalies"],
                report["lanes_without_evidence"],
            )
            self.assertIn(
                "Supervisor audit: partial (6/8 lanes judged; no evidence: "
                "resources-stalls, unknown-anomalies)",
                buffer.getvalue(),
            )

    def test_status_without_an_audit_prints_no_assurance_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            payload = state.status_payload(
                plans.inspect_plan(plan_dir), Path(tmp) / "state"
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)

            self.assertIsNone(payload["assurance"])
            self.assertIsNone(payload["assurance_report"])
            self.assertNotIn("Assurance", buffer.getvalue())


#: Every flag that widens a harness beyond its default sandbox. None of them may
#: appear in an audit invocation, whatever else the argv contains.
BYPASS_FLAGS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--allow-all-tools",
    "--allow-all-paths",
    "bypassPermissions",
    "workspace-write",
    "dontAsk",
)


class _RecordingHarness:
    """Stand-in for ``harnesses.run_harness`` that can mutate the workspace.

    A real read-only sandbox cannot be exercised offline, so the guard is tested
    the other way around: this fake plays a harness whose sandbox failed open,
    and the audit must catch it from evidence alone.
    """

    def __init__(
        self,
        *,
        writes: Path | None = None,
        action: Callable[[], None] | None = None,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.writes = writes
        self.action = action
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        self.calls.append({"name": name, **kwargs})
        access = kwargs["access"]
        assert isinstance(access, harnesses.AccessProfile)
        argv = harnesses.get_adapter(name).build_argv(
            cwd=Path(str(kwargs["cwd"])),
            plan_dir=None,
            prompt=str(kwargs["prompt"]),
            last_message_path=Path(str(kwargs["last_message_path"])),
            access=access,
        )
        if self.writes is not None:
            self.writes.write_text("escaped\n", encoding="utf-8")
        if self.action is not None:
            self.action()
        return harnesses.HarnessResult(
            argv=argv,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            last_message="audit done",
            name=name,
        )


class RestrictedAuditTests(unittest.TestCase):
    def _role(self, harness: str) -> AuditRole:
        return AuditRole(
            harness=harness,
            implementation_harness="codex" if harness != "codex" else "claude",
            source="explicit",
            timeout_seconds=30.0,
        )

    def _workspace(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)
        return workspace, plan_dir, state_dir, run_dir

    def _run(
        self,
        harness: str,
        tmp: Path,
        fake: _RecordingHarness,
        workspace: Path,
        plan_dir: Path,
        state_dir: Path,
        run_dir: Path,
        repositories: tuple[str, ...] = ("app",),
        registry_declared: bool = True,
    ) -> supervisor_audit.AuditAttemptResult:
        return supervisor_audit.run_restricted_audit(
            self._role(harness),
            run_dir=run_dir,
            run_id=RUN_ID,
            workspace_root=workspace,
            plan_dir=plan_dir,
            prompt="Audit this run.",
            repositories=repositories,
            registry_declared=registry_declared,
            state_dir=state_dir,
            _run_harness_fn=fake,
        )

    def test_audit_argv_never_carries_bypass_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()

            for harness in harnesses.HARNESS_NAMES:
                invocation = supervisor_audit.build_audit_invocation(
                    self._role(harness),
                    run_dir=run_dir,
                    prompt="Audit this run.",
                    last_message_path=run_dir / "last.txt",
                )

                for flag in BYPASS_FLAGS:
                    self.assertNotIn(flag, invocation.argv, harness)

    def test_the_audit_runs_from_a_directory_outside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))

            workdir = supervisor_audit.audit_workdir(run_dir)
            roots = supervisor_audit.audit_read_roots(run_dir)

            self.assertTrue(workdir.is_dir())
            self.assertEqual(run_dir / supervisor_audit.AUDIT_WORKDIR_NAME, workdir)
            self.assertNotIn(workspace.resolve(), [root.resolve() for root in roots])
            for root in roots:
                self.assertFalse(root.resolve().is_relative_to(workspace.resolve()))

    def test_an_audit_that_mutates_the_workspace_is_unavailable(self) -> None:
        for harness in harnesses.HARNESS_NAMES:
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
                fake = _RecordingHarness(writes=workspace / "app" / "escaped.txt")

                result = self._run(
                    harness, Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
                )

                self.assertEqual("mutation-detected", result.outcome)
                self.assertEqual("unavailable", result.state)
                self.assertEqual(
                    supervisor_audit.REASON_WORKSPACE_MUTATED, result.reason
                )
                self.assertIn("app", (result.mutation or {})["changed"])

    def test_a_failed_launch_carries_its_classification_and_redacted_streams(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness(
                returncode=1,
                stdout="chatter\n" * 4000,
                stderr=(
                    "cloning https://user:hunter2@example.com/app\n"
                    '{"error": {"message": "Selected model is at capacity.", '
                    '"codex_error_info": "server_overloaded"}}'
                ),
            )

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("transport-error", result.outcome)
            assert result.classification is not None
            self.assertEqual(
                harness_failures.MODEL_CAPACITY_MARKER, result.classification.marker
            )
            self.assertTrue(result.classification.retryable)
            self.assertIn("at capacity", result.classification.evidence)
            # The provider's own words survive, the credential does not.
            self.assertIn("server_overloaded", result.stderr_tail)
            self.assertNotIn("hunter2", result.stderr_tail)
            self.assertLessEqual(
                len(result.stdout_tail.encode("utf-8")),
                supervisor_audit.MAX_HARNESS_TAIL_BYTES,
            )
            # The tail is the end of the stream, where the error is.
            self.assertTrue(result.stderr_tail.endswith('"server_overloaded"}}'))

    def test_a_write_to_an_unregistered_sibling_leaves_the_audit_available(
        self,
    ) -> None:
        """A concurrent run's worktree is outside the boundary this checks."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            init_git_repo(workspace / "sibling")
            fake = _RecordingHarness(writes=workspace / "sibling" / "escaped.txt")

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("ok", result.outcome)
            self.assertEqual("pending", result.state)
            self.assertIsNone(result.mutation)
            self.assertIsNone(result.issue_id)
            self.assertFalse(
                (run_dir / supervisor_audit.MUTATION_ARTIFACT_NAME).exists()
            )

    def test_a_symlinked_registered_checkout_is_not_audited_as_its_target(
        self,
    ) -> None:
        """An identical clone reached through a link is a topology change."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            twin = Path(tmp) / "twin"
            subprocess.run(
                ["git", "clone", str(checkout), str(twin)],
                text=True,
                capture_output=True,
                check=True,
            )

            def swap() -> None:
                shutil.rmtree(checkout)
                checkout.symlink_to(twin)

            fake = _RecordingHarness(action=swap)

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("mutation-detected", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(supervisor_audit.REASON_WORKSPACE_MUTATED, result.reason)
            self.assertEqual(["app"], (result.mutation or {})["removed"])
            self.assertEqual({}, (result.mutation or {})["changed"])

    def test_a_checkout_symlinked_before_the_audit_cannot_be_audited(self) -> None:
        """Both snapshots omit it, so the empty comparison must not read as ok."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            twin = Path(tmp) / "twin"
            subprocess.run(
                ["git", "clone", str(checkout), str(twin)],
                text=True,
                capture_output=True,
                check=True,
            )
            shutil.rmtree(checkout)
            checkout.symlink_to(twin)
            fake = _RecordingHarness()

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertIsNone(result.mutation)
            self.assertEqual([], fake.calls)

    def test_a_registered_repository_that_stops_being_a_worktree_is_unavailable(
        self,
    ) -> None:
        """Fail-closed probe evidence after the turn is silence, not an all-clear."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            fake = _RecordingHarness(action=lambda: shutil.rmtree(checkout / ".git"))

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertIsNone(result.mutation)
            self.assertEqual(1, len(fake.calls))

    def test_a_registered_repository_unfingerprintable_throughout_is_refused(
        self,
    ) -> None:
        """Unavailable on both sides compares as unchanged, so it cannot be graded."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            checkout = workspace / "app"
            shutil.rmtree(checkout / ".git")
            escaped = checkout / "escaped.txt"
            fake = _RecordingHarness(writes=escaped)

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("unavailable", result.state)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertIsNone(result.mutation)
            self.assertEqual([], fake.calls)
            self.assertFalse(escaped.exists())

    def test_a_synthesized_run_root_that_is_not_a_worktree_is_still_audited(
        self,
    ) -> None:
        """A plan that registered nothing keeps the audit its run root allows."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            result = self._run(
                "claude",
                Path(tmp),
                fake,
                workspace,
                plan_dir,
                state_dir,
                run_dir,
                repositories=(".",),
                registry_declared=False,
            )

            self.assertEqual("ok", result.outcome)
            self.assertEqual("pending", result.state)
            self.assertEqual(1, len(fake.calls))

    def test_the_same_root_declared_by_a_registry_is_refused(self) -> None:
        """The legacy tolerance follows the caller's claim, not the path."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            result = self._run(
                "claude",
                Path(tmp),
                fake,
                workspace,
                plan_dir,
                state_dir,
                run_dir,
                repositories=(".",),
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual(
                supervisor_audit.REASON_WORKSPACE_EVIDENCE_UNAVAILABLE, result.reason
            )
            self.assertEqual([], fake.calls)

    def test_a_detected_mutation_opens_a_system_issue_with_its_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness(writes=workspace / "app" / "escaped.txt")

            result = self._run(
                "claude", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            artifact = run_dir / supervisor_audit.MUTATION_ARTIFACT_NAME
            self.assertTrue(artifact.is_file())
            self.assertIsNotNone(result.issue_id)
            recorded = json.loads(
                (state_dir / assurance_ledger.LEDGER_FILE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result.issue_id, recorded["issue_id"])
            self.assertEqual(
                supervisor_audit.MUTATION_ARTIFACT_NAME,
                recorded["payload"]["audit_artifact"],
            )
            self.assertEqual(
                "mutation-commit-guards", recorded["payload"]["finding"]["lane"]
            )

    def test_an_audit_that_leaves_the_workspace_alone_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            result = self._run(
                "copilot", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir
            )

            self.assertEqual("ok", result.outcome)
            self.assertEqual("pending", result.state)
            self.assertIsNone(result.mutation)
            self.assertFalse(
                (run_dir / supervisor_audit.MUTATION_ARTIFACT_NAME).exists()
            )

    def test_the_auditor_is_launched_read_only_without_the_plan_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()

            self._run("codex", Path(tmp), fake, workspace, plan_dir, state_dir, run_dir)

            call = fake.calls[0]
            self.assertIsNone(call["plan_dir"])
            self.assertFalse(call["unsafe_bypass"])
            self.assertEqual("audit-read-only", call["access"].name)
            self.assertEqual(run_dir / supervisor_audit.AUDIT_WORKDIR_NAME, call["cwd"])

    def test_a_harness_that_cannot_enforce_the_profile_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            fake = _RecordingHarness()
            role = AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
            )
            unenforceable = harnesses.ClaudeAdapter
            self.addCleanup(setattr, unenforceable, "enforces_audit_read_only", True)
            unenforceable.enforces_audit_read_only = False

            result = supervisor_audit.run_restricted_audit(
                role,
                run_dir=run_dir,
                run_id=RUN_ID,
                workspace_root=workspace,
                plan_dir=plan_dir,
                prompt="Audit this run.",
                repositories=("app",),
                state_dir=state_dir,
                _run_harness_fn=fake,
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("profile-unenforceable", result.reason)
            self.assertEqual([], fake.calls)


class PinnedAuditWorkspaceTests(unittest.TestCase):
    """The boundary check is judged on a tree only the audit turn can change."""

    def _role(self) -> AuditRole:
        return AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=30.0,
        )

    def _workspace(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)
        return workspace, plan_dir, state_dir, run_dir

    def _record_commit(
        self,
        run_dir: Path,
        *,
        index: int,
        repository_path: str,
        head_after: str,
    ) -> None:
        directory = run_dir / f"commit-{index}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / state.RESULT_FILE_NAME).write_text(
            json.dumps(
                {
                    "commit_index": index,
                    "repository_path": repository_path,
                    "head_after": head_after,
                }
            ),
            encoding="utf-8",
        )

    def _head(self, repository: Path) -> str:
        return git_stdout(repository, "rev-parse", "HEAD").strip()

    def _pin(self, workspace: Path, run_dir: Path, repositories: tuple[str, ...]):
        return supervisor_audit.pinned_audit_workspace(
            workspace, repositories, supervisor_audit.run_commits(run_dir, repositories)
        )

    def test_the_checkout_holds_the_commit_the_run_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))
            recorded = self._head(workspace / "app")
            (workspace / "app" / "later.txt").write_text("later\n", encoding="utf-8")
            for argv in (["add", "later.txt"], ["commit", "-m", "later"]):
                subprocess.run(
                    ["git", *argv],
                    cwd=workspace / "app",
                    text=True,
                    capture_output=True,
                    check=True,
                )
            self._record_commit(
                run_dir, index=1, repository_path="app", head_after=recorded
            )

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                self.assertTrue(pinned.pinned)
                self.assertNotEqual(workspace.resolve(), pinned.root.resolve())
                self.assertEqual(recorded, self._head(pinned.root / "app"))
                self.assertFalse((pinned.root / "app" / "later.txt").exists())
                self.assertEqual({"app": recorded}, dict(pinned.commits))

    def test_a_concurrent_write_to_the_live_workspace_is_not_the_auditors_doing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            self._record_commit(
                run_dir,
                index=1,
                repository_path="app",
                head_after=self._head(workspace / "app"),
            )
            fake = _RecordingHarness(writes=workspace / "app" / "concurrent.txt")

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                result = supervisor_audit.run_restricted_audit(
                    self._role(),
                    run_dir=run_dir,
                    run_id=RUN_ID,
                    workspace_root=workspace,
                    plan_dir=plan_dir,
                    prompt="Audit this run.",
                    repositories=("app",),
                    pinned=pinned,
                    state_dir=state_dir,
                    _run_harness_fn=fake,
                )

            self.assertEqual("ok", result.outcome)
            self.assertIsNone(result.mutation)

    def test_a_write_inside_the_pinned_checkout_is_still_a_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            self._record_commit(
                run_dir,
                index=1,
                repository_path="app",
                head_after=self._head(workspace / "app"),
            )

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                fake = _RecordingHarness(writes=pinned.root / "app" / "escaped.txt")
                result = supervisor_audit.run_restricted_audit(
                    self._role(),
                    run_dir=run_dir,
                    run_id=RUN_ID,
                    workspace_root=workspace,
                    plan_dir=plan_dir,
                    prompt="Audit this run.",
                    repositories=("app",),
                    pinned=pinned,
                    state_dir=state_dir,
                    _run_harness_fn=fake,
                )

            self.assertEqual("mutation-detected", result.outcome)
            self.assertEqual(supervisor_audit.REASON_WORKSPACE_MUTATED, result.reason)
            mutation = result.mutation or {}
            self.assertIn("app", mutation["changed"])
            self.assertTrue(mutation["workspace"]["pinned"])

    def test_the_live_workspaces_other_checkouts_stay_out_of_the_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._workspace(Path(tmp))
            head = self._head(workspace / "app")
            self._record_commit(
                run_dir, index=1, repository_path="app", head_after=head
            )
            sibling = Path(tmp) / "sibling"
            repository_evidence.add_detached_worktree(workspace / "app", sibling, head)
            self.addCleanup(
                repository_evidence.remove_worktree, workspace / "app", sibling
            )
            fake = _RecordingHarness(writes=sibling / "escaped.txt")

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                result = supervisor_audit.run_restricted_audit(
                    self._role(),
                    run_dir=run_dir,
                    run_id=RUN_ID,
                    workspace_root=workspace,
                    plan_dir=plan_dir,
                    prompt="Audit this run.",
                    repositories=("app",),
                    pinned=pinned,
                    state_dir=state_dir,
                    _run_harness_fn=fake,
                )

            self.assertEqual("ok", result.outcome)

    def test_the_checkout_and_its_registration_are_dropped_afterwards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))
            self._record_commit(
                run_dir,
                index=1,
                repository_path="app",
                head_after=self._head(workspace / "app"),
            )

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                root = pinned.root

            self.assertFalse(root.exists())
            listed = git_stdout(workspace / "app", "worktree", "list", "--porcelain")
            self.assertEqual(1, listed.count("worktree "))

    def test_a_run_that_recorded_no_commit_is_judged_on_the_live_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                self.assertFalse(pinned.pinned)
                self.assertEqual(
                    supervisor_audit.PIN_REASON_NO_RECORDED_COMMIT, pinned.reason
                )
                self.assertEqual(workspace, pinned.root)

    def test_one_repository_without_a_commit_abandons_the_whole_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))
            init_git_repo(workspace / "lib")
            self._record_commit(
                run_dir,
                index=1,
                repository_path="app",
                head_after=self._head(workspace / "app"),
            )

            self.assertIsNone(supervisor_audit.run_commits(run_dir, ("app", "lib")))

    def test_a_commit_that_cannot_be_checked_out_falls_back_to_the_live_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _, run_dir = self._workspace(Path(tmp))
            self._record_commit(
                run_dir, index=1, repository_path="app", head_after="0" * 40
            )

            with self._pin(workspace, run_dir, ("app",)) as pinned:
                self.assertEqual(
                    supervisor_audit.PIN_REASON_CHECKOUT_FAILED, pinned.reason
                )
                self.assertEqual(workspace, pinned.root)
            listed = git_stdout(workspace / "app", "worktree", "list", "--porcelain")
            self.assertEqual(1, listed.count("worktree "))

    def test_the_latest_commit_per_repository_is_the_one_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, run_dir = self._workspace(Path(tmp))
            self._record_commit(run_dir, index=9, repository_path="app", head_after="a")
            self._record_commit(
                run_dir, index=10, repository_path="app", head_after="b"
            )

            self.assertEqual(
                {"app": "b"}, supervisor_audit.run_commits(run_dir, ("app",))
            )


class EvidenceBundleTests(unittest.TestCase):
    """The audit input bundle is bounded, deterministic, and control-plane only."""

    SECRET_ARGV = "ghp_AAAAAAAAAAAAAAAAAAAA"
    SECRET_ENV = "ghp_BBBBBBBBBBBBBBBBBBBB"
    SECRET_PASSWORD = "correct-horse-battery"
    APPLICATION_SOURCE = "def transfer_funds(amount):\n    return amount * 2\n"

    QUESTION_ID = "11111111-1111-4111-8111-111111111111"
    WAIT_ID = "22222222-2222-4222-8222-222222222222"
    REPLY_ID = "33333333-3333-4333-8333-333333333333"
    RECEIPT_ID = "44444444-4444-4444-8444-444444444444"
    WAIT_END_ID = "55555555-5555-4555-8555-555555555555"
    NOTICE_ID = "66666666-6666-4666-8666-666666666666"
    FOREIGN_QUESTION_ID = "77777777-7777-4777-8777-777777777777"
    FOREIGN_REPLY_ID = "88888888-8888-4888-8888-888888888888"
    FOREIGN_RUN_ID = "20260810T101112123456Z-9999"
    QUESTION_TEXT = "Two checks print `unknown flag: --docs`. Remove them?"
    REPLY_TEXT = "Option (a), done — removed both from session-15.1.md."

    def _mailbox_records(self) -> list[dict[str, Any]]:
        """One full exchange for this run, plus another run's question."""
        return [
            {
                "created_at": "2026-08-10T10:11:12Z",
                "id": self.QUESTION_ID,
                "kind": mailbox.KIND_QUESTION,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "text": self.QUESTION_TEXT,
                "delivery_mode": mailbox.DELIVERY_MODE_WAIT,
                "run_id": RUN_ID,
                "session_id": "01",
                "turn_index": 1,
            },
            {
                "created_at": "2026-08-10T10:11:13Z",
                "id": self.WAIT_ID,
                "kind": mailbox.KIND_WAIT_START,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "to": self.QUESTION_ID,
            },
            {
                "created_at": "2026-08-10T10:11:14Z",
                "id": self.FOREIGN_QUESTION_ID,
                "kind": mailbox.KIND_QUESTION,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "text": "Another run asks something else.",
                "delivery_mode": mailbox.DELIVERY_MODE_ASYNC,
                "run_id": self.FOREIGN_RUN_ID,
                "session_id": "01",
                "turn_index": 1,
            },
            {
                "created_at": "2026-08-10T10:11:15Z",
                "id": self.REPLY_ID,
                "kind": mailbox.KIND_REPLY,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "text": self.REPLY_TEXT,
                "to": self.QUESTION_ID,
            },
            {
                "created_at": "2026-08-10T10:11:16Z",
                "id": self.FOREIGN_REPLY_ID,
                "kind": mailbox.KIND_REPLY,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "text": "Answering the other run.",
                "to": self.FOREIGN_QUESTION_ID,
            },
            {
                "created_at": "2026-08-10T10:11:17Z",
                "id": self.RECEIPT_ID,
                "kind": mailbox.KIND_RECEIPT,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "to": self.QUESTION_ID,
                "closure_id": self.REPLY_ID,
                "wait_id": self.WAIT_ID,
            },
            {
                "created_at": "2026-08-10T10:11:18Z",
                "id": self.WAIT_END_ID,
                "kind": mailbox.KIND_WAIT_END,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "to": self.QUESTION_ID,
                "wait_id": self.WAIT_ID,
                "reason": mailbox.WAIT_END_REASON_CLOSURE,
                "closure_id": self.REPLY_ID,
            },
            {
                "created_at": "2026-08-10T10:11:19Z",
                "id": self.NOTICE_ID,
                "kind": mailbox.KIND_NOTICE,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "text": "Session 01 resumed with the narrowed contract.",
                "run_id": RUN_ID,
                "session_id": "01",
                "turn_index": 1,
            },
        ]

    def _history(
        self,
        tmp: Path,
        *,
        stdout_bytes: int = 64,
        event_count: int = 3,
    ) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "workspace_root": str(workspace)}),
            encoding="utf-8",
        )

        turn_dir = run_dir / "turn-0001-01"
        turn_dir.mkdir()
        (turn_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(
                {
                    "ok": True,
                    "returncode": 0,
                    "invocation": {
                        "name": "codex",
                        "argv": ["codex", "--token", self.SECRET_ARGV, "-p", "go"],
                        "cwd": str(workspace / "app"),
                    },
                    "stdout": self.APPLICATION_SOURCE,
                    "diff": self.APPLICATION_SOURCE,
                    "verification": {
                        "checks": [{"name": "tests", "passed": True, "returncode": 0}]
                    },
                }
            ),
            encoding="utf-8",
        )
        (turn_dir / "stdout.log").write_text("o" * stdout_bytes, encoding="utf-8")
        (turn_dir / "stderr.log").write_text("e\n", encoding="utf-8")
        (turn_dir / "last-message.txt").write_text("Session done.\n", encoding="utf-8")

        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "harness": "codex",
                    "model": "gpt-5",
                    "ok": True,
                    "review_verdict": "approved",
                    "repositories": [
                        {
                            "alias": "app",
                            "path": "app",
                            "origin": "https://user:pw@github.com/owner/repo.git",
                            "resolved_root": str(workspace / "app"),
                        }
                    ],
                    "run_config": {
                        "env": {"GITHUB_TOKEN": self.SECRET_ENV},
                        "password": self.SECRET_PASSWORD,
                    },
                }
            ),
            encoding="utf-8",
        )
        with (state_dir / events.EVENTS_FILE_NAME).open(
            "w", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "name": "run_started",
                        "version": events.EVENT_SCHEMA_VERSION,
                        "created_at": "2026-08-10T10:11:12Z",
                        "payload": {"run_id": RUN_ID},
                    }
                )
                + "\n"
            )
            for index in range(event_count):
                handle.write(
                    json.dumps(
                        {
                            "name": events.TURN_HEARTBEAT_EVENT,
                            "version": events.EVENT_SCHEMA_VERSION,
                            "created_at": "2026-08-10T10:11:13Z",
                            "payload": {"run_id": RUN_ID, "index": index},
                        }
                    )
                    + "\n"
                )
            handle.write(
                json.dumps(
                    {
                        "name": events.RUN_FINISHED_EVENT,
                        "version": events.EVENT_SCHEMA_VERSION,
                        "created_at": "2026-08-10T10:11:14Z",
                        "payload": {"run_id": RUN_ID, "ok": True},
                    }
                )
                + "\n"
            )
        (state_dir / mailbox.MAILBOX_FILE_NAME).write_text(
            "".join(json.dumps(record) + "\n" for record in self._mailbox_records()),
            encoding="utf-8",
        )
        return workspace, plan_dir, state_dir, run_dir

    def _collect(
        self,
        workspace: Path,
        plan_dir: Path,
        state_dir: Path,
        run_dir: Path,
        **overrides: object,
    ) -> supervisor_audit.AuditInput:
        return supervisor_audit.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            **overrides,
        )

    def test_equivalent_histories_serialize_to_identical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "history"
            root.mkdir()
            one = self._collect(*self._history(root))
            shutil.rmtree(root)
            root.mkdir()
            two = self._collect(*self._history(root))

            self.assertEqual(one.to_bytes(), two.to_bytes())
            self.assertEqual(one.sha256, two.sha256)

    def test_relocating_a_history_preserves_its_control_plane_evidence(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            one = self._collect(*self._history(Path(first))).payload
            two = self._collect(*self._history(Path(second))).payload

            for section in (
                "turns",
                "events",
                "mailbox",
                "scopes",
                "review",
                "status",
                "registry",
            ):
                self.assertEqual(one[section], two[section], section)

    def test_collection_is_stable_across_repeated_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            self.assertEqual(
                self._collect(*history).to_bytes(), self._collect(*history).to_bytes()
            )

    def test_huge_logs_stay_under_the_serialized_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(
                Path(tmp), stdout_bytes=4 * 1024 * 1024, event_count=20000
            )

            bundle = self._collect(*history)
            payload = bundle.payload

            self.assertLessEqual(
                len(bundle.to_bytes()), supervisor_audit.AUDIT_INPUT_MAX_BYTES
            )
            self.assertTrue(
                any(
                    entry["section"] == "events"
                    and entry["reason"] == supervisor_audit.REASON_SIZE_CAP
                    for entry in payload["omissions"]
                )
            )
            terminal = [
                record
                for record in payload["events"]["records"]
                if record["name"] == events.RUN_FINISHED_EVENT
            ]
            self.assertEqual(1, len(terminal))

    def test_shedding_keeps_the_terminal_record_before_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp), event_count=20000)

            records = self._collect(*history).payload["events"]["records"]
            names = {record["name"] for record in records}

            self.assertIn(events.RUN_FINISHED_EVENT, names)
            self.assertIn("run_started", names)

    def test_application_source_and_diffs_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn("transfer_funds", text)
            self.assertIn(supervisor_audit.REASON_APPLICATION_CONTENT, text)

    def test_credentials_never_reach_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn(self.SECRET_ARGV, text)
            self.assertNotIn(self.SECRET_ENV, text)
            self.assertNotIn(self.SECRET_PASSWORD, text)
            self.assertNotIn("user:pw@", text)
            self.assertIn("github.com/owner/repo", text)

    def test_missing_artifacts_are_explicit_rather_than_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            (run_dir / "turn-0001-01" / "last-message.txt").unlink()

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload
            last_message = payload["turns"][0]["last_message"]

            self.assertFalse(last_message["available"])
            self.assertEqual(supervisor_audit.REASON_MISSING, last_message["reason"])
            self.assertTrue(
                any(
                    entry["reason"] == supervisor_audit.REASON_MISSING
                    and entry["count"] >= 1
                    and len(entry["sha256"]) == 64
                    for entry in payload["omissions"]
                )
            )

    def test_a_damaged_event_log_is_counted_and_digested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            with (state_dir / events.EVENTS_FILE_NAME).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("{not json\n")

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertEqual(1, payload["events"]["summary"]["rejected_count"])
            self.assertTrue(
                any(
                    entry["section"] == "events"
                    and entry["reason"] == supervisor_audit.REASON_MALFORMED
                    for entry in payload["omissions"]
                )
            )

    def test_the_operator_exchange_reaches_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload
            section = payload["mailbox"]

            self.assertTrue(section["summary"]["available"])
            self.assertEqual(
                [
                    (self.QUESTION_ID, mailbox.KIND_QUESTION),
                    (self.WAIT_ID, mailbox.KIND_WAIT_START),
                    (self.REPLY_ID, mailbox.KIND_REPLY),
                    (self.RECEIPT_ID, mailbox.KIND_RECEIPT),
                    (self.WAIT_END_ID, mailbox.KIND_WAIT_END),
                    (self.NOTICE_ID, mailbox.KIND_NOTICE),
                ],
                [(record["id"], record["kind"]) for record in section["records"]],
            )
            self.assertEqual(
                [1, 2, 4, 6, 7, 8],
                [record["sequence"] for record in section["records"]],
            )
            self.assertEqual("01", section["records"][0]["session_id"])
            self.assertEqual(1, section["records"][0]["turn_index"])

    def test_the_reply_prose_is_withheld_while_its_digest_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn(self.REPLY_TEXT, text)
            self.assertNotIn(self.QUESTION_TEXT, text)
            self.assertIn(
                hashlib.sha256(self.REPLY_TEXT.encode("utf-8")).hexdigest()[:16], text
            )

    def test_another_runs_exchange_is_counted_rather_than_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            text = bundle.to_bytes().decode("utf-8")

            self.assertEqual(2, bundle.payload["mailbox"]["summary"]["unscoped_count"])
            self.assertNotIn(self.FOREIGN_QUESTION_ID, text)
            self.assertNotIn(self.FOREIGN_REPLY_ID, text)

    def test_a_missing_mailbox_is_explicit_rather_than_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            (state_dir / mailbox.MAILBOX_FILE_NAME).unlink()

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload
            summary = payload["mailbox"]["summary"]

            self.assertEqual([], payload["mailbox"]["records"])
            self.assertFalse(summary["available"])
            self.assertEqual(supervisor_audit.REASON_MISSING, summary["reason"])
            self.assertTrue(
                any(
                    entry["section"] == "mailbox"
                    and entry["reason"] == supervisor_audit.REASON_MISSING
                    for entry in payload["omissions"]
                )
            )

    def test_a_damaged_mailbox_line_is_counted_and_digested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            with (state_dir / mailbox.MAILBOX_FILE_NAME).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("{not json\n")

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertEqual(1, payload["mailbox"]["summary"]["rejected_count"])
            self.assertEqual(6, len(payload["mailbox"]["records"]))
            self.assertTrue(
                any(
                    entry["section"] == "mailbox"
                    and entry["reason"] == supervisor_audit.REASON_MALFORMED
                    and len(entry["sha256"]) == 64
                    for entry in payload["omissions"]
                )
            )

    def test_the_size_cap_sheds_lifecycle_records_and_keeps_the_ruling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp), event_count=20000)

            payload = self._collect(*history, max_bytes=4 * 1024).payload
            kinds = [record["kind"] for record in payload["mailbox"]["records"]]

            self.assertEqual(
                [mailbox.KIND_QUESTION, mailbox.KIND_REPLY, mailbox.KIND_NOTICE], kinds
            )
            self.assertTrue(
                any(
                    entry["section"] == "mailbox"
                    and entry["reason"] == supervisor_audit.REASON_SIZE_CAP
                    for entry in payload["omissions"]
                )
            )

    def test_the_mailbox_is_named_in_the_evidence_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))

            self.assertIn(
                f"state/{mailbox.MAILBOX_FILE_NAME}",
                {reference.locator for reference in bundle.evidence},
            )

    def test_every_reference_resolves_inside_an_approved_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            roots = set(bundle.payload["roots"])

            self.assertTrue(bundle.evidence)
            for reference in bundle.evidence:
                self.assertIn(reference.locator.split("/", 1)[0], roots)

    def test_paths_outside_the_approved_roots_are_withheld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            result_path = run_dir / "turn-0001-01" / state.RESULT_FILE_NAME
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["invocation"]["cwd"] = "/etc/ssh/secret_config"
            result_path.write_text(json.dumps(result), encoding="utf-8")

            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn("/etc/ssh/secret_config", text)
            self.assertIn(supervisor_audit.REASON_OUTSIDE_APPROVED_ROOTS, text)

    def test_repository_fingerprints_are_reported_as_not_recorded_when_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload

            self.assertFalse(payload["repository"]["available"])
            self.assertEqual(
                supervisor_audit.REASON_NOT_RECORDED, payload["repository"]["reason"]
            )

    def test_repository_fingerprints_are_copied_when_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            snapshot = repository_evidence.capture_workspace_snapshot(
                workspace, plan_dir
            )

            payload = self._collect(
                workspace,
                plan_dir,
                state_dir,
                run_dir,
                workspace_snapshot=snapshot,
            ).payload

            self.assertTrue(payload["repository"]["available"])
            self.assertIn("app", payload["repository"]["repositories"])

    def test_the_bundle_writes_the_canonical_audit_input_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)

            written = bundle.write(run_dir, durable=False)

            self.assertEqual(bundle.to_bytes(), written.read_bytes())
            with self.assertRaises(AssuranceError):
                bundle.write(run_dir, durable=False)

    def test_scopes_and_versions_describe_the_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload

            self.assertEqual(
                [
                    {
                        "session": "session-01",
                        "available": True,
                        "reads": ["app"],
                        "writes": ["app"],
                    }
                ],
                payload["scopes"]["sessions"],
            )
            self.assertTrue(payload["versions"]["package"]["available"])
            self.assertEqual(
                events.EVENT_SCHEMA_VERSION, payload["versions"]["event_schema_version"]
            )

    def _rewrite_result(self, run_dir: Path, **fields: Any) -> None:
        path = run_dir / "turn-0001-01" / state.RESULT_FILE_NAME
        result = json.loads(path.read_text(encoding="utf-8"))
        result.update(fields)
        path.write_text(json.dumps(result), encoding="utf-8")

    def _rewrite_state(self, state_dir: Path, **fields: Any) -> None:
        path = state_dir / events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(fields)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_authorized_aliases_survive_redaction_but_secret_keys_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_result(
                run_dir,
                workspace_write_guard={
                    "passed": True,
                    "violations": [],
                    "unavailable": {},
                    "authorized_aliases": ["app"],
                },
                auth_token=self.SECRET_ARGV,
                authorization=self.SECRET_ARGV,
            )

            result = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "turns"
            ][0]["result"]

            self.assertEqual(
                ["app"], result["workspace_write_guard"]["authorized_aliases"]
            )
            self.assertEqual(supervisor_audit.REDACTED, result["auth_token"])
            self.assertEqual(supervisor_audit.REDACTED, result["authorization"])

    def test_a_reply_origin_survives_while_a_remote_is_canonicalized(self) -> None:
        for origin in runner.REPLY_ORIGINS:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
                self._rewrite_result(
                    run_dir, reply_source={"origin": origin, "reply_length": 6}
                )

                payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

                self.assertEqual(
                    origin, payload["turns"][0]["result"]["reply_source"]["origin"]
                )
                self.assertEqual(
                    "github.com/owner/repo",
                    payload["registry"]["repositories"][0]["origin"],
                )

    def test_valid_audit_role_sources_survive_but_arbitrary_source_is_redacted(
        self,
    ) -> None:
        for source in assurance_models.AUDIT_ROLE_SOURCES:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
                self._rewrite_state(
                    state_dir,
                    assurance={
                        "role": {"source": source},
                        "source": "arbitrary application text",
                    },
                )

                status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                    "status"
                ]

                self.assertEqual(source, status["assurance"]["role"]["source"])
                self.assertIn(
                    supervisor_audit.REASON_APPLICATION_CONTENT,
                    status["assurance"]["source"],
                )

    def test_control_plane_signal_sources_survive_under_any_parent(self) -> None:
        # The runner stamps this provenance under several parents, so the
        # exemption is keyed on the value rather than on the parent name.
        for source in sorted(supervisor_audit._CONTROL_PLANE_SOURCE_VALUES):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
                self._rewrite_state(
                    state_dir,
                    action_required={"reason": "runner-pause", "source": source},
                    pause_details={"source": source},
                )

                status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                    "status"
                ]

                self.assertEqual(source, status["action_required"]["source"])
                self.assertEqual(source, status["pause_details"]["source"])

    def test_an_unbounded_source_is_still_withheld_as_application_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_state(
                state_dir,
                action_required={"source": "arbitrary application text"},
                pause_details={"source": "stdout of the agent, quoted at length"},
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]

            self.assertIn(
                supervisor_audit.REASON_APPLICATION_CONTENT,
                status["action_required"]["source"],
            )
            self.assertIn(
                supervisor_audit.REASON_APPLICATION_CONTENT,
                status["pause_details"]["source"],
            )

    def test_final_review_artifacts_are_indexed_from_the_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            review.final_review_output_path(plan_dir).write_text(
                f"{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
            )
            review.final_review_artifact_path(plan_dir).write_text(
                json.dumps(
                    {
                        "version": review.REVIEW_ARTIFACT_VERSION,
                        "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            locators = {entry["locator"] for entry in payload["evidence"]}
            self.assertIn(f"plan/{review.FINAL_REVIEW_FILENAME}", locators)
            self.assertIn(f"plan/{review.FINAL_REVIEW_ARTIFACT_FILENAME}", locators)
            self.assertEqual(
                [],
                [
                    entry
                    for entry in payload["omissions"]
                    if entry["section"] == "review.artifacts"
                ],
            )

    def test_absent_final_review_artifacts_are_explained_by_an_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self.assertFalse(review.final_review_output_path(plan_dir).exists())

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            locators = {entry["locator"] for entry in payload["evidence"]}
            self.assertNotIn(f"plan/{review.FINAL_REVIEW_FILENAME}", locators)
            self.assertEqual(
                [
                    {
                        "section": "review.artifacts",
                        "reason": supervisor_audit.REASON_MISSING,
                        "count": 2,
                        "sha256": mock.ANY,
                    }
                ],
                [
                    entry
                    for entry in payload["omissions"]
                    if entry["section"] == "review.artifacts"
                ],
            )

    def test_a_failed_write_guard_makes_its_turn_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            # The turn exits zero and reports a complete status: the guard
            # result is the only record that it wrote outside its scope.
            self._rewrite_result(
                run_dir,
                status_result="complete",
                workspace_write_guard={
                    "passed": False,
                    "violations": [{"kind": "repository-added", "alias": "app"}],
                    "unavailable": {},
                    "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                },
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertTrue(payload["turns"][0]["anomalous"])

    def test_a_passing_write_guard_leaves_its_turn_unremarkable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_result(
                run_dir,
                status_result="complete",
                workspace_write_guard={
                    "passed": True,
                    "violations": [],
                    "unavailable": {},
                    "authorized_aliases": ["app"],
                },
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertFalse(payload["turns"][0]["anomalous"])

    def test_an_unjudgeable_write_guard_makes_its_turn_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_result(
                run_dir,
                status_result="complete",
                workspace_write_guard={
                    "passed": False,
                    "violations": [],
                    "unavailable": {"app": {"reason": "unreadable"}},
                },
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertTrue(payload["turns"][0]["anomalous"])

    def test_the_progress_journal_is_indexed_with_its_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            journal = plans.progress_path(plan_dir)
            journal.write_text("# Progress Journal\n\n## Session 1\n", encoding="utf-8")

            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)
            entry = bundle.payload["plan"]["progress"]
            indexed = {reference.locator: reference for reference in bundle.evidence}

            self.assertTrue(entry["available"])
            self.assertIn(entry["reference"], indexed)
            self.assertEqual(
                artifacts.sha256_hex(journal.read_bytes()),
                indexed[entry["reference"]].digest,
            )

    def test_a_missing_progress_journal_is_recorded_as_an_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload
            entry = payload["plan"]["progress"]

            self.assertFalse(entry["available"])
            self.assertEqual(supervisor_audit.REASON_MISSING, entry["reason"])
            self.assertTrue(
                any(
                    omission["section"] == "plan"
                    and omission["reason"] == supervisor_audit.REASON_MISSING
                    for omission in payload["omissions"]
                )
            )

    def test_the_observed_model_is_projected_beside_the_configured_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_state(
                state_dir,
                model="native CLI default",
                model_source="unresolved",
                observed_model="claude-opus-5",
                observed_model_source="harness_reported",
            )

            section = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "state"
            ]

            self.assertEqual("native CLI default", section["model"])
            self.assertEqual("unresolved", section["model_source"])
            self.assertEqual("native CLI default", section["requested_model"])
            self.assertEqual("unresolved", section["requested_model_source"])
            self.assertEqual("claude-opus-5", section["observed_model"])
            self.assertEqual("harness_reported", section["observed_model_source"])

    def test_the_per_role_model_projection_reaches_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_state(
                state_dir,
                model="native CLI default",
                model_source="unresolved",
                model_projection=[
                    {
                        "session_kind": "implementation",
                        "harness": "claude",
                        "requested_model": "native CLI default",
                        "requested_model_source": "unresolved",
                        "observed_models": ["claude-opus-5"],
                        "turns": 3,
                        "model": "claude-opus-5",
                        "model_source": "harness_reported",
                    }
                ],
            )

            section = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "state"
            ]

            # An audit that saw only the unresolved request could not tell that
            # the run's own turns had already answered the question.
            self.assertEqual(
                [("implementation", "claude-opus-5", "harness_reported")],
                [
                    (record["session_kind"], record["model"], record["model_source"])
                    for record in section["model_projection"]
                ],
            )

    def test_a_run_without_a_projection_records_an_empty_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            section = self._collect(*self._history(Path(tmp))).payload["state"]

            self.assertEqual([], section["model_projection"])


class _RunHistoryFixture:
    """One recorded run, shared by the deterministic and agent audit tests."""

    MANIFEST = (
        "# Manifest\n\n## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        "| app | app | https://example.invalid/app.git |\n"
    )

    def _history(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        """Write one run whose every recorded artifact agrees with the others."""
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(self.MANIFEST, encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "workspace_root": str(workspace)}),
            encoding="utf-8",
        )

        turn_dir = run_dir / "turn-0001-01"
        turn_dir.mkdir()
        self._write_result(turn_dir, self._clean_result(workspace, turn_dir))
        (turn_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
        (turn_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
        (turn_dir / "last-message.txt").write_text("Session done.\n", encoding="utf-8")

        self._write_state(state_dir, self._clean_state(workspace))
        self._write_events(
            state_dir,
            [
                ("run_started", {"run_id": RUN_ID}),
                (events.TURN_HEARTBEAT_EVENT, {"run_id": RUN_ID, "index": 0}),
                (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
            ],
        )
        return workspace, plan_dir, state_dir, run_dir

    def _clean_result(self, workspace: Path, turn_dir: Path) -> dict[str, Any]:
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "interrupted": False,
            "session_kind": "implementation",
            "artifact_dir": str(turn_dir),
            "stdout_path": str(turn_dir / "stdout.log"),
            "stderr_path": str(turn_dir / "stderr.log"),
            "last_message_path": str(turn_dir / "last-message.txt"),
            "result_path": str(turn_dir / state.RESULT_FILE_NAME),
            "invocation": {"name": "codex", "cwd": str(workspace / "app")},
            "workspace_write_guard": {
                "passed": True,
                "write_aliases": ["app"],
                "authorized_aliases": ["app"],
                "violations": [],
                "unavailable": {},
            },
            "verification": {
                "passed": True,
                "checks": [{"name": "tests", "passed": True, "returncode": 0}],
                "skipped_checks": [],
            },
        }

    def _clean_state(self, workspace: Path) -> dict[str, Any]:
        return {
            "harness": "codex",
            "model": "gpt-5",
            "ok": True,
            "workspace_root": str(workspace),
            "commit_mode": "session",
            "review_completed": True,
            "review_harness": "claude",
            "review_verdict": "approved",
            "repositories": [
                {
                    "alias": "app",
                    "path": "app",
                    "origin": "https://example.invalid/app.git",
                    "resolved_root": str(workspace / "app"),
                }
            ],
            "run_config": {"cwd": str(workspace)},
        }

    def _read_state(self, state_dir: Path) -> dict[str, Any]:
        return json.loads(
            (state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )

    def _write_state(self, state_dir: Path, payload: dict[str, Any]) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _read_result(self, run_dir: Path) -> dict[str, Any]:
        return json.loads(
            (run_dir / "turn-0001-01" / state.RESULT_FILE_NAME).read_text(
                encoding="utf-8"
            )
        )

    def _write_result(self, turn_dir: Path, payload: dict[str, Any]) -> None:
        (turn_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _write_events(
        self, state_dir: Path, records: list[tuple[str, dict[str, Any]]]
    ) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "name": name,
                    "version": events.EVENT_SCHEMA_VERSION,
                    "created_at": f"2026-08-10T10:11:{12 + index:02d}Z",
                    "payload": payload,
                }
            )
            for index, (name, payload) in enumerate(records)
        ]
        (state_dir / events.EVENTS_FILE_NAME).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _make_legacy_plan(self, history: tuple[Path, Path, Path, Path]) -> Path:
        """Rewrite the run as a registry-free legacy plan; return its repository."""
        workspace, plan_dir, state_dir, _ = history
        repository = workspace / "app"
        (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        (plan_dir / "session-01.md").write_text("# Session 1\n", encoding="utf-8")
        (plan_dir / plans.PLAN_METADATA_FILE_NAME).write_text(
            json.dumps({"version": 1, "cwd": str(repository)}), encoding="utf-8"
        )
        payload = self._read_state(state_dir)
        payload.pop("repositories")
        payload.pop("workspace_root")
        payload["commit_mode"] = None
        payload["run_config"]["cwd"] = str(repository)
        self._write_state(state_dir, payload)
        return repository

    def _bundle(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        workspace_root: Path | None = None,
    ) -> supervisor_audit.AuditInput:
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace if workspace_root is None else workspace_root,
        )


class TurnAnomalyClassificationTests(_RunHistoryFixture, unittest.TestCase):
    """A clean harness exit cannot conceal a failed gate or a refused turn."""

    def _turn(self, history: tuple[Path, Path, Path, Path]) -> dict[str, Any]:
        return self._bundle(history).payload["turns"][0]

    def test_a_failed_gate_under_a_zero_exit_is_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, _, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["verification"] = {
                "passed": False,
                "checks": [{"name": "types", "passed": False, "returncode": 1}],
                "skipped_checks": [],
            }
            self._write_result(turn_dir, result)

            turn = self._turn(history)

            self.assertEqual(0, turn["result"]["returncode"])
            self.assertTrue(turn["anomalous"])

    def test_a_turn_the_guard_refused_is_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, state_dir, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["turn_index"] = 1
            result["status_result"] = "complete"
            self._write_result(turn_dir, result)
            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_REJECTED_EVENT,
                        {"run_id": RUN_ID, "turn_index": 1, "reopened": True},
                    ),
                    (
                        events.SESSION_REOPENED_EVENT,
                        {"run_id": RUN_ID, "turn_index": 1},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            turn = self._turn(history)

            self.assertEqual("complete", turn["result"]["status_result"])
            self.assertTrue(turn["anomalous"])
            self.assertTrue(turn["rejected"])

    def test_a_rejection_naming_another_turn_leaves_this_one_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, state_dir, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["turn_index"] = 1
            self._write_result(turn_dir, result)
            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_REJECTED_EVENT,
                        {"run_id": RUN_ID, "turn_index": 7, "reopened": True},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            turn = self._turn(history)

            self.assertFalse(turn["anomalous"])
            self.assertNotIn("rejected", turn)

    def test_a_clean_turn_stays_unremarkable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            turn = self._turn(self._history(Path(tmp)))

            self.assertFalse(turn["anomalous"])
            self.assertNotIn("rejected", turn)


class CommitObservabilityTests(_RunHistoryFixture, unittest.TestCase):
    """A commit the guard saw is projected even when the runner made none."""

    HEAD_BEFORE = "ab503cce84cf73bcaadb3c5e09f395f370ca5bc0"
    HEAD_AFTER = "ede6829cedf2d8e91d1aaf91c0a8cc401dfdbe8d"

    def _worker_committed(
        self, tmp: str, **changes: Any
    ) -> tuple[Path, Path, Path, Path]:
        history = self._history(Path(tmp))
        workspace, _, state_dir, run_dir = history
        state_payload = self._read_state(state_dir)
        state_payload["commit_mode"] = None
        self._write_state(state_dir, state_payload)

        turn_dir = run_dir / "turn-0001-01"
        result = self._clean_result(workspace, turn_dir)
        result["workspace_write_guard"]["changed"] = {
            "app": changes
            or {
                "head_changed": True,
                "head_transition": {
                    "before": self.HEAD_BEFORE,
                    "after": self.HEAD_AFTER,
                    "relation": "fast-forward",
                    "commits_added": 1,
                    "commits_removed": 0,
                    "history_rewrite": False,
                },
            }
        }
        self._write_result(turn_dir, result)
        return history

    def test_a_worker_commit_under_a_null_commit_mode_is_projected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(tmp)

            commits = self._bundle(history).payload["commits"]

            self.assertIsNone(commits["commit_mode"])
            observed = commits["observed_head_transitions"]
            self.assertEqual(1, len(observed))
            self.assertEqual("app", observed[0]["repository"])
            self.assertEqual("turn-0001-01", observed[0]["turn"])
            transition = observed[0]["transition"]
            self.assertEqual("fast-forward", transition["relation"])
            self.assertEqual(1, transition["commits_added"])
            self.assertEqual(self.HEAD_BEFORE, transition["before"])
            self.assertEqual(self.HEAD_AFTER, transition["after"])

            # The move the guard saw is what landed, so the projections a
            # reader reaches for first no longer contradict it.
            self.assertEqual(1, len(commits["attempts"]))
            self.assertEqual(1, len(commits["commits"]))
            self.assertEqual(1, len(commits["commit_groups"]))
            projected = commits["commits"][0]
            self.assertEqual(
                supervisor_audit.OBSERVED_COMMIT_ORIGIN, projected["origin"]
            )
            self.assertEqual("app", projected["repository_alias"])
            self.assertEqual(self.HEAD_BEFORE, projected["head_before"])
            self.assertEqual(self.HEAD_AFTER, projected["head_after"])
            self.assertEqual(self.HEAD_AFTER, projected["commit_sha"])
            attempt = commits["attempts"][0]
            self.assertEqual("turn-0001-01", attempt["name"])
            self.assertEqual("run/turn-0001-01", attempt["reference"])
            self.assertEqual(["app"], commits["commit_groups"][0]["ordered_aliases"])

    def test_a_projected_commit_is_not_a_commit_mode_contradiction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(tmp)

            results = {
                result.check: result
                for result in supervisor_audit.run_deterministic_checks(
                    self._bundle(history)
                )
            }

            # Nobody asked the runner to make this commit, so the commit mode
            # it would have run under is absent for the same reason it is.
            self.assertEqual("pass", results["mutation-commit-guards"].status)
            self.assertIn(
                "1 commit attempt(s)", results["mutation-commit-guards"].detail
            )

    def test_a_recorded_commit_is_not_projected_a_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(tmp)
            _, _, state_dir, _ = history
            state_payload = self._read_state(state_dir)
            state_payload["commits"] = [
                {"repository_alias": "app", "head_after": self.HEAD_AFTER}
            ]
            self._write_state(state_dir, state_payload)

            commits = self._bundle(history).payload["commits"]

            self.assertEqual(1, len(commits["commits"]))
            self.assertNotIn("origin", commits["commits"][0])
            self.assertEqual([], commits["attempts"])

    def test_a_moved_ref_is_projected_even_when_head_stayed_put(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(
                tmp,
                head_changed=False,
                refs_changed=[
                    {
                        "ref": "refs/heads/feature",
                        "before": self.HEAD_BEFORE,
                        "after": self.HEAD_AFTER,
                    }
                ],
            )

            commits = self._bundle(history).payload["commits"]

            self.assertEqual([], commits["observed_head_transitions"])
            self.assertEqual(1, len(commits["commits"]))
            self.assertEqual(
                ["refs/heads/feature"], commits["commits"][0]["refs_changed"]
            )

    def test_a_run_whose_head_never_moved_projects_no_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            commits = self._bundle(self._history(Path(tmp))).payload["commits"]

            self.assertEqual([], commits["observed_head_transitions"])

    def test_a_head_move_without_a_transition_is_projected_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, _, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["workspace_write_guard"]["changed"] = {"app": {"head_changed": True}}
            self._write_result(turn_dir, result)

            observed = self._bundle(history).payload["commits"][
                "observed_head_transitions"
            ]

            self.assertEqual(1, len(observed))
            self.assertIsNone(observed[0]["transition"])


class ReopenedReadinessProjectionTests(_RunHistoryFixture, unittest.TestCase):
    """The re-gate verdict that licenses a grown plan has to reach the audit."""

    def _regated_bundle(self, tmp: str) -> dict[str, Any]:
        history = self._history(Path(tmp))
        payload = self._read_state(history[2])
        payload["readiness_gate"] = {"ok": True, "core_skipped": True, "findings": []}
        payload["readiness_gate_reopened"] = {
            "trigger": events.REVIEW_REPAIR_REOPENED_EVENT,
            "ok": True,
            "total_sessions": 20,
            "pending_sessions": 3,
            "repair_session_ids": ["7.1", "7.2", "7.3"],
            "findings": [],
        }
        self._write_state(history[2], payload)
        return self._bundle(history).payload

    def test_the_reopen_regate_verdict_reaches_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = self._regated_bundle(tmp)["status"]

            gate = status["readiness_gate_reopened"]
            self.assertTrue(gate["ok"])
            self.assertEqual(20, gate["total_sessions"])
            self.assertEqual(["7.1", "7.2", "7.3"], gate["repair_session_ids"])
            # The stale gate stays visible beside it: an auditor that sees only
            # core_skipped cannot tell a re-gated plan from an unauthorized one.
            self.assertTrue(status["readiness_gate"]["core_skipped"])

    def test_a_run_that_never_reopened_projects_no_regate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            self.assertNotIn(
                "readiness_gate_reopened", self._bundle(history).payload["status"]
            )


class ReviewAttestationProjectionTests(_RunHistoryFixture, unittest.TestCase):
    """A review that ran without committing must still be auditable."""

    def test_the_attested_review_reaches_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_requested"] = True
            payload["review_completed"] = False
            payload["review_skipped_reason"] = None
            payload["review_session_executed"] = "11"
            payload["review_verdict"] = {
                "status_line": "Status: 3 findings",
                "outcome": "findings",
                "total_findings": 3,
                "findings_by_severity": {"medium": 3},
            }
            payload["review_output_sha256"] = "a" * 64
            payload["review_artifact_sha256"] = "b" * 64
            self._write_state(history[2], payload)

            section = self._bundle(history).payload["review"]

            self.assertEqual("11", section["review_session_executed"])
            self.assertFalse(section["review_completed"])
            self.assertIsNone(section["review_skipped_reason"])
            self.assertEqual(3, section["review_verdict"]["total_findings"])
            self.assertEqual("a" * 64, section["review_output_sha256"])
            self.assertEqual("b" * 64, section["review_artifact_sha256"])


class ValidatorDiagnosticProjectionTests(_RunHistoryFixture, unittest.TestCase):
    """ASI-105: a rejected review gate must say which requirement failed.

    Its structured reason used to reach the bundle three ways and be lost in
    all three: the inline ``detail`` truncated behind a ~2 KB ``python3 -c``
    argv, the ``stderr`` preview content-stubbed, and the artifact holding the
    full stream named but never indexed as evidence.
    """

    def _rejected_gate(self, history: tuple[Path, Path, Path, Path]) -> dict[str, Any]:
        """Run the real generated gate against a heading-less review artifact.

        The command is taken from the runner's own rendering and stamped by the
        runner's own authenticator, because a diagnostic only reaches a bundle
        for a gate the runner generated.
        """
        _, plan_dir, _, run_dir = history
        review_path = plan_dir / runner.FINAL_REVIEW_FILENAME
        review_path.write_text("no heading anywhere here\n", encoding="utf-8")
        payload = next(
            item
            for item in runner._generated_review_gate_payloads(plan_dir=plan_dir)
            if item["name"] == "final-review-output-evidence-based"
        )
        contract = runner._authenticated_generated_review_contract(
            contracts.CompletionContract(
                session_id=plans.parse_session_id("1"),
                done_when=("reviewed",),
                verify=contracts.VerifyRecipe(
                    checks=(
                        contracts.CommandCheck(
                            name=payload["name"],
                            argv=tuple(payload["argv"]),
                            timeout_seconds=float(payload["timeout_seconds"]),
                            cwd=payload["cwd"],
                        ),
                    )
                ),
            ),
            plan_dir=plan_dir,
        )
        turn_dir = run_dir / "turn-0001-01"
        result = verification.run_check(
            contract.verify.checks[0],
            repo_root=run_dir,
            artifact_dir=turn_dir / "verification",
        )
        self.assertEqual("fail", result.status)
        payload = self._read_result(run_dir)
        payload["verification"] = {
            "passed": False,
            "checks": [verification._check_result_payload(result)],
        }
        self._write_result(turn_dir, payload)
        return verification._check_result_payload(result)

    def test_the_bundle_carries_the_reason_the_streams_cannot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            recorded = self._rejected_gate(history)

            check = self._bundle(history).payload["turns"][0]["result"]["verification"][
                "checks"
            ][0]

            self.assertEqual(
                {
                    "check": "final-review-output-evidence-based",
                    "reason": "missing-required-heading",
                },
                check["failure_diagnostic"],
            )
            # The two surfaces it had to survive: the stream is still withheld
            # as application content, and the detail no longer spends its inline
            # budget on the gate's own kilobytes of argv, so the reason arrives
            # whole instead of at the edge of a truncation.
            self.assertIn(supervisor_audit.REASON_APPLICATION_CONTENT, check["stderr"])
            self.assertIn("missing-required-heading", check["detail"])
            self.assertNotIn("path_re", check["detail"])
            self.assertEqual(recorded["detail"], check["detail"])
            self.assertLess(
                len(check["detail"]), supervisor_audit.MAX_INLINE_STRING_BYTES
            )

    def test_the_failed_gate_stderr_artifact_is_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._rejected_gate(history)

            evidence = self._bundle(history).payload["evidence"]

            entry = next(
                item
                for item in evidence
                if item["locator"].endswith(
                    "final-review-output-evidence-based-stderr.gz"
                )
            )
            self.assertEqual("artifact", entry["kind"])
            self.assertEqual(
                "final-review-output-evidence-based stderr", entry["detail"]
            )

    def test_a_passing_check_indexes_no_stream_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            evidence = self._bundle(history).payload["evidence"]

            self.assertFalse([item for item in evidence if ".gz" in item["locator"]])

    # --- Application-content boundary on failed-command diagnostics ---
    # A rejection reaching the bundle as structure is a promotion, and only
    # ASL's own gates are promoted. Neither what a forger attaches to a copied
    # marker nor what an ordinary failed command prints may cross with it.

    APPLICATION_TEXT = "internal proprietary application text"

    #: Application content shaped exactly like a generated gate's own values.
    APPLICATION_TOKEN = "INTERNAL_PROPRIETARY_TOKEN_ABC123"

    #: A name the runner gives one of its generated review gates.
    GATE = "final-review-artifact-consistent"

    def _recorded_command(
        self,
        history: tuple[Path, Path, Path, Path],
        stderr_line: str,
        *,
        name: str = "user-authored-command",
        generated_gate: bool = False,
    ) -> dict[str, Any]:
        """Record one real failed command and project it through a bundle."""
        _, _, _, run_dir = history
        turn_dir = run_dir / "turn-0001-01"
        result = verification.run_check(
            contracts.CommandCheck(
                name=name,
                argv=(
                    "python3",
                    "-c",
                    f"import sys; sys.stderr.write({stderr_line!r}); sys.exit(1)",
                ),
                timeout_seconds=60.0,
                generated_gate=generated_gate,
            ),
            repo_root=run_dir,
            artifact_dir=turn_dir / "verification",
        )
        payload = self._read_result(run_dir)
        payload["verification"] = {
            "passed": False,
            "checks": [verification._check_result_payload(result)],
        }
        self._write_result(turn_dir, payload)
        return self._bundle(history).payload["turns"][0]["result"]["verification"][
            "checks"
        ][0]

    def _marked_line(self, **fields: Any) -> str:
        """One rejection line carrying the exact public marker literal."""
        return (
            json.dumps(
                {
                    **fields,
                    verification.FAILURE_DIAGNOSTIC_MARKER_KEY: (
                        verification.FAILURE_DIAGNOSTIC_MARKER_VALUE
                    ),
                },
                sort_keys=True,
            )
            + "\n"
        )

    def test_a_gates_extra_field_is_withheld_as_application_content(self) -> None:
        line = self._marked_line(
            check=self.GATE,
            reason="artifact-unparsable",
            customer_payload=self.APPLICATION_TEXT,
        )
        with tempfile.TemporaryDirectory() as tmp:
            check = self._recorded_command(
                self._history(Path(tmp)), line, name=self.GATE, generated_gate=True
            )

            diagnostic = check["failure_diagnostic"]
            self.assertEqual(self.GATE, diagnostic["check"])
            self.assertEqual("artifact-unparsable", diagnostic["reason"])
            self.assertIn(
                supervisor_audit.REASON_APPLICATION_CONTENT,
                diagnostic["customer_payload"],
            )
            self.assertNotIn(self.APPLICATION_TEXT, json.dumps(check))

    def test_a_copied_marker_carries_no_application_token(self) -> None:
        """Copying the public marker buys neither provenance nor a content route.

        The forger's own check is not a gate the runner generates, and the
        token is shaped like a gate's own values, so the shape bound that used
        to guard these two fields would have let it through. What denies it is
        the closed vocabulary, on both the parse side and the collector's.
        """
        cases = {
            "token-in-reason": {
                "check": "user-authored-command",
                "reason": self.APPLICATION_TOKEN,
            },
            "token-in-check": {
                "check": self.APPLICATION_TOKEN,
                "reason": "artifact-missing",
            },
        }
        for label, fields in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                check = self._recorded_command(
                    self._history(Path(tmp)), self._marked_line(**fields)
                )

                self.assertIsNone(check["failure_diagnostic"])
                self.assertEqual("command exited 1: python3", check["detail"])
                self.assertNotIn(self.APPLICATION_TOKEN, json.dumps(check))

    def test_a_generated_gate_name_reaches_the_bundle_as_nothing(self) -> None:
        """The collision the finding describes, projected all the way through.

        A hand-authored command may be named after a generated gate and may
        print that gate's marker and one of its real reasons; only the runner's
        own recipe is a gate, so the bundle carries neither the diagnostic nor
        a diagnostic-led detail.
        """
        line = self._marked_line(check=self.GATE, reason="artifact-missing")
        with tempfile.TemporaryDirectory() as tmp:
            check = self._recorded_command(
                self._history(Path(tmp)), line, name=self.GATE
            )

            self.assertIsNone(check["failure_diagnostic"])
            self.assertEqual("command exited 1: python3", check["detail"])

    def test_the_collector_stubs_a_token_in_either_diagnostic_field(self) -> None:
        """The boundary holds for a diagnostic the collector did not parse.

        ``_Collector.redact`` is the last gate before the bundle and is reached
        by recorded evidence the current parser never saw, so it re-derives the
        vocabulary rather than trusting that promotion already checked it.
        """
        roots = supervisor_audit.EvidenceRoots.build(
            run_dir=Path.cwd(), state_dir=Path.cwd(), plan_dir=Path.cwd()
        )
        for field in ("check", "reason"):
            with self.subTest(field=field):
                diagnostic = {"check": self.GATE, "reason": "artifact-missing"}
                diagnostic[field] = self.APPLICATION_TOKEN

                projected = supervisor_audit._Collector(roots).redact(
                    {"failure_diagnostic": diagnostic}
                )

                self.assertNotIn(self.APPLICATION_TOKEN, json.dumps(projected))
                self.assertIn(
                    supervisor_audit.REASON_APPLICATION_CONTENT,
                    projected["failure_diagnostic"][field],
                )

    def test_the_collector_withholds_a_nested_diagnostic_field(self) -> None:
        """A vocabulary is a set, and a recorded bundle may hold anything.

        The collector reads evidence the current parser never produced, so a
        control-plane field holding a structure has to be withheld rather than
        raising on the membership test that guards it.
        """
        roots = supervisor_audit.EvidenceRoots.build(
            run_dir=Path.cwd(), state_dir=Path.cwd(), plan_dir=Path.cwd()
        )

        projected = supervisor_audit._Collector(roots).redact(
            {
                "failure_diagnostic": {
                    "check": {"nested": self.APPLICATION_TEXT},
                    "reason": ["artifact-missing"],
                }
            }
        )

        self.assertNotIn(self.APPLICATION_TEXT, json.dumps(projected))
        for field in ("check", "reason"):
            self.assertIn(
                supervisor_audit.REASON_APPLICATION_CONTENT,
                projected["failure_diagnostic"][field],
            )

    def test_a_plain_failed_command_leaves_no_text_in_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            check = self._recorded_command(
                self._history(Path(tmp)), f"{self.APPLICATION_TEXT}\n"
            )

            self.assertIsNone(check["failure_diagnostic"])
            self.assertEqual("command exited 1: python3", check["detail"])
            self.assertIn(supervisor_audit.REASON_APPLICATION_CONTENT, check["stderr"])
            self.assertNotIn(self.APPLICATION_TEXT, json.dumps(check))


class DeterministicCheckTests(_RunHistoryFixture, unittest.TestCase):
    """Known failure modes reproduce as findings; a clean run passes every lane."""

    def _audit(
        self, history: tuple[Path, Path, Path, Path], *, workspace_root: object = ...
    ) -> tuple[supervisor_audit.AuditInput, dict[str, DeterministicResult]]:
        workspace, plan_dir, state_dir, run_dir = history
        bundle = supervisor_audit.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace if workspace_root is ... else workspace_root,
        )
        results = supervisor_audit.run_deterministic_checks(bundle)
        return bundle, {result.check: result for result in results}

    def _statuses(self, results: dict[str, DeterministicResult]) -> dict[str, str]:
        return {check: result.status for check, result in results.items()}

    def test_a_clean_run_passes_every_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle, results = self._audit(self._history(Path(tmp)))

            self.assertEqual(
                {check: "pass" for check in results}, self._statuses(results)
            )
            self.assertEqual(
                {lane: "pass" for lane in RUBRIC_LANES},
                supervisor_audit.lane_rollup(results.values()),
            )
            self.assertEqual(
                (), supervisor_audit.findings_from_results(results.values())
            )
            self.assertTrue(bundle.evidence)

    def test_resource_check_distinguishes_recovered_and_unresolved_hangs(self) -> None:
        recovery = {
            "policy": "verified_leaf_sigterm",
            "attempted_at": "2026-08-20T12:00:00+00:00",
            "signal": "SIGTERM",
            "candidate_pids": [5001],
            "signaled_pids": [5001],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }
        block = {
            "reason": "result_received_process_alive",
            "detected_at": "2026-08-20T12:00:00+00:00",
            "recovery": recovery,
        }
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir = history[2]
            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_HANG_SUSPECTED_EVENT,
                        {"run_id": RUN_ID, "hang_suspected": block},
                    ),
                    (
                        events.TURN_HANG_CLEARED_EVENT,
                        {"run_id": RUN_ID, "hang_suspected": block},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _bundle, recovered_results = self._audit(history)
            recovered = recovered_results["resource-anomalies"]
            self.assertEqual("pass", recovered.status)
            assert recovered.detail is not None
            self.assertIn(
                "1 post-result hang(s) recovered automatically", recovered.detail
            )

            self._write_events(
                state_dir,
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.TURN_HANG_SUSPECTED_EVENT,
                        {"run_id": RUN_ID, "hang_suspected": block},
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )
            _bundle, unresolved_results = self._audit(history)
            unresolved = unresolved_results["resource-anomalies"]
            self.assertEqual("fail", unresolved.status)
            assert unresolved.detail is not None
            self.assertIn("suspected hang(s) were never cleared", unresolved.detail)

    def test_legacy_single_repository_pass_is_described_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            repository = history[0] / "app"
            (history[1] / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (history[1] / "session-01.md").rename(history[1] / "session-1.md")
            (history[1] / "session-1.md").write_text("# Session 1\n", encoding="utf-8")
            (history[1] / plans.PLAN_METADATA_FILE_NAME).write_text(
                json.dumps({"version": 1, "cwd": str(repository)}),
                encoding="utf-8",
            )

            _, results = self._audit(history, workspace_root=repository)

            verdict = results["repository-intent"]
            self.assertEqual("pass", verdict.status)
            self.assertIn("legacy plan stays within", verdict.detail)
            self.assertNotIn("declares a repository registry", verdict.detail)

    def test_a_workspace_relative_registry_path_uses_its_resolved_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_payload = self._read_state(history[2])

            self.assertEqual("app", state_payload["repositories"][0]["path"])
            _, results = self._audit(history)

            self.assertEqual("pass", results["root-registry-agreement"].status)

    def test_a_registry_reaching_outside_the_workspace_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            init_git_repo(root / "kiln")
            payload = self._read_state(history[2])
            payload["repositories"].append(
                {
                    "alias": "kiln",
                    "path": "../kiln",
                    "origin": "https://example.invalid/kiln.git",
                    "resolved_root": str(root / "kiln"),
                }
            )
            self._write_state(history[2], payload)

            _, results = self._audit(history)
            finding = supervisor_audit.findings_from_results(results.values())

            self.assertEqual("fail", results["root-registry-agreement"].status)
            self.assertIn("kiln", results["root-registry-agreement"].detail)
            self.assertEqual(
                ["identity.root-registry-agreement"],
                [item.invariant_key for item in finding],
            )
            self.assertEqual("critical", finding[0].severity)
            self.assertEqual(
                "fail",
                supervisor_audit.lane_rollup(results.values())["identity-authority"],
            )

    def test_state_and_events_that_disagree_are_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.RUN_PAUSED_EVENT,
                        {"run_id": RUN_ID, "reason": "verify-failed"},
                    ),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["state-event-agreement"].status)
            self.assertIn("pause", results["state-event-agreement"].detail)
            self.assertIn(
                "observability.state-event-agreement",
                [
                    item.invariant_key
                    for item in supervisor_audit.findings_from_results(results.values())
                ],
            )
            # The pause was never classified, so the lifecycle lane reports what
            # it could not see rather than reading the silence as a clean run.
            self.assertEqual("unavailable", results["retry-resume"].status)

    def test_a_turn_without_a_result_is_never_a_silent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            (history[3] / "turn-0001-01" / state.RESULT_FILE_NAME).unlink()

            _, results = self._audit(history)

            self.assertEqual("fail", results["terminal-artifacts"].status)
            self.assertIn(state.RESULT_FILE_NAME, results["terminal-artifacts"].detail)
            self.assertEqual("unavailable", results["mutation-commit-guards"].status)
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertNotIn(
                "pass",
                {
                    results["terminal-artifacts"].status,
                    results["mutation-commit-guards"].status,
                    results["scope-escape"].status,
                },
            )

    def test_a_harness_reviewing_its_own_work_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_harness"] = payload["harness"]
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["review-independence"].status)
            self.assertIn("its own work", results["review-independence"].detail)
            self.assertEqual(
                "fail",
                supervisor_audit.lane_rollup(results.values())[
                    "verification-review-independence"
                ],
            )

    def test_an_unnamed_implementation_harness_is_not_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload.pop("harness")
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["review-independence"].status)
            self.assertIn("did the work", results["review-independence"].detail)

    def test_a_registered_turn_without_a_write_guard_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            del payload["workspace_write_guard"]
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["mutation-commit-guards"].status)
            self.assertIn("write-guard", results["mutation-commit-guards"].detail)
            self.assertEqual("unavailable", results["scope-escape"].status)

    def test_repair_commit_mode_authorizes_attempt_under_null_ordinary_mode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_payload = self._read_state(history[2])
            state_payload["commit_mode"] = None
            state_payload["repair_commit_mode"] = "per-child"
            self._write_state(history[2], state_payload)
            commit_dir = history[3] / "commit-0001-session-01-app"
            commit_dir.mkdir()
            (commit_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(
                    {
                        "success": True,
                        "returncode": 0,
                        "repository_alias": "app",
                        "session_id": "01",
                    }
                ),
                encoding="utf-8",
            )

            _, results = self._audit(history)

            verdict = results["mutation-commit-guards"]
            self.assertEqual("pass", verdict.status, verdict.detail)

    def test_null_modes_do_not_authorize_commit_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_payload = self._read_state(history[2])
            state_payload["commit_mode"] = None
            state_payload["repair_commit_mode"] = None
            self._write_state(history[2], state_payload)
            commit_dir = history[3] / "commit-0001-session-01-app"
            commit_dir.mkdir()
            (commit_dir / state.RESULT_FILE_NAME).write_text(
                json.dumps(
                    {
                        "success": True,
                        "returncode": 0,
                        "repository_alias": "app",
                        "session_id": "01",
                    }
                ),
                encoding="utf-8",
            )

            _, results = self._audit(history)

            verdict = results["mutation-commit-guards"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("without recording the commit mode", verdict.detail)

    def test_a_legacy_implementation_turn_without_a_guard_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            repository = history[0] / "app"
            (history[1] / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (history[1] / "session-01.md").write_text("# Session 1\n", encoding="utf-8")
            (history[1] / plans.PLAN_METADATA_FILE_NAME).write_text(
                json.dumps({"version": 1, "cwd": str(repository)}),
                encoding="utf-8",
            )
            state_payload = self._read_state(history[2])
            state_payload.pop("repositories")
            state_payload.pop("workspace_root")
            state_payload["commit_mode"] = None
            state_payload["run_config"]["cwd"] = str(repository)
            self._write_state(history[2], state_payload)
            result_payload = self._read_result(history[3])
            del result_payload["workspace_write_guard"]
            self._write_result(history[3] / "turn-0001-01", result_payload)

            _, results = self._audit(history, workspace_root=repository)

            self.assertEqual("fail", results["mutation-commit-guards"].status)
            self.assertIn(
                "implementation work without a write-guard",
                results["mutation-commit-guards"].detail,
            )

    def test_a_worker_head_move_without_transition_evidence_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["workspace_write_guard"]["changed"] = {
                "app": {
                    "head_changed": True,
                    "head_before": "a" * 40,
                    "head_after": "b" * 40,
                }
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["mutation-commit-guards"].status)
            self.assertIn(
                "history transition", results["mutation-commit-guards"].detail
            )

    def test_a_recorded_worker_history_rewrite_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["workspace_write_guard"]["changed"] = {
                "app": {
                    "head_changed": True,
                    "head_before": "a" * 40,
                    "head_after": "b" * 40,
                    "head_transition": {
                        "before": "a" * 40,
                        "after": "b" * 40,
                        "relation": "rewrite",
                        "commits_added": 3,
                        "commits_removed": 3,
                        "history_rewrite": True,
                    },
                }
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            verdict = results["mutation-commit-guards"]
            self.assertEqual("pass", verdict.status)
            self.assertIn("1 worker HEAD move(s)", verdict.detail)
            self.assertIn("1 history rewrite(s)", verdict.detail)

    def test_a_widened_write_scope_with_a_passing_repair_is_contained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": RUN_ID,
                            "details": {
                                "session_id": "01",
                                "workspace_write_guard": {
                                    "passed": True,
                                    "write_aliases": ["app"],
                                    "authorized_aliases": ["app"],
                                    "violations": [],
                                    "unavailable": {},
                                },
                            },
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["scope-escape"].status)
            self.assertIn(
                "2 recorded write-guard and repair results",
                results["scope-escape"].detail,
            )

    def test_a_scope_repair_without_a_passing_guard_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": RUN_ID,
                            "details": {
                                "session_id": "01",
                                "workspace_write_guard": {},
                            },
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertIn(
                "did not record a passing guard", results["scope-escape"].detail
            )

    def _violating_guard(
        self, *, reverted: bool, attributed: bool = True
    ) -> dict[str, Any]:
        """The guard record the runner writes when a session exceeded its writes."""
        guard = {
            "passed": False,
            "session_id": "01",
            "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            "phase": "after",
            "reverted_to_pending": reverted,
            "write_aliases": ["app"],
            "authorized_aliases": ["app"],
            "violations": [{"alias": "docs", "repository": "docs"}],
            "unavailable": {},
        }
        if not attributed:
            del guard["session_id"]
        return guard

    def _scope_violation(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        reverted: bool = True,
        paused: bool = True,
        attributed: bool = True,
        pause_session: int | None = 1,
    ) -> None:
        """Record one out-of-scope write the way a real run records it.

        The same guard record reaches the turn result and the pause it raised,
        which is what the analysis has to collapse back into one write.
        ``attributed`` drops the session id from the guard and the turn result,
        leaving ``pause_session`` as the only record that can name the
        offender — or, at ``None``, leaving the violation unattributable.
        """
        guard = self._violating_guard(reverted=reverted, attributed=attributed)
        payload = self._read_result(history[3])
        payload["workspace_write_guard"] = guard
        if attributed:
            payload["session_id"] = "01"
        self._write_result(history[3] / "turn-0001-01", payload)
        details: dict[str, Any] = {"workspace_write_guard": guard}
        if pause_session is not None:
            details["session"] = pause_session
        records: list[tuple[str, dict[str, Any]]] = [
            ("run_started", {"run_id": RUN_ID})
        ]
        if paused:
            records.append(
                (
                    events.RUN_PAUSED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                        "details": details,
                    },
                )
            )
        else:
            records.append((events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}))
        self._write_events(history[2], records)

    def _write_commit(self, run_dir: Path, *, session_id: str) -> None:
        commit_dir = run_dir / "commit-0001-session-01-app"
        commit_dir.mkdir()
        self._write_result(
            commit_dir,
            {
                "success": True,
                "returncode": 0,
                "repository_alias": "app",
                "session_id": session_id,
            },
        )

    def test_a_write_the_guard_caught_is_medium_and_never_a_containment_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)

            _, results = self._audit(history)

            # A working guard is what produced this evidence, so the critical
            # containment check has nothing to report.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertIn(
                "none of them reached the tree", results["scope-escape"].detail
            )
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn("session 01", results["scope-guard-catch"].detail)
            self.assertIn("docs", results["scope-guard-catch"].detail)
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit.findings_from_results(results.values())
            }
            self.assertNotIn("packet.write-scope-containment", findings)
            caught = findings["packet.write-scope-guard-catch"]
            self.assertEqual("medium", caught.severity)
            self.assertEqual("task-packet-coherence", caught.lane)
            self.assertIn("durable write scope", caught.recommendation)
            self.assertIn("expansion and preflight", caught.recommendation)
            # Nothing is masked: the two checks share a lane and the rollup
            # keeps the worse of them.
            self.assertEqual(
                "fail",
                supervisor_audit.lane_rollup(results.values())["task-packet-coherence"],
            )

    def test_one_caught_write_is_reported_once_across_turn_and_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)

            _, results = self._audit(history)

            self.assertEqual(
                1, results["scope-guard-catch"].detail.count("wrote outside")
            )

    def test_a_violation_the_run_never_reverted_still_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, reverted=False)

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("not reverted to pending", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit.findings_from_results(results.values())
            }
            self.assertNotIn("packet.write-scope-guard-catch", findings)
            self.assertEqual(
                "critical", findings["packet.write-scope-containment"].severity
            )

    def test_a_violation_the_run_never_paused_on_still_escaped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, paused=False)

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn(
                "never paused on the violation", results["scope-escape"].detail
            )
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_a_commit_for_the_offending_session_undoes_the_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._write_commit(history[3], session_id="01")

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("a commit for that session", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_a_commit_for_another_session_leaves_the_catch_standing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history)
            self._write_commit(history[3], session_id="02")

            _, results = self._audit(history)

            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)

    def test_unnamed_scope_violation_without_commits_is_still_a_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False, pause_session=None)

            _, results = self._audit(history)

            # Nothing was committed, so "before anything was committed" is a
            # claim the bundle actually supports even with no session named.
            self.assertEqual("pass", results["scope-escape"].status)
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn("an unnamed session", results["scope-guard-catch"].detail)
            self.assertIn(
                "before anything was committed", results["scope-guard-catch"].detail
            )

    def test_unnamed_scope_violation_with_any_commit_is_not_reported_as_caught(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False, pause_session=None)
            # No session to match this commit against, so it cannot be excused
            # the way another session's commit is when the offender is named.
            self._write_commit(history[3], session_id="02")

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("cannot be ruled out", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            self.assertNotIn(
                "before anything was committed", results["scope-guard-catch"].detail
            )
            findings = {
                finding.invariant_key: finding
                for finding in supervisor_audit.findings_from_results(results.values())
            }
            self.assertNotIn("packet.write-scope-guard-catch", findings)
            self.assertEqual(
                "critical", findings["packet.write-scope-containment"].severity
            )

    def test_a_violation_named_only_by_its_pause_is_attributed_to_that_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False)

            _, results = self._audit(history)

            # The turn half is unnamed and the pause half names session 1;
            # correlating them keeps one write one claim instead of reporting
            # it twice, once as an unnamed ambiguity.
            self.assertEqual("fail", results["scope-guard-catch"].status)
            self.assertIn("session 1", results["scope-guard-catch"].detail)
            self.assertNotIn("an unnamed session", results["scope-guard-catch"].detail)
            self.assertEqual(
                1, results["scope-guard-catch"].detail.count("wrote outside")
            )
            self.assertEqual("pass", results["scope-escape"].status)

    def test_a_commit_for_the_pause_named_session_undoes_the_correlated_catch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._scope_violation(history, attributed=False)
            self._write_commit(history[3], session_id="1")

            _, results = self._audit(history)

            # Correlation is real attribution, so the commit is matched by
            # session rather than swept in as an unresolvable ambiguity.
            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn("a commit for that session", results["scope-escape"].detail)
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_a_repair_that_still_lists_violations_escaped_rather_than_caught(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                        {
                            "run_id": RUN_ID,
                            "details": {
                                "session_id": "01",
                                "workspace_write_guard": {
                                    "passed": False,
                                    "write_aliases": ["app"],
                                    "authorized_aliases": ["app"],
                                    "violations": [{"alias": "docs"}],
                                    "unavailable": {},
                                },
                            },
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["scope-escape"].status)
            self.assertIn(
                "still excludes retained changes", results["scope-escape"].detail
            )
            self.assertEqual("pass", results["scope-guard-catch"].status)

    def test_a_guard_that_could_not_look_is_never_read_as_a_catch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["workspace_write_guard"] = {
                "passed": False,
                "session_id": "01",
                "write_aliases": ["app"],
                "authorized_aliases": ["app"],
                "violations": [],
                "unavailable": {"app": {"reason": "fingerprint-failed"}},
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            # Blindness stays on the critical check; the catch check owns only
            # writes the guard demonstrably held.
            self.assertEqual("unavailable", results["scope-escape"].status)
            self.assertEqual("pass", results["scope-guard-catch"].status)
            self.assertEqual(
                "unavailable",
                supervisor_audit.lane_rollup(
                    [results["scope-escape"], results["scope-guard-catch"]]
                )["task-packet-coherence"],
            )

    def test_write_scope_severity_lives_on_the_spec_not_on_the_verdict(self) -> None:
        specs = {
            spec.invariant_key: spec
            for spec in supervisor_audit.DETERMINISTIC_CHECK_SPECS
        }
        containment = specs["packet.write-scope-containment"]
        catch = specs["packet.write-scope-guard-catch"]

        self.assertEqual("critical", containment.severity)
        self.assertEqual("medium", catch.severity)
        self.assertEqual("task-packet-coherence", containment.lane)
        self.assertEqual(containment.lane, catch.lane)
        self.assertNotEqual(containment.component, catch.component)
        # A severity on the verdict would let one issue key report critical on
        # one occurrence and medium on the next, which is what corrupts the
        # inbox's dedup history for that key.
        self.assertNotIn(
            "severity",
            {
                field.name
                for field in dataclasses.fields(supervisor_audit._Verdict)  # noqa: SLF001
            },
        )

    def test_both_write_scope_checks_share_one_analysis_helper(self) -> None:
        module = ast.parse(Path(supervisor_audit.__file__).read_text(encoding="utf-8"))
        called = {
            node.name: {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            for node in module.body
            if isinstance(node, ast.FunctionDef)
        }

        self.assertIn("_analyze_write_scope", called["_check_scope_escape"])
        self.assertIn("_analyze_write_scope", called["_check_scope_guard_catch"])

    def test_success_claimed_over_failed_verification_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": False,
                "checks": [{"name": "tests", "passed": False, "returncode": 1}],
                "skipped_checks": ["lint"],
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)
            findings = supervisor_audit.findings_from_results(results.values())

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn("verification failed", results["misleading-success"].detail)
            self.assertIn("skipped", results["misleading-success"].detail)
            self.assertEqual(
                ["observability.success-is-earned"],
                [item.invariant_key for item in findings],
            )

    def test_every_recorded_verification_failure_is_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "role": verification.CHECK_ROLE_DIAGNOSTIC,
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 2,
                            "passed_count": 1,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["unclassified-anomalies"].status)
            self.assertEqual("pass", results["verification-outcomes"].status)
            self.assertIn("accounted for", results["verification-outcomes"].detail)

    REPAIR_CYCLE = {
        "cycle": 1,
        "review_session_id": "2",
        "outcome": "findings",
        "disposition": "repairs-materialized",
    }
    ALL_CLEAR_CYCLE = {
        "cycle": 2,
        "review_session_id": "3",
        "outcome": "all-clear",
        "disposition": "all-clear",
    }

    def _defer_broad_failure(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        review_cycles: list[dict[str, Any]],
        deferred_failure_names: list[str] | None = None,
        paused: bool = False,
    ) -> None:
        """Record a turn whose broad-check failure was deferred to review."""
        result = self._read_result(history[3])
        result.update(session_id="2", turn_index=1, status_result="complete")
        failure = {
            "name": "broad:1:tests",
            "kind": "command",
            "status": "fail",
            "returncode": 1,
            "failure_reason": "command-failed",
        }
        names = (
            ["broad:1:tests"]
            if deferred_failure_names is None
            else deferred_failure_names
        )
        result["verification"] = {
            "session": "2",
            "passed": False,
            "checks": [failure],
            "skipped_checks": [],
            "diagnostic_failures": [],
            "acceptance": {
                "accepted": True,
                "reason": runner.REVIEW_BROAD_FAILURES_DEFERRED_REASON,
                "review_outcome": "findings",
                "deferred_failure_count": len(names),
                "deferred_failure_names": names,
            },
            "deferred_failures": [failure],
        }
        self._write_result(history[3] / "turn-0001-01", result)
        state_payload = self._read_state(history[2])
        state_payload["review_cycles"] = review_cycles
        if paused:
            state_payload["ok"] = False
            state_payload["paused"] = True
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.RUN_PAUSED_EVENT,
                        {"run_id": RUN_ID, "reason": "verify-failed"},
                    ),
                ],
            )
        self._write_state(history[2], state_payload)

    def test_deferred_broad_failure_is_accounted_only_after_later_all_clear(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history, review_cycles=[self.REPAIR_CYCLE, self.ALL_CLEAR_CYCLE]
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["verification-outcomes"].status)
            self.assertEqual("pass", results["misleading-success"].status)

    def test_deferred_broad_failure_without_later_all_clear_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(history, review_cycles=[self.REPAIR_CYCLE])

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn("later all-clear", results["verification-outcomes"].detail)
            self.assertEqual("fail", results["misleading-success"].status)

    def test_a_deferral_the_run_paused_before_clearing_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history, review_cycles=[self.REPAIR_CYCLE], paused=True
            )

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["verification-outcomes"].status)
            self.assertIn(
                "reached no all-clear review before the run stopped",
                results["verification-outcomes"].detail,
            )

    def test_a_paused_deferral_is_unavailable_without_a_repair_cycle(self) -> None:
        """The pending branch cannot key off a repairs-materialized source."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history,
                review_cycles=[
                    {**self.REPAIR_CYCLE, "disposition": "malformed-review-repair"}
                ],
                paused=True,
            )

            _, results = self._audit(history)

            self.assertEqual("unavailable", results["verification-outcomes"].status)

    def test_a_paused_run_still_fails_a_deferral_naming_checks_that_passed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._defer_broad_failure(
                history,
                review_cycles=[self.REPAIR_CYCLE],
                deferred_failure_names=["broad:1:docs"],
                paused=True,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)

    def test_review_findings_a_later_all_clear_closed_do_not_contradict_success(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_verdict"] = {"outcome": "findings", "total_findings": 2}
            payload["review_cycles"] = [self.REPAIR_CYCLE, self.ALL_CLEAR_CYCLE]
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("pass", results["misleading-success"].status)

    def test_review_findings_no_all_clear_closed_contradict_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_state(history[2])
            payload["review_verdict"] = {"outcome": "findings", "total_findings": 2}
            payload["review_cycles"] = [self.REPAIR_CYCLE]
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "reports 2 finding(s) and no later all-clear review closed them",
                results["misleading-success"].detail,
            )

    def test_a_skipped_diagnostic_cannot_absorb_a_failed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "role": verification.CHECK_ROLE_GATE,
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_CHECK_SKIPPED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "on_failure_of": ["lint"],
                            "reason": verification.SKIP_REASON_CONDITION_NOT_MET,
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 1,
                            "passed_count": 0,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 1,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn(
                "reported a failure against a gate",
                results["verification-outcomes"].detail,
            )

    def test_a_passing_diagnostic_cannot_absorb_a_failed_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "role": verification.CHECK_ROLE_GATE,
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "role": verification.CHECK_ROLE_DIAGNOSTIC,
                            "status": "pass",
                            "returncode": 0,
                            "failure_reason": None,
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 2,
                            "passed_count": 1,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn(
                "reported a failure against a gate",
                results["verification-outcomes"].detail,
            )

    def test_a_skipped_diagnostic_absorbs_nothing_without_recorded_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 1,
                            "passed_count": 0,
                            "gate_count": 1,
                            "diagnostic_count": 1,
                            "skipped_count": 1,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn("unidentified role", results["verification-outcomes"].detail)

    def test_a_verification_that_passed_over_a_failed_gate_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "status": "fail",
                            "returncode": 1,
                            "failure_reason": "command-failed",
                        },
                    ),
                    (
                        events.VERIFICATION_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "passed": True,
                            "executed_count": 1,
                            "passed_count": 0,
                            "gate_count": 1,
                            "diagnostic_count": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)
            findings = supervisor_audit.findings_from_results(results.values())

            self.assertEqual("pass", results["unclassified-anomalies"].status)
            self.assertEqual("fail", results["verification-outcomes"].status)
            self.assertIn(
                "verification.failure-accounting",
                [item.invariant_key for item in findings],
            )

    def _write_retry_pair(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        first: dict[str, Any] | None = None,
    ) -> None:
        """One session attempted twice: a bad turn, then a clean completion."""

        workspace, _, _, run_dir = history
        failed = self._read_result(run_dir)
        failed.update({"session_id": "01", "turn_index": 1, "status_result": "blocked"})
        failed.update(first or {})
        self._write_result(run_dir / "turn-0001-01", failed)

        retry_dir = run_dir / "turn-0002-01"
        retry_dir.mkdir()
        retry = self._clean_result(workspace, retry_dir)
        retry.update({"session_id": "01", "turn_index": 2, "status_result": "complete"})
        self._write_result(retry_dir, retry)
        (retry_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
        (retry_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
        (retry_dir / "last-message.txt").write_text("Session done.\n", encoding="utf-8")

    def test_a_retried_turn_does_not_contradict_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_retry_pair(history)

            bundle, results = self._audit(history)
            findings = supervisor_audit.findings_from_results(results.values())

            self.assertTrue(bundle.payload["turns"][0]["anomalous"])
            self.assertEqual("pass", results["misleading-success"].status)
            self.assertNotIn(
                "observability.success-is-earned",
                [item.invariant_key for item in findings],
            )

    def test_an_unretried_anomalous_turn_still_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload.update(
                {"session_id": "01", "turn_index": 1, "status_result": "blocked"}
            )
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "recorded an anomalous result", results["misleading-success"].detail
            )

    def test_a_retry_does_not_excuse_a_write_scope_breach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_retry_pair(
                history,
                first={
                    "workspace_write_guard": {
                        "passed": False,
                        "write_aliases": ["app", "other"],
                        "authorized_aliases": ["app"],
                        "violations": [{"alias": "other"}],
                        "unavailable": {},
                    }
                },
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "exceeded its declared write scope",
                results["misleading-success"].detail,
            )

    def test_an_expected_conditional_skip_does_not_contradict_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": True,
                "recipe": {"check_count": 2, "gate_count": 1, "diagnostic_count": 1},
                "checks": [{"name": "tests", "status": "pass", "returncode": 0}],
                "skipped_checks": [
                    {
                        "name": "diagnose",
                        "kind": "command",
                        "on_failure_of": ["tests"],
                        "reason": verification.SKIP_REASON_CONDITION_NOT_MET,
                    }
                ],
            }
            self._write_result(history[3] / "turn-0001-01", payload)
            self._write_events(
                history[2],
                [
                    ("run_started", {"run_id": RUN_ID}),
                    (
                        events.VERIFICATION_CHECK_SKIPPED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "diagnose",
                            "kind": "command",
                            "on_failure_of": ["tests"],
                            "reason": verification.SKIP_REASON_CONDITION_NOT_MET,
                        },
                    ),
                    (
                        events.VERIFICATION_CHECK_FINISHED_EVENT,
                        {
                            "run_id": RUN_ID,
                            "turn_index": 1,
                            "name": "tests",
                            "kind": "command",
                            "status": "pass",
                            "returncode": 0,
                        },
                    ),
                    (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
                ],
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["misleading-success"].status)
            self.assertEqual(
                (), supervisor_audit.findings_from_results(results.values())
            )

    def test_a_skipped_required_check_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": True,
                "recipe": {"check_count": 2, "gate_count": 2, "diagnostic_count": 0},
                "checks": [{"name": "tests", "status": "pass", "returncode": 0}],
                "skipped_checks": [
                    {
                        "name": "lint",
                        "kind": "command",
                        "on_failure_of": [],
                        "reason": "executable-not-found",
                    }
                ],
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "without a condition that explains the skip",
                results["misleading-success"].detail,
            )

    def test_a_verification_that_gated_nothing_contradicts_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            payload = self._read_result(history[3])
            payload["verification"] = {
                "passed": True,
                "recipe": {"check_count": 1, "gate_count": 0, "diagnostic_count": 1},
                "checks": [],
                "skipped_checks": [
                    {
                        "name": "diagnose",
                        "kind": "command",
                        "on_failure_of": ["tests"],
                        "reason": verification.SKIP_REASON_CONDITION_NOT_MET,
                    }
                ],
            }
            self._write_result(history[3] / "turn-0001-01", payload)

            _, results = self._audit(history)

            self.assertEqual("fail", results["misleading-success"].status)
            self.assertIn(
                "nothing gated the changes", results["misleading-success"].detail
            )

    def _paused_on_harness_failure(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        reason: str = events.PAUSE_REASON_TRANSPORT_FAILED,
        classification: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        session: Any = 12,
        also_failed: tuple[Any, ...] = (),
        also_recorded: tuple[tuple[str, dict[str, Any]], ...] = (),
        paused: bool = True,
    ) -> None:
        """Rewrite the run as one that paused mid-session on a harness failure.

        ``also_failed`` names further sessions whose turn failed and which the
        pause says nothing about; ``also_recorded`` adds arbitrary records
        before the pause. ``paused`` false leaves the classification behind on
        a run that is no longer paused.
        """

        state_dir = history[2]
        payload = self._read_state(state_dir)
        payload["ok"] = False
        payload["paused"] = paused
        payload["pause_reason"] = reason
        payload["pause_details"] = {
            "session": session,
            "returncode": 1,
            "failure_classification": {
                "reason": reason,
                "fingerprint": f"{reason}:model-capacity",
                "retryable": reason in events.RETRYABLE_HARNESS_FAILURE_REASONS,
                "retry_at": None,
                "returncode": 1,
                **(classification or {}),
            },
        }
        recorded = dict(pause_policy.classify_pause(reason))
        recorded.update(policy or {})
        payload["pause_policy"] = recorded
        self._write_state(state_dir, payload)
        self._write_events(
            state_dir,
            [("run_started", {"run_id": RUN_ID})]
            + [
                (
                    events.TURN_EXECUTION_FINISHED_EVENT,
                    {"run_id": RUN_ID, "session": failed, "returncode": 1},
                )
                for failed in (session, *also_failed)
            ]
            + list(also_recorded)
            + [(events.RUN_PAUSED_EVENT, {"run_id": RUN_ID, "reason": reason})],
        )

    def test_a_retryable_pause_accounts_for_the_turn_it_failed_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history)

            _, results = self._audit(history)

            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertIn(
                "transport-failed:model-capacity",
                results["retryable-transport-failure"].detail,
            )
            # The turn the run paused on is explained by the pause, so it must
            # not fall through to the catch-all that reports unknown failures.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_another_sessions_failure_is_not_excused_by_the_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history, also_failed=(9,))

            _, results = self._audit(history)

            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.TURN_EXECUTION_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_both_records_for_the_paused_turn_are_accounted_for(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history,
                also_recorded=(
                    (
                        events.TURN_FINISHED_EVENT,
                        {"run_id": RUN_ID, "session": 12, "returncode": 1},
                    ),
                ),
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_non_turn_failure_is_not_excused_by_the_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history,
                also_recorded=(
                    (
                        events.COMMIT_GROUP_FINISHED_EVENT,
                        {"run_id": RUN_ID, "session": 12, "ok": False},
                    ),
                ),
            )

            _, results = self._audit(history)

            # The pause explains one failed turn, not everything the same
            # session touched on its way there.
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.COMMIT_GROUP_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def _recorded_commit(
        self, history: tuple[Path, Path, Path, Path], **payload: Any
    ) -> None:
        """Record one commit before the run's terminal record.

        Shaped like ``runner._run_spec_commit``'s payload, which writes
        ``success`` and ``status_result`` and no ``ok`` field. A helper that adds
        one grades a record no run emits, and hides that every commit failure but
        ``nonzero-exit`` is reached at exit zero.
        """
        state_dir = history[2]
        run_dir = history[3]
        commit_dir = run_dir / "commit-0001"
        commit_dir.mkdir(exist_ok=True)
        self._write_events(
            state_dir,
            [
                ("run_started", {"run_id": RUN_ID}),
                (
                    events.COMMIT_FINISHED_EVENT,
                    {
                        "run_id": RUN_ID,
                        "commit_index": 1,
                        "artifact_dir": str(commit_dir),
                        **payload,
                    },
                ),
                (events.RUN_FINISHED_EVENT, {"run_id": RUN_ID, "ok": True}),
            ],
        )

    def test_a_failed_commit_is_read_from_the_account_it_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            commit_dir = history[3] / "commit-0001"
            self._recorded_commit(
                history,
                success=False,
                status_result="nonzero-exit",
                returncode=1,
                stderr_path=str(commit_dir / "stderr.log"),
                stderr_chars=64,
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-failure-accounting"].status)
            self.assertIn(
                "1 of 1 recorded commit(s) failed",
                results["commit-failure-accounting"].detail,
            )
            # The exemption in the catch-all covers exactly what the check above
            # graded, so a commit failure the run explained is not also reported
            # as one nothing classified.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_zero_exit_commit_failure_is_graded_like_any_other(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            # Every commit outcome but nonzero-exit is reached on a clean exit,
            # so a check that selects on the exit code alone reports the run's
            # only failed commit as no failure at all.
            self._recorded_commit(
                history,
                success=False,
                status_result="unchanged-head",
                returncode=0,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "failed without recording why",
                results["commit-failure-accounting"].detail,
            )

    def test_a_zero_exit_commit_failure_that_accounted_for_itself_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            commit_dir = history[3] / "commit-0001"
            self._recorded_commit(
                history,
                success=False,
                status_result="dirty-after-commit",
                returncode=0,
                stderr_path=str(commit_dir / "stderr.log"),
                stderr_chars=32,
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-failure-accounting"].status)
            self.assertIn(
                "1 of 1 recorded commit(s) failed",
                results["commit-failure-accounting"].detail,
            )
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_successful_commit_is_not_read_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(
                history,
                success=True,
                status_result="success",
                returncode=0,
            )

            _, results = self._audit(history)

            self.assertEqual("pass", results["commit-failure-accounting"].status)
            self.assertIn(
                "0 of 1 recorded commit(s) failed",
                results["commit-failure-accounting"].detail,
            )

    def test_a_failed_commit_that_classified_nothing_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(history, success=False, returncode=1)

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "failed without classifying the failure",
                results["commit-failure-accounting"].detail,
            )

    def test_a_commit_outcome_contradicting_its_exit_code_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(
                history,
                success=False,
                status_result="unchanged-head",
                returncode=1,
                status_error="HEAD did not move",
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "recorded unchanged-head against exit 1",
                results["commit-failure-accounting"].detail,
            )

    def test_a_commit_outcome_asl_never_writes_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._recorded_commit(
                history,
                success=False,
                status_result="exploded",
                returncode=1,
                status_error="something else entirely",
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "which is no commit outcome ASL writes",
                results["commit-failure-accounting"].detail,
            )

    def test_a_failed_commit_that_recorded_no_reason_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            commit_dir = history[3] / "commit-0001"
            self._recorded_commit(
                history,
                success=False,
                status_result="nonzero-exit",
                returncode=1,
                # An empty stream is a stream nobody can read a reason out of.
                stderr_path=str(commit_dir / "stderr.log"),
                stderr_chars=0,
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["commit-failure-accounting"].status)
            self.assertIn(
                "failed without recording why",
                results["commit-failure-accounting"].detail,
            )

    def test_a_final_failure_is_accounted_for_by_its_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history, reason="harness-missing")

            _, results = self._audit(history)

            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertIn(
                "non-retryable", results["retryable-transport-failure"].detail
            )
            # Nothing will relaunch this session, but the run still named why
            # the turn failed, so it is not a failure nothing classified.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_classification_the_run_is_no_longer_paused_on_explains_nothing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history, paused=False)

            _, results = self._audit(history)

            # A run that resumed past its pause carries the old classification
            # in state; reading it would excuse a turn nothing is holding.
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertEqual("pass", results["retryable-transport-failure"].status)
            self.assertIn(
                "did not pause on a harness failure",
                results["retryable-transport-failure"].detail,
            )

    def test_a_pause_that_named_no_reason_explains_no_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history, classification={"reason": None, "retryable": None}
            )

            _, results = self._audit(history)

            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.TURN_EXECUTION_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_a_retryable_failure_nothing_will_relaunch_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history,
                policy={
                    "recoverable": False,
                    "requires_user": True,
                    "max_retries": 0,
                },
            )

            _, results = self._audit(history)
            findings = supervisor_audit.findings_from_results(results.values())

            verdict = results["retryable-transport-failure"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("nothing will relaunch it", verdict.detail)
            self.assertIn("waits on a person no reply can help", verdict.detail)
            self.assertIn("grants max_retries=0", verdict.detail)
            self.assertIn(
                "lifecycle.retryable-transport-failure",
                [item.invariant_key for item in findings],
            )
            self.assertEqual(
                "fail",
                supervisor_audit.lane_rollup(results.values())[
                    "lifecycle-retry-resume"
                ],
            )

    def test_a_retryable_flag_the_reason_denies_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(
                history, classification={"reason": "harness-missing"}
            )

            _, results = self._audit(history)

            verdict = results["retryable-transport-failure"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("retryable=True", verdict.detail)
            self.assertIn("not one the classifier marks retryable", verdict.detail)

    def test_a_classification_with_no_policy_cannot_be_judged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._paused_on_harness_failure(history)
            payload = self._read_state(history[2])
            payload["pause_policy"] = {}
            self._write_state(history[2], payload)

            _, results = self._audit(history)

            verdict = results["retryable-transport-failure"]
            self.assertEqual("unavailable", verdict.status)
            self.assertIn("recorded no pause policy", verdict.detail)

    def _aborted_a_turn(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        session: Any = 9,
        turn_index: int = 4,
        reason: str | None = "Operator requested immediate stop",
        requested: bool = True,
        requested_turn_index: int | None = None,
        also_failed: tuple[tuple[Any, int], ...] = (),
    ) -> None:
        """Rewrite the run as one whose turn an operator killed mid-session.

        ``requested`` false drops the request record the honored abort should
        trace back to; ``requested_turn_index`` points the request at another
        turn. ``also_failed`` names further session/turn pairs that failed and
        which no abort accounts for.
        """

        state_dir = history[2]
        payload = self._read_state(state_dir)
        payload["ok"] = False
        payload["reason"] = "abandoned"
        self._write_state(state_dir, payload)
        control = {
            "run_id": RUN_ID,
            "session": session,
            "abort_reason": reason,
            "abort_requested": True,
        }
        records: list[tuple[str, dict[str, Any]]] = [
            ("run_started", {"run_id": RUN_ID})
        ]
        if requested:
            records.append(
                (
                    events.RUN_CONTROL_EVENT,
                    {
                        **control,
                        "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                        "turn_index": (
                            turn_index
                            if requested_turn_index is None
                            else requested_turn_index
                        ),
                    },
                )
            )
        records.append(
            (
                events.RUN_CONTROL_EVENT,
                {
                    **control,
                    "action": events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
                    "turn_index": turn_index,
                },
            )
        )
        records.extend(
            (
                events.TURN_FINISHED_EVENT,
                {
                    "run_id": RUN_ID,
                    "session": failed_session,
                    "turn_index": failed_turn,
                    "returncode": 137,
                },
            )
            for failed_session, failed_turn in ((session, turn_index), *also_failed)
        )
        records.append(
            ("run_finished", {"run_id": RUN_ID, "ok": False, "reason": "abandoned"})
        )
        self._write_events(state_dir, records)

    def test_a_killed_turn_is_accounted_for_by_the_abort_that_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history)

            _, results = self._audit(history)

            self.assertEqual("pass", results["operator-abort"].status)
            self.assertIn("1 turn(s) were killed", results["operator-abort"].detail)
            # The killed turn is explained by the abort, so it must not fall
            # through to the catch-all that reports unknown failures.
            self.assertEqual("pass", results["unclassified-anomalies"].status)

    def test_a_run_that_killed_nothing_owes_no_abort_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            _, results = self._audit(history)

            verdict = results["operator-abort"]
            self.assertEqual("pass", verdict.status)
            self.assertIn("killed no turn", verdict.detail)

    def test_another_turns_failure_is_not_excused_by_the_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, also_failed=((9, 5),))

            _, results = self._audit(history)

            self.assertEqual("pass", results["operator-abort"].status)
            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn(
                events.TURN_FINISHED_EVENT,
                results["unclassified-anomalies"].detail,
            )

    def test_a_kill_no_request_asked_for_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, requested=False)

            _, results = self._audit(history)
            findings = supervisor_audit.findings_from_results(results.values())

            verdict = results["operator-abort"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("no recorded request asked for", verdict.detail)
            self.assertIn(
                "lifecycle.operator-abort",
                [item.invariant_key for item in findings],
            )

    def test_a_request_naming_another_turn_does_not_cover_this_kill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, requested_turn_index=3)

            _, results = self._audit(history)

            verdict = results["operator-abort"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("no recorded request asked for", verdict.detail)

    def test_a_kill_that_names_no_reason_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._aborted_a_turn(history, reason=None)

            _, results = self._audit(history)

            verdict = results["operator-abort"]
            self.assertEqual("fail", verdict.status)
            self.assertIn("names no reason", verdict.detail)
            # An abort with no reason on it explains nothing either.
            self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_a_damaged_event_log_lands_in_the_unknown_lane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            with (history[2] / events.EVENTS_FILE_NAME).open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write("{not json\n")

            _, results = self._audit(history)

            self.assertEqual("fail", results["unclassified-anomalies"].status)
            self.assertIn("would not parse", results["unclassified-anomalies"].detail)
            self.assertEqual(
                "fail",
                supervisor_audit.lane_rollup(results.values())["unknown-anomalies"],
            )

    def test_checks_report_unavailable_when_the_workspace_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, results = self._audit(self._history(Path(tmp)), workspace_root=None)

            self.assertEqual("unavailable", results["root-registry-agreement"].status)
            self.assertEqual("unavailable", results["repository-intent"].status)
            self.assertEqual(
                "unavailable",
                supervisor_audit.lane_rollup(results.values())["identity-authority"],
            )

    def test_findings_keep_one_fingerprint_across_relocated_runs(self) -> None:
        fingerprints = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                history = self._history(Path(tmp))
                payload = self._read_state(history[2])
                payload["review_harness"] = payload["harness"]
                self._write_state(history[2], payload)

                _, results = self._audit(history)
                fingerprints.append(
                    [
                        item.fingerprint
                        for item in supervisor_audit.findings_from_results(
                            results.values()
                        )
                    ]
                )

        self.assertEqual(1, len(fingerprints[0]))
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_every_cited_locator_is_declared_in_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            (history[3] / "turn-0001-01" / state.RESULT_FILE_NAME).unlink()

            bundle, results = self._audit(history)
            declared = {reference.locator for reference in bundle.evidence}

            self.assertEqual(
                len(supervisor_audit.DETERMINISTIC_CHECK_SPECS), len(results)
            )
            for result in results.values():
                self.assertTrue(result.evidence, result.check)
                self.assertLessEqual(set(result.evidence), declared, result.check)


#: Injected so an audit's timestamp is an input rather than a wall-clock read,
#: which is what lets two identical runs produce byte-identical artifacts.
FIXED_CLOCK = datetime(2026, 8, 10, 10, 11, 12, tzinfo=UTC)


class _ScriptedAuditor:
    """Stand-in auditor with scripted answers that records every prompt it saw.

    Replies are consumed in order and the last one repeats, so a test that wants
    the same failure on both attempts scripts it once.
    """

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.names: list[str] = []

    def __call__(self, name: str, **kwargs: object) -> harnesses.HarnessResult:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        self.names.append(name)
        access = kwargs["access"]
        assert isinstance(access, harnesses.AccessProfile)
        argv = harnesses.get_adapter(name).build_argv(
            cwd=Path(str(kwargs["cwd"])),
            plan_dir=None,
            prompt=prompt,
            last_message_path=Path(str(kwargs["last_message_path"])),
            access=access,
        )
        index = min(len(self.prompts), len(self.replies)) - 1
        reply = self.replies[index]
        spec = reply if isinstance(reply, dict) else {"message": reply}
        return harnesses.HarnessResult(
            argv=argv,
            returncode=int(spec.get("returncode", 0)),
            stdout=str(spec.get("stdout", "")),
            stderr=str(spec.get("stderr", "")),
            last_message=str(spec.get("message", "")),
            name=name,
            timed_out=bool(spec.get("timed_out", False)),
            interrupted=bool(spec.get("interrupted", False)),
        )


class AgentAuditEngineTests(_RunHistoryFixture, unittest.TestCase):
    """The semantic auditor is validated strictly and never trusted as output."""

    def _role(self) -> AuditRole:
        return AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=30.0,
        )

    def _run(
        self,
        history: tuple[Path, Path, Path, Path],
        auditor: _ScriptedAuditor,
        *,
        bundle: supervisor_audit.AuditInput | None = None,
        sleeps: list[float] | None = None,
        workspace_root: Path | None = None,
        repositories: tuple[str, ...] = ("app",),
    ) -> supervisor_audit.AgentAuditOutcome:
        workspace, plan_dir, state_dir, run_dir = history
        return supervisor_audit.run_agent_audit(
            self._role(),
            bundle=self._bundle(history) if bundle is None else bundle,
            run_id=RUN_ID,
            run_dir=run_dir,
            workspace_root=workspace if workspace_root is None else workspace_root,
            plan_dir=plan_dir,
            repositories=repositories,
            state_dir=state_dir,
            durable=False,
            now=lambda: FIXED_CLOCK,
            _run_harness_fn=auditor,
            _sleep_fn=(sleeps if sleeps is not None else []).append,
        )

    def _self_reviewed(self, history: tuple[Path, Path, Path, Path]) -> None:
        """Make one deterministic check fail, so the run has findings of its own."""
        payload = self._read_state(history[2])
        payload["review_harness"] = payload["harness"]
        self._write_state(history[2], payload)

    def _lanes(
        self, locator: str, statuses: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        claimed = statuses or {}
        return [
            {
                "lane": lane,
                "status": claimed.get(lane, "pass"),
                "summary": f"{lane} judged from the recorded control-plane evidence",
                "evidence": [locator],
            }
            for lane in RUBRIC_LANES
        ]

    def _finding(self, locator: str, **overrides: object) -> dict[str, Any]:
        finding: dict[str, Any] = {
            "lane": "identity-authority",
            "severity": "medium",
            "category": "identity",
            "component": "workspace-registry",
            "invariant_key": "identity.agent-observed-drift",
            "summary": "The registry named a repository the manifest never declared.",
            "recommendation": "Re-derive the registry from the manifest before a run.",
            "evidence": [locator],
            "identity": {"decision": "new"},
        }
        finding.update(overrides)
        return finding

    def _reply(
        self,
        lanes: list[dict[str, Any]],
        findings: list[dict[str, Any]] | None = None,
        *,
        prose: str = "",
    ) -> str:
        body = json.dumps(
            {
                "schema_version": supervisor_audit.AGENT_OUTPUT_SCHEMA_VERSION,
                "lanes": lanes,
                "findings": findings or [],
            }
        )
        return f"{prose}```json\n{body}\n```\n"

    def _reports(self, run_dir: Path, audit: object) -> tuple[dict[str, Any], str]:
        report, markdown = supervisor_audit.write_audit_reports(
            run_dir, audit, durable=False
        )
        return (
            json.loads(report.read_text(encoding="utf-8")),
            markdown.read_text(encoding="utf-8"),
        )

    def test_a_clean_run_with_every_lane_proven_is_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertIsNone(outcome.audit.reason)
            self.assertEqual((), outcome.audit.findings)
            self.assertEqual(["ok"], [item.outcome for item in outcome.attempts])
            self.assertEqual(
                list(RUBRIC_LANES), [lane.lane for lane in outcome.audit.lanes]
            )
            self.assertIsNone(outcome.mutation_issue_id)

    def test_an_agent_finding_is_reported_with_an_asl_assigned_fingerprint(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [self._finding(locator)],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertEqual("findings", outcome.audit.state)
            self.assertIsNone(outcome.audit.reason)
            finding = outcome.audit.findings[0]
            self.assertEqual("identity.agent-observed-drift", finding.invariant_key)
            self.assertEqual(64, len(finding.fingerprint))
            self.assertEqual((locator,), finding.evidence)
            self.assertEqual("fail", outcome.audit.lanes[0].status)

    def test_an_agent_that_assigns_its_own_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator),
                    [self._finding(locator, issue_id="ISSUE-0001")],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("malformed-output", outcome.attempts[0].outcome)
            self.assertIn("never", outcome.attempts[0].detail)
            self.assertIn("issue IDs", outcome.attempts[0].detail)

    def test_a_malformed_answer_is_retried_in_a_fresh_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                "I could not be bothered to answer in JSON.",
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["malformed-output", "ok"], [item.outcome for item in outcome.attempts]
            )
            retry = auditor.prompts[1]
            self.assertIn("Your previous answer was rejected", retry)
            self.assertIn("not parseable JSON", retry)
            self.assertIn("Operational evidence for run", retry)
            self.assertNotIn("could not be bothered", retry)

    def test_two_malformed_answers_keep_the_deterministic_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            auditor = _ScriptedAuditor("{not json")

            outcome = self._run(history, auditor)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual("malformed-output", outcome.audit.reason)
            self.assertEqual(MAX_AUDIT_ATTEMPTS, len(outcome.attempts))
            self.assertEqual(
                len(supervisor_audit.DETERMINISTIC_CHECK_SPECS),
                len(outcome.audit.deterministic),
            )
            self.assertTrue(outcome.audit.findings)
            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertEqual("fail", statuses["verification-review-independence"])

    def test_a_timeout_spends_the_delivery_budget_before_going_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = _ScriptedAuditor({"message": "", "timed_out": True})
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(supervisor_audit.REASON_TIMEOUT, outcome.audit.reason)
            self.assertEqual(
                ["timeout"] * MAX_AUDIT_DELIVERY_ATTEMPTS,
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual(MAX_AUDIT_DELIVERY_ATTEMPTS, len(auditor.prompts))
            self.assertEqual(
                list(supervisor_audit.AUDIT_TRANSPORT_RETRY_DELAYS_SECONDS), sleeps
            )

    def test_a_transport_failure_backs_off_and_reuses_the_same_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = _ScriptedAuditor(
                {"message": "", "returncode": 1, "stderr": "connection reset by peer"}
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                supervisor_audit.REASON_HARNESS_FAILED, outcome.audit.reason
            )
            self.assertEqual(
                ["transport-error"] * MAX_AUDIT_DELIVERY_ATTEMPTS,
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual([15.0, 60.0, 120.0], sleeps)
            # The agent never saw the prompt, so nothing about it is corrected:
            # every relaunch repeats it verbatim.
            self.assertEqual({auditor.prompts[0]}, set(auditor.prompts))
            self.assertNotIn("Your previous answer was rejected", auditor.prompts[-1])

    def test_a_capacity_outage_that_clears_still_earns_a_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            capacity = {
                "message": "",
                "returncode": 1,
                "stderr": (
                    '{"error": {"message": "Selected model is at capacity. '
                    'Please try a different model.", '
                    '"codex_error_info": "server_overloaded"}}'
                ),
            }
            auditor = _ScriptedAuditor(
                capacity,
                capacity,
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, bundle=bundle, sleeps=sleeps)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["transport-error", "transport-error", "ok"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual([15.0, 60.0], sleeps)

            attempt_dir = history[3] / assurance_models.attempt_dir_name(1)
            recorded = json.loads(
                (attempt_dir / supervisor_audit.ATTEMPT_OUTCOME_FILE).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                harness_failures.MODEL_CAPACITY_MARKER,
                recorded["classification"]["marker"],
            )
            self.assertIn("at capacity", recorded["classification"]["evidence"])
            self.assertIn(
                "server_overloaded",
                (attempt_dir / supervisor_audit.ATTEMPT_STDERR_FILE).read_text(
                    encoding="utf-8"
                ),
            )
            # An attempt that produced no stdout leaves no empty file behind.
            self.assertFalse(
                (attempt_dir / supervisor_audit.ATTEMPT_STDOUT_FILE).exists()
            )

    def test_a_capacity_outage_is_recorded_without_swapping_the_auditor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            role = AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
                model="claude-opus-4",
            )
            auditor = _ScriptedAuditor(
                {
                    "message": "",
                    "returncode": 1,
                    "stderr": '{"codex_error_info": "server_overloaded"}',
                }
            )

            supervisor_audit.run_agent_audit(
                role,
                bundle=self._bundle((workspace, plan_dir, state_dir, run_dir)),
                run_id=RUN_ID,
                run_dir=run_dir,
                workspace_root=workspace,
                plan_dir=plan_dir,
                repositories=("app",),
                state_dir=state_dir,
                durable=False,
                now=lambda: FIXED_CLOCK,
                _run_harness_fn=auditor,
                _sleep_fn=lambda _seconds: None,
            )

            recorded = model_availability.availability(
                state.state_root(state_dir), "claude-opus-4"
            )
            self.assertFalse(recorded.available)
            self.assertEqual(harness_failures.MODEL_CAPACITY_MARKER, recorded.reason)
            # The outage is recorded, never routed around: every relaunch used
            # the auditor the role named.
            self.assertEqual({"claude"}, set(auditor.names))

    def test_a_failure_that_can_never_succeed_is_not_waited_out(self) -> None:
        """The shared classifier, not the outcome slug, decides what is transient."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = _ScriptedAuditor(
                {"message": "", "returncode": 7, "stderr": "fake harness failure"}
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, sleeps=sleeps)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                ["transport-error"], [item.outcome for item in outcome.attempts]
            )
            self.assertEqual([], sleeps)

    def test_delivery_failures_do_not_spend_the_answer_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                {"message": "", "returncode": 1, "stderr": "socket hang up"},
                "{not json",
                self._reply(self._lanes(bundle.evidence[0].locator)),
            )

            outcome = self._run(history, auditor, bundle=bundle, sleeps=[])

            self.assertTrue(outcome.agent_covered)
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertEqual(
                ["transport-error", "malformed-output", "ok"],
                [item.outcome for item in outcome.attempts],
            )

    def test_an_interrupted_attempt_is_never_retried(self) -> None:
        """The reply a harness returns after Ctrl+C, not a raised exception."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = _ScriptedAuditor(
                {"message": "", "returncode": 130, "interrupted": True}
            )

            outcome = self._run(history, auditor)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, outcome.audit.reason)
            self.assertEqual(
                ["interrupted"], [item.outcome for item in outcome.attempts]
            )
            self.assertEqual(1, len(auditor.prompts))

    def test_an_interrupt_outranks_the_nonzero_exit_it_arrives_with(self) -> None:
        """130 is the harness reporting the interrupt, not a transport failure."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            auditor = _ScriptedAuditor(
                {"message": "", "returncode": 130, "interrupted": True}
            )

            attempt = supervisor_audit.run_restricted_audit(
                self._role(),
                run_dir=run_dir,
                run_id=RUN_ID,
                workspace_root=workspace,
                plan_dir=plan_dir,
                prompt="grade this run",
                repositories=("app",),
                state_dir=state_dir,
                _run_harness_fn=auditor,
            )

            self.assertEqual("interrupted", attempt.outcome)
            self.assertEqual("unavailable", attempt.state)
            self.assertEqual(supervisor_audit.REASON_INTERRUPTED, attempt.reason)
            self.assertNotIn("interrupted", supervisor_audit.RETRYABLE_ATTEMPT_OUTCOMES)

    def test_a_localized_finding_names_its_source_in_both_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            localization=[
                                "package/runner.py:8129",
                                "skills/asl/SKILL.md",
                            ],
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)
            report, markdown = self._reports(history[3], outcome.audit)

            self.assertEqual(
                ("package/runner.py:8129", "skills/asl/SKILL.md"),
                outcome.audit.findings[0].localization,
            )
            self.assertEqual(
                ["package/runner.py:8129", "skills/asl/SKILL.md"],
                report["findings"][0]["localization"],
            )
            self.assertIn("`package/runner.py:8129`", markdown)

    def test_a_localization_outside_the_source_roots_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [self._finding(locator, localization=["workspace/app/main.py:12"])],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("invalid-reference", outcome.audit.reason)
            self.assertIn("outside the read-only source roots", auditor.prompts[1])

    def test_evidence_the_bundle_never_declared_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            auditor = _ScriptedAuditor(
                self._reply(self._lanes("run/invented-by-the-agent.json"))
            )

            outcome = self._run(history, auditor)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("invalid-reference", outcome.audit.reason)
            self.assertEqual(
                ["invalid-reference", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )

    def test_an_answer_missing_a_rubric_lane_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator)[:3])
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("missing-lanes", outcome.audit.reason)
            self.assertEqual("missing-lanes", outcome.attempts[0].outcome)

    def test_a_lane_the_agent_could_not_judge_is_never_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(
                        bundle.evidence[0].locator, {"resources-stalls": "unavailable"}
                    )
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertTrue(outcome.agent_covered)
            self.assertEqual("partial", outcome.audit.state)
            self.assertEqual(
                supervisor_audit.REASON_LANE_EVIDENCE_ABSENT, outcome.audit.reason
            )
            self.assertEqual("unavailable", statuses["resources-stalls"])
            self.assertEqual((), outcome.audit.findings)

    def test_a_lane_the_agent_failed_is_reported_as_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(
                        bundle.evidence[0].locator, {"resources-stalls": "fail"}
                    )
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            # A failing lane with no finding to explain it is not a verdict the
            # audit can stand behind, so it degrades rather than reading partial.
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                supervisor_audit.REASON_LANE_COVERAGE, outcome.audit.reason
            )

    def test_a_legacy_plan_without_a_registry_binding_can_be_all_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            repository = self._make_legacy_plan(history)
            bundle = self._bundle(history, workspace_root=repository)
            auditor = _ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(
                history,
                auditor,
                bundle=bundle,
                workspace_root=repository,
                repositories=(),
            )
            checks = {
                result.check: result.status for result in outcome.audit.deterministic
            }
            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}

            # The incident this pins: a check that could not read its evidence
            # used to drag a lane the agent had judged down to `unavailable`,
            # so no legacy plan could ever earn a clean audit.
            self.assertEqual("unavailable", checks["root-registry-agreement"])
            self.assertEqual("pass", statuses["identity-authority"])
            self.assertEqual("all-clear", outcome.audit.state)
            self.assertIsNone(outcome.audit.reason)

    def test_an_agent_pass_never_overrules_a_deterministic_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(history, auditor, bundle=bundle)

            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertEqual("fail", statuses["verification-review-independence"])
            self.assertEqual("findings", outcome.audit.state)

    def test_no_agent_markdown_reaches_the_operator_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            lanes = self._lanes(bundle.evidence[0].locator)
            lanes[0]["summary"] = "table | breaker\nwith a newline"
            auditor = _ScriptedAuditor(
                self._reply(
                    lanes, prose="## Trust me instead\n\nI rewrote the run.\n\n"
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)
            _, markdown = self._reports(history[3], outcome.audit)

            self.assertNotIn("Trust me instead", markdown)
            self.assertNotIn("I rewrote the run.", markdown)
            self.assertIn("table \\| breaker with a newline", markdown)

    def test_the_prompt_orders_evidence_before_any_source_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._bundle(self._history(Path(tmp)))

            prompt = supervisor_audit.build_audit_prompt(
                bundle, catalog=supervisor_audit.EMPTY_AGENT_CATALOG
            )

            self.assertLess(
                prompt.index("Analyze the operational evidence below first"),
                prompt.index("read ASL source to localize it"),
            )
            self.assertIn("You are not reviewing the application", prompt)
            self.assertIn(
                "Reviewing, grading, or commenting on the application", prompt
            )
            self.assertIn("Assigning issue IDs, fingerprints", prompt)
            self.assertIn("Editing, creating, or deleting any file", prompt)
            self.assertIn(
                "refused or paused before launching an unauthorized turn", prompt
            )
            self.assertIn("registry-free legacy plan confined", prompt)
            self.assertIn("`model_projection` is authoritative", prompt)
            self.assertIn("An unresolved", prompt)
            self.assertIn("requested default beside a resolved", prompt)
            self.assertIn("`findings-defer-broad-failures`", prompt)
            self.assertIn("later review reaches all-clear", prompt)
            for lane in RUBRIC_LANES:
                self.assertIn(f"- {lane}", prompt)
            for root in supervisor_audit.LOCALIZATION_ROOTS:
                self.assertIn(f"`{root}`", prompt)

    def test_identical_inputs_produce_identical_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            declared = [entry.locator for entry in bundle.evidence]
            results = supervisor_audit.run_deterministic_checks(bundle)
            reply = self._reply(
                self._lanes(declared[0], {"identity-authority": "fail"}),
                [self._finding(declared[0])],
            )
            rendered = []
            for index in range(2):
                directory = Path(tmp) / f"render-{index}"
                directory.mkdir()
                audit = supervisor_audit.render_supervisor_audit(
                    run_id=RUN_ID,
                    bundle=bundle,
                    results=results,
                    generated_at=FIXED_CLOCK.isoformat(),
                    agent=supervisor_audit.parse_agent_audit(
                        reply,
                        declared=declared,
                        catalog=supervisor_audit.EMPTY_AGENT_CATALOG,
                    ),
                )
                report, markdown = supervisor_audit.write_audit_reports(
                    directory, audit, durable=False
                )
                rendered.append((report.read_bytes(), markdown.read_bytes()))

            self.assertEqual(rendered[0], rendered[1])

    def test_the_audit_leaves_the_workspace_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, plan_dir, _, run_dir = history
            before = repository_evidence.capture_workspace_snapshot(workspace, plan_dir)
            auditor = _ScriptedAuditor("not an audit")

            outcome = self._run(history, auditor)
            comparison = repository_evidence.compare_workspace_snapshots(
                before,
                repository_evidence.capture_workspace_snapshot(workspace, plan_dir),
            )

            self.assertEqual({}, dict(comparison["changed"]))
            self.assertEqual([], comparison["added"])
            self.assertEqual([], comparison["removed"])
            self.assertIsNone(outcome.mutation_issue_id)
            self.assertFalse(
                (run_dir / supervisor_audit.MUTATION_ARTIFACT_NAME).exists()
            )

    def test_every_attempt_is_archived_without_inlining_the_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            run_dir = history[3]
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                "not json", self._reply(self._lanes(bundle.evidence[0].locator))
            )

            outcome = self._run(history, auditor, bundle=bundle)

            for attempt in outcome.attempts:
                directory = run_dir / str(attempt.artifact_dir)
                self.assertTrue(
                    (directory / supervisor_audit.ATTEMPT_PROMPT_FILE).is_file()
                )
                self.assertTrue(
                    (directory / supervisor_audit.ATTEMPT_MESSAGE_FILE).is_file()
                )
                recorded = json.loads(
                    (directory / supervisor_audit.ATTEMPT_OUTCOME_FILE).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(attempt.outcome, recorded["outcome"])
                self.assertFalse(
                    any("Operational evidence" in item for item in recorded["argv"])
                )

    def test_every_rubric_lane_owns_at_least_one_check(self) -> None:
        covered = {spec.lane for spec in supervisor_audit.DETERMINISTIC_CHECK_SPECS}

        self.assertEqual(set(RUBRIC_LANES), covered)


class AgentIdentityDecisionTests(_RunHistoryFixture, unittest.TestCase):
    """Every finding states one identity, and only from the supplied catalog."""

    _lanes = AgentAuditEngineTests._lanes
    _finding = AgentAuditEngineTests._finding
    _reply = AgentAuditEngineTests._reply
    _role = AgentAuditEngineTests._role
    _run = AgentAuditEngineTests._run
    _self_reviewed = AgentAuditEngineTests._self_reviewed

    def _open_issue(self, state_dir: Path, *, invariant: str) -> str:
        """Open one canonical issue in the ledger the audit will read."""
        record = assurance_ledger.append_issue_observation(
            IssueObservation(
                run_id=RUN_ID,
                finding=Finding(
                    category="identity",
                    component="workspace-registry",
                    invariant_key=invariant,
                    lane="identity-authority",
                    severity="medium",
                    summary="The registry disagreed with the manifest.",
                    recommendation="Re-derive the registry from the manifest.",
                    evidence=("supervisor-audit-input.json",),
                ),
                observed_at="2026-08-01T00:00:00+00:00",
            ),
            state_dir=state_dir,
        )
        assert record.issue_id is not None
        return record.issue_id

    def _rejected(
        self, history: tuple[Path, Path, Path, Path], **identity: object
    ) -> AuditAttempt:
        bundle = self._bundle(history)
        locator = bundle.evidence[0].locator
        auditor = _ScriptedAuditor(
            self._reply(
                self._lanes(locator, {"identity-authority": "fail"}),
                [self._finding(locator, **identity)],
            )
        )
        outcome = self._run(history, auditor, bundle=bundle)
        self.assertFalse(outcome.agent_covered)
        return outcome.attempts[0]

    def test_a_finding_without_an_identity_decision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            finding = self._finding(locator)
            del finding["identity"]
            auditor = _ScriptedAuditor(self._reply(self._lanes(locator), [finding]))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("malformed-output", outcome.attempts[0].outcome)
            self.assertIn("exactly one 'identity' object", outcome.attempts[0].detail)

    def test_an_identity_decision_outside_the_two_choices_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)), identity={"decision": "merge"}
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("existing, new", attempt.detail)

    def test_an_identity_object_with_an_unknown_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)),
                identity={"decision": "new", "confidence": 0.9},
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("unknown key(s) confidence", attempt.detail)

    def test_a_new_allocation_request_may_not_name_an_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)),
                identity={"decision": "new", "issue_id": "ASI-1"},
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("must name no 'issue_id'", attempt.detail)

    def test_a_malformed_issue_id_never_reaches_the_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = self._rejected(
                self._history(Path(tmp)),
                identity={"decision": "existing", "issue_id": "the registry one"},
            )

            self.assertEqual("malformed-output", attempt.outcome)
            self.assertIn("must name the 'issue_id'", attempt.detail)

    def test_an_invented_issue_id_is_refused_as_an_invalid_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._open_issue(history[2], invariant="identity.first")

            attempt = self._rejected(
                history, identity={"decision": "existing", "issue_id": "ASI-4242"}
            )

            self.assertEqual("invalid-reference", attempt.outcome)
            self.assertIn("ASI-4242", attempt.detail)
            self.assertIn("does not list", attempt.detail)

    def test_a_merged_away_issue_id_is_not_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            source = self._open_issue(history[2], invariant="identity.source")
            target = self._open_issue(history[2], invariant="identity.target")
            assurance_projection.record_merge(source, target, state_dir=history[2])

            attempt = self._rejected(
                history, identity={"decision": "existing", "issue_id": source}
            )

            self.assertEqual("invalid-reference", attempt.outcome)
            self.assertIn("merged away", attempt.detail)

    def test_a_catalog_selection_is_accepted_and_carried_with_its_finding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._open_issue(history[2], invariant="identity.known")
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": issue_id},
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertIsNotNone(outcome.agent)
            assert outcome.agent is not None
            finding, decision = outcome.agent.decisions[0]
            self.assertEqual("identity.agent-observed-drift", finding.invariant_key)
            self.assertEqual("existing", decision.decision)
            self.assertEqual(issue_id, decision.issue_id)
            self.assertFalse(decision.is_new)

    def test_a_new_allocation_request_is_carried_without_an_issue_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [self._finding(locator)],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertTrue(decision.is_new)
            self.assertIsNone(decision.issue_id)

    def test_a_v1_answer_carries_no_identity_and_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            body = json.dumps(
                {
                    "schema_version": 1,
                    "lanes": self._lanes(locator),
                    "findings": [],
                }
            )
            auditor = _ScriptedAuditor(f"```json\n{body}\n```\n")

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("malformed-output", outcome.attempts[0].outcome)
            self.assertIn(
                f"schema_version {supervisor_audit.AGENT_OUTPUT_SCHEMA_VERSION}",
                outcome.attempts[0].detail,
            )

    def test_an_identity_rejection_keeps_the_deterministic_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": "ASI-77"},
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual("invalid-reference", outcome.audit.reason)
            self.assertTrue(outcome.audit.findings)
            statuses = {lane.lane: lane.status for lane in outcome.audit.lanes}
            self.assertEqual("fail", statuses["verification-review-independence"])

    def test_the_prompt_supplies_the_catalog_with_its_digest_and_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._open_issue(history[2], invariant="identity.listed")
            catalog = supervisor_audit.read_agent_catalog(history[2])

            prompt = supervisor_audit.build_audit_prompt(
                self._bundle(history), catalog=catalog
            )

            self.assertIn(catalog.digest, prompt)
            self.assertIn(f'"issue_id":"{issue_id}"', prompt)
            self.assertIn('"omitted":0', prompt)
            self.assertLess(
                prompt.index("Every finding carries exactly one `identity` object"),
                prompt.index(catalog.digest),
            )

    def test_the_retry_repeats_the_catalog_and_the_identity_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._open_issue(history[2], invariant="identity.listed")
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": "ASI-9001"},
                        )
                    ],
                ),
                self._reply(self._lanes(locator)),
            )

            outcome = self._run(history, auditor, bundle=bundle)
            retry = auditor.prompts[1]

            self.assertTrue(outcome.agent_covered)
            self.assertIn("Your previous answer was rejected", retry)
            self.assertIn("ASI-9001", retry)
            self.assertIn(
                "An ID it does not list cannot be selected no matter where you saw it",
                retry,
            )
            self.assertIn(f'"issue_id":"{issue_id}"', retry)
            self.assertNotIn("identity.agent-observed-drift", retry)

    def test_an_unreadable_ledger_supplies_the_empty_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            (state_dir / assurance_ledger.LEDGER_FILE_NAME).mkdir(parents=True)

            catalog = supervisor_audit.read_agent_catalog(state_dir)

            self.assertEqual(supervisor_audit.EMPTY_AGENT_CATALOG, catalog)
            self.assertEqual((), catalog.entries)

    def test_a_missing_ledger_is_an_empty_catalog_rather_than_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = supervisor_audit.read_agent_catalog(Path(tmp) / "absent")

            self.assertEqual((), catalog.entries)
            self.assertEqual((), catalog.omitted)


class FailClosedIdentityTests(_RunHistoryFixture, unittest.TestCase):
    """A renamed recurrence is asked about once, then refused rather than split."""

    _lanes = AgentAuditEngineTests._lanes
    _finding = AgentAuditEngineTests._finding
    _reply = AgentAuditEngineTests._reply
    _role = AgentAuditEngineTests._role
    _run = AgentAuditEngineTests._run
    _self_reviewed = AgentAuditEngineTests._self_reviewed

    #: The summary the agent finding carries, so an issue seeded with it is a
    #: perfect normalized-summary match and nothing else.
    SUMMARY = "The registry named a repository the manifest never declared."

    def _seed(self, state_dir: Path, **overrides: object) -> str:
        payload: dict[str, Any] = {
            "category": "identity",
            "component": "workspace-registry",
            "invariant_key": "identity.registry-drift",
            "lane": "identity-authority",
            "severity": "medium",
            "summary": self.SUMMARY,
            "recommendation": "Re-derive the registry from the manifest.",
            "evidence": ("supervisor-audit-input.json",),
        }
        payload.update(overrides)
        record = assurance_ledger.append_issue_observation(
            IssueObservation(
                run_id=RUN_ID,
                finding=Finding(**payload),
                observed_at="2026-08-01T00:00:00+00:00",
            ),
            state_dir=state_dir,
        )
        assert record.issue_id is not None
        return record.issue_id

    def _new_answer(self, locator: str) -> str:
        return self._reply(
            self._lanes(locator, {"identity-authority": "fail"}),
            [self._finding(locator)],
        )

    def test_an_exact_fingerprint_is_reused_without_spending_a_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._seed(
                history[2], invariant_key="identity.agent-observed-drift"
            )
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            self.assertEqual(["ok"], [item.outcome for item in outcome.attempts])
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertEqual("existing", decision.decision)
            self.assertEqual(issue_id, decision.issue_id)
            self.assertEqual("recurrence", decision.source)

    def test_a_renamed_recurrence_is_retried_with_the_candidate_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            issue_id = self._seed(history[2])
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._new_answer(locator),
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": issue_id},
                        )
                    ],
                ),
            )

            outcome = self._run(history, auditor, bundle=bundle)
            retry = auditor.prompts[1]

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(2, len(auditor.prompts))
            self.assertEqual(
                ["invalid-reference", "ok"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertIn("Your previous answer was well-formed", retry)
            self.assertNotIn("Your previous answer was rejected", retry)
            self.assertIn(f'{{"issue_id":"{issue_id}","reasons":["summary"]', retry)
            self.assertIn('"similarity":1.0', retry)
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertEqual(issue_id, decision.issue_id)
            self.assertEqual("agent", decision.source)

    def test_a_second_new_answer_fails_closed_without_allocating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._self_reviewed(history)
            issue_id = self._seed(history[2])
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertIsNone(outcome.agent)
            self.assertEqual(2, len(auditor.prompts))
            self.assertEqual("unavailable", outcome.audit.state)
            self.assertEqual(
                supervisor_audit.REASON_IDENTITY_AMBIGUOUS, outcome.audit.reason
            )
            self.assertEqual(
                ["invalid-reference", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertIn(issue_id, outcome.attempts[0].detail)
            # The reproducible half of the audit is not lost with the agent half.
            self.assertTrue(outcome.audit.findings)
            self.assertNotIn(
                "identity.agent-observed-drift",
                [finding.invariant_key for finding in outcome.audit.findings],
            )

    def test_the_candidate_list_is_offered_once_and_never_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(history[2])
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            self._run(history, auditor, bundle=bundle)

            self.assertEqual(MAX_AUDIT_ATTEMPTS, len(auditor.prompts))
            self.assertNotIn("Your previous answer was well-formed", auditor.prompts[0])
            self.assertIn("Your previous answer was well-formed", auditor.prompts[1])

    def test_an_undelivered_turn_does_not_consume_the_candidate_offer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(history[2])
            bundle = self._bundle(history)
            answer = self._new_answer(bundle.evidence[0].locator)
            auditor = _ScriptedAuditor(
                answer,
                {"message": "", "returncode": 1, "stderr": "socket hang up"},
                answer,
            )
            sleeps: list[float] = []

            outcome = self._run(history, auditor, bundle=bundle, sleeps=sleeps)

            self.assertEqual(
                ["invalid-reference", "transport-error", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual([15.0], sleeps)
            # The relaunch after the outage carries the candidate list the
            # undelivered turn was meant to show.
            self.assertIn("Your previous answer was well-formed", auditor.prompts[1])
            self.assertEqual(auditor.prompts[1], auditor.prompts[2])

    def test_a_wasted_first_attempt_leaves_no_turn_for_the_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(history[2])
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(
                "the audit went fine", self._new_answer(bundle.evidence[0].locator)
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertFalse(outcome.agent_covered)
            self.assertEqual(
                ["malformed-output", "invalid-reference"],
                [item.outcome for item in outcome.attempts],
            )
            self.assertEqual(
                supervisor_audit.REASON_IDENTITY_AMBIGUOUS, outcome.audit.reason
            )
            self.assertNotIn("Your previous answer was well-formed", auditor.prompts[1])
            self.assertIn("Your previous answer was rejected", auditor.prompts[1])

    def test_a_distinct_near_neighbour_is_allocated_rather_than_questioned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._seed(
                history[2],
                invariant_key="scope.no-escape",
                summary="A commit landed outside the declared write scope.",
            )
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertTrue(decision.is_new)
            self.assertEqual("agent", decision.source)

    def test_a_selection_the_validator_accepted_is_never_second_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            chosen = self._seed(history[2], invariant_key="identity.chosen")
            self._seed(history[2])
            bundle = self._bundle(history)
            locator = bundle.evidence[0].locator
            auditor = _ScriptedAuditor(
                self._reply(
                    self._lanes(locator, {"identity-authority": "fail"}),
                    [
                        self._finding(
                            locator,
                            identity={"decision": "existing", "issue_id": chosen},
                        )
                    ],
                )
            )

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            assert outcome.agent is not None
            _, decision = outcome.agent.decisions[0]
            self.assertEqual(chosen, decision.issue_id)

    def test_an_empty_inbox_leaves_a_new_allocation_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            bundle = self._bundle(history)
            auditor = _ScriptedAuditor(self._new_answer(bundle.evidence[0].locator))

            outcome = self._run(history, auditor, bundle=bundle)

            self.assertTrue(outcome.agent_covered)
            self.assertEqual(1, len(auditor.prompts))
            assert outcome.agent is not None
            self.assertTrue(outcome.agent.decisions[0][1].is_new)

    def test_an_identity_source_outside_the_two_is_refused(self) -> None:
        with self.assertRaises(supervisor_audit.AgentOutputError) as raised:
            supervisor_audit.IdentityDecision(decision="new", source="guess")

        self.assertEqual("malformed-output", raised.exception.outcome)

    def test_resolution_names_every_candidate_it_could_not_settle(self) -> None:
        resolution = supervisor_audit.IdentityResolution(
            audit=supervisor_audit.AgentAudit(lanes=(), findings=(), identities=()),
            candidates=(
                assurance_projection.IdentityCandidate(
                    issue_id="ASI-3", reasons=("summary",), similarity=0.9
                ),
                assurance_projection.IdentityCandidate(
                    issue_id="ASI-7", reasons=("invariant",)
                ),
            ),
            ambiguous=2,
        )

        self.assertFalse(resolution.settled)
        self.assertIn("2 finding(s)", resolution.detail)
        self.assertIn("ASI-3", resolution.detail)
        self.assertIn("ASI-7", resolution.detail)


def _catalog_entry(number: int, *, last_seen_at: str) -> CatalogEntry:
    return CatalogEntry(
        issue_id=f"ASI-{number}",
        state="open",
        severity="medium",
        category="identity",
        component="workspace-registry",
        invariant_key=f"identity.invariant-{number:04d}",
        lane="identity-authority",
        summary="The registry named a repository the manifest never declared. " * 8,
        fingerprints=(f"{number:064d}",),
        identity_keys=(),
        primary_identity_key=None,
        localization=(),
        summary_tokens=(),
        invariant_tokens=(),
        occurrence_count=1,
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at=last_seen_at,
    )


class AgentCatalogTests(unittest.TestCase):
    """The supplied catalog is bounded, ordered, and honest about what it drops."""

    def _catalog(self, count: int) -> IdentityCatalog:
        return IdentityCatalog(
            entries=tuple(
                _catalog_entry(
                    number, last_seen_at=f"2026-02-{number % 28 + 1:02d}T00:00:00+00:00"
                )
                for number in range(1, count + 1)
            )
        )

    def test_a_small_catalog_is_supplied_whole(self) -> None:
        supplied = supervisor_audit.build_agent_catalog(self._catalog(5))

        self.assertEqual(5, len(supplied.entries))
        self.assertEqual((), supplied.omitted)
        self.assertEqual(0, supplied.to_dict()["omitted"])

    def test_a_large_catalog_is_bounded_and_reports_what_it_dropped(self) -> None:
        catalog = self._catalog(1000)

        supplied = supervisor_audit.build_agent_catalog(catalog)
        rendered = json.dumps(supplied.to_dict(), separators=(",", ":"))

        self.assertLessEqual(
            len(rendered.encode("utf-8")), supervisor_audit.MAX_AGENT_CATALOG_BYTES
        )
        self.assertTrue(supplied.omitted)
        self.assertEqual(
            len(catalog.entries), len(supplied.entries) + len(supplied.omitted)
        )
        self.assertEqual(len(supplied.omitted), supplied.to_dict()["omitted"])
        self.assertEqual(set(), supplied.issue_ids.intersection(supplied.omitted))

    def test_the_bound_is_spent_on_the_most_recently_seen_issues(self) -> None:
        catalog = IdentityCatalog(
            entries=(
                _catalog_entry(1, last_seen_at="2026-01-01T00:00:00+00:00"),
                _catalog_entry(2, last_seen_at="2026-06-01T00:00:00+00:00"),
                _catalog_entry(3, last_seen_at="2026-03-01T00:00:00+00:00"),
            )
        )
        whole = supervisor_audit.build_agent_catalog(catalog)
        # Room for the whole listing less one entry and the comma before it.
        bound = len(canonical_json_bytes(whole.to_dict())) - len(
            canonical_json_bytes(whole.entries[0].to_dict())
        )

        with mock.patch.object(supervisor_audit, "MAX_AGENT_CATALOG_BYTES", bound):
            supplied = supervisor_audit.build_agent_catalog(catalog)

        self.assertEqual(["ASI-2", "ASI-3"], [e.issue_id for e in supplied.entries])
        self.assertEqual(("ASI-1",), supplied.omitted)

    def test_supplied_entries_are_listed_in_issue_order(self) -> None:
        supplied = supervisor_audit.build_agent_catalog(self._catalog(30))

        self.assertEqual(
            [f"ASI-{number}" for number in range(1, 31)],
            [entry.issue_id for entry in supplied.entries],
        )

    def test_the_digest_names_the_whole_catalog_not_the_sample(self) -> None:
        catalog = self._catalog(1000)

        supplied = supervisor_audit.build_agent_catalog(catalog)

        self.assertTrue(supplied.omitted)
        self.assertEqual(catalog.digest, supplied.digest)

    def test_an_omitted_issue_is_refused_without_being_denied(self) -> None:
        catalog = self._catalog(1000)
        supplied = supervisor_audit.build_agent_catalog(catalog)
        omitted = supplied.omitted[0]
        reply = json.dumps(
            {
                "schema_version": supervisor_audit.AGENT_OUTPUT_SCHEMA_VERSION,
                "lanes": [
                    {
                        "lane": lane,
                        "status": "pass",
                        "summary": f"{lane} judged from the recorded evidence",
                        "evidence": ["run:supervisor-audit-input.json"],
                    }
                    for lane in RUBRIC_LANES
                ],
                "findings": [
                    {
                        "lane": "identity-authority",
                        "severity": "medium",
                        "category": "identity",
                        "component": "workspace-registry",
                        "invariant_key": "identity.agent-observed-drift",
                        "summary": "The registry named an undeclared repository.",
                        "recommendation": "Re-derive the registry.",
                        "evidence": ["run:supervisor-audit-input.json"],
                        "identity": {"decision": "existing", "issue_id": omitted},
                    }
                ],
            }
        )

        with self.assertRaises(supervisor_audit.AgentOutputError) as raised:
            supervisor_audit.parse_agent_audit(
                f"```json\n{reply}\n```",
                declared=["run:supervisor-audit-input.json"],
                catalog=supplied,
            )

        self.assertEqual("invalid-reference", raised.exception.outcome)
        self.assertIn("size bound kept", raised.exception.detail)
        self.assertNotIn("does not list", raised.exception.detail)

    def test_a_candidate_the_bound_dropped_is_promoted_for_the_retry(self) -> None:
        catalog = self._catalog(1000)
        supplied = supervisor_audit.build_agent_catalog(catalog)
        omitted = supplied.omitted[0]

        widened = supervisor_audit.widen_agent_catalog(supplied, catalog, [omitted])

        self.assertIn(omitted, widened.issue_ids)
        self.assertNotIn(omitted, widened.omitted)
        self.assertEqual(len(supplied.entries) + 1, len(widened.entries))
        self.assertEqual(len(supplied.omitted) - 1, len(widened.omitted))
        self.assertEqual(
            sorted(widened.entries, key=lambda entry: issue_id_number(entry.issue_id)),
            list(widened.entries),
        )

    def test_widening_restates_the_same_inbox_and_keeps_the_digest(self) -> None:
        catalog = self._catalog(1000)
        supplied = supervisor_audit.build_agent_catalog(catalog)

        widened = supervisor_audit.widen_agent_catalog(
            supplied, catalog, list(supplied.omitted[:3])
        )

        self.assertEqual(supplied.digest, widened.digest)
        self.assertEqual(
            len(catalog.entries), len(widened.entries) + len(widened.omitted)
        )

    def test_widening_toward_a_listed_or_unknown_issue_changes_nothing(self) -> None:
        catalog = self._catalog(5)
        supplied = supervisor_audit.build_agent_catalog(catalog)

        self.assertIs(
            supplied, supervisor_audit.widen_agent_catalog(supplied, catalog, ["ASI-1"])
        )
        self.assertIs(
            supplied,
            supervisor_audit.widen_agent_catalog(supplied, catalog, ["ASI-4242"]),
        )


class ReportStagingTests(unittest.TestCase):
    """Canonical reports appear together, at their canonical names, or not at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.audit = SupervisorAudit(
            run_id=RUN_ID,
            state="all-clear",
            input_digest="b" * 64,
            generated_at="2026-08-10T10:11:12+00:00",
            evidence=(EvidenceRef(kind="event", locator="events.jsonl"),),
            lanes=tuple(
                LaneEvidence(
                    lane=lane,
                    status="pass",
                    summary=f"{lane} judged from the event ledger",
                    evidence=("events.jsonl",),
                )
                for lane in RUBRIC_LANES
            ),
        )

    @property
    def canonical(self) -> tuple[Path, Path]:
        return (
            self.run_dir / audit_artifact_name(AUDIT_REPORT_FILENAME),
            self.run_dir / audit_artifact_name(AUDIT_MARKDOWN_FILENAME),
        )

    def stage(self) -> supervisor_audit.StagedAuditReports:
        return supervisor_audit.stage_audit_reports(
            self.run_dir, self.audit, durable=False
        )

    def test_staging_writes_both_reports_outside_their_canonical_names(self) -> None:
        staged = self.stage()

        self.assertEqual(2, len(staged.entries))
        for path, canonical, digest in staged.entries:
            self.assertTrue(path.is_file())
            self.assertFalse(canonical.exists())
            self.assertEqual(digest, artifacts.sha256_hex(path.read_bytes()))
        self.assertEqual(
            ["run/supervisor-audit.json", "run/supervisor-audit.md"],
            [reference.locator for reference in staged.references],
        )

    def test_promotion_gives_both_reports_their_canonical_names(self) -> None:
        staged = self.stage()

        staged.promote()

        report, markdown = self.canonical
        self.assertTrue(report.is_file())
        self.assertTrue(markdown.is_file())
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8"))["run_id"], RUN_ID
        )

    def test_a_taken_canonical_name_refuses_the_whole_promotion(self) -> None:
        staged = self.stage()
        report, markdown = self.canonical
        report.write_text("an earlier publication already claimed this name\n")

        with self.assertRaises(AssuranceError) as caught:
            staged.promote()

        self.assertIn("could not promote audit report", str(caught.exception))
        # The markdown is ordered after the report, so nothing was linked before the
        # refusal; the assertion that matters is that it is not left behind either.
        self.assertFalse(markdown.exists())

    def test_a_refusal_after_a_partial_promotion_leaves_no_report(self) -> None:
        staged = self.stage()
        report, markdown = self.canonical
        markdown.write_text("an earlier publication already claimed this name\n")
        original = markdown.read_bytes()

        with self.assertRaises(AssuranceError):
            staged.promote()

        self.assertFalse(report.exists())
        self.assertEqual(original, markdown.read_bytes())

    def test_unpromoting_frees_the_canonical_names_for_another_attempt(self) -> None:
        staged = self.stage()
        staged.promote()

        staged.unpromote()

        for path in self.canonical:
            self.assertFalse(path.exists())
        staged.promote()
        for path in self.canonical:
            self.assertTrue(path.is_file())

    def test_discarding_removes_the_staging_directory_but_not_the_reports(self) -> None:
        staged = self.stage()
        staged.promote()

        staged.discard()

        self.assertFalse(staged.directory.exists())
        for path in self.canonical:
            self.assertTrue(path.is_file())

    def test_staging_clears_scratch_an_earlier_attempt_left_behind(self) -> None:
        abandoned = self.run_dir / supervisor_audit.AUDIT_STAGING_DIR_NAME
        abandoned.mkdir()
        (abandoned / "half-written.json").write_text("{")

        staged = self.stage()

        self.assertFalse((abandoned / "half-written.json").exists())
        for path, _, _ in staged.entries:
            self.assertTrue(path.is_file())

    def test_an_unwritable_run_directory_refuses_staging(self) -> None:
        blocked = self.run_dir / "blocked"
        blocked.write_text("a file where the run directory should be\n")

        with self.assertRaises(AssuranceError) as caught:
            supervisor_audit.stage_audit_reports(blocked, self.audit, durable=False)

        self.assertIn("could not create audit staging directory", str(caught.exception))

    def test_writing_reports_twice_refuses_the_second_attempt(self) -> None:
        supervisor_audit.write_audit_reports(self.run_dir, self.audit, durable=False)

        with self.assertRaises(AssuranceError):
            supervisor_audit.write_audit_reports(
                self.run_dir, self.audit, durable=False
            )

        self.assertFalse(
            (self.run_dir / supervisor_audit.AUDIT_STAGING_DIR_NAME).exists()
        )


if __name__ == "__main__":
    unittest.main()
