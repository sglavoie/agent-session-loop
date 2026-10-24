"""Test asl workbench recovery kinds."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    workbench_recovery_build,
    workbench_recovery_models,
)
from tests import support_asl_workbench_recovery as fixtures
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
    _structured_review_finding,
    write_completed_review_plan,
    write_review_cycle_event,
)

ASL_TEST_PORTABLE = True


class WorkbenchRecoveryTests(fixtures.WorkbenchRecoveryCase):
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


if __name__ == "__main__":
    unittest.main()
