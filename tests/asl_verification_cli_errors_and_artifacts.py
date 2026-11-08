"""Errors and artifacts cases for VerifyCliTests."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_session_loop import artifacts
from tests.support import (
    REPO_ROOT,
    _contract_block,
    write_plan,
)

from .asl_verification_cli_support import VerifyCliTestsBase


class VerifyCliTests(VerifyCliTestsBase):
    def test_verify_json_refuses_a_missing_cwd_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            contract = _contract_block('{"type": "file_exists", "path": "a"}')
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
            missing = tmp_dir / "missing-root"

            completed = self._run_verify(plan_dir, session="1", cwd=missing)

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("cwd-error", payload["reason"])
            self.assertEqual(f"cwd does not exist: {missing}", payload["error"])

    def test_verify_json_passed_command_detail_is_shell_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            argv = [sys.executable, "-c", "print('a b')"]
            contract = _contract_block(
                '{"type": "command", "name": "cmd", '
                f'"argv": {json.dumps(argv)}, "timeout_seconds": 30}}'
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stdout)
            detail = json.loads(completed.stdout)["checks"][0]["detail"]
            self.assertEqual(f"command exited 0: {shlex.join(argv)}", detail)
            self.assertEqual(argv, shlex.split(detail.split(": ", 1)[1]))

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

    def test_verify_json_reports_a_non_utf8_session_file_as_unreadable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block('{"type": "file_exists", "path": "a"}')
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
            session_file = plan_dir / "session-1.md"
            with session_file.open("ab") as handle:
                handle.write(b"\nnotes \xff\xfe\n")

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-contract", payload["reason"])
            self.assertIn("cannot read", payload["error"])
            self.assertIn("utf-8", payload["error"])

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

    def test_verify_json_reports_an_absent_session_as_invalid_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = self._run_verify(plan_dir, session="3", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertEqual("invalid-session", payload["reason"])
            self.assertEqual(
                f"session 3 is not present in {plan_dir}", payload["error"]
            )
            self.assertNotIn("repair_hint", payload)

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
