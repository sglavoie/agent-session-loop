"""Asl acceptance audit regressions."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    assurance_models_summary,
    events,
    plans_metadata,
    plans_session_scope,
    state,
    supervisor_audit,
    supervisor_audit_evidence_input,
)
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_models_summary import AuditRole
from tests.support import init_git_repo

from .support_asl_acceptance import (
    FIXED_CLOCK,
    MANIFEST,
    RUN_ID,
    _agent_reply,
    _Auditor,
)


class AdvisoryVerdictTests(unittest.TestCase):
    """A finding is reported, never enforced: the run's own record is final."""

    def _history(self, tmp: Path) -> tuple[Path, Path, Path, Path]:
        workspace = tmp / "workspace"
        init_git_repo(workspace / "app")
        plan_dir = tmp / "plan"
        plan_dir.mkdir()
        state_dir = tmp / "state" / "planhash"
        run_dir = state_dir / "runs" / RUN_ID
        run_dir.mkdir(parents=True)

        (plan_dir / "context.md").write_text("Plan context.\n", encoding="utf-8")
        (plan_dir / "manifest.md").write_text(MANIFEST, encoding="utf-8")
        (plan_dir / "session-01.md").write_text(
            "# Session 1\n\n"
            f"{plans_session_scope.READS_REPOSITORIES_PREFIX} app\n"
            f"{plans_session_scope.WRITES_REPOSITORIES_PREFIX} app\n",
            encoding="utf-8",
        )
        (plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME).write_text(
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
                    "timed_out": False,
                    "interrupted": False,
                    "session_kind": "implementation",
                    "artifact_dir": str(turn_dir),
                    "stdout_path": str(turn_dir / "stdout.log"),
                    "stderr_path": str(turn_dir / "stderr.log"),
                    "last_message_path": str(turn_dir / "last-message.txt"),
                    "result_path": str(turn_dir / state.RESULT_FILE_NAME),
                    "invocation": {"name": "codex", "cwd": str(workspace / "app")},
                    # No workspace_write_guard: a completed turn that recorded
                    # no guard result is the gap the audit is supposed to find.
                    "verification": {
                        "passed": True,
                        "checks": [{"name": "tests", "passed": True, "returncode": 0}],
                        "skipped_checks": [],
                    },
                }
            ),
            encoding="utf-8",
        )
        (turn_dir / "stdout.log").write_text("session complete\n", encoding="utf-8")
        (turn_dir / "stderr.log").write_text("no warnings\n", encoding="utf-8")
        (turn_dir / "last-message.txt").write_text("Done.\n", encoding="utf-8")

        (state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "harness": "codex",
                    "ok": True,
                    "paused": False,
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
            ),
            encoding="utf-8",
        )
        (state_dir / events.EVENTS_FILE_NAME).write_text(
            json.dumps(
                {
                    "name": "run_started",
                    "version": events.EVENT_SCHEMA_VERSION,
                    "created_at": "2026-08-10T10:11:12Z",
                    "payload": {"run_id": RUN_ID},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return workspace, plan_dir, state_dir, run_dir

    def _plan(self) -> supervisor_audit.AuditPlan:
        return supervisor_audit.AuditPlan(
            config=supervisor_audit.AuditConfig(
                enabled=True, harness="claude", timeout_seconds=30
            ),
            role=AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=30.0,
            ),
            state="pending",
            reason=None,
        )

    def _audit(
        self, history: tuple[Path, Path, Path, Path]
    ) -> assurance_models_summary.AssuranceSummary:
        workspace, plan_dir, state_dir, run_dir = history
        bundle = supervisor_audit_evidence_input.collect_audit_input(
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            terminal_pending=True,
        )
        return supervisor_audit.audit_terminal_run(
            self._plan(),
            run_id=RUN_ID,
            run_dir=run_dir,
            state_dir=state_dir,
            plan_dir=plan_dir,
            workspace_root=workspace,
            repositories=("app",),
            ledger_state_dir=state_dir.parent,
            durable=False,
            now=lambda: FIXED_CLOCK,
            _run_harness_fn=_Auditor(_agent_reply(bundle.evidence[0].locator)),
        )

    def test_a_deterministic_failure_ends_the_run_findings_and_opens_one_issue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))

            summary = self._audit(history)

            self.assertEqual("findings", summary.state, summary.reason)
            self.assertEqual(1, len(summary.issue_ids), summary.issue_ids)
            self.assertTrue(summary.issue_ids[0].startswith("ASI-"))
            inbox = projection.read_projection(state_dir=history[2].parent)
            issue = inbox.issue(summary.issue_ids[0])
            self.assertIsNotNone(issue)
            self.assertEqual("open", issue.state)

    def test_auditing_rewrites_nothing_the_run_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            state_dir = history[2]
            recorded = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(state_dir.rglob("*"))
                if path.is_file()
            }

            summary = self._audit(history)

            self.assertEqual("findings", summary.state, summary.reason)
            for path, digest in recorded.items():
                with self.subTest(path=path.name):
                    self.assertEqual(
                        digest, hashlib.sha256(path.read_bytes()).hexdigest()
                    )
            written = {
                path
                for path in state_dir.rglob("*")
                if path.is_file() and path not in recorded
            }
            run_dir = history[3]
            for path in written:
                with self.subTest(path=path.name):
                    self.assertTrue(path.is_relative_to(run_dir))


if __name__ == "__main__":
    unittest.main()
