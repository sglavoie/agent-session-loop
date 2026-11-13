"""Doctor, unlock and sessions CLI output tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    cli,
)
from tests.support import (
    patched_env,
)

from . import (
    asl_cli_json_doctor_unlock_sessions_doctor,
    asl_cli_json_doctor_unlock_sessions_sessions,
    asl_cli_json_doctor_unlock_sessions_unlock,
)


class JsonDoctorUnlockSessionsCliTests(
    asl_cli_json_doctor_unlock_sessions_doctor.JsonDoctorUnlockSessionsCliTests,
    asl_cli_json_doctor_unlock_sessions_unlock.JsonDoctorUnlockSessionsCliTests,
    asl_cli_json_doctor_unlock_sessions_sessions.JsonDoctorUnlockSessionsCliTests,
):
    """Preserve the established test IDs across the focused case modules."""


class MissingPlanDirectoryReasonTests(unittest.TestCase):
    """A PLAN_DIR that does not exist is `plan-not-found` on every command.

    Each command keeps its own reason for a plan that exists but cannot be
    read; only the missing directory is reported the same way everywhere, so
    a script can branch on one word whichever command it ran.
    """

    def test_every_plan_scoped_command_reports_plan_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = str(root / "missing-plan")
            state_dir = str(root / "state")
            commands = {
                "status": ["status", missing, "--state-dir", state_dir],
                "questions": ["questions", missing, "--state-dir", state_dir],
                "review-verdict": ["review-verdict", missing, "--state-dir", state_dir],
                "run": ["run", missing, "--state-dir", state_dir],
                "doctor": ["doctor", missing, "--harness", "codex"],
                "contract-check": ["contract-check", missing],
                "baseline": ["baseline", missing],
                "register-repositories": [
                    "register-repositories",
                    missing,
                    "--state-dir",
                    state_dir,
                ],
                "compact-state": ["compact-state", missing, "--state-dir", state_dir],
                "unlock": ["unlock", missing, "--state-dir", state_dir],
                "gc": ["gc", missing, "--state-dir", state_dir],
                "acknowledge-external-changes": [
                    "acknowledge-external-changes",
                    missing,
                    "--session",
                    "1",
                    "--repository",
                    "pi",
                    "--state-dir",
                    state_dir,
                ],
                "accept-ref-updates": [
                    "accept-ref-updates",
                    missing,
                    "--session",
                    "1",
                    "--state-dir",
                    state_dir,
                ],
            }
            with patched_env(
                HOME=str(root / "home"),
                XDG_STATE_HOME=str(root / "xdg-state"),
                XDG_CONFIG_HOME=str(root / "xdg-config"),
            ):
                for name, argv in commands.items():
                    with self.subTest(command=name):
                        stdout = io.StringIO()
                        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                            code = cli.main([*argv, "--json"])
                        payload = json.loads(stdout.getvalue())
                        self.assertEqual(1, code, payload)
                        self.assertIs(False, payload["ok"])
                        self.assertEqual(name, payload["command"])
                        self.assertEqual("plan-not-found", payload["reason"])
                        self.assertIn("plan directory does not exist", payload["error"])
                        # Nothing ran, so nothing paused.
                        self.assertIs(False, payload["paused"])
            self.assertFalse(Path(missing).exists())


if __name__ == "__main__":
    unittest.main()
