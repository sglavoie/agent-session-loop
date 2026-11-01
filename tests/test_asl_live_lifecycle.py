"""Exercise the live suite's real CLI workflow using a protocol fixture."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_session_loop import harness_compatibility
from tests import asl_live_lifecycle as lifecycle
from tests import asl_live_redirect_reporting as entry
from tests.asl_live_lifecycle_process import MAX_HARNESS_LAUNCHES, REPO_ROOT, Commands

ASL_TEST_LANE = "integration"


class LifecycleAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)

    def test_default_cannot_create_workspace_or_launch_paid_work(self) -> None:
        with (
            mock.patch.object(
                lifecycle, "disposable_workspace", side_effect=AssertionError
            ),
            mock.patch.dict(os.environ, {"ASL_LIVE_ACCEPTANCE": "0"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = entry.main(["--suite", "lifecycle", "--harness", "codex", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertFalse(report["complete"])
        self.assertFalse(report["model_use"])
        self.assertIsNone(report["harnesses"][0]["verified_version"])

    def test_expired_budget_does_not_start_a_child(self) -> None:
        commands = Commands(self.root, deadline=1)
        commands.until = 0
        with (
            mock.patch("subprocess.Popen", side_effect=AssertionError),
            self.assertRaises(TimeoutError),
        ):
            commands.asl("must-not-run", "status", "missing")

    def test_timeout_stops_the_owned_child(self) -> None:
        (self.root / "repo").mkdir()
        marker = self.root / "child.pid"
        commands = Commands(self.root, deadline=10)
        with self.assertRaises(subprocess.TimeoutExpired):
            commands.call(
                "timeout",
                [
                    sys.executable,
                    "-c",
                    (
                        "import os, time; from pathlib import Path; "
                        f"Path({str(marker)!r}).write_text(str(os.getpid())); "
                        "time.sleep(60)"
                    ),
                ],
                timeout=1,
            )
        self.assertTrue(marker.is_file())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)
        self.assertEqual(1, len(commands.calls))
        self.assertTrue(Path(commands.calls[0]["stderr"]).is_file())

    def test_interrupt_does_not_start_another_requested_harness(self) -> None:
        args = entry.build_parser().parse_args(
            [
                "--suite",
                "lifecycle",
                "--all-harnesses",
                "--live",
                "--json",
            ]
        )
        with (
            mock.patch.object(
                lifecycle,
                "run_lifecycle",
                return_value={
                    "status": "failed",
                    "interrupted": True,
                },
            ) as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            code = lifecycle.lifecycle_main(
                args,
                requested=("codex", "claude"),
                opted_in=True,
            )
        self.assertEqual(1, code)
        self.assertEqual(1, run.call_count)
        self.assertFalse(json.loads(output.getvalue())["complete"])

    def test_module_entry_point_probes_user_cli(self) -> None:
        executable = self.root / "codex"
        executable.write_text("#!/bin/sh\nprintf 'codex-cli 0.154.0\\n'\n")
        executable.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(self.root) + os.pathsep + env["PATH"]
        env["ASL_LIVE_ACCEPTANCE"] = "0"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.asl_live_redirect_acceptance",
                "--suite",
                "lifecycle",
                "--harness",
                "codex",
                "--json",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual("0.154.0", report["harnesses"][0]["version"])
        self.assertFalse(report["complete"])

    def test_launch_budget_preserves_native_config_and_refuses_extra_work(self) -> None:
        native_config = str(self.root / "native config")
        executable = self.root / "native-harness"
        executable.write_text(
            "#!/bin/sh\n"
            '[ "$XDG_CONFIG_HOME" = ' + shlex.quote(native_config) + " ] || exit 9\n"
            "exit 0\n"
        )
        executable.chmod(0o755)
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": native_config}):
            commands = Commands(self.root, deadline=30)
        commands.bound_harness("codex", str(executable))
        wrapper = str(self.root / "bounded-harness")
        for _ in range(MAX_HARNESS_LAUNCHES):
            completed = subprocess.run([wrapper, "turn"], env=commands.env, timeout=5)
            self.assertEqual(0, completed.returncode)
        refused = subprocess.run(
            [wrapper, "turn"],
            env=commands.env,
            capture_output=True,
            timeout=5,
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn(b"launch budget exhausted", refused.stderr)
        probe = subprocess.run([wrapper, "--version"], env=commands.env, timeout=5)
        self.assertEqual(0, probe.returncode)
        self.assertEqual(
            MAX_HARNESS_LAUNCHES,
            len((self.root / "harness-launches.jsonl").read_text().splitlines()),
        )

    def test_failed_sequence_retains_evidence_without_verifying_version(self) -> None:
        compat = SimpleNamespace(
            version="0.154.0",
            status="supported",
            executable="/not-launched",
            blocks=False,
        )
        with (
            mock.patch.object(
                harness_compatibility, "check_harness", return_value=compat
            ),
            mock.patch.object(
                lifecycle, "run_sequence", side_effect=RuntimeError("test failure")
            ),
        ):
            result = lifecycle.run_lifecycle(
                "codex", opted_in=True, deadline=30, keep=False
            )
        artifacts = Path(result["artifacts"])
        self.addCleanup(lifecycle.shutil.rmtree, artifacts)
        self.assertEqual("failed", result["status"])
        self.assertIsNone(result["verified_version"])
        self.assertEqual([], result["checks"])
        saved = json.loads((artifacts / "acceptance.json").read_text())
        self.assertIn("test failure", saved["detail"])

    def test_complete_workflow_through_real_commands(self) -> None:
        fixture = Path(__file__).parent / "fixtures/fake_lifecycle_codex.py"
        executable = self.root / "codex"
        executable.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f'{shlex.quote(str(fixture))} "$@"\n'
        )
        executable.chmod(0o755)
        config = self.root / "config/agent-session-loop/harnesses.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"codex": {"executable": str(executable)}}))
        with (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = entry.main(
                [
                    "--suite",
                    "lifecycle",
                    "--harness",
                    "codex",
                    "--live",
                    "--deadline",
                    "120",
                    "--keep-workspaces",
                    "--json",
                    "--report",
                    str(self.root / "report.json"),
                ]
            )
        report = json.loads(output.getvalue())
        self.assertEqual(report, json.loads((self.root / "report.json").read_text()))
        result = report["harnesses"][0]
        artifacts = result.get("artifacts")
        if artifacts and result["status"] == "passed":
            self.addCleanup(lifecycle.shutil.rmtree, artifacts, True)
        self.assertEqual("passed", result["status"], result["detail"])
        self.assertEqual(0, code)
        self.assertTrue(report["complete"])
        self.assertEqual("0.154.0", result["verified_version"])
        self.assertEqual(list(lifecycle.CHECKS), result["checks"])
        self.assertEqual(1, result["evidence"]["delivery_count"])
        launches = (Path(artifacts) / "harness-launches.jsonl").read_text().splitlines()
        self.assertEqual(4, len(launches))
        self.assertTrue((Path(artifacts) / "acceptance.json").is_file())


if __name__ == "__main__":
    unittest.main()
