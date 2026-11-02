"""Crash recovery acceptance through real commands with no provider calls."""

from __future__ import annotations

import copy
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_session_loop import harness_compatibility, process_tree
from tests import asl_live_recovery as recovery
from tests import asl_live_redirect_reporting as entry
from tests.asl_live_lifecycle_process import Commands
from tests.asl_live_recovery_process import CrashingRun, start_run

ASL_TEST_LANE = "integration"


class RecoveryAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)

    def args(self, *extra: str) -> list[str]:
        return ["--suite", "recovery", "--harness", "codex", "--json", *extra]

    def install_fixture(self) -> None:
        fixture = Path(__file__).parent / "fixtures/fake_recovery_codex.py"
        executable = self.root / "codex"
        executable.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
            f'{shlex.quote(str(fixture))} "$@"\n'
        )
        executable.chmod(0o700)
        config = self.root / "config/agent-session-loop/harnesses.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"codex": {"executable": str(executable)}}))

    def test_default_only_probes_version(self) -> None:
        with (
            mock.patch.object(
                recovery, "disposable_workspace", side_effect=AssertionError
            ),
            mock.patch.dict(os.environ, {"ASL_LIVE_ACCEPTANCE": "0"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(0, entry.main(self.args()))
        report = json.loads(output.getvalue())
        self.assertFalse(report["complete"])
        self.assertFalse(report["model_use"])
        self.assertEqual("skipped", report["harnesses"][0]["status"])
        self.assertIsNone(report["harnesses"][0]["verified_version"])

    def test_changed_sources_cannot_produce_complete_report(self) -> None:
        args = entry.build_parser().parse_args(self.args("--live"))
        with (
            mock.patch.object(
                recovery,
                "source_identity",
                side_effect=[{"sha256": "a"}, {"sha256": "b"}],
            ),
            mock.patch.object(
                recovery, "run_recovery", return_value={"status": "passed"}
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                1, recovery.recovery_main(args, requested=("codex",), opted_in=True)
            )
        report = json.loads(output.getvalue())
        self.assertFalse(report["complete"])
        self.assertFalse(report["source_unchanged"])

    def test_interruption_does_not_launch_another_harness(self) -> None:
        args = entry.build_parser().parse_args(self.args("--live"))
        with (
            mock.patch.object(
                recovery,
                "run_recovery",
                return_value={"status": "failed", "interrupted": True},
            ) as run,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                1,
                recovery.recovery_main(
                    args, requested=("codex", "claude"), opted_in=True
                ),
            )
        self.assertEqual(1, run.call_count)
        self.assertFalse(json.loads(output.getvalue())["complete"])

    def test_missing_or_unversioned_harness_cannot_create_workspace(self) -> None:
        for executable, version in ((None, "0.154.0"), ("/not-launched", None)):
            compat = SimpleNamespace(
                executable=executable, version=version, blocks=False, status="unknown"
            )
            with (
                self.subTest(executable=executable, version=version),
                mock.patch.object(
                    harness_compatibility, "check_harness", return_value=compat
                ),
                mock.patch.object(
                    recovery, "disposable_workspace", side_effect=AssertionError
                ),
            ):
                result = recovery.run_recovery(
                    "codex", opted_in=True, deadline=30, keep=False
                )
            self.assertEqual("skipped", result["status"])
            self.assertIsNone(result["verified_version"])

    def test_changed_version_invalidates_the_recovery(self) -> None:
        def probe(version: str | None) -> SimpleNamespace:
            return SimpleNamespace(
                executable="/not-launched",
                version=version,
                blocks=False,
                status="unknown",
            )

        for after in ("changed", None):
            with (
                self.subTest(after=after),
                mock.patch.object(
                    harness_compatibility,
                    "check_harness",
                    side_effect=[probe("initial"), probe(after)],
                ),
                mock.patch.object(recovery, "run_sequence", return_value={}),
            ):
                result = recovery.run_recovery(
                    "codex", opted_in=True, deadline=30, keep=False
                )
            self.addCleanup(shutil.rmtree, result["artifacts"])
            self.assertEqual("failed", result["status"])
            self.assertIsNone(result["verified_version"])
            self.assertEqual([], result["checks"])

    def test_crash_unlock_and_verified_resume_through_real_commands(self) -> None:
        self.install_fixture()
        with (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")}),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = entry.main(
                self.args(
                    "--live",
                    "--deadline",
                    "120",
                    "--keep-workspaces",
                    "--report",
                    str(self.root / "report.json"),
                )
            )
        report = json.loads(output.getvalue())
        result = report["harnesses"][0]
        if result["status"] == "passed":
            self.addCleanup(shutil.rmtree, result["artifacts"])
        self.assertEqual("passed", result["status"], result["detail"])
        self.assertEqual(0, code)
        self.assertEqual(report, json.loads((self.root / "report.json").read_text()))
        self.assertTrue(report["complete"])
        self.assertTrue(report["source_unchanged"])
        self.assertEqual("0.154.0", result["verified_version"])
        self.assertEqual(list(recovery.CHECKS), result["checks"])
        root = Path(result["artifacts"])
        self.assertTrue((root / "crash-processes.json").is_file())
        self.assertTrue((root / "before-crash.json").is_file())
        self.assertTrue((root / "stale-lock.json").is_file())
        self.assertTrue((root / "acceptance.json").is_file())
        launches = (root / "harness-launches.jsonl").read_text().splitlines()
        self.assertEqual(4, len(launches))
        self.assertEqual(4, len(result["evidence"]["harness_processes"]))
        self.assertTrue(all(json.loads(row)["birth"] for row in launches))
        unlock = next(call for call in result["commands"] if call["label"] == "unlock")
        self.assertNotIn("--force", unlock["argv"])
        # Every success claim below has a false-pass refusal, even if the
        # resumed process happened to exit successfully.
        for mutation in (
            "not-prepared",
            "no-crash",
            "no-sample",
            "not-a-descendant",
            "survivor",
            "unverified-first",
            "wrong-harness",
            "replay",
            "unverified-resume",
            "missing-events",
            "no-unlock",
            "active-lock",
            "active-turn",
            "changed-edits",
            "changed-verifier",
            "changed-handoff",
            "failed-product",
            "no-conversation-pause",
            "unauthorized-launch",
            "no-fresh-authorization",
            "missing-preserved-input",
            "missing-frozen-input",
            "live-harness-process",
        ):
            evidence = copy.deepcopy(result["evidence"])
            if mutation == "not-prepared":
                evidence["prepare"]["marked_loop_ready"] = False
            elif mutation == "no-crash":
                evidence["processes"]["returncode"] = 0
            elif mutation == "no-sample":
                evidence["processes"]["descendants"]["collected"] = False
            elif mutation == "not-a-descendant":
                evidence["processes"]["checkpoint"]["pid"] = -1
            elif mutation == "survivor":
                evidence["final_process_confirmation"]["complete"] = False
            elif mutation == "unverified-first":
                evidence["before"]["turns"][0]["verification"]["passed"] = False
            elif mutation == "wrong-harness":
                evidence["final_run"]["turns"][0]["harness"] = "claude"
            elif mutation == "replay":
                evidence["final_run"]["turns"][0]["session_id"] = "1"
            elif mutation == "unverified-resume":
                evidence["final_run"]["turns"][0]["verification"]["passed"] = False
            elif mutation == "missing-events":
                evidence["events"] = []
            elif mutation == "no-unlock":
                evidence["unlock"]["removed"] = False
            elif mutation == "active-lock":
                evidence["lock_present_after"] = True
            elif mutation == "active-turn":
                evidence["final_run"]["active_turn"] = {"session_id": "2"}
            elif mutation == "changed-edits":
                evidence["preserved"]["checkpoint.txt"] = False
            elif mutation == "changed-verifier":
                evidence["frozen"]["verify_recovery.py"] = False
            elif mutation == "changed-handoff":
                evidence["session1_handoff_unchanged"] = False
            elif mutation == "failed-product":
                evidence["product_verification"]["returncode"] = 1
            elif mutation == "no-conversation-pause":
                evidence["resume_refusal"]["reason"] = "all-complete"
            elif mutation == "unauthorized-launch":
                evidence["refusal_launches_after"] += 1
            elif mutation == "no-fresh-authorization":
                evidence["resume_argv"].remove("--fresh-conversation")
            elif mutation == "missing-preserved-input":
                del evidence["preserved"]["checkpoint.txt"]
            elif mutation == "missing-frozen-input":
                del evidence["frozen"]["verify_recovery.py"]
            else:
                evidence["harness_processes"][-1]["confirmation"]["complete"] = False
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                recovery.verify_recovery_evidence(evidence, "codex")

    def test_budget_expiry_cleans_up_the_live_runner_and_sampled_child(self) -> None:
        (self.root / "repo").mkdir()
        marker = self.root / "child.pid"
        commands = Commands(self.root, deadline=30)
        script = (
            "import subprocess, sys, time; from pathlib import Path; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)']); "
            f"Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)"
        )
        with (
            self.assertRaises(TimeoutError),
            start_run(commands, [sys.executable, "-c", script]) as run,
        ):
            until = time.monotonic() + 10
            while not marker.exists():
                self.assertIsNone(run.child.poll())
                self.assertLess(time.monotonic(), until)
                time.sleep(0.01)
            run.descendants = process_tree.collect_descendant_evidence(run.child.pid)
            self.assertTrue(run.descendants["collected"])
            commands.until = 0
            commands.remaining()
        self.assertIsNotNone(run.child.poll())
        until = time.monotonic() + 10
        while not run.confirm()["complete"]:
            self.assertLess(time.monotonic(), until)
            time.sleep(0.05)
        self.assertEqual(-9, commands.calls[0]["returncode"])

    def test_changed_runner_identity_refuses_crash_signal(self) -> None:
        (self.root / "repo").mkdir()
        commands = Commands(self.root, deadline=30)
        with start_run(
            commands, [sys.executable, "-c", "import time; time.sleep(60)"]
        ) as run:
            with (
                mock.patch.object(
                    process_tree, "process_birth", return_value="different"
                ),
                mock.patch("tests.asl_live_recovery_process.os.kill") as kill,
                self.assertRaisesRegex(RuntimeError, "changed identity"),
            ):
                run.crash()
            kill.assert_not_called()
            self.assertIsNone(run.child.poll())

    def test_interrupt_during_initial_sampling_still_reaps_the_runner(self) -> None:
        (self.root / "repo").mkdir()
        commands = Commands(self.root, deadline=30)
        with (
            mock.patch.object(
                process_tree, "process_birth", side_effect=KeyboardInterrupt
            ),
            mock.patch.object(
                CrashingRun, "cleanup", autospec=True, side_effect=CrashingRun.cleanup
            ) as cleanup,
            self.assertRaises(KeyboardInterrupt),
            start_run(commands, [sys.executable, "-c", "import time; time.sleep(60)"]),
        ):
            self.fail("the initial process sample must be interrupted")
        cleanup.assert_called_once()
        self.assertIsNotNone(cleanup.call_args.args[0].child.poll())
        self.assertEqual(-9, commands.calls[0]["returncode"])

    def test_replaced_verifier_retains_failed_evidence(self) -> None:
        self.install_fixture()
        with mock.patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "ASL_RECOVERY_FIXTURE_MODE": "tamper-verifier",
            },
        ):
            result = recovery.run_recovery(
                "codex", opted_in=True, deadline=120, keep=False
            )
        self.addCleanup(shutil.rmtree, result["artifacts"])
        self.assertEqual("failed", result["status"], result["detail"])
        self.assertIsNone(result["verified_version"])
        self.assertEqual([], result["checks"])
        self.assertIn("changed frozen", result["detail"])
        root = Path(result["artifacts"])
        saved = json.loads((root / "acceptance.json").read_text())
        self.assertEqual("failed", saved["status"])
        self.assertTrue((root / "crash-processes.json").is_file())
        self.assertTrue(
            all(Path(call["stderr"]).is_file() for call in saved["commands"])
        )

    def test_replay_and_changed_partial_edits_fail_independent_verification(
        self,
    ) -> None:
        self.install_fixture()
        for mode in ("replay-session-1", "replace-checkpoint"):
            with (
                self.subTest(mode=mode),
                mock.patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(self.root / "config"),
                        "ASL_RECOVERY_FIXTURE_MODE": mode,
                    },
                ),
            ):
                result = recovery.run_recovery(
                    "codex", opted_in=True, deadline=120, keep=False
                )
                self.addCleanup(shutil.rmtree, result["artifacts"])
                self.assertEqual("failed", result["status"], result["detail"])
                self.assertIn("verify-failed", result["detail"])
                self.assertIsNone(result["verified_version"])
                self.assertEqual([], result["checks"])


if __name__ == "__main__":
    unittest.main()
