"""Shared constants and base classes for the reply flow test modules."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    qna,
    run_coordination,
    run_persistence,
    runner,
    state,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    write_plan,
)

# The runner publishes its mailbox question about a second into a turn, but
# these tests observe that publish from a helper thread or from a sibling
# process while the ``--all`` gate drives every shard through one thread pool.
# This module's shard has been measured at 426s there against 190s bare, and
# the former inline 5-10s windows were close enough to real latency to fail as
# a timeout rather than as the assertion each test actually makes. Like the
# bounds in tests/test_asl_harness_*.py this is a hang backstop, not
# a latency assertion: it exists so a runner that never publishes surfaces as a
# failure instead of running out the suite's own clock.
QUESTION_PUBLISH_TIMEOUT_SECONDS = 120

# The matching backstop for joining a helper thread once the run has returned.
# It has to outlast the poll window above, which starts before the run does.
HELPER_THREAD_JOIN_TIMEOUT_SECONDS = 120

# A full ``agent_session_loop`` invocation driven as a subprocess. Same
# reasoning as the bound above, and the same value as the constant of this name
# in tests/test_asl_harness_*.py: these invocations take a few
# seconds idle and half again that under the parallel shards, so the former
# 10-30s bounds were latency assertions by accident.
LOOP_RUN_TIMEOUT_SECONDS = 120


class ReplyResumeFlowCase(unittest.TestCase):
    """Shared helpers for the reply/resume flow unit tests."""

    def _seed_paused_plan(
        self,
        tmp_dir: Path,
        *,
        last_message_text: str = "Should we use 1a or 1b?\n",
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
    ) -> tuple[Path, Path, Path]:
        init_git_repo(tmp_dir)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, statuses)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        last_message_path = tmp_dir / "last-message.txt"
        last_message_path.write_text(last_message_text, encoding="utf-8")
        turn = {
            "run_id": "run-prev",
            "turn_index": 2,
            "session_id": paused_session_id,
            "session": int(paused_session_id),
            "session_number": int(paused_session_id),
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": "high",
            "effort_source": "flag",
            "artifact_dir": str(plan_state / "runs" / "run-prev" / "turn-0002"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": False,
            "paused": True,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": int(paused_session_id)},
            "details": {"session": int(paused_session_id)},
            "run_id": "run-prev",
            "run_dir": str(plan_state / "runs" / "run-prev"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": "high",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir, last_message_path

    def _build_args(
        self,
        plan_dir: Path,
        state_dir: Path,
        *,
        reply: str | None,
        dry_run: bool = False,
        json_output: bool = True,
        cwd: Path | None = None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(plan_dir),
            cwd=str(cwd if cwd is not None else REPO_ROOT),
            harness="codex",
            review_harness=None,
            state_dir=str(state_dir),
            json=json_output,
            dry_run=dry_run,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
            reply=reply,
        )

    def _run_capture(
        self,
        args: argparse.Namespace,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = runner.run_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def _run_suspend_grace_wait(
        self,
        *,
        settings: run_coordination.MailboxWaitSettings,
        lease_fresh_from: float | None = None,
        lease_stale_from: float | None = None,
        reply_from: float | None = None,
        suspend_seconds: float = 3600.0,
    ) -> tuple[run_coordination.MailboxWaitResult, float, list[float], list[float]]:
        """Run a wait whose first poll sleep hides a machine suspend.

        The wall clock jumps ``suspend_seconds`` during the first sleep, so
        detection fires at monotonic 1.0 and the lease grace covers awake
        probes until ``1.0 + MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS``.
        The lease reads stale until ``lease_fresh_from``, stale again from
        ``lease_stale_from``, and the mailbox gains a reply at ``reply_from``;
        ``None`` means never. A supervised wait has no terminal instant, so a
        run that turns the lease fresh must also say when supervision stops.
        """
        question_id = "00000000-0000-0000-0000-000000000044"
        question = mailbox_models.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox_vocabulary.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        reply = mailbox_models.MailboxMessage(
            seq=2,
            id="00000000-0000-0000-0000-000000000045",
            kind=mailbox_vocabulary.KIND_REPLY,
            created_at="2026-07-24T12:00:01+00:00",
            text="Option A",
            to=question_id,
        )
        mailbox_path = Path("/tmp/mailbox.jsonl")
        waiting_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question,),
            last_seq=1,
        )
        replied_view = mailbox_snapshot.MailboxView(
            mailbox_path=mailbox_path,
            messages=(question, reply),
            last_seq=2,
        )
        lease_record = mailbox_models.MailboxLease(
            supervisor_id="00000000-0000-0000-0000-000000000046",
            updated_at="2026-07-24T12:00:00+00:00",
        )
        lease_path = Path("/tmp/mailbox-lease.json")
        fresh_lease = mailbox_models.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=True,
        )
        stale_lease = mailbox_models.MailboxLeaseView(
            lease_path=lease_path,
            lease=lease_record,
            fresh=False,
            reason=mailbox_constants.LEASE_EXPIRED,
        )
        epoch = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
        now = [0.0]
        wall_jump = [0.0]
        probe_times: list[float] = []
        sleeps: list[float] = []

        def monotonic() -> float:
            return now[0]

        def wall_clock() -> datetime:
            return epoch + timedelta(seconds=now[0] + wall_jump[0])

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds
            if wall_jump[0] == 0.0:
                wall_jump[0] = suspend_seconds

        def read_lease(*args: object, **kwargs: object) -> object:
            probe_times.append(now[0])
            if lease_stale_from is not None and now[0] >= lease_stale_from:
                return stale_lease
            if lease_fresh_from is not None and now[0] >= lease_fresh_from:
                return fresh_lease
            return stale_lease

        def read_mailbox(*args: object, **kwargs: object) -> object:
            if reply_from is not None and now[0] >= reply_from:
                return replied_view
            return waiting_view

        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                side_effect=lambda path: (now[0],),
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=read_mailbox,
            ),
            mock.patch.object(
                mailbox_leases,
                "read_lease",
                side_effect=read_lease,
            ),
        ):
            result = run_coordination.wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload={},
                question_id=question_id,
                run_id="RUN-TEST",
                settings=settings,
                persistence=run_persistence._RunPersistence(
                    write_state=False,
                    append_events=False,
                ),
                started_monotonic=0.0,
                monotonic=monotonic,
                sleep=sleep,
                wall_clock=wall_clock,
            )
        return result, now[0], probe_times, sleeps

    @staticmethod
    def _question_context(last_message: str, last_message_path: Path) -> qna.QnAContext:
        return qna.QnAContext(
            plan_dir=Path("/tmp/plan"),
            state_dir=Path("/tmp/state"),
            state_path=Path("/tmp/state/state.json"),
            run_id="run-bounded",
            run_dir=None,
            pause_reason="blocked",
            pause_message="Need a choice.",
            pause_details={},
            harness="codex",
            model=None,
            model_source=None,
            effort=None,
            effort_source=None,
            session_id="1",
            next_pending_id="1",
            turn_index=2,
            artifact_dir=None,
            stdout_path=None,
            stderr_path=None,
            last_message_path=last_message_path,
            result_path=None,
            last_message=last_message,
        )


class EndToEndReplyResumeCase(unittest.TestCase):
    """Subprocess coverage for paused replies and live mailbox resolutions.

    Unit tests already cover empty-reply, no-paused-run, reply-context-mismatch,
    storage races, and runner wait mechanics. These tests exercise the public
    CLI through ``python -m agent_session_loop`` so runner state, mailbox
    ownership, fake harness prompts, and persisted artifacts compose correctly
    at the process boundary the docs describe.
    """

    REPLY_TEXT = "1a, 2b, 3a"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...] = ("pending",),
        *,
        contracts_by_id: dict[str, str] | None = None,
    ) -> None:
        if contracts_by_id is None:
            contracts_by_id = {
                str(index): _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    f'"name": "readme-{index}"}}',
                    session=str(index),
                )
                for index in range(1, len(statuses) + 1)
            }
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)

    def _base_env(self, mode: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_STEP_SLEEP": "0",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        return env

    def _run_run(
        self,
        *,
        mode: str,
        reply: str | None = None,
        dry_run: bool = False,
        json_output: bool = True,
        allow_not_loop_ready: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if json_output:
            argv.append("--json")
        if dry_run:
            argv.append("--dry-run")
        if reply is not None:
            argv.extend(["--reply", reply])
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self._base_env(mode),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        payload = json.loads(completed.stdout) if json_output else {}
        return completed, payload

    def _run_questions(
        self,
        *,
        json_output: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "questions",
            str(self.plan_dir),
            "--state-dir",
            str(self.state_dir),
        ]
        if json_output:
            argv.append("--json")
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self._base_env("reply-aware"),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        if json_output:
            return completed, json.loads(completed.stdout)
        return completed, completed.stdout

    def _start_live_mailbox_run(self) -> subprocess.Popen[str]:
        mailbox_appenders.append_guidance(
            self.plan_dir,
            "Keep this run supervised.",
            state_dir=self.state_dir,
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000081",
            state_dir=self.state_dir,
        )
        env = self._base_env("reply-aware")
        env.update(
            {
                run_coordination.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "3",
                run_coordination.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "5",
                run_coordination.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "0.02",
                run_coordination.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "0.1",
            }
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "run",
                str(self.plan_dir),
                "--cwd",
                str(self.repo_root),
                "--harness",
                "codex",
                "--state-dir",
                str(self.state_dir),
                "--allow-not-loop-ready",
                "--json",
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process

    def _wait_for_runner_question(self) -> str:
        state_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir) / events.STATE_FILE_NAME
        )
        deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if state_path.exists():
                try:
                    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
                except OSError, json.JSONDecodeError:
                    state_payload = {}
                waiting = state_payload.get("waiting_for_reply")
                if isinstance(waiting, dict):
                    question_id = waiting.get("question_id")
                    if isinstance(question_id, str):
                        return question_id
            time.sleep(0.02)
        self.fail("runner did not publish a live mailbox question")

    def _resolve_live_question(
        self,
        question_id: str,
        *,
        action: str,
        note: str,
    ) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "inbox",
                "resolve",
                str(self.plan_dir),
                "--to",
                question_id,
                "--action",
                action,
                "--note",
                note,
                "--state-dir",
                str(self.state_dir),
                "--json",
            ],
            cwd=REPO_ROOT,
            env=self._base_env("reply-aware"),
            text=True,
            capture_output=True,
            timeout=LOOP_RUN_TIMEOUT_SECONDS,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(action, payload["message"]["action"])
        self.assertEqual(note, payload["message"]["note"])
        return payload

    def _assert_resolution_wait_cleared_promptly(
        self,
        resolution_payload: dict[str, Any],
        records: list[dict[str, Any]],
        *,
        action: str,
    ) -> None:
        cleared = [
            record
            for record in records
            if record.get("name") == events.MAILBOX_EVENT
            and isinstance(record.get("payload"), dict)
            and record["payload"].get("action") == events.MAILBOX_ACTION_WAIT_CLEARED
        ]
        self.assertEqual(1, len(cleared), records)
        cleared_payload = cleared[0]["payload"]
        message = resolution_payload["message"]
        self.assertEqual("resolution", cleared_payload["reason"])
        self.assertEqual(action, cleared_payload["resolution_action"])
        self.assertEqual(message["id"], cleared_payload["resolution_message_id"])
        self.assertEqual(message["seq"], cleared_payload["resolution_seq"])
        resolution_at = datetime.fromisoformat(message["created_at"])
        cleared_at = datetime.fromisoformat(cleared[0]["created_at"])
        reaction_seconds = (cleared_at - resolution_at).total_seconds()
        self.assertGreaterEqual(reaction_seconds, 0)
        self.assertLessEqual(reaction_seconds, 2.0)
