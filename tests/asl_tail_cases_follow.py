from .asl_tail_support import *


class TailFollowerFollowTests(TailFollowerTestCase):
    def cleanup_follower(self, follower: tail.TailFollower) -> None:
        follower.stop()
        follower.join(1.0)

    def test_tail_follower_starts_at_eof_for_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stderr_log = tmp_dir / "stderr.log"
            stdout_log.write_text("old stdout\n", encoding="utf-8")
            stderr_log.write_text("old stderr\n", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
                "stderr_path": str(stderr_log),
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
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_payload,
                    created_at="2026-06-11T12:00:00+00:00",
                ),
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
            self.append_log_line(stdout_log, "new stdout\n")
            events.append_event(
                events_path,
                events.Event(
                    name="turn_finished",
                    payload={
                        **turn_payload,
                        "status_result": "complete",
                        "returncode": 0,
                        "duration_seconds": 1.2,
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "session 1/1 finished" in out.getvalue()
                    and "new stdout" in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("old stdout", text)
            self.assertNotIn("old stderr", text)
            self.assertNotIn("session 1/1 started", text)
            self.assertIn("stdout session=1 turn=1 | new stdout", text)
            self.assertIn("session 1/1 finished", text)

    def test_tail_follower_discovers_log_targets_from_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": None, "turns": []},
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
            stdout_log.write_text("discovered stdout\n", encoding="utf-8")
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload={
                        "run_id": "run-1",
                        "session": 1,
                        "session_id": "1",
                        "total_sessions": 1,
                        "turn_index": 1,
                        "stdout_path": str(stdout_log),
                    },
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "session 1/1 started" in out.getvalue()
                    and "stdout session=1 turn=1 | discovered stdout" in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))

    def test_tail_follower_stop_cancels_following_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": None, "turns": []},
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
            self.assertFalse(follower.join(0.02))
            follower.stop()
            self.assertTrue(follower.join(1.0))
            self.assertTrue(follower.done)
            before = out.getvalue()
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload={"run_id": "run-1", "session": 1, "turn_index": 1},
                ),
            )
            time.sleep(0.05)
            self.assertEqual(before, out.getvalue())

    def test_tail_follower_stops_after_run_finished_and_drains_logs(self) -> None:
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
                {"run_id": "run-1", "active_turn": turn_payload, "turns": []},
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
            self.append_log_line(stdout_log, "final stdout\n")
            events.append_event(
                events_path,
                events.Event(
                    name="run_finished",
                    payload={
                        "run_id": "run-1",
                        "ok": True,
                        "reason": "all-complete",
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("run finished: run run-1", text)
            self.assertIn("stdout session=1 turn=1 | final stdout", text)

    def test_tail_follower_keeps_following_a_truncated_events_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                Path(tmp)
            )
            self.write_state_payload(
                state_path, {"run_id": "run-1", "run_finished_at": None}
            )

            def started(session_id: str) -> events.Event:
                return events.Event(
                    name="turn_started",
                    payload={
                        "run_id": "run-1",
                        "session_id": session_id,
                        "total_sessions": 9,
                        "turn_index": 1,
                    },
                    created_at="2026-06-11T12:00:00+00:00",
                )

            for session_id in ("1", "2", "3"):
                events.append_event(events_path, started(session_id))
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir, state_dir=state_dir, out=out, poll_interval=0.01
            )
            self.addCleanup(self.cleanup_follower, follower)
            follower.start()

            # Rewritten shorter: the next record is still printed, not lost to
            # an offset counted against the old file.
            events_path.write_text("", encoding="utf-8")
            events.append_event(events_path, started("4"))

            wait_until(lambda: "session 4/9 started" in out.getvalue())
            self.assertNotIn("session 3/9 started", out.getvalue())

    def test_tail_follower_stops_on_synthetic_abandoned_run_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
            }
            # The state file still looks in-flight: only the synthetic event
            # can terminate the follower here, which is the whole reason the
            # abandoned record reuses the `run_finished` name.
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [],
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
                    name=events.RUN_FINISHED_EVENT,
                    payload={
                        "run_id": "run-1",
                        "ok": False,
                        "paused": False,
                        "reason": "abandoned",
                        "duration_seconds": None,
                        "synthetic": True,
                        "outcome": events.RUN_OUTCOME_ABANDONED,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            self.assertIn("run stopped: run run-1, reason abandoned", out.getvalue())

    def test_tail_follower_suppresses_event_lines_when_disabled(self) -> None:
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
            self.append_log_line(stdout_log, "streamed stdout\n")

            wait_until(
                lambda: "stdout session=1 turn=1 | streamed stdout" in out.getvalue()
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("heartbeat", text)
            self.assertNotIn("started", text)

    def test_tail_follower_drains_run_finished_when_events_suppressed(self) -> None:
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
                {"run_id": "run-1", "active_turn": turn_payload, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
                include_events=False,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.append_log_line(stdout_log, "tail line\n")
            events.append_event(
                events_path,
                events.Event(
                    name="run_finished",
                    payload={
                        "run_id": "run-1",
                        "ok": True,
                        "reason": "all-complete",
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("stdout session=1 turn=1 | tail line", text)
            self.assertNotIn("run finished", text)
