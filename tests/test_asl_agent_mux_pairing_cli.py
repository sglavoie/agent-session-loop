"""``asl agent-mux pairing`` record shape and CLI surface."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_session_loop import (
    agent_mux_pairing,
    agent_mux_projection,
    cli,
)
from tests.asl_agent_mux_pairing_support import CURRENT, PairingTestBase


class PairingShapeTests(PairingTestBase):
    """Every outcome presents the same keys."""

    EXPECTED_KEYS = {
        "ok",
        "reason",
        "asl_version",
        "executable",
        "contract_set_id",
        "bridge_protocol",
        "state_schema_version",
        "state_root",
        "state_root_present",
        "migrate_requested",
        "migrated",
        "plans",
        "active_incompatible_runs",
        "migrations",
        "issues",
    }

    def test_clean_root_shape(self) -> None:
        self.assertEqual(set(self._survey()) - {"error"}, self.EXPECTED_KEYS)

    def test_refusing_root_shape(self) -> None:
        self._seed("live", version=2, locked=True)
        report = self._survey()
        self.assertEqual(set(report) - {"error"}, self.EXPECTED_KEYS)
        self.assertIn("error", report)

    def test_unscannable_root_shape(self) -> None:
        """A root that is not a directory still answers with every key."""
        not_a_dir = self.tmp_path / "file-root"
        not_a_dir.write_text("", encoding="utf-8")
        report = agent_mux_pairing.survey(state_dir=not_a_dir)
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], agent_mux_pairing.REASON_STATE_ROOT_ERROR)
        self.assertEqual(set(report) - {"error"}, self.EXPECTED_KEYS)

    def test_report_is_json_serializable(self) -> None:
        self._seed("live", version=2, locked=True)
        self._seed("legacy", version=2)
        json.dumps(self._survey(), sort_keys=True)


class PairingCLITests(PairingTestBase):
    """The command surface Agent Mux actually invokes."""

    def _run(self, *argv: str) -> tuple[int, str, str]:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["agent-mux", "pairing", "--state-dir", str(self.state_dir), *argv]
        )
        handler = cli.resolve_handler(args)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = handler(args)
        return code, out.getvalue(), err.getvalue()

    def test_clean_root_exits_zero_with_a_success_envelope(self) -> None:
        code, out, _ = self._run("--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], agent_mux_pairing.COMMAND_NAME)
        self.assertEqual(payload["reason"], agent_mux_pairing.REASON_OK)

    def test_refusal_exits_nonzero_with_an_error_envelope(self) -> None:
        self._seed("live", version=2, locked=True)
        code, out, _ = self._run("--json")
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["reason"], agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS
        )
        self.assertTrue(payload["error"])
        self.assertFalse(payload["paused"])
        self.assertEqual(len(payload["active_incompatible_runs"]), 1)

    def test_reason_is_present_on_both_envelopes(self) -> None:
        """A consumer branches on `reason` alone, success or failure."""
        code, out, _ = self._run("--json")
        self.assertEqual(code, 0)
        self.assertIn("reason", json.loads(out))

    def test_text_output_names_the_refusing_run_on_stderr_and_stdout(self) -> None:
        plan_dir = self._seed("live", version=2, locked=True)
        code, out, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn(str(plan_dir), out)
        self.assertIn(str(os.getpid()), out)
        self.assertIn(agent_mux_pairing.COMMAND_NAME, err)

    def test_text_output_reports_build_identity(self) -> None:
        _, out, _ = self._run()
        self.assertIn(agent_mux_projection.CONTRACT_SET_ID, out)
        self.assertIn(str(self.state_dir), out)

    def test_migrate_flag_reaches_the_survey(self) -> None:
        plan_dir = self._seed("legacy", version=2)
        code, out, _ = self._run("--migrate", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["migrated"])
        self.assertEqual(self._state_object(plan_dir)["version"], CURRENT)

    def test_json_output_is_a_single_line(self) -> None:
        self._seed("legacy", version=2)
        _, out, _ = self._run("--json")
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_module_main_matches_the_cli_handler(self) -> None:
        self._seed("live", version=2, locked=True)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = agent_mux_pairing.main(
                ["--state-dir", str(self.state_dir), "--json"]
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(out.getvalue())["reason"],
            agent_mux_pairing.REASON_ACTIVE_INCOMPATIBLE_RUNS,
        )


if __name__ == "__main__":
    unittest.main()
