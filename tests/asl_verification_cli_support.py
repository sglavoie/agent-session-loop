"""`asl verify` command tests."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import (
    REPO_ROOT,
)


class VerifyCliTestsBase(unittest.TestCase):
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
