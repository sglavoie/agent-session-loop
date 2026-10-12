"""ParserAndStateTests (part 11).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    pause_policy,
    plans,
    state,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_status_projects_waiting_for_reply_without_hiding_pause_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "turn_index": 2,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "hard_deadline": "2026-07-24T12:15:00+00:00",
                "last_lease_activity_at": "2026-07-24T12:01:00+00:00",
            }
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "paused": True,
                        "reason": "blocked",
                        # The runner persists this block alongside every pause;
                        # it is what keeps the pause legible after the
                        # abandonment backfill rewrites the top-level reason.
                        "pause_policy": pause_policy.classify_pause("blocked"),
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": waiting,
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(waiting, payload["runner_state"]["waiting_for_reply"])
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                payload["waiting_for_reply_status"],
            )
            self.assertTrue(payload["runner_state"]["paused"])
            self.assertEqual(
                "blocked", payload["runner_state"]["pause_policy"]["reason"]
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)
            output = buffer.getvalue()
            self.assertIn("Mailbox: disabled", output)
            self.assertIn(
                "Waiting for reply: question question-1 session 1 turn 2",
                output,
            )
            self.assertIn(
                "Deadlines: base 2026-07-24T12:05:00+00:00, "
                "hard 2026-07-24T12:15:00+00:00",
                output,
            )

    def test_status_projects_live_waiting_as_nonterminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "turn_index": 2,
                "started_at": "2999-07-24T12:00:00+00:00",
                "base_deadline": "2999-07-24T12:05:00+00:00",
                "hard_deadline": "2999-07-24T12:15:00+00:00",
                "last_lease_activity_at": None,
            }
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "ok": True,
                        "paused": False,
                        "reason": "",
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": waiting,
                    }
                ),
                encoding="utf-8",
            )
            lock_info = state.acquire_lock(plan_dir, state_dir)

            try:
                payload = state.status_payload(
                    plans.inspect_plan(plan_dir),
                    state_dir,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(waiting, payload["runner_state"]["waiting_for_reply"])
            self.assertTrue(lock_info.active)
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_LIVE,
                payload["waiting_for_reply_status"],
            )
            self.assertTrue(payload["runner_state"]["ok"])
            self.assertFalse(payload["runner_state"]["paused"])
            self.assertIsNone(payload["runner_state"]["pause_policy"])

    def test_runner_state_payload_requires_explicit_lock_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "waiting_for_reply": {
                            "hard_deadline": "2999-07-24T12:15:00+00:00",
                        }
                    }
                ),
                encoding="utf-8",
            )

            # Bound dynamically because the assertion is about runtime argument
            # binding, which a static signature check rejects before it runs.
            payload_builder: Any = state._runner_state_payload
            with self.assertRaisesRegex(TypeError, "lock_info"):
                payload_builder(state_path)

            payload = state._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                payload["waiting_for_reply_status"],
            )

    def test_runner_state_payload_exposes_review_harness(self) -> None:
        # `asl control stop-after-current` reports whether the stop also
        # prevents a pending review, which it can only know from the run's
        # configured review harness.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "harness": "codex",
                        "run_config": {"review_harness": "claude"},
                    }
                ),
                encoding="utf-8",
            )

            payload = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )

            self.assertEqual("codex", payload["harness"])
            self.assertEqual("claude", payload["review_harness"])

            state_path.write_text(
                json.dumps({"run_id": "run-1", "harness": "codex"}),
                encoding="utf-8",
            )
            without_review = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )
            self.assertIsNone(without_review["review_harness"])

    def test_runner_state_payload_exposes_finalization_and_control(self) -> None:
        # A supervisor reading a run that holds the lock with no turn in flight
        # needs both: the phase says the run is working, and `control` is the
        # only record of the request that ended it.
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / events.STATE_FILE_NAME
            finalization = {
                "run_id": "run-1",
                "phase": "assurance-audit",
                "elapsed_seconds": 42.0,
                "cancellable": True,
            }
            state_path.write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "harness": "codex",
                        "active_finalization": finalization,
                        "control": {"last_action": "finalization-abort-honored"},
                    }
                ),
                encoding="utf-8",
            )

            payload = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )

            self.assertEqual(finalization, payload["active_finalization"])
            self.assertEqual(
                "finalization-abort-honored", payload["control"]["last_action"]
            )

            state_path.write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
            without = state._runner_state_payload(
                state_path, lock_info=self._active_lock()
            )
            self.assertIsNone(without["active_finalization"])
            self.assertIsNone(without["control"])

    def test_historical_hard_deadline_waits_classify_unchanged(self) -> None:
        # Records persisted before the ceiling began rolling carry only the
        # bare timestamp; they must classify exactly as they always did.
        lock_info = self._active_lock()
        started = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        historical = {
            "question_id": "question-1",
            "started_at": started.isoformat(),
            "base_deadline": (started + timedelta(minutes=5)).isoformat(),
            "hard_deadline": (started + timedelta(minutes=30)).isoformat(),
            "last_lease_activity_at": (started + timedelta(minutes=29)).isoformat(),
        }

        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_LIVE,
            state.waiting_for_reply_status(
                historical,
                lock_info=lock_info,
                now=started + timedelta(minutes=29),
            ),
        )
        # Recent lease activity cannot roll a historical record forward: it
        # never recorded the ceiling span that would justify the extension.
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                historical,
                lock_info=lock_info,
                now=started + timedelta(minutes=30),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                historical,
                lock_info=None,
                now=started + timedelta(minutes=10),
            ),
        )
        for broken in ({}, {"hard_deadline": ""}, {"hard_deadline": "nope"}):
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_INVALID,
                state.waiting_for_reply_status(
                    broken,
                    lock_info=lock_info,
                    now=started,
                ),
                msg=f"{broken!r} should be invalid",
            )

    def test_rolling_ceiling_keeps_supervised_wait_live(self) -> None:
        lock_info = self._active_lock()
        started = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        supervised = {
            "question_id": "question-1",
            "started_at": started.isoformat(),
            "base_deadline": (started + timedelta(minutes=5)).isoformat(),
            "unsupervised_deadline_at": (started + timedelta(minutes=30)).isoformat(),
            "unsupervised_ceiling_seconds": 1800.0,
            "last_lease_activity_at": (started + timedelta(minutes=90)).isoformat(),
        }

        # Two hours in, the persisted anchor is 90 minutes stale, but the last
        # lease proved a supervisor alive 30 minutes ago.
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_LIVE,
            state.waiting_for_reply_status(
                supervised,
                lock_info=lock_info,
                now=started + timedelta(minutes=119),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                supervised,
                lock_info=lock_info,
                now=started + timedelta(minutes=120),
            ),
        )
        # An absent lock still expires the wait immediately: the projected
        # ceiling only says nobody proved supervision, not that a runner
        # remains to accept the reply.
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                supervised,
                lock_info=None,
                now=started + timedelta(minutes=119),
            ),
        )

    def test_unwatched_wait_expires_at_its_persisted_ceiling(self) -> None:
        lock_info = self._active_lock()
        started = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
        unwatched = {
            "question_id": "question-1",
            "started_at": started.isoformat(),
            "base_deadline": (started + timedelta(minutes=5)).isoformat(),
            "unsupervised_deadline_at": (started + timedelta(minutes=30)).isoformat(),
            "unsupervised_ceiling_seconds": 1800.0,
            "last_lease_activity_at": None,
        }

        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_LIVE,
            state.waiting_for_reply_status(
                unwatched,
                lock_info=lock_info,
                now=started + timedelta(minutes=29),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_EXPIRED,
            state.waiting_for_reply_status(
                unwatched,
                lock_info=lock_info,
                now=started + timedelta(minutes=30),
            ),
        )
        self.assertEqual(
            state.WAITING_FOR_REPLY_STATUS_INVALID,
            state.waiting_for_reply_status(
                {**unwatched, "unsupervised_deadline_at": "nope"},
                lock_info=lock_info,
                now=started,
            ),
        )
        # A malformed span degrades to the persisted anchor rather than
        # discarding a wait that is otherwise well formed.
        for bad_ceiling in (0, -1.0, float("inf"), "1800", True, None):
            self.assertEqual(
                (started + timedelta(minutes=30)),
                state.waiting_for_reply_ceiling(
                    {
                        **unwatched,
                        "unsupervised_ceiling_seconds": bad_ceiling,
                        "last_lease_activity_at": (
                            started + timedelta(minutes=90)
                        ).isoformat(),
                    }
                ),
                msg=f"ceiling {bad_ceiling!r} should not roll the deadline",
            )

    def test_status_text_labels_the_rolling_ceiling(self) -> None:
        # The historical `hard ...` rendering is pinned by
        # test_status_projects_waiting_for_reply_without_hiding_pause_state.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            (plan_state / events.STATE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "run_id": "run-1",
                        "ok": True,
                        "paused": False,
                        "reason": "",
                        "run_started_at": "2026-07-24T11:59:00+00:00",
                        "run_finished_at": None,
                        "waiting_for_reply": {
                            "question_id": "question-1",
                            "session_id": "1",
                            "turn_index": 2,
                            "started_at": "2026-07-24T12:00:00+00:00",
                            "base_deadline": "2026-07-24T12:05:00+00:00",
                            "unsupervised_deadline_at": ("2026-07-24T12:30:00+00:00"),
                            "unsupervised_ceiling_seconds": 1800.0,
                            "last_lease_activity_at": None,
                        },
                    }
                ),
                encoding="utf-8",
            )

            payload = state.status_payload(
                plans.inspect_plan(plan_dir),
                state_dir,
            )
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                state._print_text_status(payload)

            self.assertIn(
                "Deadlines: base 2026-07-24T12:05:00+00:00, "
                "unsupervised 2026-07-24T12:30:00+00:00",
                buffer.getvalue(),
            )

    def test_status_payload_progress_journal_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            journal = payload["progress_journal"]
            self.assertFalse(journal["present"])
            self.assertIsNone(journal["entry_count"])
            self.assertIsNone(journal["last_appended"])
            self.assertIsNone(journal["error"])
            self.assertTrue(journal["path"].endswith("/progress.md"))

    def test_status_payload_progress_journal_with_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete", "pending"))

            plans.append_progress_entry(
                plan_dir,
                plans.ProgressEntry(
                    session_id=plans.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                    files_modified=("cli.py",),
                ),
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            journal = payload["progress_journal"]
            self.assertTrue(journal["present"])
            self.assertEqual(1, journal["entry_count"])
            self.assertEqual("2026-06-19T14:22:00Z", journal["last_appended"])
            self.assertIsNone(journal["error"])

    def test_status_payload_progress_journal_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            plans.progress_path(plan_dir).write_text(
                "# Progress Journal\n\nmissing version comment\n",
                encoding="utf-8",
            )

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            journal = payload["progress_journal"]
            self.assertTrue(journal["present"])
            self.assertIsNone(journal["entry_count"])
            self.assertIsNone(journal["last_appended"])
            self.assertIsNotNone(journal["error"])
