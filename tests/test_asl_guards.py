"""Run-scoped command guard tests: rules, the gate, and its lifecycle.

Enforcement is proved by running real processes against a fake ``git``
installed on a temporary ``PATH``. Nothing here touches a real network-affecting
command: the point is that the gate intercepts a bare-name resolution, and a
fake executable proves that more honestly than a real one, which would also
have to be prevented from doing its job.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    command_guard,
    control_cli,
    control_queue,
    events,
    guards,
    harnesses,
    intervention,
    run_coordination,
    state,
)
from tests.support import REPO_ROOT, _write_lock_file, _write_state_payload, write_plan

FAKE_GIT_SOURCE = """#!/bin/sh
echo "fake git $*"
exit 0
"""


def install_fake_git(bin_dir: Path, name: str = "git") -> Path:
    """Install a harmless stand-in for a command an operator would guard."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / name
    fake.write_text(FAKE_GIT_SOURCE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return fake


class CommandRuleParsingTests(unittest.TestCase):
    """A rule is an argv prefix, and everything else is refused by name."""

    def test_posix_tokenization_splits_and_respects_quotes(self) -> None:
        self.assertEqual(("git", "push"), guards.parse_command_rule("git push"))
        self.assertEqual(
            ("git", "push", "origin", "main"),
            guards.parse_command_rule("  git   push origin main  "),
        )
        self.assertEqual(
            ("git", "commit", "-m", "two words"),
            guards.parse_command_rule('git commit -m "two words"'),
        )

    def test_single_token_rule_is_accepted(self) -> None:
        self.assertEqual(("git",), guards.parse_command_rule("git"))

    def test_rejects_every_non_argv_construct_with_a_stable_reason(self) -> None:
        cases = {
            "": guards.REJECT_EMPTY_COMMAND_RULE,
            "   ": guards.REJECT_EMPTY_COMMAND_RULE,
            'git "unterminated': guards.REJECT_UNPARSABLE_COMMAND_RULE,
            "/usr/bin/git push": guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            "./git push": guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            "../git push": guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            "git push && rm -rf /": guards.REJECT_SHELL_OPERATOR,
            "git push | tee log": guards.REJECT_SHELL_OPERATOR,
            "git push origin $BRANCH": guards.REJECT_SHELL_OPERATOR,
            "git push origin *": guards.REJECT_GLOB,
            "git push origin main?": guards.REJECT_GLOB,
            "GIT_DIR=/tmp git push": guards.REJECT_ENVIRONMENT_ASSIGNMENT,
        }
        for rule, reason in cases.items():
            with self.subTest(rule=rule):
                with self.assertRaises(guards.GuardRuleError) as caught:
                    guards.parse_command_rule(rule)
                self.assertEqual(reason, caught.exception.reason)

    def test_empty_token_is_rejected(self) -> None:
        with self.assertRaises(guards.GuardRuleError) as caught:
            guards.parse_command_rule("git '' push")
        self.assertEqual(guards.REJECT_EMPTY_TOKEN, caught.exception.reason)

    def test_matching_is_case_sensitive_and_prefix_only(self) -> None:
        policy = {
            "version": guards.POLICY_VERSION,
            "guards": [{"id": "g-1", "command": ["git", "push", "origin"]}],
        }
        self.assertIsNotNone(
            command_guard.matched_guard(policy, "git", ["push", "origin", "main"])
        )
        self.assertIsNotNone(
            command_guard.matched_guard(policy, "git", ["push", "origin"])
        )
        self.assertIsNone(command_guard.matched_guard(policy, "git", ["push"]))
        self.assertIsNone(
            command_guard.matched_guard(policy, "git", ["Push", "origin"])
        )
        self.assertIsNone(
            command_guard.matched_guard(policy, "git", ["fetch", "origin"])
        )

    def test_one_token_rule_blocks_every_invocation(self) -> None:
        policy = {
            "version": guards.POLICY_VERSION,
            "guards": [{"id": "g-1", "command": ["git"]}],
        }
        for argv in ([], ["status"], ["push", "origin", "main"]):
            with self.subTest(argv=argv):
                self.assertIsNotNone(command_guard.matched_guard(policy, "git", argv))


class GuardControllerTests(unittest.TestCase):
    """Activation order, atomic materialization, and the active-set contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.run_dir = self.root / "runs" / "run-1"
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.run_dir,
            path_value=str(self.bin_dir),
        )

    def read_policy(self) -> dict[str, Any]:
        return json.loads(self.controller.policy_file.read_text(encoding="utf-8"))

    def test_add_materializes_policy_and_shim_and_reports_applied(self) -> None:
        outcome = self.controller.add(
            [("git", "push", "origin", "main")], reason="protect main"
        )
        self.assertEqual(guards.ACTION_APPLIED, outcome.action)
        self.assertTrue(outcome.ok)
        self.assertEqual(1, len(outcome.guard_ids))

        policy = self.read_policy()
        self.assertEqual(guards.POLICY_VERSION, policy["version"])
        self.assertEqual("run-1", policy["run_id"])
        self.assertEqual(
            [["git", "push", "origin", "main"]],
            [guard["command"] for guard in policy["guards"]],
        )
        # The original was resolved once, at activation, outside guard-bin.
        self.assertEqual(str(self.bin_dir / "git"), policy["executables"]["git"])

        shim = self.controller.guard_bin / "git"
        self.assertTrue(shim.exists())
        self.assertTrue(os.access(shim, os.X_OK))

    def test_add_is_all_or_nothing_across_rules(self) -> None:
        outcome = self.controller.add(
            [("git", "push"), ("definitely-not-installed", "run")]
        )
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EXECUTABLE_NOT_FOUND, outcome.reason)
        self.assertEqual((), self.controller.active())
        self.assertFalse(self.controller.policy_file.exists())

    def test_add_rejects_an_empty_rule_set(self) -> None:
        outcome = self.controller.add([])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_EMPTY_COMMAND_RULE, outcome.reason)

    def test_remove_updates_policy_before_reporting_success(self) -> None:
        applied = self.controller.add([("git", "push")])
        guard_id = applied.guard_ids[0]
        removed = self.controller.remove(guard_id)
        self.assertEqual(guards.ACTION_REMOVED, removed.action)
        self.assertEqual([], self.read_policy()["guards"])
        self.assertEqual((), self.controller.active())
        # The shim stays; it is now a plain delegator.
        self.assertTrue((self.controller.guard_bin / "git").exists())

    def test_remove_of_an_unknown_guard_is_rejected(self) -> None:
        outcome = self.controller.remove("g-nope")
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_UNKNOWN_GUARD, outcome.reason)

    def test_expire_clears_the_policy_and_refuses_later_mutations(self) -> None:
        self.controller.add([("git", "push")])
        expired = self.controller.expire()
        self.assertEqual(1, len(expired))
        self.assertFalse(self.controller.policy_file.exists())
        self.assertEqual((), self.controller.active())

        outcome = self.controller.add([("git", "push")])
        self.assertEqual(guards.ACTION_REJECTED, outcome.action)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, outcome.reason)
        self.assertEqual(guards.ACTION_REJECTED, self.controller.remove("g-1").action)

    def test_expire_is_idempotent(self) -> None:
        self.controller.add([("git", "push")])
        self.assertEqual(1, len(self.controller.expire()))
        self.assertEqual((), self.controller.expire())

    def test_prepare_turn_creates_guard_bin_and_prepends_path(self) -> None:
        env = self.controller.prepare_turn({"PATH": str(self.bin_dir), "KEEP": "1"})
        self.assertTrue(self.controller.guard_bin.is_dir())
        self.assertEqual(
            str(self.controller.guard_bin), env["PATH"].split(os.pathsep)[0]
        )
        # Nothing else about the environment is disturbed.
        self.assertEqual("1", env["KEEP"])
        self.assertIn(str(self.bin_dir), env["PATH"].split(os.pathsep))
        # No guard is active, so the empty directory is the whole overlay.
        self.assertFalse(self.controller.policy_file.exists())
        self.assertEqual([], list(self.controller.guard_bin.iterdir()))

    def test_prepare_turn_rearms_a_guard_added_during_an_earlier_turn(self) -> None:
        self.controller.add([("git", "push")])
        self.controller.policy_file.unlink()
        self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        self.assertTrue(self.controller.policy_file.exists())

    def test_prepare_turn_does_not_duplicate_the_guard_bin_entry(self) -> None:
        first = self.controller.prepare_turn({"PATH": str(self.bin_dir)})
        second = self.controller.prepare_turn(first)
        self.assertEqual(
            1,
            second["PATH"].split(os.pathsep).count(str(self.controller.guard_bin)),
        )

    def test_active_payload_describes_the_rule_for_status(self) -> None:
        self.controller.add([("git", "push", "origin", "main")], reason="why")
        payload = self.controller.to_payload()[0]
        self.assertEqual(["git", "push", "origin", "main"], payload["command"])
        self.assertEqual("git push origin main", payload["rendered_command"])
        self.assertEqual("git", payload["executable"])
        self.assertEqual("why", payload["reason"])


class CommandGateEnforcementTests(unittest.TestCase):
    """The gate, exercised by real processes resolving a bare name on PATH."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
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
        install_fake_git(self.bin_dir)
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
            mock.patch.object(harnesses.subprocess, "Popen", _Recorder),
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


class GuardControlQueueTests(unittest.TestCase):
    """Guard mutations are durable, validated commands like every other."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = self.root / "state"

    def test_add_guard_round_trips_through_the_queue(self) -> None:
        queued = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_ADD_GUARD,
            state_dir=self.state_dir,
            deny_commands=["git push origin main"],
            expected_run_id="run-1",
            reason="protect main",
        )
        self.assertEqual((("git", "push", "origin", "main"),), queued.deny_commands)
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.rejected)
        command = drained.accepted[0]
        self.assertEqual(control_queue.COMMAND_ADD_GUARD, command.command)
        self.assertEqual((("git", "push", "origin", "main"),), command.deny_commands)
        self.assertEqual("run-1", command.expected_run_id)
        self.assertEqual("protect main", command.reason)

    def test_remove_guard_round_trips_through_the_queue(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REMOVE_GUARD,
            state_dir=self.state_dir,
            guard_id="g-abc",
            expected_run_id="run-1",
        )
        command = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        ).accepted[0]
        self.assertEqual("g-abc", command.guard_id)
        self.assertEqual("run-1", command.expected_run_id)

    def test_append_rejects_malformed_guard_commands(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            (
                {"deny_commands": ["git push"]},
                control_queue.REJECT_MISSING_EXPECTED_RUN_ID,
            ),
            ({"expected_run_id": "run-1"}, control_queue.REJECT_MISSING_GUARD_RULE),
            (
                {"deny_commands": [], "expected_run_id": "run-1"},
                control_queue.REJECT_MISSING_GUARD_RULE,
            ),
            (
                {"deny_commands": ["/bin/git push"], "expected_run_id": "run-1"},
                guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            ),
            (
                {"deny_commands": ["git push && rm"], "expected_run_id": "run-1"},
                guards.REJECT_SHELL_OPERATOR,
            ),
        ]
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(control_queue.ControlQueueError) as caught:
                    control_queue.append_command(
                        self.plan_dir,
                        control_queue.COMMAND_ADD_GUARD,
                        state_dir=self.state_dir,
                        **kwargs,
                    )
                self.assertEqual(reason, str(caught.exception))

    def test_remove_without_a_guard_id_is_rejected(self) -> None:
        with self.assertRaises(control_queue.ControlQueueError) as caught:
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REMOVE_GUARD,
                state_dir=self.state_dir,
                expected_run_id="run-1",
            )
        self.assertEqual(control_queue.REJECT_MISSING_GUARD_ID, str(caught.exception))

    def test_drain_rejects_a_hand_written_line_with_the_same_reason(self) -> None:
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps(
                {
                    "id": "c-1",
                    "command": control_queue.COMMAND_ADD_GUARD,
                    "deny_commands": ["git push"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.accepted)
        self.assertEqual(
            control_queue.REJECT_MISSING_EXPECTED_RUN_ID, drained.rejected[0].reason
        )


class _RecordingPersistence:
    """The coordination persistence seam, keeping events and dropping state."""

    def __init__(self, append_event) -> None:
        self.lock = threading.RLock()
        self.append_event = append_event
        self.states: list[dict[str, Any]] = []

    def write_state(self, payload: dict[str, Any]) -> None:
        self.states.append(dict(payload))


class GuardDrainTests(unittest.TestCase):
    """The runner applies guard commands only to the run they name."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        install_fake_git(self.bin_dir)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = self.root / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True, exist_ok=True)
        self.event_path = self.plan_state / events.EVENTS_FILE_NAME
        self.payload: dict[str, Any] = {"run_id": "run-1"}
        self.control_payload = run_coordination.initial_control_payload()
        self.persistence = _RecordingPersistence(self.append_event)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.plan_state / "runs" / "run-1",
            path_value=str(self.bin_dir),
        )

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def guard_events(self) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [
            record["payload"]
            for record in records
            if record["payload"].get("action") in events.GUARD_TERMINAL_ACTIONS
        ]

    def drain(self) -> None:
        run_coordination.drain_external_control_queue(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=run_coordination.run_control.RunControlState(),
            seen_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            context=None,
            persistence=self.persistence,
            run_control_lock=mock.MagicMock(),
            guard_controller=self.controller,
        )

    def queue(self, **kwargs: Any) -> None:
        control_queue.append_command(
            self.plan_dir,
            kwargs.pop("command"),
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_guard_add_is_applied_and_recorded(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push origin main"],
            expected_run_id="run-1",
        )
        self.drain()
        recorded = self.guard_events()
        self.assertEqual(1, len(recorded))
        self.assertEqual(events.RUN_CONTROL_ACTION_GUARD_APPLIED, recorded[0]["action"])
        self.assertEqual(1, len(self.controller.active()))
        self.assertEqual(1, len(self.control_payload["guards"]))
        self.assertEqual(
            self.control_payload["guards"], self.payload["control"]["guards"]
        )

    def test_guard_for_a_different_run_is_rejected_at_drain(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push"],
            expected_run_id="run-0",
        )
        self.drain()
        recorded = self.guard_events()
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_REJECTED, recorded[0]["action"]
        )
        self.assertEqual(guards.REJECT_RUN_CHANGED, recorded[0]["reason"])
        self.assertEqual((), self.controller.active())

    def test_guard_command_without_a_controller_is_rejected(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push"],
            expected_run_id="run-1",
        )
        self.controller = None  # type: ignore[assignment]
        self.drain()
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, self.guard_events()[0]["reason"])

    def test_guard_remove_is_applied_and_recorded(self) -> None:
        applied = self.controller.add([("git", "push")])
        self.queue(
            command=control_queue.COMMAND_REMOVE_GUARD,
            guard_id=applied.guard_ids[0],
            expected_run_id="run-1",
        )
        self.drain()
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_REMOVED, self.guard_events()[0]["action"]
        )
        self.assertEqual((), self.controller.active())
        self.assertEqual([], self.control_payload["guards"])

    def test_removing_an_unknown_guard_is_rejected(self) -> None:
        self.queue(
            command=control_queue.COMMAND_REMOVE_GUARD,
            guard_id="g-nope",
            expected_run_id="run-1",
        )
        self.drain()
        self.assertEqual(guards.REJECT_UNKNOWN_GUARD, self.guard_events()[0]["reason"])

    def test_expiry_records_the_guards_it_ended(self) -> None:
        self.controller.add([("git", "push")])
        run_coordination.expire_run_guards(
            controller=self.controller,
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            persistence=self.persistence,
        )
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        expired = [
            record["payload"]
            for record in records
            if record["payload"].get("action")
            == events.RUN_CONTROL_ACTION_GUARDS_EXPIRED
        ]
        self.assertEqual(1, len(expired))
        self.assertEqual(1, len(expired[0]["guard_ids"]))
        self.assertEqual([], self.control_payload["guards"])

    def test_expiry_of_an_empty_gate_records_nothing(self) -> None:
        run_coordination.expire_run_guards(
            controller=self.controller,
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            persistence=self.persistence,
        )
        self.assertFalse(self.event_path.exists())


class GuardCliTests(unittest.TestCase):
    """The operator surface: refuses without a run, waits for enforcement."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.root / "state"

    def args(self, **overrides: Any) -> Any:
        namespace = {
            "plan_dir": str(self.plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
            "wait": False,
            "wait_timeout": None,
            "deny_command": None,
            "reason": None,
            "id": None,
        }
        namespace.update(overrides)
        return type("Args", (), namespace)()

    def activate_run(self, run_id: str = "run-1") -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": run_id, "started_at": "2026-01-01T00:00:00+00:00"},
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())

    def capture(self, handler, args) -> tuple[int, dict[str, Any]]:
        import contextlib
        import io as _io

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = handler(args)
        return code, json.loads(buffer.getvalue())

    def test_add_without_a_rule_is_refused_before_anything_is_written(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args, self.args()
        )
        self.assertEqual(1, code)
        self.assertEqual(control_queue.REJECT_MISSING_GUARD_RULE, payload["reason"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_add_names_the_rule_that_is_malformed(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["/bin/git push"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_EXECUTABLE_NOT_BARE_NAME, payload["reason"])

    def test_add_without_an_active_run_is_refused(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["git push"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, payload["reason"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_remove_without_an_active_run_is_refused(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_remove_from_args,
            self.args(id="g-abc"),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, payload["reason"])

    def test_add_enqueues_with_the_active_run_id(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["git push origin main"], reason="protect"),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual("run-1", payload["run_id"])
        self.assertEqual([["git", "push", "origin", "main"]], payload["deny_commands"])
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("run-1", drained.accepted[0].expected_run_id)

    def test_list_reports_the_active_set_and_the_coverage_statement(self) -> None:
        self.activate_run()
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "control": {
                    "guards": [{"id": "g-1", "command": ["git", "push"]}],
                },
            },
        )
        code, payload = self.capture(
            control_cli.control_guard_list_from_args, self.args()
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["run_active"])
        self.assertEqual("g-1", payload["guards"][0]["id"])
        self.assertEqual(
            guards.COMMAND_GATE_COVERAGE["denied_exit_code"],
            payload["command_gate"]["denied_exit_code"],
        )

    def test_list_without_a_run_reports_no_enforcement(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_list_from_args, self.args()
        )
        self.assertEqual(0, code)
        self.assertFalse(payload["run_active"])
        self.assertEqual([], payload["guards"])

    def test_wait_resolves_on_the_matching_guard_outcome(self) -> None:
        self.activate_run()
        events_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

        def fake_dispatch(*_args: Any, **kwargs: Any) -> Any:
            command = control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_ADD_GUARD,
                state_dir=self.state_dir,
                deny_commands=list(kwargs["deny_commands"]),
                expected_run_id=kwargs["expected_run_id"],
            )
            with open(events_path, "a", encoding="utf-8") as handle:
                for action, command_id in (
                    (
                        events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                        command.id,
                    ),
                    # A concurrent operator's guard must not answer this wait.
                    (events.RUN_CONTROL_ACTION_GUARD_APPLIED, "other-command"),
                    (events.RUN_CONTROL_ACTION_GUARD_APPLIED, command.id),
                ):
                    handle.write(
                        json.dumps(
                            {
                                "name": events.RUN_CONTROL_EVENT,
                                "payload": {
                                    "action": action,
                                    "command_id": command_id,
                                    "guard_ids": ["g-1"],
                                },
                            }
                        )
                        + "\n"
                    )
            return intervention.ControlDispatch(
                command=command,
                queue_path=control_queue.queue_path_for_plan(
                    self.plan_dir, self.state_dir
                ),
                signal=control_cli.control_signal.NotifyResult(
                    delivered=False, reason="no-runner"
                ),
            )

        with mock.patch.object(
            control_cli.intervention, "dispatch_control_command", fake_dispatch
        ):
            code, payload = self.capture(
                control_cli.control_guard_add_from_args,
                self.args(deny_command=["git push"], wait=True, wait_timeout=5.0),
            )
        self.assertEqual(0, code, payload)
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_APPLIED, payload["wait"]["terminal_action"]
        )
        # A guard outcome is final on its own; it must not be reported as a
        # run that is still finalizing.
        self.assertNotIn("note", payload["wait"])

    def test_wait_reports_a_refusal_as_a_failure(self) -> None:
        self.activate_run()
        events_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

        def fake_dispatch(*_args: Any, **kwargs: Any) -> Any:
            command = control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_ADD_GUARD,
                state_dir=self.state_dir,
                deny_commands=list(kwargs["deny_commands"]),
                expected_run_id=kwargs["expected_run_id"],
            )
            with open(events_path, "a", encoding="utf-8") as handle:
                for action, extra in (
                    (events.RUN_CONTROL_ACTION_COMMAND_OBSERVED, {}),
                    (
                        events.RUN_CONTROL_ACTION_GUARD_REJECTED,
                        {"reason": guards.REJECT_RUN_CHANGED, "detail": "run-0"},
                    ),
                ):
                    handle.write(
                        json.dumps(
                            {
                                "name": events.RUN_CONTROL_EVENT,
                                "payload": {
                                    "action": action,
                                    "command_id": command.id,
                                    **extra,
                                },
                            }
                        )
                        + "\n"
                    )
            return intervention.ControlDispatch(
                command=command,
                queue_path=control_queue.queue_path_for_plan(
                    self.plan_dir, self.state_dir
                ),
                signal=control_cli.control_signal.NotifyResult(
                    delivered=False, reason="no-runner"
                ),
            )

        with mock.patch.object(
            control_cli.intervention, "dispatch_control_command", fake_dispatch
        ):
            code, payload = self.capture(
                control_cli.control_guard_add_from_args,
                self.args(deny_command=["git push"], wait=True, wait_timeout=5.0),
            )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_RUN_CHANGED, payload["reason"])


class GuardStatusTests(unittest.TestCase):
    """Status reports the gate a live run is enforcing, and nothing more."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.root / "state"

    def status(self) -> dict[str, Any]:
        from agent_session_loop import plans

        return state.status_payload(plans.inspect_plan(self.plan_dir), self.state_dir)

    def test_no_run_reports_an_empty_gate_with_its_coverage(self) -> None:
        payload = self.status()
        self.assertEqual([], payload["command_guards"]["active"])
        self.assertEqual(0, payload["command_guards"]["count"])
        self.assertEqual(
            guards.COMMAND_GATE_COVERAGE["mechanism"],
            payload["command_guards"]["coverage"]["mechanism"],
        )

    def test_active_run_reports_its_guards(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "control": {
                    "guards": [
                        {
                            "id": "g-1",
                            "command": ["git", "push"],
                            "rendered_command": "git push",
                        }
                    ]
                },
            },
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        payload = self.status()
        self.assertEqual(1, payload["command_guards"]["count"])
        self.assertEqual("g-1", payload["command_guards"]["active"][0]["id"])

    def test_a_finished_run_reports_no_enforcement(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "run_finished_at": "2026-01-01T01:00:00+00:00",
                "control": {"guards": [{"id": "g-1", "command": ["git", "push"]}]},
            },
        )
        payload = self.status()
        self.assertEqual([], payload["command_guards"]["active"])


class GuardDocumentationTests(unittest.TestCase):
    """Every stable reason a guard can produce is documented."""

    def test_guard_reasons_are_documented(self) -> None:
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        reasons = [
            guards.REJECT_NO_ACTIVE_RUN,
            guards.REJECT_RUN_CHANGED,
            guards.REJECT_UNKNOWN_GUARD,
            guards.REJECT_EXECUTABLE_NOT_FOUND,
            guards.REJECT_ACTIVATION_FAILED,
            guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            guards.REJECT_SHELL_OPERATOR,
            guards.REJECT_GLOB,
            guards.REJECT_ENVIRONMENT_ASSIGNMENT,
            control_queue.REJECT_MISSING_EXPECTED_RUN_ID,
            control_queue.REJECT_MISSING_GUARD_RULE,
            control_queue.REJECT_MISSING_GUARD_ID,
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(f"`{reason}`", text)

    def test_bypasses_are_documented(self) -> None:
        text = (REPO_ROOT / "docs" / "agent-session-loop.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("asl control guard add", text)
        for bypass in guards.COMMAND_GATE_COVERAGE["does_not_cover"]:
            with self.subTest(bypass=bypass):
                self.assertIn(bypass, text)


if __name__ == "__main__":
    unittest.main()
