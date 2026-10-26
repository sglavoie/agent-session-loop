"""Child output that is not UTF-8 is decoded, never raised past its reader."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    git_subprocess,
    harness_process,
    harnesses,
    run_commit_process,
)

_EMIT_INVALID_UTF8 = (
    "import sys; sys.stdout.buffer.write(b'ok \\xff tail\\n'); "
    "sys.stderr.buffer.write(b'err \\xfe\\n')"
)


class InvalidUtf8OutputTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_a_harness_turn_survives_a_byte_that_is_not_utf8(self) -> None:
        adapter = harnesses.get_adapter("copilot")
        invocation = harnesses.HarnessInvocation(
            name="copilot",
            argv=[sys.executable, "-c", _EMIT_INVALID_UTF8],
            cwd=self.tmp,
        )
        # The post-turn sweep shells out to `ps`; nothing here is about it.
        with mock.patch.object(harness_process, "sweep_enabled", return_value=False):
            result = adapter.run(
                cwd=self.tmp,
                plan_dir=self.tmp,
                prompt="do the work",
                invocation=invocation,
                timeout=60,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual("ok � tail\n", result.stdout)
        self.assertEqual("err �\n", result.stderr)

    def test_a_bounded_commit_survives_a_byte_that_is_not_utf8(self) -> None:
        completed, timed_out, _sweep = run_commit_process._run_bounded_asl_commit(
            [sys.executable, "-c", _EMIT_INVALID_UTF8], cwd=self.tmp, timeout=60
        )

        self.assertFalse(timed_out)
        self.assertEqual("ok � tail\n", completed.stdout)
        self.assertEqual("err �\n", completed.stderr)

    def test_run_git_round_trips_bytes_that_are_not_utf8(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        (self.tmp / "blob").write_bytes(b"caf\xe9\n")
        git_subprocess.run_git(self.tmp, ["add", "blob"])

        completed = git_subprocess.run_git(self.tmp, ["show", ":blob"])

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("caf\udce9\n", completed.stdout)
        self.assertEqual(
            b"caf\xe9\n", completed.stdout.encode("utf-8", "surrogateescape")
        )


if __name__ == "__main__":
    unittest.main()
