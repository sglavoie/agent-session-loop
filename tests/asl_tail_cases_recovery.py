from unittest import mock

from .asl_tail_support import *


class TailFollowerRecoveryTests(TailFollowerTestCase):
    def test_existing_log_position_restarts_on_changed_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "stdout.log"
            log.write_text("old first\nold second\n", encoding="utf-8")
            target = {
                "path": log,
                "stream": "stdout",
                "session": "1",
                "turn_index": 1,
            }
            offsets: dict[str, int] = {}
            tail_logs._set_offsets_to_last_line({str(log): target}, offsets)

            replacement = Path(tmp) / "replacement.log"
            replacement.write_text(
                "new first\nnew second\nnew third\n", encoding="utf-8"
            )
            os.replace(replacement, log)

            self.assertEqual(
                [
                    "stdout session=1 turn=1 | new first",
                    "stdout session=1 turn=1 | new second",
                    "stdout session=1 turn=1 | new third",
                ],
                tail_logs._read_new_log_text(target, offsets),
            )

    def test_recent_replay_follows_a_line_appended_after_its_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout_log = Path(tmp) / "stdout.log"
            stdout_log.write_text("first\n", encoding="utf-8")
            record = {
                "name": "turn_started",
                "payload": {
                    "run_id": "r",
                    "session_id": "1",
                    "turn_index": 1,
                    "stdout_path": str(stdout_log),
                },
            }
            snapshot = tail_logs._read_log_snapshot

            def append_after_read(target, *, final=True):
                result = snapshot(target, final=final)
                self.append_log_line(stdout_log, "second\n")
                return result

            targets: dict[str, dict[str, object]] = {}
            offsets: dict[str, int] = {}
            out = io.StringIO()
            with mock.patch.object(
                tail_logs, "_read_log_snapshot", side_effect=append_after_read
            ):
                tail_logs._emit_recent(
                    records=[record],
                    state_payload=None,
                    run_id="r",
                    lines=40,
                    out=out,
                    targets=targets,
                    offsets=offsets,
                    final=False,
                )
            for target in targets.values():
                tail_logs._print_lines(
                    tail_logs._read_new_log_text(target, offsets), out
                )
            self.assertEqual(out.getvalue().count("| first\n"), 1)
            self.assertEqual(out.getvalue().count("| second\n"), 1)

    def test_initial_replay_follows_a_line_appended_after_its_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, _ = self.setup_tail_files(tmp_dir)
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("first\n", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session_id": "1",
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": turn_payload},
            )
            snapshot = tail_logs._read_log_snapshot

            def append_after_read(target, *, final=True):
                result = snapshot(target, final=final)
                self.append_log_line(stdout_log, "second\n")
                return result

            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                include_events=False,
                initial_log_lines=40,
                initial_log_scope="active",
            )
            with mock.patch.object(
                tail_logs, "_read_log_snapshot", side_effect=append_after_read
            ):
                follow_loop = follower._build_loop_from_eof()
            follow_loop._poll_once()
            self.assertEqual(out.getvalue().count("| first\n"), 1)
            self.assertEqual(out.getvalue().count("| second\n"), 1)

    def test_tail_follower_initial_active_replay_emits_recent_logs_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text(
                "first line\nsecond line\nthird line\n", encoding="utf-8"
            )
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 2,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=40,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(lambda: "stdout session=1 turn=2 | third line" in out.getvalue())
            self.append_log_line(stdout_log, "fourth line\n")
            wait_until(
                lambda: "stdout session=1 turn=2 | fourth line" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertEqual(text.count("stdout session=1 turn=2 | first line"), 1)
            self.assertEqual(text.count("stdout session=1 turn=2 | second line"), 1)
            self.assertEqual(text.count("stdout session=1 turn=2 | third line"), 1)
            self.assertEqual(text.count("stdout session=1 turn=2 | fourth line"), 1)

    def test_initial_replay_holds_back_a_line_still_being_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("first line\nhalf-wr", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=40,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(lambda: "| first line" in out.getvalue())
            self.append_log_line(stdout_log, "itten line\n")
            wait_until(lambda: "| half-written line" in out.getvalue())
            follower.stop()
            self.assertTrue(follower.join(1.0))
            self.assertEqual(
                out.getvalue().splitlines(),
                [
                    "stdout session=1 turn=1 | first line",
                    "stdout session=1 turn=1 | half-written line",
                ],
            )

    def test_tail_recent_replay_leaves_an_unfinished_line_to_the_follow(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stdout_log = Path(tmp) / "stdout.log"
            stdout_log.write_bytes(b"first\nhalf-wr")
            record = {
                "name": "turn_started",
                "payload": {
                    "run_id": "r",
                    "session_id": "1",
                    "turn_index": 1,
                    "stdout_path": str(stdout_log),
                },
            }
            targets: dict[str, dict[str, object]] = {}
            offsets: dict[str, int] = {}
            following = io.StringIO()
            tail_logs._emit_recent(
                records=[record],
                state_payload=None,
                run_id="r",
                lines=40,
                out=following,
                targets=targets,
                offsets=offsets,
                final=False,
            )
            with stdout_log.open("ab") as log_file:
                log_file.write(b"itten line\n")
            for target in targets.values():
                tail_logs._print_lines(
                    tail_logs._read_new_log_text(target, offsets), following
                )
            log_lines = [
                line for line in following.getvalue().splitlines() if "|" in line
            ]
            self.assertEqual(
                log_lines,
                [
                    "stdout session=1 turn=1 | first",
                    "stdout session=1 turn=1 | half-written line",
                ],
            )

            # A replay nothing follows prints the line as it stands.
            with stdout_log.open("ab") as log_file:
                log_file.write(b"last-wr")
            snapshot = io.StringIO()
            tail_logs._emit_recent(
                records=[record],
                state_payload=None,
                run_id="r",
                lines=40,
                out=snapshot,
                targets={},
                offsets={},
                final=True,
            )
            self.assertIn("| last-wr", snapshot.getvalue())

    def test_tail_follower_initial_active_replay_skips_completed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            old_stdout = tmp_dir / "old-stdout.log"
            active_stdout = tmp_dir / "active-stdout.log"
            old_stdout.write_text("old line\n", encoding="utf-8")
            active_stdout.write_text("active line\n", encoding="utf-8")
            completed_turn = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "turn_index": 1,
                "stdout_path": str(old_stdout),
            }
            active_turn = {
                "run_id": "run-1",
                "session": 2,
                "session_id": "2",
                "total_sessions": 2,
                "turn_index": 2,
                "stdout_path": str(active_stdout),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": active_turn,
                    "turns": [completed_turn, active_turn],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=40,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(
                lambda: "stdout session=2 turn=2 | active line" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("old line", text)
            self.assertIn("stdout session=2 turn=2 | active line", text)

    def test_tail_follower_renders_heartbeat_events_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            events.append_event(
                events_path,
                events.Event(
                    name=events.TURN_HEARTBEAT_EVENT,
                    payload={
                        **turn_payload,
                        "elapsed_seconds": 5.0,
                        "idle_seconds": 1.0,
                        "last_activity_at": "2026-06-11T12:00:00+00:00",
                        "last_activity_stream": "stdout",
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(lambda: "session 1/1 heartbeat" in out.getvalue())
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("session 1/1 heartbeat (elapsed", text)
            self.assertIn("stream stdout", text)

    def test_tail_follower_inline_mode_does_not_duplicate_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=run_reporting.INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            for index in range(3):
                events.append_event(
                    events_path,
                    events.Event(
                        name=events.TURN_HEARTBEAT_EVENT,
                        payload={
                            **turn_payload,
                            "elapsed_seconds": float(index + 1) * 5.0,
                            "idle_seconds": 0.5,
                            "last_activity_at": "2026-06-11T12:00:00+00:00",
                            "last_activity_stream": "stdout",
                        },
                        created_at=f"2026-06-11T12:00:0{index + 1}+00:00",
                    ),
                )
            self.append_log_line(stdout_log, "live stdout line\n")

            wait_until(
                lambda: "stdout session=1 turn=1 | live stdout line" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertEqual(0, text.count("heartbeat"))
            self.assertNotIn("event turn_heartbeat", text)
            self.assertIn("stdout session=1 turn=1 | live stdout line", text)

    def test_tail_follower_inline_mode_discovers_later_session_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log_one = tmp_dir / "stdout-1.log"
            stderr_log_one = tmp_dir / "stderr-1.log"
            stdout_log_one.write_text("session 1 startup\n", encoding="utf-8")
            stderr_log_one.write_text("", encoding="utf-8")
            turn_one = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "turn_index": 1,
                "stdout_path": str(stdout_log_one),
                "stderr_path": str(stderr_log_one),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_one,
                    "turns": [turn_one],
                    "run_finished_at": None,
                },
            )
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_one,
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=run_reporting.INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            wait_until(
                lambda: "stdout session=1 turn=1 | session 1 startup" in out.getvalue()
            )

            stdout_log_two = tmp_dir / "stdout-2.log"
            stderr_log_two = tmp_dir / "stderr-2.log"
            stdout_log_two.write_text("session 2 stdout body\n", encoding="utf-8")
            stderr_log_two.write_text("session 2 stderr body\n", encoding="utf-8")
            turn_two = {
                "run_id": "run-1",
                "session": 2,
                "session_id": "2",
                "total_sessions": 2,
                "turn_index": 2,
                "stdout_path": str(stdout_log_two),
                "stderr_path": str(stderr_log_two),
            }
            events.append_event(
                events_path,
                events.Event(
                    name="turn_finished",
                    payload={
                        **turn_one,
                        "status_result": "complete",
                        "returncode": 0,
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_two,
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "stdout session=2 turn=2 | session 2 stdout body" in out.getvalue()
                    and "stderr session=2 turn=2 | session 2 stderr body"
                    in out.getvalue()
                )
            )
            self.append_log_line(stdout_log_two, "session 2 stdout follow-up\n")
            wait_until(
                lambda: (
                    "stdout session=2 turn=2 | session 2 stdout follow-up"
                    in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("session 1/2 started", text)
            self.assertNotIn("session 1/2 finished", text)
            self.assertNotIn("session 2/2 started", text)
            self.assertIn("stdout session=2 turn=2 | session 2 stdout body", text)
            self.assertIn("stderr session=2 turn=2 | session 2 stderr body", text)
            self.assertIn("stdout session=2 turn=2 | session 2 stdout follow-up", text)

    def test_tail_follower_inline_mode_discovers_commit_log_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            turn_stdout = tmp_dir / "turn-stdout.log"
            turn_stdout.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(turn_stdout),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
                initial_log_lines=run_reporting.INLINE_TAIL_INITIAL_LOG_LINES,
                initial_log_scope="active",
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()

            commit_stdout = tmp_dir / "commit-stdout.log"
            commit_stderr = tmp_dir / "commit-stderr.log"
            commit_stdout.write_text("commit stdout body\n", encoding="utf-8")
            commit_stderr.write_text("commit stderr body\n", encoding="utf-8")
            commit_payload = {
                "run_id": "run-1",
                "commit_index": 1,
                "scope": "session",
                "session": 1,
                "session_id": "1",
                "stdout_path": str(commit_stdout),
                "stderr_path": str(commit_stderr),
            }
            events.append_event(
                events_path,
                events.Event(
                    name=events.COMMIT_STARTED_EVENT,
                    payload=commit_payload,
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "stdout commit=1 scope=session session=1 | commit stdout body"
                    in out.getvalue()
                    and "stderr commit=1 scope=session session=1 | commit stderr body"
                    in out.getvalue()
                )
            )
            self.append_log_line(commit_stdout, "commit stdout follow-up\n")
            wait_until(
                lambda: (
                    "stdout commit=1 scope=session session=1 | commit stdout follow-up"
                    in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("commit 1 started", text)
            self.assertIn(
                "stdout commit=1 scope=session session=1 | commit stdout body", text
            )
            self.assertIn(
                "stderr commit=1 scope=session session=1 | commit stderr body", text
            )
            self.assertIn(
                "stdout commit=1 scope=session session=1 | commit stdout follow-up",
                text,
            )
