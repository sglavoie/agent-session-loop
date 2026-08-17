"""Audit run configuration, independent role resolution, and persistence."""

from __future__ import annotations

import argparse
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
    events,
    harness_failures,
    harnesses,
    model_availability,
    plans,
    repository_evidence,
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
from tests.support import init_git_repo, write_plan

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


class EvidenceBundleTests(unittest.TestCase):
    """The audit input bundle is bounded, deterministic, and control-plane only."""

    SECRET_ARGV = "ghp_AAAAAAAAAAAAAAAAAAAA"
    SECRET_ENV = "ghp_BBBBBBBBBBBBBBBBBBBB"
    SECRET_PASSWORD = "correct-horse-battery"
    APPLICATION_SOURCE = "def transfer_funds(amount):\n    return amount * 2\n"

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
                            "path": str(workspace / "app"),
                            "origin": "https://user:pw@github.com/owner/repo.git",
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
            self.assertEqual("claude-opus-5", section["observed_model"])
            self.assertEqual("harness_reported", section["observed_model_source"])


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
                    "path": str(workspace / "app"),
                    "origin": "https://example.invalid/app.git",
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

    def test_a_registry_reaching_outside_the_workspace_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = self._history(root)
            init_git_repo(root / "kiln")
            payload = self._read_state(history[2])
            payload["repositories"].append(
                {
                    "alias": "kiln",
                    "path": str(root / "kiln"),
                    "origin": "https://example.invalid/kiln.git",
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
