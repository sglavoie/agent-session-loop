"""Status and dry-run JSON output tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    config,
    harnesses,
    plans_snapshot,
    responses,
    state_status,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
    write_plan_with_ids,
)
from tests.support_asl_cli_json import JsonCliHelpers


class JsonStatusAndDryRunCliTests(JsonCliHelpers, unittest.TestCase):
    """Status and dry-run JSON output tests."""

    def test_status_and_dry_run_json_outputs_nested_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan_with_ids(plan_dir, (("1", "complete"), ("1.1", "pending")))

            status_payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )
            self.assertEqual("1.1", status_payload["next_pending"])
            self.assertEqual("1.1", status_payload["next_pending_id"])
            integer_session = status_payload["sessions"][0]
            nested_session = status_payload["sessions"][1]
            self.assertEqual("1", integer_session["id"])
            self.assertEqual(1, integer_session["number"])
            self.assertEqual("1.1", nested_session["id"])
            self.assertNotIn("number", nested_session)

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("1.1", payload["initial_snapshot"]["next_pending"])
            self.assertEqual("1.1", payload["initial_snapshot"]["next_pending_id"])
            self.assertEqual("1.1", payload["planned"]["next_pending"])
            self.assertEqual("1.1", payload["planned"]["next_pending_id"])
            nested = payload["initial_snapshot"]["sessions"][1]
            self.assertEqual("1.1", nested["id"])
            self.assertNotIn("number", nested)

    def test_status_next_command_pending_shows_per_harness_invocations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("complete", "pending"))

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )
            next_command = payload["next_command"]
            self.assertFalse(next_command["complete"])
            self.assertEqual("2", next_command["next_pending_id"])
            invocations = next_command["next_steps"][0]["invocations"]
            resolved_plan_dir = payload["plan_dir"]
            self.assertEqual(f"$session-run {resolved_plan_dir}/", invocations["codex"])
            self.assertEqual(
                f"/session-run {resolved_plan_dir}/", invocations["claude"]
            )
            self.assertEqual(
                f"/session-run {resolved_plan_dir}/", invocations["copilot"]
            )

    def test_status_next_command_complete_uses_exact_session_run_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("complete", "complete"))

            payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_dir
            )
            self.assertEqual(
                {
                    "complete": True,
                    "message": "All sessions implemented - nothing left to run.",
                },
                payload["next_command"],
            )
            self.assertEqual(
                responses.ALL_SESSIONS_COMPLETE_MESSAGE,
                payload["next_command"]["message"],
            )

    def test_dry_run_json_no_unsafe_bypass_uses_harness_default_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--no-unsafe-bypass",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            invocation = payload["planned"]["invocation"]
            self.assertEqual(harnesses.TRANSPORT_APP_SERVER, invocation["transport"])
            thread_params = invocation["turn_plan"]["thread_params"]
            self.assertEqual("workspace-write", thread_params["sandbox"])
            self.assertEqual("never", thread_params["approvalPolicy"])
            self.assertNotIn(
                "--dangerously-bypass-approvals-and-sandbox",
                json.dumps([invocation["argv"], invocation["turn_plan"]]),
            )

            copilot_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(tmp_dir / "copilot-state"),
                    "--dry-run",
                    "--json",
                    "--harness",
                    "copilot",
                    "--no-unsafe-bypass",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            copilot_payload = json.loads(copilot_completed.stdout)
            copilot_argv = copilot_payload["planned"]["invocation"]["argv"]
            self.assertIn("--no-ask-user", copilot_argv)
            self.assertIn("--silent", copilot_argv)
            self.assertIn("-C", copilot_argv)
            self.assertIn("--add-dir", copilot_argv)
            self.assertNotIn("--allow-all-tools", copilot_argv)
            self.assertNotIn("--allow-all-paths", copilot_argv)

    def test_dry_run_json_copilot_default_unsafe_bypass_allows_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--harness",
                    "copilot",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            argv = payload["planned"]["invocation"]["argv"]
            self.assertIn("--allow-all-tools", argv)
            self.assertIn("--allow-all-paths", argv)
            self.assertIn("--no-ask-user", argv)
            self.assertIn("--silent", argv)

    def test_run_json_uses_current_directory_when_cwd_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            invocation = payload["planned"]["invocation"]
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(REPO_ROOT), invocation["cwd"])
            # The working directory reaches codex through `thread/start` now.
            self.assertEqual(
                str(REPO_ROOT), invocation["turn_plan"]["thread_params"]["cwd"]
            )

    def test_dry_run_json_reports_native_model_metadata_without_argv_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            home = tmp_dir / "home"
            codex_config = home / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True)
            codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("gpt-5-codex", payload["model"])
            self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, payload["model_source"])
            self.assertIsNone(payload["effort"])
            self.assertIsNone(payload["effort_source"])
            planned = payload["planned"]
            invocation = planned["invocation"]
            self.assertEqual("gpt-5-codex", planned["model"])
            self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, planned["model_source"])
            self.assertIsNone(planned["effort"])
            self.assertIsNone(planned["effort_source"])
            self.assertEqual("gpt-5-codex", invocation["model"])
            self.assertEqual(
                config.MODEL_SOURCE_CODEX_CONFIG, invocation["model_source"]
            )
            self.assertIsNone(invocation["effort"])
            self.assertIsNone(invocation["effort_source"])
            self.assertNotIn("--model", invocation["argv"])
            self.assertNotIn("-c", invocation["argv"])


if __name__ == "__main__":
    unittest.main()
