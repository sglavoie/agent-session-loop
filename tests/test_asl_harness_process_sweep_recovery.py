"""Process sweep events and post-result leaf recovery."""

from __future__ import annotations

import io
import json
import stat
import sys
import tempfile
import textwrap
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    events,
    plans_snapshot,
    run_commit,
    runner,
    state,
    state_locks,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    FAKE_CODEX_FIXTURE,
    FakeRunControlListener,
    assert_marked_pids_exited,
    init_git_repo,
    kill_marked_pids,
    patched_env,
    read_jsonl,
    write_plan,
)

ASL_TEST_LANE = "integration"


class ProcessSweepAndLeafRecoveryTests(FakeHarnessIntegrationBase):
    def test_turn_sweep_that_killed_something_appends_a_process_sweep_event(
        self,
    ) -> None:
        """A leaking turn is recorded; the sweeps that found nothing are not."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        run_cwd = tmp_dir / "repo"
        init_git_repo(run_cwd)
        marker_dir = tmp_dir / "leftovers"
        marker_dir.mkdir()
        self.addCleanup(kill_marked_pids, marker_dir)
        marker = marker_dir / "turn"
        leftover = self._leftover_script(tmp_dir, marker)
        harness = tmp_dir / "leaking-codex"
        harness.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import pathlib, subprocess, sys, time",
                    f"sys.path.insert(0, {str(FAKE_CODEX_FIXTURE.parent)!r})",
                    f"marker = pathlib.Path({str(marker)!r})",
                    "subprocess.Popen(",
                    f"    [sys.executable, {str(leftover)!r}, str(marker)],",
                    "    stdout=subprocess.DEVNULL,",
                    "    stderr=subprocess.DEVNULL,",
                    ")",
                    "while not marker.exists():",
                    "    time.sleep(0.01)",
                    "import fake_codex",
                    "raise SystemExit(fake_codex.main())",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        harness.chmod(harness.stat().st_mode | stat.S_IXUSR)

        exit_code, payload, plan_dir, state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=FakeRunControlListener(),
                cwd=run_cwd,
                harness_config_overrides={"codex": str(harness)},
            )
        )

        self.assertEqual(0, exit_code, payload)
        leaked = int(marker.read_text(encoding="utf-8"))
        sweeps = self.sweep_events(plan_dir, state_dir)
        # One turn leaked, and nothing else in the run did: a sweep with an
        # empty report is the normal case and stays out of the log.
        self.assertEqual(1, len(sweeps), sweeps)
        self.assertEqual(events.EVENT_SCHEMA_VERSION, sweeps[0]["version"])
        sweep = sweeps[0]["payload"]
        self.assertEqual("turn", sweep["phase"])
        self.assertEqual("test-run", sweep["run_id"])
        self.assertEqual(1, sweep["turn_index"])
        self.assertEqual(1, sweep["session"])
        self.assertEqual("1", sweep["session_id"])
        self.assertNotIn("check", sweep)
        self.assertEqual([leaked], sweep["killed"])
        self.assertEqual([], sweep["kill_failed"])
        self.assertEqual([], sweep["survivors_after"])
        # It stayed in the turn's group, so the pgid enumeration named it.
        self.assertEqual([leaked], sweep["enumeration"]["pgid_members"])
        assert_marked_pids_exited(marker_dir, label="leaked turn child")

    def test_verification_sweep_records_the_check_that_leaked(self) -> None:
        """The same event, from the other sweep site, carries the check name."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        run_cwd = tmp_dir / "repo"
        init_git_repo(run_cwd)
        marker_dir = tmp_dir / "leftovers"
        marker_dir.mkdir()
        self.addCleanup(kill_marked_pids, marker_dir)
        marker = marker_dir / "check"
        leftover = self._leftover_script(tmp_dir, marker)
        check_script = tmp_dir / "leaky-check.sh"
        check_script.write_text(
            "#!/bin/sh\n"
            f'"{sys.executable}" "{leftover}" "{marker}" '
            "</dev/null >/dev/null 2>&1 &\n"
            # Exiting before the leftover has published its PID would leave the
            # assertions below racing a process the sweep already killed.
            f'until [ -f "{marker}" ]; do sleep 0.05; done\n'
            "exit 0\n",
            encoding="utf-8",
        )
        contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- it runs",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": "1",
                        "checks": [
                            {
                                "name": "leaky-check",
                                "type": "command",
                                "argv": ["sh", str(check_script)],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                    indent=2,
                ),
                "```",
            ]
        )

        def inject_contract(plan_dir: Path, _state_dir: Path) -> None:
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})

        exit_code, payload, plan_dir, state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=FakeRunControlListener(),
                cwd=run_cwd,
                before_run=inject_contract,
            )
        )

        self.assertEqual(0, exit_code, payload)
        leaked = int(marker.read_text(encoding="utf-8"))
        sweeps = self.sweep_events(plan_dir, state_dir)
        self.assertEqual(1, len(sweeps), sweeps)
        self.assertEqual(events.EVENT_SCHEMA_VERSION, sweeps[0]["version"])
        sweep = sweeps[0]["payload"]
        self.assertEqual("verification", sweep["phase"])
        self.assertEqual("leaky-check", sweep["check"])
        self.assertEqual("test-run", sweep["run_id"])
        self.assertEqual(1, sweep["session"])
        self.assertEqual([leaked], sweep["killed"])
        self.assertEqual([], sweep["kill_failed"])
        self.assertEqual([], sweep["survivors_after"])
        assert_marked_pids_exited(marker_dir, label="leaked check child")

    def test_post_result_leaf_recovery_advances_and_releases_lock(self) -> None:
        """A terminal fake-Claude result with a live leaf recovers in-place."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            workspace = tmp_dir / "workspace"
            config_home = tmp_dir / "config"
            control_dir = tmp_dir / "fake-claude-state"
            control_dir.mkdir()
            write_plan(plan_dir, ("pending", "pending"))
            init_git_repo(workspace)

            leaf_script = tmp_dir / "fake-claude-leaf.py"
            leaf_script.write_text(
                textwrap.dedent(
                    """\
                    import os
                    import pathlib
                    import signal
                    import sys
                    import time

                    control = pathlib.Path(os.environ["FAKE_CLAUDE_STATE"])
                    (control / "leaf-pid").write_text(str(os.getpid()))

                    def stop(*_args):
                        (control / "leaf-termed").write_text("yes")
                        raise SystemExit(0)

                    signal.signal(signal.SIGTERM, stop)
                    (control / "leaf-ready").write_text("yes")
                    time.sleep(60)
                    """
                ),
                encoding="utf-8",
            )
            fake_claude = tmp_dir / "fake-claude"
            fake_claude.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json
                    import os
                    import pathlib
                    import subprocess
                    import sys

                    plan_index = sys.argv.index("--add-dir") + 1
                    plan = pathlib.Path(sys.argv[plan_index])
                    control = pathlib.Path(os.environ["FAKE_CLAUDE_STATE"])
                    pending = [
                        path
                        for path in sorted(plan.glob("session-*.md"))
                        if "- **Status**: pending" in path.read_text()
                    ]
                    target = pending[0]
                    text = target.read_text()
                    text = text.replace(
                        "- **Status**: pending", "- **Status**: complete", 1
                    )
                    text = text.replace(
                        "- **Key decisions**: (none)",
                        "- **Key decisions**: fake Claude completed the session.",
                        1,
                    )
                    target.write_text(text)
                    result = {
                        "type": "result",
                        "subtype": "success",
                        "result": "Runner status: complete",
                        "num_turns": 1,
                    }
                    first = not (control / "first-used").exists()
                    if first:
                        (control / "first-used").write_text("yes")
                        leaf = subprocess.Popen(
                            [sys.executable, os.environ["FAKE_CLAUDE_LEAF"]]
                        )
                        while not (control / "leaf-ready").exists():
                            pass
                        print(json.dumps(result), flush=True)
                        leaf.wait()
                        message = {
                            "type": "assistant",
                            "message": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "background task reaped",
                                    }
                                ]
                            },
                        }
                        print(json.dumps(message), flush=True)
                        print(json.dumps(result), flush=True)
                    else:
                        print(json.dumps(result), flush=True)
                    """
                ),
                encoding="utf-8",
            )
            fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IXUSR)
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"claude": {"executable": str(fake_claude)}}),
                encoding="utf-8",
            )
            argv = [
                "run",
                str(plan_dir),
                "--cwd",
                str(workspace),
                "--harness",
                "claude",
                "--state-dir",
                str(state_dir),
                "--json",
                "--unsafe-bypass",
                "--allow-not-loop-ready",
                "--no-mailbox",
            ]
            args = cli.build_parser().parse_args(argv)
            real_live_turn = run_commit._LiveTurn

            def fast_live_turn(*call_args, **call_kwargs):
                call_kwargs["heartbeat_interval"] = 0.05
                return real_live_turn(*call_args, **call_kwargs)

            output = io.StringIO()
            with (
                patched_env(
                    XDG_CONFIG_HOME=str(config_home),
                    FAKE_CLAUDE_STATE=str(control_dir),
                    FAKE_CLAUDE_LEAF=str(leaf_script),
                    ASL_HANG_RESULT_GRACE_SECONDS="0.05",
                    ASL_HANG_CONFIRM_SAMPLES="1",
                ),
                mock.patch.object(run_commit, "_LiveTurn", side_effect=fast_live_turn),
                redirect_stdout(output),
            ):
                code = runner.run_from_args(args, run_argv=argv)

            self.assertEqual(0, code, output.getvalue())
            self.assertTrue((control_dir / "leaf-termed").exists())
            self.assertEqual(
                ["complete", "complete"],
                [
                    session.status
                    for session in plans_snapshot.inspect_plan(plan_dir).sessions
                ],
            )
            lock = state_locks.read_lock_info(plan_dir, state_dir)
            self.assertIsNone(lock)
            records = read_jsonl(
                state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
            )
            suspected = [
                record
                for record in records
                if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
            ]
            self.assertEqual(1, len(suspected))
            recovery = suspected[0]["payload"]["hang_suspected"]["recovery"]
            leaf_pid = int((control_dir / "leaf-pid").read_text())
            self.assertEqual([leaf_pid], recovery["candidate_pids"])
            self.assertEqual([leaf_pid], recovery["signaled_pids"])
            self.assertEqual([], recovery["signal_failures"])
            self.assertNotIn("worker_hung", output.getvalue())
