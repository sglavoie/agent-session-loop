"""Harness reader joins and hooks."""

from __future__ import annotations

import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    config,
    harness_claude,
    harness_types,
    harnesses,
    run_commit,
    run_persistence,
    run_reporting,
)
from tests.harness_integration_support import _PythonScriptCopilotAdapter
from tests.support import (
    assert_marked_pids_alive,
    kill_marked_pids,
    patched_env,
    wait_until,
    write_process_tree_scripts,
)


class HarnessReaderJoinAndHookTests(unittest.TestCase):
    """Direct adapter-run tests for bounded reader joins and observation hooks."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)

    def _write_executable(self, name: str, source: str) -> Path:
        path = self.tmp_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_sixty_thousand_stream_lines_keep_exact_logs_and_bounded_writes(
        self,
    ) -> None:
        line_count = 30_000
        fake_harness = self._write_executable(
            "fake-copilot-burst",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys, threading",
                    f"line_count = {line_count}",
                    "def emit(stream, prefix):",
                    "    for index in range(line_count):",
                    "        stream.write(f'{prefix}-{index:05d}\\n')",
                    "    stream.flush()",
                    "threads = [",
                    "    threading.Thread(target=emit, args=(sys.stdout, 'out')) ,",
                    "    threading.Thread(target=emit, args=(sys.stderr, 'err')) ,",
                    "]",
                    "for thread in threads:",
                    "    thread.start()",
                    "for thread in threads:",
                    "    thread.join()",
                    "",
                ]
            ),
        )
        state_path = self.tmp_dir / "state.json"
        event_path = self.tmp_dir / "events.jsonl"
        started_at = clock.isoformat(clock.utc_now())
        payload: dict[str, Any] = {"state_path": str(state_path)}
        active_turn = {
            "run_id": "run-burst",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "total_sessions": 1,
            "harness": "copilot",
            "artifact_dir": str(self.tmp_dir / "turn"),
            "stdout_path": str(self.tmp_dir / "stdout.log"),
            "stderr_path": str(self.tmp_dir / "stderr.log"),
            "last_message_path": str(self.tmp_dir / "last-message.txt"),
            "result_path": str(self.tmp_dir / "result.json"),
            "started_at": started_at,
            "last_activity_at": started_at,
            "heartbeat_count": 0,
            "stdout_chars": 0,
            "stderr_chars": 0,
        }
        persistence = run_persistence._RunPersistence(
            write_state=False, append_events=False
        )
        live_turn = run_commit._LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=run_reporting._ProgressReporter(enabled=False),
            active_turn=active_turn,
            persistence=persistence,
            heartbeat_interval=3600,
        )
        write_state = mock.Mock(wraps=persistence.write_state)

        with mock.patch.object(persistence, "write_state", write_state):
            live_turn.start()
            result = _PythonScriptCopilotAdapter(fake_harness).run(
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="noop",
                stream_callback=live_turn.record_activity,
            )
            active_snapshot = dict(payload["active_turn"])
            live_turn.finish(clock.utc_now())

        expected_stdout = "".join(f"out-{index:05d}\n" for index in range(line_count))
        expected_stderr = "".join(f"err-{index:05d}\n" for index in range(line_count))
        self.assertEqual(expected_stdout, result.stdout)
        self.assertEqual(expected_stderr, result.stderr)
        self.assertEqual(2, write_state.call_count)
        self.assertEqual(len(expected_stdout), active_snapshot["stdout_chars"])
        self.assertEqual(len(expected_stderr), active_snapshot["stderr_chars"])
        self.assertIn(active_snapshot["last_activity_stream"], {"stdout", "stderr"})
        assert result.stream_io is not None
        self.assertEqual(line_count, result.stream_io["stdout_chunks"])
        self.assertEqual(line_count, result.stream_io["stderr_chunks"])
        self.assertEqual(len(expected_stdout), result.stream_io["stdout_chars"])
        self.assertEqual(len(expected_stderr), result.stream_io["stderr_chars"])
        self.assertGreater(result.stream_io["callback_seconds"], 0.0)

    def test_callback_seconds_accumulates_across_both_reader_threads(self) -> None:
        fake_harness = self._write_executable(
            "fake-copilot-callback-timing",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import sys, threading",
                    "def emit(stream, prefix):",
                    "    stream.write(f'{prefix}-1\\n{prefix}-2\\n')",
                    "    stream.flush()",
                    "threads = [",
                    "    threading.Thread(target=emit, args=(sys.stdout, 'out')) ,",
                    "    threading.Thread(target=emit, args=(sys.stderr, 'err')) ,",
                    "]",
                    "for thread in threads:",
                    "    thread.start()",
                    "for thread in threads:",
                    "    thread.join()",
                    "",
                ]
            ),
        )

        def slow_callback(stream_name: harnesses.StreamName, text: str) -> None:
            del stream_name, text
            time.sleep(0.003)

        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            stream_callback=slow_callback,
        )

        assert result.stream_io is not None
        self.assertEqual(2, result.stream_io["stdout_chunks"])
        self.assertEqual(2, result.stream_io["stderr_chunks"])
        self.assertGreaterEqual(result.stream_io["callback_seconds"], 0.01)

    def test_reader_join_bounded_despite_pipe_holding_grandchild(self) -> None:
        root = self.tmp_dir / "trees"
        _parent_script, grandchild_script = write_process_tree_scripts(root)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        fake_harness = self._write_executable(
            "fake-copilot",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(root)!r})",
                    "subprocess.Popen(",
                    "    [",
                    "        sys.executable,",
                    f"        {str(grandchild_script)!r},",
                    "        'harness',",
                    "        str(root),",
                    "    ],",
                    "    start_new_session=True,",
                    ")",
                    "while not (root / 'grandchildren' / 'harness').exists():",
                    "    time.sleep(0.01)",
                    "print('harness done', flush=True)",
                    "",
                ]
            ),
        )
        fired: list[int] = []

        start = time.monotonic()
        result = _PythonScriptCopilotAdapter(fake_harness).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_final_result=lambda: fired.append(1),
        )
        elapsed = time.monotonic() - start

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("harness done", result.stdout)
        # The detached grandchild inherited the output pipes and is still
        # alive, so the reader threads never saw EOF: control returned through
        # the bounded join instead of waiting out the grandchild's 60s sleep.
        self.assertLess(elapsed, 30.0)
        assert_marked_pids_alive(root / "grandchildren", label="detached grandchild")
        # The passthrough processor never reports a final result, so the hook
        # stays silent for harnesses without structured output.
        self.assertEqual([], fired)

    def test_hooks_receive_real_pid_and_fire_final_result_once(self) -> None:
        pid_path = self.tmp_dir / "harness-pid"
        fake_claude = self._write_executable(
            "fake-claude",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, pathlib",
                    f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))",
                    "records = [",
                    "    {",
                    "        'type': 'system',",
                    "        'subtype': 'init',",
                    "        'model': 'claude-test',",
                    "        'session_id': 'session-hooks',",
                    "    },",
                    "    {",
                    "        'type': 'assistant',",
                    "        'message': {",
                    "            'content': [{'type': 'text', 'text': 'working'}],",
                    "        },",
                    "    },",
                    "    {",
                    "        'type': 'result',",
                    "        'subtype': 'success',",
                    "        'result': 'done',",
                    "        'num_turns': 1,",
                    "        'duration_ms': 10,",
                    "    },",
                    "    {'type': 'system', 'subtype': 'status', 'status': 'late'},",
                    "]",
                    "for record in records:",
                    "    print(json.dumps(record), flush=True)",
                    "print('trailing plain line', flush=True)",
                    "",
                ]
            ),
        )
        config_home = self.tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"claude": {"executable": str(fake_claude)}}),
            encoding="utf-8",
        )
        spawn_pids: list[int] = []
        fired: list[int] = []
        observed: list[str] = []

        with patched_env(XDG_CONFIG_HOME=str(config_home)):
            result = harnesses.run_harness(
                "claude",
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="/session-run plan/",
                on_spawn=spawn_pids.append,
                on_final_result=lambda: fired.append(1),
                on_observed_model=observed.append,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([int(pid_path.read_text(encoding="utf-8"))], spawn_pids)
        # Two stdout reads follow the result record; a second fire would have
        # landed there if the Event guard did not hold.
        self.assertEqual(1, len(fired))
        # The init record names the model the harness actually ran, and the
        # same Event guard keeps a later record from firing the hook again.
        self.assertEqual(["claude-test"], observed)
        self.assertEqual("claude-test", result.observed_model)
        self.assertIn("claude result: success", result.stdout)
        self.assertIn("claude status: late", result.stdout)

    def test_a_new_request_rearms_the_final_result_hook(self) -> None:
        fake_claude = self._write_executable(
            "fake-claude",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json",
                    "records = [",
                    "    {'type': 'result', 'subtype': 'success', 'num_turns': 0},",
                    "    {'type': 'result', 'subtype': 'success', 'num_turns': 0},",
                    "    {'type': 'system', 'subtype': 'init', 'session_id': 's'},",
                    "    {'type': 'result', 'subtype': 'success', 'num_turns': 1},",
                    "]",
                    "for record in records:",
                    "    print(json.dumps(record), flush=True)",
                    "",
                ]
            ),
        )
        fired: list[int] = []

        result = harness_claude.ClaudeAdapter(executable=str(fake_claude)).run(
            cwd=self.tmp_dir,
            plan_dir=self.tmp_dir,
            prompt="noop",
            on_final_result=lambda: fired.append(1),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        # The duplicate result belongs to the first request; the result after
        # the new init is a distinct terminal result and must reset the hang
        # sentinel's post-result clock.
        self.assertEqual([1, 1], fired)

    def test_late_write_after_abandoned_readers_cannot_resurrect_finished_state(
        self,
    ) -> None:
        root = self.tmp_dir / "trees"
        for name in ("pending", "grandchildren", "late"):
            (root / name).mkdir(parents=True, exist_ok=True)
        self.addCleanup(kill_marked_pids, root / "grandchildren")
        grandchild_script = root / "late_writer.py"
        grandchild_script.write_text(
            "\n".join(
                [
                    "import json, os, pathlib, sys, time",
                    "root = pathlib.Path(sys.argv[1])",
                    "pending = root / 'pending' / 'harness-grandchild'",
                    "pending.write_text(str(os.getpid()))",
                    "os.replace(pending, root / 'grandchildren' / 'harness')",
                    "while not (root / 'trigger').exists():",
                    "    time.sleep(0.01)",
                    "record = {",
                    "    'type': 'result',",
                    "    'subtype': 'success',",
                    "    'result': 'resurrection',",
                    "}",
                    "sys.stdout.write(json.dumps(record) + '\\n')",
                    "sys.stdout.write('resurrection plain line\\n')",
                    "sys.stdout.flush()",
                    "(root / 'late' / 'harness').write_text('wrote')",
                    "time.sleep(60)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        fake_claude = self._write_executable(
            "fake-claude",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, pathlib, subprocess, sys, time",
                    f"root = pathlib.Path({str(root)!r})",
                    "subprocess.Popen(",
                    "    [",
                    "        sys.executable,",
                    f"        {str(grandchild_script)!r},",
                    "        str(root),",
                    "    ],",
                    "    start_new_session=True,",
                    ")",
                    "while not (root / 'grandchildren' / 'harness').exists():",
                    "    time.sleep(0.01)",
                    "message = {'content': [{'type': 'text', 'text': 'working'}]}",
                    "print(",
                    "    json.dumps({'type': 'assistant', 'message': message}),",
                    "    flush=True,",
                    ")",
                    "while not (root / 'drained').exists():",
                    "    time.sleep(0.01)",
                    "",
                ]
            ),
        )

        state_path = self.tmp_dir / "state.json"
        event_path = self.tmp_dir / "events.jsonl"
        stdout_log = self.tmp_dir / "stdout.log"
        stderr_log = self.tmp_dir / "stderr.log"
        payload: dict[str, Any] = {"state_path": str(state_path)}
        started_at = clock.isoformat(clock.utc_now())

        def make_active_turn(session: int) -> dict[str, Any]:
            return {
                "run_id": "run-late-writer",
                "turn_index": session,
                "session": session,
                "session_id": str(session),
                "total_sessions": 2,
                "harness": "claude",
                "started_at": started_at,
                "last_activity_at": started_at,
                "heartbeat_count": 0,
            }

        live_turn = run_commit._LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=mock.Mock(),
            active_turn=make_active_turn(1),
            heartbeat_interval=3600.0,
        )
        fired: list[int] = []

        def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
            log_path = stdout_log if stream_name == "stdout" else stderr_log
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(text)
            live_turn.record_activity(stream_name, text)
            if "working" in text:
                (root / "drained").write_text("ok", encoding="utf-8")

        live_turn.start()
        with mock.patch.object(harness_types, "_ABANDONED_READER_JOIN_SECONDS", 0.5):
            result = harness_claude.ClaudeAdapter(executable=str(fake_claude)).run(
                cwd=self.tmp_dir,
                plan_dir=self.tmp_dir,
                prompt="noop",
                stream_callback=stream_callback,
                on_final_result=lambda: fired.append(1),
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("claude text: working", result.stdout)
        self.assertEqual([], fired)

        live_turn.finish(clock.utc_now())
        later_turn = run_commit._LiveTurn(
            payload=payload,
            event_path=event_path,
            progress=mock.Mock(),
            active_turn=make_active_turn(2),
            heartbeat_interval=3600.0,
        )
        later_turn.start()
        self.addCleanup(later_turn.stop)

        state_before = state_path.read_bytes()
        events_before = event_path.read_bytes() if event_path.exists() else b""
        stdout_log_before = stdout_log.read_bytes()
        stderr_log_before = stderr_log.read_bytes() if stderr_log.exists() else b""

        (root / "trigger").write_text("go", encoding="utf-8")
        wait_until((root / "late" / "harness").exists)
        # Grace period for the abandoned reader to wake on the late write:
        # the quiescence guard makes it exit without side effects, so nothing
        # asserted below may change.
        time.sleep(1.0)

        self.assertEqual([], fired)
        self.assertNotIn("resurrection", result.stdout)
        self.assertEqual(state_before, state_path.read_bytes())
        self.assertEqual(
            events_before,
            event_path.read_bytes() if event_path.exists() else b"",
        )
        self.assertEqual(stdout_log_before, stdout_log.read_bytes())
        self.assertEqual(
            stderr_log_before,
            stderr_log.read_bytes() if stderr_log.exists() else b"",
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(2, state["active_turn"]["session"])
        assert_marked_pids_alive(
            root / "grandchildren", label="late-writing grandchild"
        )
