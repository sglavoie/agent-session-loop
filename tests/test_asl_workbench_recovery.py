"""Workbench recovery tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    intervention,
    preflight_runtime_hints,
    redirect_cli,
    review_continuation,
    review_repair,
    run_inputs,
    state,
    workbench_launch,
    workbench_recovery_build,
    workbench_recovery_models,
    workbench_registry_models,
)
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
    _make_workbench_plan,
    _structured_review_finding,
    _write_lock_file,
    _write_state_payload,
    write_completed_review_plan,
    write_review_cycle_event,
)


class WorkbenchRecoveryTests(unittest.TestCase):
    def _recovery_for_payload(
        self,
        tmp_path: Path,
        payload: dict[str, object] | None,
        *,
        plan_name: str,
        lock_pid: int | None = None,
        pid_running=None,
    ) -> workbench_recovery_models.WorkbenchRecovery:
        plan_dir = _make_workbench_plan(tmp_path, plan_name)
        state_root = tmp_path / "state"
        if payload is not None:
            _write_state_payload(plan_dir, state_root, payload)
        if lock_pid is not None:
            _write_lock_file(plan_dir, state_root, pid=lock_pid)
        discovered = _discovered(plan_dir)
        if pid_running is None:
            return workbench_recovery_build.build_workbench_recovery(
                discovered, state_dir=state_root
            )
        with mock.patch.object(state, "is_pid_running", side_effect=pid_running):
            return workbench_recovery_build.build_workbench_recovery(
                discovered, state_dir=state_root
            )

    def test_ready_plan_has_no_recovery_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            )

            self.assertEqual("none", recovery.kind)
            self.assertFalse(recovery.recoverable)
            payload = recovery.to_payload()
            self.assertEqual("none", payload["compact"]["kind"])

    def test_review_budget_recovery_reports_required_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "budget-review",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "cycles-disabled",
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("review-continuation", recovery.kind)
            self.assertEqual("Higher review-fix cap required", recovery.title)
            self.assertIn("1 or higher", recovery.summary)
            self.assertIn("--review-harness", recovery.summary)
            action = recovery.actions[0]
            self.assertEqual("review-profile", action.id)
            self.assertEqual("profile", action.target)
            self.assertIn("--review-harness", action.description)
            self.assertIn("--max-review-fix-cycles", action.description)
            payload = recovery.to_payload()
            self.assertEqual(
                "higher-cap-required",
                payload["review_resume"]["blocker"],
            )
            self.assertEqual(
                payload["review_resume"],
                payload["compact"]["review_resume"],
            )
            launch_argv = workbench_launch.build_launch_argv(
                plan_dir,
                profile=workbench_registry_models.LaunchProfile(
                    harness="codex",
                    review_harness="claude",
                    max_review_fix_cycles=payload["review_resume"][
                        "minimum_required_cap"
                    ],
                ),
            )
            self.assertEqual(
                "claude", launch_argv[launch_argv.index("--review-harness") + 1]
            )
            self.assertEqual(
                str(payload["review_resume"]["minimum_required_cap"]),
                launch_argv[launch_argv.index("--max-review-fix-cycles") + 1],
            )

    def test_completed_review_repairs_recommend_rereview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "rereview",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            materialized = review_repair.materialize_review_repair_sessions(
                plan_dir, state_dir=state_root
            )
            self.assertTrue(materialized.ok, materialized.error)
            child = materialized.children[0].path
            child.write_text(
                child.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete"
                ),
                encoding="utf-8",
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("review-continuation", recovery.kind)
            self.assertEqual("rereview", recovery.review_resume["action"])
            self.assertIn("review", recovery.summary.lower())
            self.assertIn("--review-harness", recovery.summary)
            self.assertEqual("review-profile", recovery.actions[0].id)
            self.assertTrue(recovery.recoverable)

    def test_ready_continuation_names_cap_above_runner_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path, "ready-continuation")
            required_cap = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES + 1
            resume = review_continuation.ReviewResume(
                source_cycle=2,
                review_session_id="2",
                source_disposition="cycles-disabled",
                action="materialize-repairs",
                eligible=True,
                blocker=None,
                historical_materializations=2,
                minimum_required_cap=required_cap,
                selected_cap=run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            )

            with mock.patch.object(
                review_continuation,
                "evaluate_review_continuation",
                return_value=resume,
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=tmp_path / "state"
                )

            self.assertEqual("Review continuation ready", recovery.title)
            self.assertIn("--review-harness", recovery.summary)
            self.assertIn(
                f"--max-review-fix-cycles to {required_cap}", recovery.summary
            )
            self.assertIn(f"{required_cap} or higher", recovery.actions[0].description)

    def test_ready_continuation_exhausted_group_avoids_cap_guidance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path, "ready-continuation-exhausted")
            required_cap = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES + 1
            resume = review_continuation.ReviewResume(
                source_cycle=2,
                review_session_id="2",
                source_disposition="cycle-cap-exhausted",
                action="materialize-repairs",
                eligible=True,
                blocker=None,
                historical_materializations=2,
                minimum_required_cap=required_cap,
                selected_cap=run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
                max_repair_group_attempts=2,
                repair_group_attempts={"runner": 2},
                repair_groups=(
                    {
                        "repair_group": "runner",
                        "repair_session_id": "2.1",
                        "finding_ids": ["F1"],
                    },
                ),
            )

            with mock.patch.object(
                review_continuation,
                "evaluate_review_continuation",
                return_value=resume,
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=tmp_path / "state"
                )

            self.assertEqual("Review continuation ready", recovery.title)
            self.assertIn('repair group "runner"', recovery.summary)
            self.assertIn("2 attempts", recovery.summary)
            self.assertIn("fix it manually or re-split", recovery.summary)
            self.assertNotIn(
                f"--max-review-fix-cycles to {required_cap}", recovery.summary
            )
            self.assertEqual("inspect-artifacts", recovery.actions[0].id)
            self.assertNotIn("--max-review-fix-cycles", recovery.actions[0].description)

    def test_ready_rereview_omits_cap_above_runner_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path, "ready-rereview")
            required_cap = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES + 1
            resume = review_continuation.ReviewResume(
                source_cycle=2,
                review_session_id="2",
                source_disposition="repairs-materialized",
                action="rereview",
                eligible=True,
                blocker=None,
                historical_materializations=2,
                minimum_required_cap=required_cap,
                selected_cap=run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            )

            with mock.patch.object(
                review_continuation,
                "evaluate_review_continuation",
                return_value=resume,
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=tmp_path / "state"
                )

            self.assertEqual("Review continuation ready", recovery.title)
            self.assertIn("--review-harness", recovery.summary)
            self.assertIn("--review-harness", recovery.actions[0].description)
            self.assertNotIn("--max-review-fix-cycles", recovery.summary)
            self.assertNotIn("--max-review-fix-cycles", recovery.actions[0].description)

    def test_state_read_warning_details_cover_stable_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cases = (
                (
                    "state-malformed-json",
                    lambda path: path.write_text("{", encoding="utf-8"),
                    "state.json unreadable:",
                ),
                (
                    "state-unreadable",
                    lambda path: path.mkdir(),
                    "state.json unreadable:",
                ),
                (
                    "state-invalid-json-shape",
                    lambda path: path.write_text("[]", encoding="utf-8"),
                    "state.json did not contain a JSON object",
                ),
            )

            for expected_code, write_state, expected_prefix in cases:
                with self.subTest(code=expected_code):
                    plan_dir = _make_workbench_plan(
                        tmp_path, name=f"warning-{expected_code}"
                    )
                    state_root = tmp_path / "state"
                    state_path = (
                        state.plan_state_dir(plan_dir, state_root)
                        / events.STATE_FILE_NAME
                    )
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    write_state(state_path)

                    recovery = workbench_recovery_build.build_workbench_recovery(
                        _discovered(plan_dir), state_dir=state_root
                    )
                    payload = recovery.to_payload()

                    self.assertEqual(1, len(recovery.warnings))
                    self.assertTrue(recovery.warnings[0].startswith(expected_prefix))
                    self.assertEqual(list(recovery.warnings), payload["warnings"])
                    self.assertEqual(1, len(recovery.warning_details))
                    self.assertEqual(1, len(payload["warning_details"]))
                    self.assertEqual(expected_code, recovery.warning_details[0].code)
                    self.assertEqual(
                        recovery.warnings[0], recovery.warning_details[0].message
                    )
                    self.assertEqual("warning", recovery.warning_details[0].severity)
                    self.assertEqual(
                        {
                            "code": expected_code,
                            "message": recovery.warnings[0],
                            "severity": "warning",
                        },
                        payload["warning_details"][0],
                    )
                    self.assertEqual(1, payload["compact"]["warning_count"])
                    self.assertNotIn("warning_details", payload["compact"])

    def test_state_missing_race_warning_detail_has_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            state_path = (
                state.plan_state_dir(plan_dir, state_root) / events.STATE_FILE_NAME
            )
            original_exists = Path.exists
            original_read_text = Path.read_text

            def exists_with_state_race(path: Path) -> bool:
                if path == state_path:
                    return True
                return original_exists(path)

            def read_text_with_state_race(path: Path, *args, **kwargs) -> str:
                if path == state_path:
                    raise FileNotFoundError(state_path)
                return original_read_text(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "exists", new=exists_with_state_race),
                mock.patch.object(Path, "read_text", new=read_text_with_state_race),
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=state_root
                )

            payload = recovery.to_payload()

            self.assertEqual(
                ("state.json disappeared before it could be read",),
                recovery.warnings,
            )
            self.assertEqual(
                [
                    {
                        "code": "state-missing-race",
                        "message": recovery.warnings[0],
                        "severity": "warning",
                    }
                ],
                payload["warning_details"],
            )

    def test_seeded_state_fixtures_cover_every_recovery_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cases: tuple[tuple[str, dict[str, object] | None, dict], ...] = (
                ("none", None, {}),
                (
                    "reply-needed",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "agent-question",
                        "pause_reason": "agent-question",
                        "pause_message": "Need an operator decision",
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 1,
                                "last_message_path": str(
                                    tmp_path / "reply" / "last-message.txt"
                                ),
                            }
                        ],
                        "run_id": "run-reply",
                    },
                    {},
                ),
                (
                    "not-loop-ready",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "not-loop-ready",
                        "pause_reason": "not-loop-ready",
                        "pause_message": "preflight failed",
                        "pause_details": {"readiness_state": "reviewed"},
                        "run_id": "run-ready",
                    },
                    {},
                ),
                (
                    "verify-failed",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "verify-failed",
                        "pause_reason": "verify-failed",
                        "pause_message": "verification failed",
                        "pause_details": {
                            "failures": [
                                {"name": "unit", "failure_reason": "command-failed"}
                            ]
                        },
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 2,
                                "verification": {
                                    "reason": "verify-failed",
                                    "passed": False,
                                    "checks": [
                                        {
                                            "name": "unit",
                                            "status": "fail",
                                            "failure_reason": "command-failed",
                                        }
                                    ],
                                },
                            }
                        ],
                        "run_id": "run-verify",
                    },
                    {},
                ),
                (
                    "no-contract",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "no-contract",
                        "pause_reason": "no-contract",
                        "pause_message": "session has no Completion Contract",
                        "pause_details": {"session_id": "1"},
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 3,
                                "verification": {
                                    "reason": "no-contract",
                                    "passed": False,
                                    "checks": [],
                                },
                            }
                        ],
                        "run_id": "run-no-contract",
                    },
                    {},
                ),
                (
                    "malformed-contract",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "malformed-contract",
                        "pause_reason": "malformed-contract",
                        "pause_message": "Completion Contract JSON is invalid",
                        "pause_details": {"session_id": "1"},
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 4,
                                "verification": {
                                    "reason": "malformed-contract",
                                    "passed": False,
                                    "checks": [],
                                },
                            }
                        ],
                        "run_id": "run-malformed-contract",
                    },
                    {},
                ),
                (
                    "stale-lock",
                    None,
                    {
                        "lock_pid": _WORKBENCH_DEAD_PID,
                        "pid_running": lambda pid: pid == os.getpid(),
                    },
                ),
                (
                    "stale-state",
                    {
                        "ok": True,
                        "paused": False,
                        "run_id": "run-orphan",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": None,
                        "active_turn": None,
                        "active_verification": None,
                    },
                    {},
                ),
                (
                    "commit-preflight",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "commit-preflight",
                        "pause_reason": "commit-preflight",
                        "pause_message": "worktree is dirty",
                        "run_id": "run-commit-preflight",
                    },
                    {},
                ),
                (
                    "commit-failed",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": "commit-failed",
                        "pause_reason": "commit-failed",
                        "pause_message": "commit failed",
                        "pause_details": {
                            "commit": {
                                "session_id": "1",
                                "commit_index": 1,
                                "success": False,
                                "status_result": "nonzero-exit",
                            }
                        },
                        "run_id": "run-commit-failed",
                    },
                    {},
                ),
                # Both recovery-decision pauses reach the same kind, and both are
                # enumerated: the kind is only useful if it carries the decision
                # apart, and a single case would let the second pause regress to
                # whichever card the first one happens to build.
                (
                    "conversation-recovery",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
                        "pause_reason": (
                            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
                        ),
                        "pause_message": "recorded conversation cannot be resumed",
                        "run_id": "run-fresh-conversation",
                    },
                    {},
                ),
                (
                    "conversation-recovery",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
                        "pause_reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
                        "pause_message": "predecessor run could not be shown stopped",
                        "run_id": "run-successor-blocked",
                    },
                    {},
                ),
                (
                    "generic",
                    {
                        "ok": False,
                        "paused": False,
                        "reason": "nonzero-exit",
                        "run_id": "run-generic",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": "2026-06-19T12:05:00+00:00",
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 1,
                                "returncode": 2,
                            }
                        ],
                    },
                    {},
                ),
            )
            observed: set[str] = set()
            # Indexed rather than named by kind: more than one pause reaches the
            # same kind, and two cases sharing a plan directory would have the
            # second silently overwrite the first one's seeded state.
            for index, (expected_kind, payload, options) in enumerate(cases):
                with self.subTest(case=index, kind=expected_kind):
                    recovery = self._recovery_for_payload(
                        tmp_path,
                        payload,
                        plan_name=f"kind-{index:02d}-{expected_kind}",
                        **options,
                    )
                    self.assertEqual(expected_kind, recovery.kind)
                    observed.add(recovery.kind)

            continuation_plan = write_completed_review_plan(
                tmp_path / "kind-review-continuation",
                [_structured_review_finding()],
            )
            write_review_cycle_event(
                continuation_plan,
                tmp_path / "state",
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "cycles-disabled",
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            )
            continuation = workbench_recovery_build.build_workbench_recovery(
                _discovered(continuation_plan), state_dir=tmp_path / "state"
            )
            self.assertEqual("review-continuation", continuation.kind)
            observed.add(continuation.kind)

            self.assertEqual(set(workbench_recovery_models.RECOVERY_KINDS), observed)

    def test_conversation_recovery_reports_the_decision_and_its_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            recovery = self._recovery_for_payload(
                tmp_path,
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
                    "pause_reason": (
                        events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
                    ),
                    "pause_message": "recorded conversation cannot be resumed",
                    "run_id": "run-fresh",
                },
                plan_name="fresh-conversation",
            )

            self.assertEqual("conversation-recovery", recovery.kind)
            self.assertTrue(recovery.recoverable)
            expected = redirect_cli.recovery_commands(
                recovery.plan_dir,
                str(state_root),
                decision=intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
            )
            self.assertEqual(
                [expected["inspect_command"], expected["authorization_command"]],
                [command.command for command in recovery.commands],
            )
            # The card never performs the recovery: consuming a one-use
            # authorization has to be a deliberate command, not a POST the
            # browser can repeat.
            self.assertNotIn("endpoint", {action.kind for action in recovery.actions})
            self.assertIn(
                redirect_cli.FRESH_CONVERSATION_NOTE,
                [row.detail for row in recovery.evidence],
            )
            self.assertIn(
                intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
                [row.value for row in recovery.evidence],
            )

    def test_blocked_successor_recovery_offers_no_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recovery = self._recovery_for_payload(
                tmp_path,
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
                    "pause_reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
                    "pause_message": "predecessor run could not be shown stopped",
                    "run_id": "run-blocked",
                },
                plan_name="successor-blocked",
            )

            self.assertEqual("conversation-recovery", recovery.kind)
            self.assertEqual("error", recovery.severity)
            # One command, and it is the read. Offering a second would imply an
            # authorization exists for a pause that has none.
            self.assertEqual(1, len(recovery.commands))
            self.assertIn("redirect-status", recovery.commands[0].command)
            self.assertNotIn(
                redirect_cli.FRESH_CONVERSATION_NOTE,
                [row.detail for row in recovery.evidence],
            )
            self.assertIn(
                intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE,
                [row.value for row in recovery.evidence],
            )

    def test_submission_recovery_offers_inspection_and_an_explicit_decision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = self._recovery_for_payload(
                Path(tmp),
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
                    "pause_reason": events.PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
                    "pause_message": "The native submission outcome is unknown",
                    "run_id": "run-uncertain",
                },
                plan_name="submission-uncertain",
            )
            self.assertEqual("conversation-recovery", recovery.kind)
            self.assertEqual(2, len(recovery.commands))
            self.assertIn("redirect-status", recovery.commands[0].command)
            self.assertIn("--outcome OUTCOME", recovery.commands[1].command)
            self.assertIn("resolve-redirect", recovery.commands[1].command)
            self.assertTrue(
                all(action.kind != "endpoint" for action in recovery.actions)
            )

    def test_registered_pause_reasons_have_explicit_recovery_mapping(self) -> None:
        mapped_reasons = set(
            workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON
        )
        generic_reasons = set(workbench_recovery_models.GENERIC_RECOVERY_PAUSE_REASONS)
        registered_reasons = set(events.PAUSE_REASONS)

        self.assertFalse(mapped_reasons & generic_reasons)
        self.assertEqual(registered_reasons, mapped_reasons | generic_reasons)
        self.assertNotIn(
            "generic",
            set(
                workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON.values()
            ),
        )
        self.assertLessEqual(
            set(
                workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON.values()
            ),
            set(workbench_recovery_models.RECOVERY_KINDS) - {"generic"},
        )
        self.assertIn(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            generic_reasons,
        )

    def test_generic_pause_reason_allowlist_uses_generic_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-exit" / "turn-0001"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "nonzero-exit",
                    "pause_reason": "nonzero-exit",
                    "pause_message": "harness exited with status 2",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "returncode": 2,
                            "result_path": str(artifact_dir / "result.json"),
                        }
                    ],
                    "run_id": "run-exit",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("generic", recovery.kind)
            action_targets = {action.target for action in recovery.actions}
            self.assertNotIn("reply", action_targets)

    def test_unknown_pause_reason_falls_back_by_runner_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            unknown_reason = "new-runner-reason"
            cases = (
                (
                    "paused",
                    "reply-needed",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": unknown_reason,
                        "pause_reason": unknown_reason,
                        "pause_message": "operator input required",
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 2,
                                "last_message_path": str(
                                    tmp_path
                                    / "runs"
                                    / "run-paused"
                                    / "turn-0002"
                                    / "last-message.txt"
                                ),
                            }
                        ],
                        "run_id": "run-paused",
                    },
                    "reply",
                ),
                (
                    "finished",
                    "generic",
                    {
                        "ok": False,
                        "paused": False,
                        "reason": unknown_reason,
                        "pause_message": "runner stopped with a new reason",
                        "run_id": "run-finished",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": "2026-06-19T12:05:00+00:00",
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 2,
                                "returncode": 2,
                                "result_path": str(
                                    tmp_path
                                    / "runs"
                                    / "run-finished"
                                    / "turn-0002"
                                    / "result.json"
                                ),
                            }
                        ],
                    },
                    "launch",
                ),
            )

            for name, expected_kind, payload, expected_target in cases:
                with self.subTest(lifecycle=name):
                    plan_dir = _make_workbench_plan(tmp_path, name=f"unknown-{name}")
                    _write_state_payload(plan_dir, state_root, payload)

                    recovery = workbench_recovery_build.build_workbench_recovery(
                        _discovered(plan_dir), state_dir=state_root
                    )

                    self.assertEqual(expected_kind, recovery.kind)
                    self.assertEqual("1", recovery.session_id)
                    self.assertEqual(2, recovery.turn_index)
                    action_targets = {action.target for action in recovery.actions}
                    self.assertIn(expected_target, action_targets)
                    if expected_kind == "generic":
                        self.assertNotIn("reply", action_targets)

    def test_not_loop_ready_recovery_surfaces_gate_and_repair_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            gate = {
                "ok": False,
                "readiness_state": "reviewed",
                "expected_readiness_sha256": "e" * 64,
                "recorded_readiness_sha256": "r" * 64,
                "findings_by_severity": {"red": 1, "yellow": 0, "green": 0},
                "findings": [
                    {
                        "severity": "red",
                        "category": "manifest-readiness",
                        "message": "readiness state is reviewed",
                    }
                ],
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "not-loop-ready",
                    "pause_reason": "not-loop-ready",
                    "pause_message": "preflight failed: 1 red finding(s)",
                    "pause_details": gate,
                    "readiness_gate": gate,
                    "run_id": "run-ready",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("not-loop-ready", recovery.kind)
            self.assertTrue(recovery.recoverable)
            self.assertGreaterEqual(len(recovery.commands), 2)
            self.assertIn("--mark-loop-ready", recovery.commands[0].command)
            self.assertTrue(
                any(
                    "plan-preflight-iterate" in command.command
                    for command in recovery.commands
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/launch" and action["target"] == "launch"
                    for action in action_payload
                )
            )
            evidence_payload = [row.to_payload() for row in recovery.evidence]
            self.assertTrue(
                any(row["value"] == "manifest-readiness" for row in evidence_payload)
            )

    def test_reply_needed_recovery_uses_latest_paused_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            older_artifact = tmp_path / "runs" / "run-reply" / "turn-0001"
            latest_artifact = tmp_path / "runs" / "run-reply" / "turn-0002"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "pause_reason": "agent-question",
                    "pause_message": "Need a decision",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "last_message_path": str(
                                older_artifact / "last-message.txt"
                            ),
                            "result_path": str(older_artifact / "result.json"),
                        },
                        {
                            "session_id": "2",
                            "turn_index": 2,
                            "last_message_path": str(
                                latest_artifact / "last-message.txt"
                            ),
                            "result_path": str(latest_artifact / "result.json"),
                        },
                    ],
                    "run_id": "run-reply",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("reply-needed", recovery.kind)
            self.assertEqual("2", recovery.session_id)
            self.assertEqual(2, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "paused-turn"
                    and artifact["path"] == str(latest_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "paused-turn"
                    and artifact["path"] == str(older_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/reply" and action["target"] == "reply"
                    for action in action_payload
                )
            )

    def test_stale_lock_recovery_unlocks_through_existing_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_lock_file(plan_dir, state_root, pid=_WORKBENCH_DEAD_PID)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=state_root
                )

            self.assertEqual("stale-lock", recovery.kind)
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/unlock" and action["target"] == "unlock"
                    for action in action_payload
                )
            )

    def test_stale_state_recovery_unlocks_orphaned_runner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run-orphan",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("stale-state", recovery.kind)
            self.assertIn("no lock file", recovery.summary)
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/unlock" and action["target"] == "unlock"
                    for action in action_payload
                )
            )

    def test_stale_state_precedes_stale_lock_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run-orphan",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=_WORKBENCH_DEAD_PID)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=state_root
                )

            self.assertEqual("stale-state", recovery.kind)
            self.assertIn("lock is stale", recovery.summary)
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/unlock" and action["target"] == "unlock"
                    for action in action_payload
                )
            )

    def test_active_run_precedes_paused_recovery_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-active" / "turn-0002"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed",
                    "run_id": "run-active",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": {
                        "session_id": "1",
                        "turn_index": 2,
                        "result_path": str(artifact_dir / "result.json"),
                    },
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("none", recovery.kind)
            self.assertEqual("Run in progress", recovery.title)
            self.assertFalse(recovery.recoverable)
            self.assertEqual("1", recovery.session_id)
            self.assertEqual(2, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "active-turn"
                    and artifact["path"] == str(artifact_dir / "result.json")
                    for artifact in artifact_payload
                )
            )

    def test_verify_failed_recovery_uses_runtime_hint_and_failed_turn_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-verify" / "turn-0001"
            result_path = artifact_dir / "result.json"
            turn = {
                "run_id": "run-verify",
                "session_id": "1",
                "turn_index": 1,
                "artifact_dir": str(artifact_dir),
                "stdout_path": str(artifact_dir / "stdout.log"),
                "stderr_path": str(artifact_dir / "stderr.log"),
                "last_message_path": str(artifact_dir / "last-message.txt"),
                "result_path": str(result_path),
                "verification": {
                    "session": "1",
                    "reason": "verify-failed",
                    "passed": False,
                    "checks": [
                        {
                            "name": "missing-file",
                            "status": "fail",
                            "failure_reason": "file-missing",
                        }
                    ],
                },
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed for session 1",
                    "pause_details": {
                        "failures": [
                            {
                                "name": "missing-file",
                                "failure_reason": "file-missing",
                            }
                        ]
                    },
                    "turns": [turn],
                    "run_id": "run-verify",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verify-failed", recovery.kind)
            self.assertEqual("1", recovery.session_id)
            self.assertEqual(1, recovery.turn_index)
            command_payload = [command.to_payload() for command in recovery.commands]
            self.assertTrue(
                any("asl verify" in command["command"] for command in command_payload)
            )
            self.assertTrue(
                any(
                    "asl preflight" in command["command"] for command in command_payload
                )
            )
            evidence_values = [row.value for row in recovery.evidence]
            self.assertIn("file-missing", evidence_values)
            self.assertTrue(
                any(
                    row.value and "Create the expected artifact" in row.value
                    for row in recovery.evidence
                )
            )
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(result_path)
                    for artifact in artifact_payload
                )
            )
            self.assertTrue(
                any(
                    row.label == "Failed check" and row.value == "missing-file"
                    for row in recovery.evidence
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/launch" and action["target"] == "launch"
                    for action in action_payload
                )
            )

    def test_verify_failed_recovery_follows_v2_check_artifact_references(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-verify" / "turn-0001"
            verification_dir = artifact_dir / "verification"
            stdout_artifact = verification_dir / "c1-stdout.gz"
            stderr_artifact = verification_dir / "c1-stderr.gz"
            turn = {
                "run_id": "run-verify",
                "session_id": "1",
                "turn_index": 1,
                "result_path": str(artifact_dir / "result.json"),
                "verification": {
                    "session": "1",
                    "reason": "verify-failed",
                    "passed": False,
                    "checks": [
                        {
                            "name": "c1",
                            "status": "fail",
                            "failure_reason": "command-failed",
                            "stdout_artifact_path": str(stdout_artifact),
                            "stderr_artifact_path": str(stderr_artifact),
                        }
                    ],
                },
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed for session 1",
                    "turns": [turn],
                    "run_id": "run-verify",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            check_refs = [
                artifact
                for artifact in artifact_payload
                if artifact["source"] == "verification-check"
            ]
            self.assertEqual(2, len(check_refs))
            paths = {artifact["path"] for artifact in check_refs}
            self.assertIn(str(stdout_artifact), paths)
            self.assertIn(str(stderr_artifact), paths)
            self.assertTrue(
                all(
                    artifact["kind"] == "verification-output" for artifact in check_refs
                )
            )

    def test_verify_failed_recovery_uses_latest_matching_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            older_artifact = tmp_path / "runs" / "run-verify" / "turn-0001"
            latest_artifact = tmp_path / "runs" / "run-verify" / "turn-0005"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "result_path": str(older_artifact / "result.json"),
                            "verification": {
                                "reason": "verify-failed",
                                "passed": False,
                                "checks": [
                                    {
                                        "name": "old",
                                        "status": "fail",
                                        "failure_reason": "command-failed",
                                    }
                                ],
                            },
                        },
                        {
                            "session_id": "2",
                            "turn_index": 5,
                            "result_path": str(latest_artifact / "result.json"),
                            "verification": {
                                "reason": "verify-failed",
                                "passed": False,
                                "checks": [
                                    {
                                        "name": "new",
                                        "status": "fail",
                                        "failure_reason": "file-error",
                                    }
                                ],
                            },
                        },
                    ],
                    "run_id": "run-verify",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verify-failed", recovery.kind)
            self.assertEqual("2", recovery.session_id)
            self.assertEqual(5, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(latest_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(older_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )

    def test_finished_verify_failed_uses_verification_failure_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-verify" / "turn-0003"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "reason": "verify-failed",
                    "pause_message": "verification failed after runner exit",
                    "run_id": "run-verify",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                    "turns": [
                        {
                            "session_id": "2",
                            "turn_index": 3,
                            "result_path": str(artifact_dir / "result.json"),
                            "verification": {
                                "reason": "verify-failed",
                                "passed": False,
                                "checks": [
                                    {
                                        "name": "unit",
                                        "status": "fail",
                                        "failure_reason": "command-failed",
                                    }
                                ],
                            },
                        }
                    ],
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verify-failed", recovery.kind)
            self.assertEqual("2", recovery.session_id)
            self.assertEqual(3, recovery.turn_index)
            command_payload = [command.to_payload() for command in recovery.commands]
            self.assertTrue(
                any("asl verify" in command["command"] for command in command_payload)
            )
            action_targets = {action.target for action in recovery.actions}
            self.assertIn("artifacts", action_targets)
            self.assertIn("launch", action_targets)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(artifact_dir / "result.json")
                    for artifact in artifact_payload
                )
            )

    def test_verify_failed_recovery_maps_every_failure_reason_to_runtime_hint(
        self,
    ) -> None:
        failure_reasons = (
            "command-failed",
            "command-timeout",
            "command-not-found",
            "command-error",
            "file-missing",
            "file-error",
            "regex-not-matched",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for failure_reason in failure_reasons:
                with self.subTest(failure_reason=failure_reason):
                    recovery = self._recovery_for_payload(
                        tmp_path,
                        {
                            "ok": False,
                            "paused": True,
                            "reason": "verify-failed",
                            "pause_reason": "verify-failed",
                            "pause_message": "verification failed",
                            "pause_details": {
                                "failures": [
                                    {
                                        "name": "check",
                                        "failure_reason": failure_reason,
                                    }
                                ]
                            },
                            "turns": [
                                {
                                    "session_id": "1",
                                    "turn_index": 1,
                                    "verification": {
                                        "reason": "verify-failed",
                                        "passed": False,
                                        "checks": [
                                            {
                                                "name": "check",
                                                "status": "fail",
                                                "failure_reason": failure_reason,
                                            }
                                        ],
                                    },
                                }
                            ],
                            "run_id": f"run-{failure_reason}",
                        },
                        plan_name=f"hint-{failure_reason}",
                    )
                    hint = preflight_runtime_hints.runtime_repair_hint(
                        "verify-failed", failure_reason=failure_reason
                    )
                    self.assertIsNotNone(hint)
                    assert hint is not None
                    self.assertTrue(
                        any(
                            row.label == "Repair hint"
                            and row.detail == hint.check_id
                            and row.value == hint.message
                            for row in recovery.evidence
                        )
                    )

    def test_generic_recovery_uses_latest_nonzero_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            older_artifact = tmp_path / "runs" / "run-exit" / "turn-0001"
            latest_artifact = tmp_path / "runs" / "run-exit" / "turn-0004"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "reason": "nonzero-exit",
                    "pause_message": "harness exited with return code 7",
                    "run_id": "run-exit",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "returncode": 2,
                            "result_path": str(older_artifact / "result.json"),
                        },
                        {
                            "session_id": "2",
                            "turn_index": 2,
                            "returncode": 0,
                            "result_path": str(
                                tmp_path / "runs" / "run-exit" / "turn-0002"
                            ),
                        },
                        {
                            "session_id": "3",
                            "turn_index": 4,
                            "returncode": 7,
                            "result_path": str(latest_artifact / "result.json"),
                        },
                    ],
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("generic", recovery.kind)
            self.assertEqual("3", recovery.session_id)
            self.assertEqual(4, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(latest_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(older_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )

    def test_commit_failed_recovery_prefers_failed_commit_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            active_artifact = tmp_path / "runs" / "run-commit" / "turn-0001"
            commit_artifact = tmp_path / "runs" / "run-commit" / "commit-0001-1"
            commit = {
                "run_id": "run-commit",
                "commit_index": 1,
                "scope": "session",
                "session_id": "1",
                "artifact_dir": str(commit_artifact),
                "stdout_path": str(commit_artifact / "stdout.log"),
                "stderr_path": str(commit_artifact / "stderr.log"),
                "result_path": str(commit_artifact / "result.json"),
                "success": False,
                "status_result": "nonzero-exit",
                "returncode": 23,
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "commit-failed",
                    "pause_reason": "commit-failed",
                    "pause_message": "auto-commit failed after session 1",
                    "pause_details": {"commit": commit},
                    "active_turn": {
                        "run_id": "run-commit",
                        "session_id": "1",
                        "turn_index": 1,
                        "artifact_dir": str(active_artifact),
                        "result_path": str(active_artifact / "result.json"),
                    },
                    "commits": [commit],
                    "run_id": "run-commit",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("commit-failed", recovery.kind)
            self.assertEqual(1, recovery.commit_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-commit"
                    and artifact["path"] == str(commit_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "active-turn" for artifact in artifact_payload
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(action["target"] == "launch" for action in action_payload)
            )
            self.assertTrue(
                any(action["target"] == "profile" for action in action_payload)
            )

    def test_commit_failed_recovery_matches_paused_and_finished_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            cases = (
                ("paused", True, None),
                ("finished", False, "2026-06-19T12:05:00+00:00"),
            )

            for name, paused, finished_at in cases:
                with self.subTest(lifecycle=name):
                    plan_dir = _make_workbench_plan(tmp_path, name=f"commit-{name}")
                    commit_artifact = (
                        tmp_path / "runs" / f"run-commit-{name}" / "commit-0002-1"
                    )
                    commit = {
                        "run_id": f"run-commit-{name}",
                        "commit_index": 1,
                        "scope": "session",
                        "session_id": "2",
                        "artifact_dir": str(commit_artifact),
                        "stdout_path": str(commit_artifact / "stdout.log"),
                        "stderr_path": str(commit_artifact / "stderr.log"),
                        "result_path": str(commit_artifact / "result.json"),
                        "success": False,
                        "status_result": "nonzero-exit",
                        "returncode": 23,
                    }
                    _write_state_payload(
                        plan_dir,
                        state_root,
                        {
                            "ok": False,
                            "paused": paused,
                            "reason": "commit-failed",
                            "pause_reason": "commit-failed" if paused else None,
                            "pause_message": "auto-commit failed after session 2",
                            "pause_details": {"commit": commit},
                            "run_id": f"run-commit-{name}",
                            "run_started_at": "2026-06-19T12:00:00+00:00",
                            "run_finished_at": finished_at,
                            "active_turn": None,
                            "active_verification": None,
                            "commits": [commit],
                        },
                    )

                    recovery = workbench_recovery_build.build_workbench_recovery(
                        _discovered(plan_dir), state_dir=state_root
                    )

                    self.assertEqual("commit-failed", recovery.kind)
                    self.assertEqual("2", recovery.session_id)
                    self.assertEqual(1, recovery.commit_index)
                    action_targets = {action.target for action in recovery.actions}
                    self.assertIn("artifacts", action_targets)
                    self.assertIn("launch", action_targets)
                    self.assertIn("profile", action_targets)
                    artifact_payload = [
                        artifact.to_payload() for artifact in recovery.artifacts
                    ]
                    self.assertTrue(
                        any(
                            artifact["source"] == "failed-commit"
                            and artifact["path"] == str(commit_artifact / "result.json")
                            for artifact in artifact_payload
                        )
                    )

    def test_harness_unavailable_recovery_guides_to_profile_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            config_path = tmp_path / "harnesses.json"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "harness-unavailable",
                    "pause_reason": "harness-unavailable",
                    "pause_message": "claude executable not found",
                    "pause_details": {
                        "session_id": "1",
                        "requested_harness": "claude",
                        "config_path": str(config_path),
                        "error": "claude executable not found on PATH",
                    },
                    "run_id": "run-harness",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("generic", recovery.kind)
            self.assertIn("Harness", recovery.title)
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(action["target"] == "profile" for action in action_payload)
            )
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["kind"] == "config"
                    and artifact["path"] == str(config_path)
                    for artifact in artifact_payload
                )
            )


if __name__ == "__main__":
    unittest.main()
