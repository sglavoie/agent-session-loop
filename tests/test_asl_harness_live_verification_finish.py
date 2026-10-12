"""Live verification finish, shutdown and interrupt behavior."""

from __future__ import annotations

import io
import json
import tempfile
import threading
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    contracts,
    events,
    live_verification,
    plans,
    run_persistence,
    run_reporting,
    verification,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    assert_marked_pids_exited,
    read_jsonl,
    wait_until,
    write_process_tree_scripts,
)


class LiveVerificationFinishTests(FakeHarnessIntegrationBase):
    def test_live_verification_finish_drops_late_check_callbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            progress = run_reporting._ProgressReporter(enabled=True)
            stderr = io.StringIO()
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=progress,
                persistence=run_persistence._RunPersistence(),
                run_id="run-6",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("first", "second"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=60,
            )

            with redirect_stderr(stderr):
                live.start()
                live.check_started(
                    check_index=0,
                    name="first",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                live.finish(passed=False, passed_count=0, reason="interrupted")
                event_count_at_finish = len(read_jsonl(event_path))

                # Late callbacks from checks the interrupt abandoned.
                live.check_finished(
                    check_index=0,
                    name="first",
                    kind="command",
                    role=verification.CHECK_ROLE_GATE,
                    status="pass",
                    duration_seconds=1.0,
                    failure_reason=None,
                    returncode=0,
                )
                live.check_started(
                    check_index=1,
                    name="second",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                self.assertIsNone(live._record_heartbeat())

            self.assertEqual(event_count_at_finish, len(read_jsonl(event_path)))
            self.assertEqual(
                events.VERIFICATION_FINISHED_EVENT,
                read_jsonl(event_path)[-1]["name"],
            )
            self.assertIsNone(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "active_verification"
                ]
            )
            stderr_text = stderr.getvalue()
            self.assertNotIn("verification check 1/2 finished", stderr_text)
            self.assertNotIn("verification check 2/2 started", stderr_text)

    def test_live_verification_callback_during_heartbeat_shutdown_is_inert(
        self,
    ) -> None:
        """A callback landing while `finish` joins the heartbeat thread is inert.

        `finish` claims the terminal transition before the join, so a worker
        callback that arrives during that wait can neither append a check event
        nor resurrect `active_verification`. The heartbeat thread is parked
        inside its own progress write, which holds `finish` in its shutdown for
        as long as the test needs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            progress = run_reporting._ProgressReporter(enabled=True)
            stderr = io.StringIO()
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload={
                    "state_path": str(state_path),
                    "active_turn": None,
                    "active_verification": None,
                },
                progress=progress,
                persistence=run_persistence._RunPersistence(),
                run_id="run-8",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("first", "second"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=0.01,
            )

            parked = threading.Event()
            release = threading.Event()
            emit_heartbeat = progress.verification_heartbeat

            def parked_heartbeat(heartbeat: dict[str, Any]) -> None:
                parked.set()
                release.wait(timeout=30)
                emit_heartbeat(heartbeat)

            finisher = threading.Thread(
                target=lambda: live.finish(
                    passed=False, passed_count=0, reason="interrupted"
                )
            )
            with (
                redirect_stderr(stderr),
                mock.patch.object(progress, "verification_heartbeat", parked_heartbeat),
            ):
                live.start()
                live.check_started(
                    check_index=0,
                    name="first",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                wait_until(parked.is_set)
                finisher.start()
                try:
                    # `finish` sets the stop flag on its way into the heartbeat
                    # join, and the parked heartbeat holds the progress lock,
                    # so it provably has not returned when this callback lands.
                    wait_until(live._stop.is_set)
                    self.assertTrue(finisher.is_alive())
                    live.check_finished(
                        check_index=0,
                        name="first",
                        kind="command",
                        role=verification.CHECK_ROLE_GATE,
                        status="pass",
                        duration_seconds=1.0,
                        failure_reason=None,
                        returncode=0,
                    )
                finally:
                    release.set()
                    finisher.join(timeout=30)
            self.assertFalse(finisher.is_alive())

            self.assertEqual(
                [
                    events.VERIFICATION_STARTED_EVENT,
                    events.VERIFICATION_CHECK_STARTED_EVENT,
                    events.VERIFICATION_HEARTBEAT_EVENT,
                    events.VERIFICATION_FINISHED_EVENT,
                ],
                [record["name"] for record in read_jsonl(event_path)],
            )
            self.assertIsNone(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "active_verification"
                ]
            )
            stderr_lines = [
                line for line in stderr.getvalue().splitlines() if line.strip()
            ]
            self.assertNotIn("verification check 1/2 finished", stderr.getvalue())
            self.assertIn("verification failed for session 1/1", stderr_lines[-1])

    def test_live_verification_finish_waits_for_stalled_check_progress(
        self,
    ) -> None:
        """A callback stalled mid-write cannot print behind the terminal line.

        The callback passes its lifecycle check and stalls before its progress
        line reaches stderr, then `finish` runs to completion in that window.
        The terminal message has to queue behind the stalled write instead of
        racing ahead of it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            progress = run_reporting._ProgressReporter(enabled=True)
            stderr = io.StringIO()
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload={
                    "state_path": str(state_path),
                    "active_turn": None,
                    "active_verification": None,
                },
                progress=progress,
                persistence=run_persistence._RunPersistence(),
                run_id="run-9",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=2,
                check_names=("first", "second"),
                check_kinds=("command", "command"),
                parallel=True,
                heartbeat_interval=60,
            )

            parked = threading.Event()
            release = threading.Event()
            emit_check_finished = progress.verification_check_finished

            def parked_check_finished(check: dict[str, Any]) -> None:
                parked.set()
                release.wait(timeout=30)
                emit_check_finished(check)

            worker = threading.Thread(
                target=lambda: live.check_finished(
                    check_index=0,
                    name="first",
                    kind="command",
                    role=verification.CHECK_ROLE_GATE,
                    status="pass",
                    duration_seconds=1.0,
                    failure_reason=None,
                    returncode=0,
                )
            )
            finisher = threading.Thread(
                target=lambda: live.finish(
                    passed=False, passed_count=0, reason="interrupted"
                )
            )
            with (
                redirect_stderr(stderr),
                mock.patch.object(
                    progress, "verification_check_finished", parked_check_finished
                ),
            ):
                live.start()
                live.check_started(
                    check_index=0,
                    name="first",
                    kind="command",
                    argv=("python3",),
                    cwd=None,
                )
                worker.start()
                try:
                    wait_until(parked.is_set)
                    finisher.start()
                    # The terminal event is durable before the terminal
                    # message is written, so `finish` is now past its
                    # lifecycle block and can only be waiting for the stalled
                    # write to release the output.
                    wait_until(
                        lambda: (
                            read_jsonl(event_path)[-1]["name"]
                            == events.VERIFICATION_FINISHED_EVENT
                        )
                    )
                    self.assertTrue(finisher.is_alive())
                finally:
                    release.set()
                    worker.join(timeout=30)
                    finisher.join(timeout=30)
            self.assertFalse(worker.is_alive())
            self.assertFalse(finisher.is_alive())

            self.assertEqual(
                [
                    events.VERIFICATION_STARTED_EVENT,
                    events.VERIFICATION_CHECK_STARTED_EVENT,
                    events.VERIFICATION_CHECK_FINISHED_EVENT,
                    events.VERIFICATION_FINISHED_EVENT,
                ],
                [record["name"] for record in read_jsonl(event_path)],
            )
            stderr_lines = [
                line for line in stderr.getvalue().splitlines() if line.strip()
            ]
            self.assertIn("verification check 1/2 finished", stderr_lines[-2])
            self.assertIn("verification failed for session 1/1", stderr_lines[-1])

    def test_parallel_interrupt_leaves_verification_finished_terminal(
        self,
    ) -> None:
        """A main-thread interrupt mid-parallel-recipe ends the phase cleanly.

        `as_completed` is where a real Ctrl-C surfaces: the main thread waits
        there while worker threads run the command checks, so the
        KeyboardInterrupt is raised in `run_verification`'s own frame with
        several checks still live. Every process of every live check that is
        still in the check's process group — the command itself and the
        descendants it spawned — must be dead before the interrupt reaches the
        runner, so nothing abandoned can resurrect `active_verification` or
        append behind `verification_finished`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            script, grandchild_script = write_process_tree_scripts(tmp_dir)
            started_dir = tmp_dir / "started"
            total = 6
            running = 4  # `run_verification` caps the pool at four workers.
            checks = tuple(
                contracts.CommandCheck(
                    name=f"cmd-{index}",
                    argv=(
                        "python3",
                        str(script),
                        f"cmd-{index}",
                        str(tmp_dir),
                        str(grandchild_script),
                    ),
                    timeout_seconds=120.0,
                )
                for index in range(total)
            )
            contract = contracts.CompletionContract(
                session_id=plans.parse_session_id("1"),
                done_when=("done",),
                verify=contracts.VerifyRecipe(checks=checks, parallel=True),
            )

            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "active_turn": None,
                "active_verification": None,
            }
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload=payload,
                progress=run_reporting._ProgressReporter(enabled=False),
                persistence=run_persistence._RunPersistence(),
                run_id="run-7",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=total,
                check_names=tuple(check.name for check in checks),
                check_kinds=("command",) * total,
                parallel=True,
                heartbeat_interval=0.01,
            )

            finishes: list[int] = []

            def on_check_started(event: verification.CheckStartedEvent) -> None:
                live.check_started(
                    check_index=event.check_index,
                    name=event.name,
                    kind=event.kind,
                    argv=event.argv,
                    cwd=event.cwd,
                )

            def on_check_finished(event: verification.CheckFinishedEvent) -> None:
                live.check_finished(
                    check_index=event.check_index,
                    name=event.name,
                    kind=event.kind,
                    role=event.role,
                    status=event.result.status,
                    duration_seconds=event.result.duration_seconds,
                    failure_reason=event.result.failure_reason,
                    returncode=event.result.returncode,
                )
                finishes.append(event.check_index)

            def interrupt_once_all_running(_futures: object) -> None:
                wait_until(lambda: len(list(started_dir.iterdir())) == running)
                raise KeyboardInterrupt

            live.start()
            with (
                mock.patch.object(
                    verification, "as_completed", interrupt_once_all_running
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                verification.run_verification(
                    contract,
                    repo_root=repo_root,
                    observer=verification.VerificationObserver(
                        on_check_started=on_check_started,
                        on_check_finished=on_check_finished,
                    ),
                )

            # Every process the interrupt abandoned — command and grandchild
            # alike — is already dead, so nothing can report back or touch the
            # repository after control returned.
            started_markers = sorted(started_dir.iterdir())
            assert_marked_pids_exited(started_dir, label="command process")
            assert_marked_pids_exited(tmp_dir / "grandchildren", label="grandchild")
            self.assertEqual([], list((tmp_dir / "late").iterdir()))
            self.assertEqual([], finishes)

            live.finish(passed=False, passed_count=0, reason="interrupted")
            events_at_finish = read_jsonl(event_path)

            # The two futures still queued behind the four workers were
            # cancelled rather than executed.
            self.assertEqual(running, len(started_markers))
            self.assertEqual(
                events.VERIFICATION_FINISHED_EVENT, events_at_finish[-1]["name"]
            )
            self.assertEqual("interrupted", events_at_finish[-1]["payload"]["reason"])
            self.assertEqual(events_at_finish, read_jsonl(event_path))
            self.assertIsNone(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "active_verification"
                ]
            )

    def test_live_verification_started_event_omits_parallel_when_sequential(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            live = live_verification._LiveVerification(
                event_path=event_path,
                payload={
                    "state_path": str(state_path),
                    "active_turn": None,
                    "active_verification": None,
                },
                progress=run_reporting._ProgressReporter(enabled=False),
                persistence=run_persistence._RunPersistence(),
                run_id="run-4",
                turn_index=1,
                session_id="1",
                session_label="1/1",
                total_sessions=1,
                check_count=1,
                check_names=("only",),
                check_kinds=("command",),
                heartbeat_interval=60,
            )

            live.start()
            live.finish(passed=True, passed_count=1, reason="ok")

            started_event = next(
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.VERIFICATION_STARTED_EVENT
            )
            self.assertNotIn("parallel", started_event)
