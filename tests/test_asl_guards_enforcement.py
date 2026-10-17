"""Tests for test asl guards enforcement."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    guards,
    harness_execution,
    harnesses,
)
from tests import support_asl_guards as fixtures


class CommandGateEnforcementTests(unittest.TestCase):
    """The gate, exercised by real processes resolving a bare name on PATH."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        fixtures.install_fake_git(self.bin_dir)
        self.run_dir = self.root / "runs" / "run-1"
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.run_dir,
            path_value=str(self.bin_dir),
        )
        base = dict(os.environ)
        base["PATH"] = os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"])
        self.env = self.controller.prepare_turn(base)

    def arm(self, *rules: tuple[str, ...]) -> str:
        outcome = self.controller.add(
            list(rules) or [("git", "push", "origin", "main")]
        )
        self.assertTrue(outcome.ok, outcome.to_payload())
        # A guard added mid-turn installs into a directory the running turn's
        # PATH already names, so the environment is not rebuilt here.
        return outcome.guard_ids[0]

    def run_argv(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            env=self.env,
            capture_output=True,
            text=True,
            cwd=self.root,
            check=False,
        )

    def deny_result(self) -> subprocess.CompletedProcess[str]:
        """The invocation the default rule denies."""
        return self.run_argv(["git", "push", "origin", "main"])

    def test_unguarded_invocation_delegates_to_the_real_executable(self) -> None:
        self.arm()
        result = self.run_argv(["git", "status"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git status", result.stdout)

    def test_guarded_invocation_is_denied_with_a_stable_message(self) -> None:
        guard_id = self.arm()
        result = self.run_argv(["git", "push", "origin", "main"])
        self.assertEqual(guards.DENIED_EXIT_CODE, result.returncode)
        self.assertEqual(126, result.returncode)
        self.assertIn(guard_id, result.stderr)
        self.assertIn("git push origin main", result.stderr)
        self.assertEqual("", result.stdout)

    def test_denial_covers_longer_argv_that_starts_with_the_prefix(self) -> None:
        self.arm()
        result = self.run_argv(["git", "push", "origin", "main", "--force"])
        self.assertEqual(126, result.returncode)

    def test_gate_covers_shell_env_and_python_subprocess_resolution(self) -> None:
        self.arm()
        denied = "git push origin main"
        allowed = "git status"
        cases = {
            "sh -c": (["sh", "-c", denied], ["sh", "-c", allowed]),
            "env": (
                ["env", "GUARD_TEST=1", "git", "push", "origin", "main"],
                ["env", "GUARD_TEST=1", "git", "status"],
            ),
            "python subprocess": (
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys;"
                    "sys.exit(subprocess.run("
                    "['git','push','origin','main']).returncode)",
                ],
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys;"
                    "sys.exit(subprocess.run(['git','status']).returncode)",
                ],
            ),
        }
        for label, (denied_argv, allowed_argv) in cases.items():
            with self.subTest(path=label):
                self.assertEqual(126, self.run_argv(denied_argv).returncode)
                self.assertEqual(0, self.run_argv(allowed_argv).returncode)

    def test_documented_bypasses_are_real(self) -> None:
        """The coverage statement is verified, not merely asserted."""
        self.arm()
        # An absolute path never consults PATH, so the shim is not reached.
        absolute = self.run_argv([str(self.bin_dir / "git"), "push", "origin", "main"])
        self.assertEqual(0, absolute.returncode)
        self.assertIn("fake git push", absolute.stdout)
        # A process that rewrites PATH out from under the overlay is likewise
        # outside the gate. This is why the guarantee is operational.
        rewritten = subprocess.run(
            ["sh", "-c", "git push origin main"],
            env={
                **self.env,
                "PATH": os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"]),
            },
            capture_output=True,
            text=True,
            cwd=self.root,
            check=False,
        )
        self.assertEqual(0, rewritten.returncode)
        self.assertIn(
            "an absolute or relative path to the real executable",
            guards.COMMAND_GATE_COVERAGE["does_not_cover"],
        )

    def test_removal_disables_enforcement_within_the_same_turn(self) -> None:
        guard_id = self.arm()
        self.assertEqual(126, self.deny_result().returncode)
        self.assertEqual(guards.ACTION_REMOVED, self.controller.remove(guard_id).action)
        result = self.run_argv(["git", "push", "origin", "main"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git push", result.stdout)

    def test_expiry_disables_enforcement_and_leaves_an_inert_shim(self) -> None:
        self.arm()
        self.assertEqual(126, self.deny_result().returncode)
        self.controller.expire()
        result = self.run_argv(["git", "push", "origin", "main"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git push", result.stdout)
        self.assertTrue((self.controller.guard_bin / "git").exists())

    def test_shim_never_resolves_to_itself(self) -> None:
        """Delegation excludes guard-bin, so a lost policy cannot loop."""
        self.arm()
        self.controller.policy_file.unlink()
        result = self.run_argv(["git", "status"])
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fake git status", result.stdout)

    def test_multiple_rules_share_one_shim(self) -> None:
        outcome = self.controller.add(
            [("git", "push", "origin", "main"), ("git", "tag", "-d")]
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(126, self.deny_result().returncode)
        self.assertEqual(126, self.run_argv(["git", "tag", "-d", "v1"]).returncode)
        self.assertEqual(0, self.run_argv(["git", "tag"]).returncode)


class GuardHarnessEnvironmentTests(unittest.TestCase):
    """Every harness launches its turn inside the run's overlay."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        fixtures.install_fake_git(self.bin_dir)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.root / "runs" / "run-1",
            path_value=str(self.bin_dir),
        )
        self.controller.add([("git", "push", "origin", "main")])
        base = dict(os.environ)
        base["PATH"] = os.pathsep.join([str(self.bin_dir), "/usr/bin", "/bin"])
        self.env = self.controller.prepare_turn(base)

    def spawn_env(self, name: str, env: Any) -> dict[str, str] | None:
        """Return the environment the adapter would hand to ``Popen``."""
        recorded: dict[str, Any] = {}

        class _Recorder:
            def __init__(self, argv: Any, **kwargs: Any) -> None:
                recorded["argv"] = list(argv)
                recorded["env"] = kwargs.get("env")
                raise FileNotFoundError("stop after spawn arguments")

        invocation = harnesses.HarnessInvocation(
            name=name,
            argv=[name, "--version"],
            cwd=self.root,
            transport=harnesses.TRANSPORT_EXEC,
        )
        with (
            mock.patch.object(harness_execution.subprocess, "Popen", _Recorder),
            self.assertRaises(harnesses.HarnessError),
        ):
            harnesses.get_adapter(name).run(
                cwd=self.root,
                plan_dir=None,
                prompt="hello",
                invocation=invocation,
                env=env,
            )
        return recorded["env"]

    def test_every_harness_spawns_with_the_guarded_path(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                child_env = self.spawn_env(name, self.env)
                self.assertIsNotNone(child_env)
                self.assertEqual(
                    str(self.controller.guard_bin),
                    child_env["PATH"].split(os.pathsep)[0],
                )

    def test_a_child_of_any_harness_environment_is_denied(self) -> None:
        """The environment each harness receives is itself enforcing."""
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                result = subprocess.run(
                    ["sh", "-c", "git push origin main"],
                    env={**self.env, "ASL_HARNESS": name},
                    capture_output=True,
                    text=True,
                    cwd=self.root,
                    check=False,
                )
                self.assertEqual(126, result.returncode)

    def test_run_harness_forwards_the_turn_environment_to_the_adapter(self) -> None:
        """The seam the runner actually calls, not just the adapter beneath it."""
        seen: dict[str, Any] = {}

        def record(_self: Any = None, **kwargs: Any) -> str:
            seen["env"] = kwargs.get("env")
            return "result"

        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                seen.clear()
                with mock.patch.object(
                    type(harnesses.get_adapter(name)), "run", record
                ):
                    harnesses.run_harness(
                        name,
                        cwd=self.root,
                        plan_dir=None,
                        prompt="hello",
                        env=self.env,
                    )
                self.assertEqual(self.env, seen["env"])

    def test_omitting_env_still_inherits_the_parent_environment(self) -> None:
        self.assertIsNone(self.spawn_env("codex", None))


if __name__ == "__main__":
    unittest.main()
