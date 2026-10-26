"""`asl verify` command tests."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import artifacts, plans_files, verification_cli
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)


class VerifyCliTests(unittest.TestCase):
    def _run_verify(
        self,
        plan_dir: Path,
        *,
        session: str,
        cwd: Path,
        json_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(plan_dir),
            "--session",
            session,
            "--cwd",
            str(cwd),
        ]
        if json_output:
            argv.append("--json")
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_verify_json_passes_when_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("ok\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "target.txt", "name": "target"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["passed"])
            self.assertEqual("verify", payload["command"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertEqual(
                str(plans_files.canonicalize_path(repo_root)), payload["cwd"]
            )
            self.assertEqual(str(plan_dir), payload["plan_dir"])
            self.assertEqual(1, payload["recipe"]["check_count"])
            self.assertEqual(1, payload["recipe"]["gate_count"])
            self.assertEqual(0, payload["recipe"]["diagnostic_count"])
            self.assertEqual(["file_exists"], payload["recipe"]["kinds"])
            self.assertEqual(["target"], payload["recipe"]["names"])
            self.assertEqual([None], payload["recipe"]["repos"])
            self.assertEqual([], payload["skipped_checks"])
            self.assertEqual([], payload["diagnostic_failures"])
            self.assertEqual(1, len(payload["checks"]))
            check = payload["checks"][0]
            self.assertEqual("target", check["name"])
            self.assertEqual("file_exists", check["kind"])
            self.assertEqual("pass", check["status"])
            self.assertIsNone(check["failure_reason"])

    def test_verify_json_reports_registered_symlink_escape_without_traceback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:1]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            outside = root / "outside"
            outside.mkdir()
            (outside / "target.txt").write_text("outside\n", encoding="utf-8")
            (repositories["SVC"] / "escape").symlink_to(
                outside, target_is_directory=True
            )
            plan_dir = root / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "path": "escape/target.txt", '
                '"name": "escape", "repo": "SVC"}'
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract},
                specs=specs,
            )

            completed = self._run_verify(plan_dir, session="1", cwd=workspace_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-contract", payload["reason"])
            self.assertEqual("escape", payload["check"])
            self.assertEqual("SVC", payload["repo"])
            self.assertEqual("escape", payload["checks"][0]["name"])
            self.assertEqual("SVC", payload["checks"][0]["repo"])
            self.assertIn("escapes repository SVC", payload["error"])

    def test_verify_json_interprets_cwd_as_registered_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace_root = root / "workspace"
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            repositories = init_multi_repo_workspace(workspace_root, specs)
            (repositories["OAK"] / "target.txt").write_text("ok\n", encoding="utf-8")
            plan_dir = root / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "path": "target.txt", '
                '"name": "target", "repo": "OAK"}'
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract},
                specs=specs,
            )

            completed = self._run_verify(plan_dir, session="1", cwd=workspace_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(str(workspace_root.resolve()), payload["workspace_root"])
            self.assertEqual(
                ["SVC", "OAK"],
                [repository["alias"] for repository in payload["repositories"]],
            )
            check = payload["checks"][0]
            self.assertEqual("OAK", check["repo"])
            self.assertEqual("OAK:target.txt", check["qualified_path"])
            self.assertEqual(str(repositories["OAK"].resolve()), check["resolved_root"])

    def test_verify_json_failure_reports_check_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["passed"])
            self.assertFalse(payload["paused"])
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertIn("verification check", payload["error"])
            self.assertEqual(1, len(payload["checks"]))
            check = payload["checks"][0]
            self.assertEqual("absent", check["name"])
            self.assertEqual("fail", check["status"])
            self.assertEqual("file-missing", check["failure_reason"])
            self.assertIn("absent.txt", check["detail"])

    def test_verify_json_skips_conditional_diagnostic_when_gate_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("ok\n", encoding="utf-8")
            contract = _contract_block(
                ", ".join(
                    (
                        '{"type": "file_exists", "path": "target.txt", '
                        '"name": "suite"}',
                        '{"type": "file_exists", "path": "focused.txt", '
                        '"name": "focused", "on_failure_of": ["suite"]}',
                    )
                )
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["passed"])
            self.assertEqual(1, payload["recipe"]["gate_count"])
            self.assertEqual(1, payload["recipe"]["diagnostic_count"])
            self.assertEqual(["suite"], [row["name"] for row in payload["checks"]])
            self.assertEqual(
                ["focused"], [row["name"] for row in payload["skipped_checks"]]
            )
            self.assertEqual(["suite"], payload["skipped_checks"][0]["on_failure_of"])
            self.assertEqual([], payload["diagnostic_failures"])

    def test_verify_json_no_contract_returns_no_contract_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("no-contract", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertEqual([], payload["checks"])
            self.assertEqual(0, payload["recipe"]["check_count"])
            self.assertEqual(0, payload["recipe"]["gate_count"])
            self.assertEqual(0, payload["recipe"]["diagnostic_count"])
            self.assertEqual([], payload["skipped_checks"])
            self.assertEqual([], payload["diagnostic_failures"])
            self.assertFalse(payload["passed"])

    def test_verify_json_no_contract_attaches_skill_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            payload = json.loads(completed.stdout)
            self.assertEqual("no-contract", payload["reason"])
            # The runtime repair hint resolves for no-contract.
            self.assertEqual("runtime.no-contract", payload["repair_hint"]["check_id"])
            remediation = payload["skill_remediation"]
            # Names both escape hatches with the plan directory substituted.
            self.assertIn("--allow-not-loop-ready", remediation["message"])
            self.assertIn("--mark-loop-ready", remediation["message"])
            commands = [step["command"] for step in remediation["next_steps"]]
            self.assertIn(f"asl run {plan_dir} --allow-not-loop-ready", commands)
            self.assertIn(
                f"asl preflight {plan_dir} --json --mark-loop-ready", commands
            )

    def test_no_contract_remediation_commands_are_shell_safe(self) -> None:
        # A plan directory carrying whitespace and shell metacharacters must
        # still round-trip as a single argument in every advertised no-contract
        # remediation command (both next_steps commands and the message text).
        weird_plan = "/plans/de mo; rm -rf $(evil) 'q'"
        remediation = verification_cli._no_contract_skill_remediation(weird_plan)
        commands = [step["command"] for step in remediation["next_steps"]]
        self.assertEqual(
            [
                ["asl", "run", weird_plan, "--allow-not-loop-ready"],
                ["asl", "preflight", weird_plan, "--json", "--mark-loop-ready"],
            ],
            [shlex.split(command) for command in commands],
        )
        # The prose message embeds the same shell-safe command strings, so a
        # reader copy-pasting them into a shell also preserves the path.
        for command in commands:
            self.assertIn(f"`{command}`", remediation["message"])
            self.assertNotIn(weird_plan, remediation["message"].replace(command, ""))

    def test_verify_json_command_not_found_hint_names_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            argv = ["definitely-not-a-real-binary-xyz", "--version"]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual(
                "command-not-found", payload["checks"][0]["failure_reason"]
            )
            hint = payload["repair_hint"]
            self.assertEqual(
                "runtime.verify-failed.command-not-found", hint["check_id"]
            )
            # Interpreter guidance now lives authoritatively in the hint.
            self.assertIn("python3", hint["message"])

    def test_verify_json_failure_attaches_repair_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            payload = json.loads(completed.stdout)
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual(
                "runtime.verify-failed.file-missing",
                payload["repair_hint"]["check_id"],
            )
            # A non-no-contract failure carries no skill_remediation.
            self.assertNotIn("skill_remediation", payload)

    def test_verify_json_malformed_contract_reports_plan_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- something",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{not valid json",
                    "```",
                ]
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-contract", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertIn("Verify JSON is invalid", payload["error"])

    def test_verify_json_invalid_session_id_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "verify",
                    str(plan_dir),
                    "--session",
                    "not-a-session",
                    "--cwd",
                    str(repo_root),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("invalid-session", payload["reason"])
            self.assertEqual("not-a-session", payload["session"])

    def test_verify_reports_a_missing_plan_directory_before_its_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            missing = tmp_dir / "missing-plan"

            completed = self._run_verify(missing, session="1", cwd=repo_root)
            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("plan-not-found", payload["reason"])
            self.assertIn("plan directory does not exist", payload["error"])
            self.assertNotIn("session file", payload["error"])

            text = self._run_verify(
                missing, session="1", cwd=repo_root, json_output=False
            )
            self.assertEqual(1, text.returncode)
            self.assertIn("asl verify: plan directory does not exist", text.stderr)

    def test_verify_text_output_shows_per_check_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "present.txt").write_text("yes\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "present.txt", "name": "present"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(
                plan_dir, session="1", cwd=repo_root, json_output=False
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Session: 1", completed.stdout)
            self.assertIn("[PASS] file_exists present", completed.stdout)
            self.assertIn("Verify: passed", completed.stdout)

    def test_verify_text_output_reports_failure_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(
                plan_dir, session="1", cwd=repo_root, json_output=False
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("[FAIL] file_exists absent", completed.stdout)
            self.assertIn("Verify: failed", completed.stderr)

    def test_verify_json_check_payload_carries_bounded_stream_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            argv = [
                "python3",
                "-c",
                "import sys; sys.stdout.write('ok output'); sys.exit(0)",
            ]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            check = payload["checks"][0]
            self.assertEqual("ok output", check["stdout"])
            self.assertEqual(len(b"ok output"), check["stdout_byte_count"])
            self.assertFalse(check["stdout_truncated"])
            self.assertIsNone(check["stdout_artifact_path"])
            self.assertEqual(64, len(check["stdout_sha256"]))

    def test_verify_artifact_dir_retains_failed_command_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            artifact_dir = tmp_dir / "verify-artifacts"
            argv = [
                "python3",
                "-c",
                "import sys; sys.stdout.write('failure detail'); sys.exit(4)",
            ]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "verify",
                    str(plan_dir),
                    "--session",
                    "1",
                    "--cwd",
                    str(repo_root),
                    "--artifact-dir",
                    str(artifact_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            check = payload["checks"][0]
            self.assertEqual("command-failed", check["failure_reason"])
            self.assertEqual("failure detail", check["stdout"])
            artifact_path = check["stdout_artifact_path"]
            self.assertIsNotNone(artifact_path)
            recovered = artifacts.read_gzip(Path(artifact_path))
            self.assertEqual(b"failure detail", recovered)
