"""Test asl assurance cli pending."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_session_loop import (
    assurance_cli,
    cli,
    state,
    state_assurance_pending,
)
from tests import support_asl_assurance_cli as fixtures


class AssuranceProcessPendingTests(fixtures.AssuranceCliTestCase):
    """The one command here that runs a harness, and what it refuses to run."""

    def seed_pending_run(
        self,
        plan_name: str,
        run_id: str,
        *,
        skip: str | None = state_assurance_pending.ASSURANCE_SKIP_PREDATES,
    ) -> Path:
        """Record that one run of ``plan_name`` died still owing an audit."""
        plan_dir = self.seed_run_directory(plan_name, run_id)
        run_dir = (
            state.plan_state_dir(plan_dir, self.state_dir)
            / state.RUNS_DIR_NAME
            / run_id
        )
        pending_schema = state_assurance_pending.ASSURANCE_PENDING_SCHEMA_VERSION
        (run_dir / state_assurance_pending.ASSURANCE_PENDING_FILE_NAME).write_text(
            json.dumps(
                {
                    "schema_version": pending_schema,
                    "run_id": run_id,
                    "recorded_at": fixtures.TIMESTAMP,
                    "trigger": "test",
                    "skip": skip,
                    "plan_dir": str(plan_dir),
                    "workspace_root": str(plan_dir),
                    "run_config": {"harness": "codex", "audit_harness": "claude"},
                }
            ),
            encoding="utf-8",
        )
        return plan_dir

    def test_process_pending_is_wired_with_documented_defaults(self) -> None:
        args = self.parser.parse_args(["assurance", "process-pending"])

        self.assertEqual(
            "assurance_cli:assurance_process_pending_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args),
            assurance_cli.assurance_process_pending_from_args,
        )
        self.assertIsNone(args.limit)
        self.assertIsNone(args.run_id)
        self.assertIsNone(args.plan)
        self.assertIsNone(args.audit_harness)
        self.assertIsNone(args.audit_timeout)
        self.assertFalse(args.json)

    def test_an_empty_corpus_has_no_backlog_rather_than_an_error(self) -> None:
        code, out, err = self.run_cli(["assurance", "process-pending"])

        self.assertEqual(0, code, err)
        self.assertIn("No run is recorded as owing an audit.", out)
        self.assertFalse(self.ledger_path.exists())

    def test_history_that_predates_auditing_is_reported_never_audited(self) -> None:
        self.seed_pending_run("alpha", fixtures.RUN_ID)

        code, payload, err = self.run_json(["assurance", "process-pending"])

        self.assertEqual(0, code, err)
        self.assertEqual(1, payload["candidate_count"])
        self.assertEqual(0, payload["audited_count"])
        self.assertEqual(20, payload["limit"])
        self.assertEqual(
            [
                {
                    "run_id": fixtures.RUN_ID,
                    "outcome": "skipped",
                    "plan_id": "alpha",
                    "state": None,
                    "reason": state_assurance_pending.ASSURANCE_SKIP_PREDATES,
                    "issue_ids": [],
                    "archived_artifacts": [],
                }
            ],
            payload["results"],
        )
        self.assertFalse(self.ledger_path.exists())

    def test_the_text_form_names_the_run_and_what_became_of_it(self) -> None:
        self.seed_pending_run("alpha", fixtures.RUN_ID)

        code, out, err = self.run_cli(["assurance", "process-pending"])

        self.assertEqual(0, code, err)
        self.assertIn("1 recorded, 0 audited (limit 20)", out)
        self.assertIn(fixtures.RUN_ID, out)
        self.assertIn(state_assurance_pending.ASSURANCE_SKIP_PREDATES, out)

    def test_a_non_positive_limit_is_refused_before_anything_runs(self) -> None:
        self.seed_pending_run("alpha", fixtures.RUN_ID)

        code, out, err = self.run_cli(["assurance", "process-pending", "--limit", "0"])

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("--limit must be a positive integer; got 0", err)

    def test_a_non_positive_audit_timeout_is_refused(self) -> None:
        code, payload, _ = self.run_json(
            ["assurance", "process-pending", "--audit-timeout", "0"]
        )

        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])

    def test_a_run_with_no_recorded_debt_is_refused(self) -> None:
        code, payload, _ = self.run_json(
            [
                "assurance",
                "process-pending",
                "--run-id",
                fixtures.RUN_ID,
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("unknown-run", payload["reason"])
        self.assertIn("has no recorded pending audit", payload["error"])

    def test_one_run_id_under_two_plans_is_refused_rather_than_guessed(self) -> None:
        self.seed_pending_run("alpha", fixtures.RUN_ID)
        self.seed_pending_run("beta", fixtures.RUN_ID)

        code, out, err = self.run_cli(
            [
                "assurance",
                "process-pending",
                "--run-id",
                fixtures.RUN_ID,
            ]
        )

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("names a pending audit under 2 plans", err)
        self.assertIn("pass --plan PLAN_DIR_OR_ID", err)

    def test_a_plan_narrows_the_pass_to_its_own_runs(self) -> None:
        plan_dir = self.seed_pending_run("alpha", fixtures.RUN_ID)
        self.seed_pending_run("beta", fixtures.OTHER_RUN_ID)

        code, payload, err = self.run_json(
            ["assurance", "process-pending", "--plan", str(plan_dir)]
        )

        self.assertEqual(0, code, err)
        self.assertEqual(
            [fixtures.RUN_ID],
            [row["run_id"] for row in payload["results"]],
        )


if __name__ == "__main__":
    unittest.main()
