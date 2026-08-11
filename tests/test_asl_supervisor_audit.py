"""Audit run configuration, independent role resolution, and persistence."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from agent_session_loop import assurance_ledger
from agent_session_loop import cli
from agent_session_loop import events
from agent_session_loop import harnesses
from agent_session_loop import plans
from agent_session_loop import run_flags
from agent_session_loop import runner
from agent_session_loop import state
from agent_session_loop import supervisor_audit
from agent_session_loop.assurance_models import (
    MAX_AUDIT_ATTEMPTS,
    AssuranceError,
    AuditRole,
)

from tests.support import init_git_repo, write_plan


RUN_ID = "20260810T101112123456Z-4242"


def _parse(tokens: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(tokens)


def _namespace(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
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
            _namespace(
                harness="codex", review_harness="claude", audit_harness="claude"
            )
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
        role = self._role(
            harness="codex", review_harness="claude", audit_timeout=900
        )

        self.assertEqual(900.0, role.timeout_seconds)
        self.assertEqual(
            (900.0,) * MAX_AUDIT_ATTEMPTS, supervisor_audit.attempt_timeouts(role)
        )
        self.assertEqual(2, MAX_AUDIT_ATTEMPTS)


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
            _namespace(
                harness="codex", review_harness="claude", supervisor_audit=False
            )
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

        eligible = supervisor_audit.initial_summary(
            plan, run_id=RUN_ID, eligible=True
        )
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
        return runner._run_config_payload(
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
            payload = self._initial_payload(
                Path(tmp), ["--review-harness", "claude"]
            )

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
            self.assertEqual([600.0, 600.0], audit["attempt_timeouts"])

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
                "Supervisor audit: pending (claude via review role, "
                "600s per attempt)",
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

    def __init__(self, *, writes: Path | None = None, returncode: int = 0) -> None:
        self.writes = writes
        self.returncode = returncode
        self.calls: list[dict[str, object]] = []

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
        return harnesses.HarnessResult(
            argv=argv,
            returncode=self.returncode,
            stdout="",
            stderr="",
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
    ) -> supervisor_audit.AuditAttemptResult:
        return supervisor_audit.run_restricted_audit(
            self._role(harness),
            run_dir=run_dir,
            run_id=RUN_ID,
            workspace_root=workspace,
            plan_dir=plan_dir,
            prompt="Audit this run.",
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
                state_dir=state_dir,
                _run_harness_fn=fake,
            )

            self.assertEqual("unavailable", result.outcome)
            self.assertEqual("profile-unenforceable", result.reason)
            self.assertEqual([], fake.calls)


if __name__ == "__main__":
    unittest.main()
