from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_session_loop import config, events, harnesses, plans, runner, state


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_codex.py"


@contextmanager
def patched_env(**updates: str | None):
    originals = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def write_plan(plan_dir: Path, statuses: tuple[str, ...]) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    for number, status_value in enumerate(statuses, start=1):
        (plan_dir / f"session-{number}.md").write_text(
            "\n".join(
                [
                    f"# Session {number}: Test",
                    "",
                    "Instructions.",
                    "",
                    "## Handoff",
                    "",
                    f"- **Status**: {status_value}",
                    "- **Files modified**: (none)",
                    "- **Key decisions**: (none)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def install_fake_codex(tmp_dir: Path) -> Path:
    fake_path = tmp_dir / "codex"
    shutil.copyfile(FAKE_CODEX_FIXTURE, fake_path)
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path


def fail_open_for_path(target: Path):
    canonical_target = plans.canonicalize_path(target)
    original_open = Path.open

    def failing_open(path: Path, *args, **kwargs):
        if plans.canonicalize_path(path) == canonical_target:
            raise PermissionError(errno.EACCES, "permission denied", os.fspath(canonical_target))
        return original_open(path, *args, **kwargs)

    return mock.patch.object(Path, "open", failing_open)


class ParserAndStateTests(unittest.TestCase):
    def test_inspect_plan_sorts_exact_session_files_and_finds_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending", "complete"))
            (plan_dir / "session-10.expand.md").write_text("ignored\n", encoding="utf-8")
            (plan_dir / "session-10.md").write_text(
                "# Session 10\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual([1, 2, 3, 10], [session.number for session in snapshot.sessions])
            self.assertEqual(["complete", "pending", "complete", "pending"], [session.status for session in snapshot.sessions])
            self.assertEqual(2, snapshot.next_pending.number)
            self.assertEqual(plans.path_sha256(plan_dir), snapshot.path_hash)

    def test_unknown_and_empty_statuses_are_reported_as_invalid_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Status**: maybe\n",
                encoding="utf-8",
            )
            unknown = plans.inspect_plan(plan_dir)
            self.assertEqual("unknown", unknown.sessions[0].status)
            self.assertEqual("unknown-status", runner._invalid_status_pause(unknown)[0])

            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Files modified**: none\n",
                encoding="utf-8",
            )
            empty = plans.inspect_plan(plan_dir)
            self.assertEqual("empty", empty.sessions[0].status)
            self.assertEqual("malformed-plan", runner._invalid_status_pause(empty)[0])

    def test_inspect_plan_wraps_plan_file_read_failures(self) -> None:
        cases = (
            "context.md",
            "manifest.md",
            "parallel-validation.md",
            "session-1.expand.md",
        )

        for relative_name in cases:
            with self.subTest(relative_name=relative_name):
                with tempfile.TemporaryDirectory() as tmp:
                    plan_dir = Path(tmp) / "plan"
                    write_plan(plan_dir, ("pending",))
                    target = plan_dir / relative_name
                    if not target.exists():
                        target.write_text("extra plan metadata\n", encoding="utf-8")

                    with fail_open_for_path(target):
                        with self.assertRaises(plans.PlanError) as raised:
                            plans.inspect_plan(plan_dir)

                    message = str(raised.exception)
                    self.assertIn(str(plans.canonicalize_path(target)), message)
                    self.assertIn("permission denied", message.lower())
                    self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_inspect_plan_wraps_session_discovery_read_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            canonical_plan_dir = plans.canonicalize_path(plan_dir)
            original_iterdir = Path.iterdir

            def failing_iterdir(path: Path):
                if plans.canonicalize_path(path) == canonical_plan_dir:
                    raise PermissionError(errno.EACCES, "permission denied", os.fspath(canonical_plan_dir))
                return original_iterdir(path)

            with mock.patch.object(Path, "iterdir", failing_iterdir):
                with self.assertRaises(plans.PlanError) as raised:
                    plans.inspect_plan(plan_dir)

            message = str(raised.exception)
            self.assertIn(str(canonical_plan_dir), message)
            self.assertIn("permission denied", message.lower())
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_acquire_lock_rejects_active_lock_and_replaces_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            lock_info = state.acquire_lock(plan_dir, state_dir)
            self.assertTrue(lock_info.active)
            with self.assertRaises(state.StateError):
                state.acquire_lock(plan_dir, state_dir)
            state.release_lock(plan_dir, state_dir)

            lock_path = state.lock_path_for_plan(plan_dir, state_dir)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps({"pid": 0, "plan_dir": str(plans.canonicalize_path(plan_dir))}),
                encoding="utf-8",
            )
            replaced = state.acquire_lock(plan_dir, state_dir)
            self.assertTrue(replaced.active)
            state.release_lock(plan_dir, state_dir)

    def test_status_payload_includes_json_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertTrue(payload["state_path"].endswith("/state.json"))
            self.assertTrue(payload["events_path"].endswith("/events.jsonl"))
            self.assertEqual(1, payload["counts"]["pending"])


class CommandAndConfigTests(unittest.TestCase):
    def test_codex_command_uses_workspace_write_or_unsafe_bypass(self) -> None:
        adapter = harnesses.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        last_message = Path("/tmp/state/last-message-1.txt")

        safe = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            last_message_path=last_message,
        )
        self.assertIn("-s", safe)
        self.assertIn("workspace-write", safe)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", safe)
        self.assertNotIn("-a", safe)

        unsafe = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            unsafe_bypass=True,
            last_message_path=last_message,
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", unsafe)
        self.assertNotIn("workspace-write", unsafe)

    def test_adapter_command_shapes_for_claude_and_copilot(self) -> None:
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")

        claude = harnesses.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
        )
        self.assertEqual("dontAsk", claude[claude.index("--permission-mode") + 1])
        self.assertIn("--add-dir", claude)

        copilot = harnesses.CopilotAdapter(executable="copilot").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
        )
        self.assertIn("--allow-all-tools", copilot)
        self.assertIn("--allow-all-paths", copilot)
        self.assertEqual(str(cwd), copilot[copilot.index("-C") + 1])

    def test_custom_harness_config_accepts_supported_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "harnesses.json"
            cases = [
                ({"codex": {"executable": "/bin/codex"}}, "/bin/codex"),
                ({"codex": "/bin/codex-short"}, "/bin/codex-short"),
                ({"harnesses": {"codex": {"executable": "/bin/codex-nested"}}}, "/bin/codex-nested"),
            ]

            for raw, expected in cases:
                with self.subTest(raw=raw):
                    config_path.write_text(json.dumps(raw), encoding="utf-8")
                    self.assertEqual(
                        {"codex": {"executable": expected}},
                        config.load_harness_config(config_path),
                    )
                    self.assertEqual(expected, config.configured_harness_executable("codex", config_path))

    def test_build_session_prompt_uses_harness_skill_prefix(self) -> None:
        self.assertTrue(runner.build_session_prompt("codex", "/tmp/plan").startswith("$session-run "))
        self.assertTrue(runner.build_session_prompt("claude", "/tmp/plan").startswith("/session-run "))


class FakeHarnessIntegrationTests(unittest.TestCase):
    def setup_loop(
        self,
        *,
        statuses: tuple[str, ...],
    ) -> tuple[Path, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fake_codex = install_fake_codex(tmp_dir)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        write_plan(plan_dir, statuses)
        return plan_dir, state_dir, config_home, fake_codex

    def run_loop_command(
        self,
        *,
        mode: str,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        timeout: int | None = None,
        max_sessions: int | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        command = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(plan_dir),
            "--cwd",
            str(REPO_ROOT),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
        ]
        if timeout is not None:
            command.extend(["--timeout", str(timeout)])
        if max_sessions is not None:
            command.extend(["--max-sessions", str(max_sessions)])

        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["FAKE_CODEX_MODE"] = mode
        env["FAKE_CODEX_SLEEP"] = "5"
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def run_loop(
        self,
        *,
        mode: str,
        statuses: tuple[str, ...],
        timeout: int | None = None,
        max_sessions: int | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], Path, Path, Path]:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(statuses=statuses)
        completed, payload = self.run_loop_command(
            mode=mode,
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            timeout=timeout,
            max_sessions=max_sessions,
        )
        return completed, payload, plan_dir, state_dir, fake_codex

    def test_successful_fake_harness_turn_persists_state_and_artifacts(self) -> None:
        completed, payload, plan_dir, _state_dir, fake_codex = self.run_loop(
            mode="success",
            statuses=("pending", "pending"),
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(["codex"], payload["harness_config"]["configured_harnesses"])

        turn = payload["turns"][0]
        self.assertEqual(str(fake_codex), turn["argv"][0])
        self.assertEqual(payload["run_id"], turn["run_id"])
        self.assertEqual(1, turn["turn_index"])
        self.assertEqual(1, turn["session"])
        self.assertEqual(
            Path(payload["state_dir"]) / "runs" / payload["run_id"],
            Path(payload["run_dir"]),
        )
        artifact_dir = Path(turn["artifact_dir"])
        self.assertEqual(
            Path(payload["run_dir"]) / "turn-0001-session-1",
            artifact_dir,
        )
        self.assertEqual(artifact_dir / "stdout.log", Path(turn["stdout_path"]))
        self.assertEqual(artifact_dir / "stderr.log", Path(turn["stderr_path"]))
        self.assertEqual(
            artifact_dir / "last-message.txt",
            Path(turn["last_message_path"]),
        )
        self.assertEqual(artifact_dir / "result.json", Path(turn["result_path"]))
        for key in ("stdout_path", "stderr_path", "last_message_path", "result_path"):
            self.assertTrue(Path(turn[key]).exists(), key)
        events_payloads = [
            json.loads(line)
            for line in Path(payload["events_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        turn_started = next(
            event for event in events_payloads if event["name"] == "turn_started"
        )
        self.assertEqual(payload["run_id"], turn_started["payload"]["run_id"])
        self.assertEqual(1, turn_started["payload"]["turn_index"])
        self.assertEqual(str(artifact_dir), turn_started["payload"]["artifact_dir"])
        turn_finished = next(
            event for event in events_payloads if event["name"] == "turn_finished"
        )
        self.assertEqual(payload["run_id"], turn_finished["payload"]["run_id"])
        self.assertEqual(1, turn_finished["payload"]["turn_index"])
        self.assertEqual(1, turn_finished["payload"]["session"])
        self.assertEqual(str(artifact_dir), turn_finished["payload"]["artifact_dir"])
        for key in ("stdout_path", "stderr_path", "last_message_path", "result_path"):
            self.assertEqual(turn[key], turn_finished["payload"][key])
            self.assertEqual(artifact_dir, Path(turn_finished["payload"][key]).parent)
        self.assertTrue(Path(payload["state_path"]).exists())
        self.assertTrue(Path(payload["events_path"]).exists())
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_retry_after_failed_turn_keeps_first_run_artifacts(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        first_completed, first_payload = self.run_loop_command(
            mode="failure",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        self.assertEqual(1, first_completed.returncode)
        self.assertFalse(first_payload["ok"])
        first_turn = first_payload["turns"][0]
        first_stderr = Path(first_turn["stderr_path"])
        first_result = Path(first_turn["result_path"])
        first_artifact_dir = Path(first_turn["artifact_dir"])
        self.assertEqual(
            Path(first_payload["run_dir"]) / "turn-0001-session-1",
            first_artifact_dir,
        )
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        original_stderr = first_stderr.read_text(encoding="utf-8")
        original_result = json.loads(first_result.read_text(encoding="utf-8"))
        self.assertIn("fake codex failure", original_stderr)
        self.assertEqual(7, original_result["returncode"])
        self.assertEqual(first_payload["run_id"], original_result["run_id"])

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
        )

        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertNotEqual(first_payload["run_id"], second_payload["run_id"])
        second_turn = second_payload["turns"][0]
        self.assertNotEqual(first_artifact_dir, Path(second_turn["artifact_dir"]))
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        self.assertEqual(original_stderr, first_stderr.read_text(encoding="utf-8"))
        self.assertEqual(
            original_result,
            json.loads(first_result.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_nonzero_fake_harness_pauses_without_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="failure",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)

    def test_timed_out_fake_harness_reports_timeout_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="timeout",
            statuses=("pending",),
            timeout=1,
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertTrue(payload["turns"][0]["timed_out"])

    def test_bad_fake_harness_progress_reports_multiple_status_changes(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="multiple-progress",
            statuses=("pending", "pending"),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("multiple-status-changes", payload["pause_reason"])
        self.assertEqual(2, len(payload["observed_status_changes"]))

    def test_no_progress_fake_harness_reports_no_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="no-progress",
            statuses=("pending",),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertEqual("no-status-change", payload["reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual([], payload["observed_status_changes"])
        self.assertEqual(payload["pause_details"], payload["details"])
        self.assertEqual(expected, payload["details"]["expected_status_changes"])
        self.assertEqual([], payload["details"]["observed_status_changes"])

    def test_unexpected_progress_reports_unexpected_status_change(self) -> None:
        completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="unexpected-progress",
            statuses=("pending", "pending"),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        observed = [{"number": 2, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual(observed, payload["observed_status_changes"])
        self.assertEqual(observed, payload["details"]["observed_status_changes"])
        self.assertEqual(1, payload["final_snapshot"]["next_pending"])
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_runner_pause_signal_reports_runner_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("runner-pause", payload["pause_reason"])
        self.assertEqual("operator requested pause", payload["pause_message"])
        self.assertEqual({"session": 1, "source": "last_message"}, payload["details"])
        turn = payload["turns"][0]
        self.assertEqual("operator requested pause", turn["runner_pause"])
        self.assertEqual("last_message", turn["runner_pause_source"])

    def test_malformed_after_turn_reports_malformed_plan_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="malformed-after-turn",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertEqual(
            "plan could not be inspected after the harness turn",
            payload["pause_message"],
        )
        self.assertEqual(1, payload["details"]["session"])
        self.assertIn("missing context.md", payload["details"]["error"])
        turn = payload["turns"][0]
        self.assertIsNone(turn["after"])
        self.assertIn("missing context.md", turn["plan_error"])


class JsonCliTests(unittest.TestCase):
    def test_status_and_dry_run_json_outputs_include_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            status_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            status_payload = json.loads(status_completed.stdout)
            self.assertTrue(status_payload["ok"])
            self.assertEqual("status", status_payload["command"])
            self.assertEqual(1, status_payload["next_pending"])
            self.assertTrue(status_payload["state_path"].endswith("/state.json"))
            self.assertTrue(status_payload["events_path"].endswith("/events.jsonl"))

            run_completed = subprocess.run(
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
                text=True,
                capture_output=True,
                check=True,
            )
            run_payload = json.loads(run_completed.stdout)
            self.assertTrue(run_payload["ok"])
            self.assertEqual("run", run_payload["command"])
            self.assertEqual({}, run_payload["details"])
            self.assertEqual("dry-run", run_payload["reason"])
            self.assertIn("harness_config", run_payload)
            self.assertEqual(1, run_payload["planned"]["next_pending"])
            self.assertTrue(run_payload["state_path"].endswith("/state.json"))
            self.assertTrue(run_payload["events_path"].endswith("/events.jsonl"))

    def test_status_json_error_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
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
            self.assertFalse(payload["paused"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("missing context.md", payload["error"])
            self.assertEqual({}, payload["details"])

    def test_doctor_json_outputs_harness_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
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
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("codex", payload["harness"])
            self.assertEqual(str(fake_codex), payload["resolved_executable"])
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(plans.canonicalize_path(plan_dir)), payload["plan_dir"])
            self.assertEqual(1, payload["next_pending"])
            self.assertTrue(payload["prompt"].startswith("$session-run "))
            self.assertEqual(["codex"], payload["harness_config"]["configured_harnesses"])
            self.assertEqual(str(fake_codex), payload["invocation"]["argv"][0])
            self.assertEqual(payload["invocation"]["argv"], payload["invocation_argv"])

    def test_doctor_json_error_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
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
            self.assertFalse(payload["paused"])
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("missing context.md", payload["error"])
            self.assertEqual({}, payload["details"])
            self.assertEqual("codex", payload["harness"])
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(plan_dir), payload["plan_dir"])

    def test_unlock_json_success_reports_lock_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "unlock",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("unlock", payload["command"])
            self.assertFalse(payload["had_lock"])
            self.assertFalse(payload["removed"])
            self.assertFalse(payload["active"])
            self.assertFalse(payload["stale"])
            self.assertTrue(payload["lock_path"].endswith("/lock.json"))
            self.assertIn("no lock present", payload["message"])

    def test_unlock_json_active_lock_failure_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            lock_info = state.acquire_lock(plan_dir, state_dir)

            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "unlock",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("unlock", payload["command"])
            self.assertEqual("active-lock", payload["reason"])
            self.assertIn("active lock", payload["error"])
            self.assertEqual(str(lock_info.path), payload["lock_path"])
            self.assertEqual(str(lock_info.path), payload["details"]["lock_path"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)),
                payload["details"]["plan_dir"],
            )


if __name__ == "__main__":
    unittest.main()
