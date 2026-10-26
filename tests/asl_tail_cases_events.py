from .asl_tail_support import *


class TailFollowerEventsTests(TailFollowerTestCase):
    def test_agent_mux_delivery_failures_have_a_correlation_summary(self) -> None:
        message = tail._event_message(
            events.AGENT_MUX_DELIVERY_EVENT,
            {
                "agent_mux_event": "AgentSessionLoopTurnStarted",
                "source_event_id": "run-1:2",
                "attempt_count": 3,
                "final_status": "retryable_exhausted",
                "trace_id": "trace-3",
                "error_classification": "retryable_outcome",
            },
        )
        self.assertIn("AgentMux delivery retryable_exhausted", message)
        self.assertIn("source run-1:2", message)
        self.assertIn("attempts 3", message)
        self.assertIn("trace trace-3", message)

    def test_commit_group_preview_bounds_and_coerces_durable_values(self) -> None:
        remaining = list(range(25))
        successful = [
            {"alias": index, "commit_sha": index + 100} for index in range(25)
        ]
        payload = {
            "phase": "updated",
            "group": {
                "scope": "end",
                "remaining_aliases": remaining,
                "successful_repositories": successful,
            },
        }

        message = tail._event_message(
            events.COMMIT_GROUP_UPDATED_EVENT,
            payload,
        )

        self.assertIn(
            "remaining " + ",".join(str(index) for index in range(20)) + " (+5 more)",
            message,
        )
        self.assertIn(
            "partial SHAs "
            + ",".join(f"{index}={index + 100}" for index in range(20))
            + " (+5 more)",
            message,
        )
        self.assertNotIn("20=120", message)
        self.assertEqual(25, len(payload["group"]["remaining_aliases"]))
        self.assertEqual(25, len(payload["group"]["successful_repositories"]))

    def test_model_control_events_include_target_changes_and_overrides(
        self,
    ) -> None:
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": (events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED),
                "message": "implementation model controls updated",
                "stop_after_current": False,
                "target_harness": "claude",
                "changed_fields": ["model", "effort"],
                "implementation_overrides": {
                    "claude": {
                        "model": "claude-sonnet-4-5",
                        "effort": "high",
                    }
                },
                "session": "4",
                "total_sessions": 13,
                "turn_index": 1,
            },
        )

        self.assertIn("target harness claude", message)
        self.assertIn("changed model, effort", message)
        self.assertIn(
            'effective overrides {"claude": {"effort": "high", '
            '"model": "claude-sonnet-4-5"}}',
            message,
        )
        self.assertIn("session 4/13", message)
        self.assertIn("turn 1", message)

    def test_run_control_status_line_omits_abort_when_none_pending(
        self,
    ) -> None:
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
                "message": "stop after current session requested",
                "stop_after_current": True,
                "abort_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
                "abort_deadline": None,
            },
        )

        self.assertEqual(
            "run control: stop after current session requested (stop-after-current on)",
            message,
        )

    def test_run_control_status_line_counts_down_pending_abort_grace(
        self,
    ) -> None:
        deadline = datetime.now(UTC) + timedelta(seconds=90)
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                "message": "abort of the in-flight turn requested",
                "stop_after_current": False,
                "abort_requested": True,
                "abort_grace_seconds": 120,
                "abort_reason": "supervisor stop",
                "abort_deadline": deadline.isoformat(),
            },
        )

        self.assertIn("abort pending", message)
        self.assertIn("abort grace 120s", message)
        self.assertRegex(message, r"\b(89|90)s remaining\b")
        self.assertIn("abort reason supervisor stop", message)

    def test_run_control_status_line_marks_expired_abort_grace(self) -> None:
        # The deadline is the runner's clock, not the reader's: a tail
        # attached after the window closed must not print a negative count.
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                "message": "abort of the in-flight turn requested",
                "stop_after_current": False,
                "abort_requested": True,
                "abort_grace_seconds": 5,
                "abort_deadline": "2020-01-01T00:00:00+00:00",
            },
        )

        self.assertIn("abort pending", message)
        self.assertIn("grace expired", message)
        self.assertNotIn("remaining", message)

    def test_a_naive_timestamp_is_read_as_utc_not_local_time(self) -> None:
        # Every other reader takes a zone-less persisted timestamp as UTC; the
        # tail must not shift the same record by the reader's local offset.
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()

        def restore() -> None:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

        self.addCleanup(restore)

        # Local wall-clock time, with the offset that makes it unambiguous.
        self.assertEqual(
            "2024-01-01 22:04:05-05:00",
            tail._format_local_datetime("2024-01-02T03:04:05"),
        )

    def test_run_control_status_line_reports_unarmed_abort(self) -> None:
        # A main-thread checkpoint observes the abort with no turn to kill, so
        # nothing stamps a deadline before the action is recorded.
        message = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                "message": "abort of the in-flight turn requested",
                "stop_after_current": False,
                "abort_requested": True,
                "abort_grace_seconds": 120,
                "abort_deadline": None,
            },
        )

        self.assertIn("abort pending", message)
        self.assertIn("grace not started", message)

    def test_run_control_status_line_clears_abort_on_terminal_actions(
        self,
    ) -> None:
        # Both terminal actions still carry `abort_requested` from the action
        # they consumed, so the renderer keys off the action name instead.
        for action in (
            events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
        ):
            with self.subTest(action=action):
                message = tail._event_message(
                    events.RUN_CONTROL_EVENT,
                    {
                        "action": action,
                        "message": action,
                        "stop_after_current": True,
                        "abort_requested": True,
                        "abort_grace_seconds": 120,
                        "abort_reason": "supervisor stop",
                        "abort_deadline": None,
                    },
                )

                self.assertIn("abort resolved", message)
                self.assertIn("abort grace 120s", message)
                self.assertNotIn("abort pending", message)
                self.assertNotIn("remaining", message)
                self.assertNotIn("grace not started", message)

    def test_tail_skips_a_non_utf8_event_line_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _state_path, events_path = self.setup_tail_files(
                Path(tmp)
            )
            events.append_event(
                events_path,
                events.Event(
                    name="run_started",
                    payload={"run_id": "run-1", "plan_dir": str(plan_dir)},
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )
            with events_path.open("ab") as handle:
                handle.write(b"\xff\n")
            events.append_event(
                events_path,
                events.Event(
                    name="run_finished",
                    payload={"run_id": "run-1", "ok": True, "reason": "all-complete"},
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            out = io.StringIO()
            tail.tail_plan(plan_dir, state_dir=state_dir, follow=False, out=out)

            self.assertIn("run finished: run run-1", out.getvalue())

    def test_recent_activity_ends_on_the_run_outcome_not_the_last_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text(
                "".join(f"output {index}\n" for index in range(50)), encoding="utf-8"
            )
            turn = {
                "run_id": "run-1",
                "session_id": "1",
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            for name, payload, second in (
                ("run_started", {"run_id": "run-1"}, "00"),
                ("turn_started", turn, "01"),
                ("turn_finished", {**turn, "status": "complete"}, "02"),
                ("run_finished", {"run_id": "run-1", "ok": True}, "03"),
            ):
                events.append_event(
                    events_path,
                    events.Event(
                        name=name,
                        payload={**payload, "reason": "all-complete"},
                        created_at=f"2026-06-11T12:00:{second}+00:00",
                    ),
                )

            out = io.StringIO()
            tail.tail_plan(
                plan_dir, state_dir=state_dir, lines=5, follow=False, out=out
            )

            lines = out.getvalue().splitlines()
            self.assertEqual(5, len(lines))
            # The turn's output sits before the event that finished the turn,
            # so the window still ends on the run's outcome.
            self.assertTrue(lines[-3].endswith("| output 49"), lines)
            self.assertIn("run finished: run run-1", lines[-1])
