"""Audit run flags, role resolution, planning, and run-config persistence."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from agent_session_loop import (
    events,
    run_entry,
    run_flags,
    run_payloads,
    state,
    supervisor_audit,
)
from agent_session_loop.assurance_models import (
    MAX_AUDIT_ATTEMPT_RECORDS,
    MAX_AUDIT_ATTEMPTS,
    MAX_AUDIT_DELIVERY_ATTEMPTS,
    MAX_AUDIT_SHRINK_ATTEMPTS,
    AssuranceError,
)
from tests.support_supervisor_audit import RUN_ID, namespace, parse


class AuditFlagSurfaceTests(unittest.TestCase):
    """The audit flags reach the CLI, supervisor, and resume parsers."""

    def test_cli_defaults_enable_audit_with_the_default_timeout(self) -> None:
        args = parse(["run", "/tmp/plan"])

        self.assertTrue(args.supervisor_audit)
        self.assertEqual(600, args.audit_timeout)
        self.assertIsNone(args.audit_harness)
        self.assertIsNone(args.audit_harness_model)
        self.assertIsNone(args.audit_effort)

    def test_cli_accepts_every_audit_flag(self) -> None:
        args = parse(
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
            parse(["run", "/tmp/plan", "--supervisor-audit", "--no-supervisor-audit"])

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
            namespace(harness="codex", audit_harness="codex")
        )

        self.assertEqual(
            ["--audit-harness must differ from --harness (both set to 'codex')"],
            [error.message for error in errors],
        )

    def test_audit_harness_may_equal_review_harness(self) -> None:
        errors = run_flags.validate_run_namespace(
            namespace(harness="codex", review_harness="claude", audit_harness="claude")
        )

        self.assertEqual([], errors)

    def test_audit_harness_model_does_not_require_audit_harness(self) -> None:
        # An explicit audit model also overrides the model an inherited review
        # role would have supplied, so it is meaningful on its own.
        errors = run_flags.validate_run_namespace(
            namespace(
                harness="codex",
                review_harness="claude",
                audit_harness_model="claude-opus-4",
            )
        )

        self.assertEqual([], errors)

    def test_audit_timeout_must_be_positive(self) -> None:
        errors = run_flags.validate_run_namespace(namespace(audit_timeout=0))

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
        return supervisor_audit.plan_audit(namespace(**overrides)).role

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

    def test_an_unpermitted_audit_harness_is_refused_at_resolution(self) -> None:
        # `asl assurance process-pending --audit-harness` takes a free string,
        # so the closed vocabulary is applied where the role is built rather
        # than trusted from the flag.
        config = supervisor_audit.AuditConfig(harness="gpt")

        with self.assertRaises(AssuranceError):
            supervisor_audit.resolve_role(config, implementation_harness="codex")

    def test_an_unpermitted_implementation_harness_is_refused_at_resolution(
        self,
    ) -> None:
        config = supervisor_audit.AuditConfig(harness="codex")

        with self.assertRaises(AssuranceError):
            supervisor_audit.resolve_role(config, implementation_harness="gpt")

    def test_an_equal_unpermitted_pair_is_refused_rather_than_read_as_no_role(
        self,
    ) -> None:
        # Equality alone does not mean "no independent role": two names outside
        # the vocabulary describe a malformed configuration, so the closed
        # vocabulary is applied before the pair is compared.
        config = supervisor_audit.AuditConfig(harness="gpt")

        with self.assertRaises(AssuranceError):
            supervisor_audit.resolve_role(config, implementation_harness="gpt")

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

    def test_answer_delivery_and_shrink_budgets_are_separate(self) -> None:
        # Three different failures, three budgets: a bad answer is the agent's
        # doing and stays cheap, an undelivered request is the provider's and
        # gets its own longer budget, and a request refused for size is the
        # caller's own and buys exactly one smaller retry. The record cap admits
        # all three spent in full, so no attempt an audit really made is
        # unrecordable.
        self.assertEqual(2, MAX_AUDIT_ATTEMPTS)
        self.assertEqual(4, MAX_AUDIT_DELIVERY_ATTEMPTS)
        self.assertEqual(1, MAX_AUDIT_SHRINK_ATTEMPTS)
        self.assertEqual(
            MAX_AUDIT_ATTEMPTS
            + MAX_AUDIT_DELIVERY_ATTEMPTS
            + MAX_AUDIT_SHRINK_ATTEMPTS,
            MAX_AUDIT_ATTEMPT_RECORDS,
        )


class AuditPlanTests(unittest.TestCase):
    """The enable/skip decision and the assurance object a run starts with."""

    def test_enabled_run_with_a_role_is_pending(self) -> None:
        plan = supervisor_audit.plan_audit(
            namespace(harness="codex", review_harness="claude")
        )

        self.assertEqual("pending", plan.state)
        self.assertIsNone(plan.reason)
        self.assertEqual("claude", plan.role.harness)

    def test_opting_out_skips_without_resolving_a_role(self) -> None:
        plan = supervisor_audit.plan_audit(
            namespace(harness="codex", review_harness="claude", supervisor_audit=False)
        )

        self.assertEqual("skipped", plan.state)
        self.assertEqual(supervisor_audit.REASON_OPTED_OUT, plan.reason)
        self.assertIsNone(plan.role)

    def test_no_independent_role_stays_enabled_and_ends_unavailable(self) -> None:
        plan = supervisor_audit.plan_audit(namespace(harness="codex"))

        self.assertTrue(plan.config.enabled)
        self.assertEqual("unavailable", plan.state)
        self.assertEqual(supervisor_audit.REASON_NO_INDEPENDENT_ROLE, plan.reason)
        self.assertIsNone(plan.role)

    def test_an_equal_unpermitted_pair_is_not_planned_as_unavailable(self) -> None:
        # "No independent role" is a statement about two *permitted* harnesses
        # being the same one. A pair that is outside the vocabulary is a
        # malformed configuration and must raise instead of planning a run
        # whose audit is merely recorded as unavailable.
        with self.assertRaises(AssuranceError):
            supervisor_audit.plan_audit(namespace(harness="gpt", audit_harness="gpt"))

    def test_pending_is_persisted_only_for_an_eligible_run(self) -> None:
        plan = supervisor_audit.plan_audit(
            namespace(harness="codex", review_harness="claude")
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
                namespace(harness="codex", supervisor_audit=False)
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
                namespace(harness="codex", review_harness="claude")
            ),
            run_id=RUN_ID,
            eligible=True,
        )
        unavailable = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(namespace(harness="codex")),
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
        args = parse(["run", "/tmp/plan", *tokens])
        return run_payloads._run_config_payload(
            args, cwd=Path("/tmp"), commit_mode=None
        )

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

            args = run_entry._apply_resume_config(parse(tokens), tokens[1:])

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

            args = run_entry._apply_resume_config(parse(tokens), tokens[1:])

            self.assertFalse(args.supervisor_audit)
            self.assertEqual(120, args.audit_timeout)
            # Unpassed flags still rehydrate.
            self.assertEqual("claude", args.audit_harness)

    def test_resume_command_reconstruction_restates_an_explicit_role(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                namespace(
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

        command = run_entry._reconstruct_resume_command(
            "/tmp/plan", None, {"assurance": summary.to_dict()}
        )

        self.assertIn("--audit-harness claude", command)
        self.assertIn("--audit-harness-model claude-opus-4", command)
        self.assertIn("--audit-effort high", command)
        self.assertIn("--audit-timeout 900", command)

    def test_resume_command_reconstruction_restates_an_opt_out(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                namespace(harness="codex", supervisor_audit=False)
            ),
            run_id=RUN_ID,
            eligible=True,
        )

        command = run_entry._reconstruct_resume_command(
            "/tmp/plan", None, {"assurance": summary.to_dict()}
        )

        self.assertIn("--no-supervisor-audit", command)
        self.assertNotIn("--audit-harness", command)

    def test_reconstruction_leaves_an_inherited_role_to_the_review_flag(self) -> None:
        summary = supervisor_audit.initial_summary(
            supervisor_audit.plan_audit(
                namespace(
                    harness="codex",
                    review_harness="claude",
                    review_harness_model="claude-opus-4",
                )
            ),
            run_id=RUN_ID,
            eligible=True,
        )

        command = run_entry._reconstruct_resume_command(
            "/tmp/plan",
            None,
            {"review_harness": "claude", "assurance": summary.to_dict()},
        )

        self.assertIn("--review-harness claude", command)
        self.assertNotIn("--audit-harness ", command)
        # The effective model is restated so the rebuilt role is identical.
        self.assertIn("--audit-harness-model claude-opus-4", command)
        self.assertNotIn("--audit-timeout", command)


if __name__ == "__main__":
    unittest.main()
