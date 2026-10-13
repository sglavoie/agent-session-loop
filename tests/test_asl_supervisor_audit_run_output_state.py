"""Audit run output and state projection."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    events,
    plans_files,
    plans_snapshot,
    run_payloads,
    run_selection,
    runner,
    state,
    state_payloads,
    state_status,
    state_status_text,
    supervisor_audit,
    supervisor_audit_agent,
)
from agent_session_loop.assurance_models import (
    AUDIT_REPORT_FILENAME,
    MAX_AUDIT_ATTEMPT_RECORDS,
    RUBRIC_LANES,
)
from tests.support import write_plan
from tests.support_supervisor_audit import RUN_ID, namespace, parse


class AuditRunOutputTests(unittest.TestCase):
    """Fresh, no-role, opted-out, and dry-run behavior in text and JSON."""

    def _initial_payload(self, tmp_dir: Path, tokens: list[str]) -> dict:
        plan_dir = tmp_dir / "plan"
        write_plan(plan_dir, ("pending",))
        args = parse(["run", str(plan_dir), *tokens])
        plan_state = state.plan_state_dir(plan_dir, tmp_dir / "state")
        lock_info = state.LockInfo(
            path=plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans_files.canonicalize_path(plan_dir)),
            created_at="2026-08-10T10:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )
        return run_payloads._initial_payload(
            args=args,
            snapshot=plans_snapshot.inspect_plan(plan_dir),
            state_dir=plan_state,
            run_dir=plan_state / "runs" / RUN_ID,
            event_path=plan_state / events.EVENTS_FILE_NAME,
            state_path=plan_state / events.STATE_FILE_NAME,
            lock_info=lock_info,
            cwd=tmp_dir,
            run_id=RUN_ID,
            commit_mode=None,
            mailbox_activation=run_selection._mailbox_activation_payload(
                args, plan_dir
            ),
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
            code = runner.run_from_args(parse(argv), run_argv=argv)
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
                    namespace(harness="codex", review_harness="claude")
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

            payload = state_payloads._runner_state_payload(state_path, lock_info=None)

            self.assertTrue(payload["supervisor_audit"])
            self.assertEqual("pending", payload["assurance"]["state"])

    def test_state_without_an_assurance_object_reports_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            state_path.write_text(json.dumps({"run_id": RUN_ID}), encoding="utf-8")

            payload = state_payloads._runner_state_payload(state_path, lock_info=None)

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
            state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            ),
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
                state_status_text._print_text_status(payload)
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
                        "reason": supervisor_audit_agent.REASON_LANE_EVIDENCE_ABSENT,
                        "artifacts": [
                            {"kind": "artifact", "locator": "run/supervisor-audit.json"}
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        return state_status.status_payload(
            plans_snapshot.inspect_plan(plan_dir), state_dir
        )

    def test_a_partial_audit_reports_which_lanes_had_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._partial_status(tmp)

            report = payload["assurance_report"]
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)

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

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), Path(tmp) / "state"
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state_status_text._print_text_status(payload)

            self.assertIsNone(payload["assurance"])
            self.assertIsNone(payload["assurance_report"])
            self.assertNotIn("Assurance", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
