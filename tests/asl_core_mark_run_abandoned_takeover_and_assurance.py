"""Takeover and assurance cases for MarkRunAbandonedTests."""

from __future__ import annotations

import contextlib
import io
import os
import socket
from datetime import UTC, datetime
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    plans_files,
    state,
    state_abandonment,
    state_assurance_pending,
    state_locks,
)
from tests.support import (
    read_jsonl,
)

from .asl_core_mark_run_abandoned_support import (
    MarkRunAbandonedTestsBase,
)


class MarkRunAbandonedTests(MarkRunAbandonedTestsBase):
    def test_acquire_lock_takeover_marks_abandoned_and_records_hostname(self) -> None:
        self.write_unfinished_state(run_id="run-dead")
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())

        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(os.getpid(), lock_info.pid)
        self.assertEqual(socket.gethostname(), lock_info.raw["hostname"])
        self.assertEqual(4, lock_info.raw["version"])
        self.assertIs(lock_info.raw["process_start_utc"], True)

        persisted = self.persisted_state()
        self.assertEqual(state_abandonment.ABANDONED_REASON, persisted["reason"])
        self.assertEqual(
            state_abandonment.ABANDONED_TRIGGER_LOCK_TAKEOVER,
            persisted["abandoned"]["trigger"],
        )
        records = read_jsonl(self.events_path)
        self.assertEqual(1, len(records))
        self.assertEqual("run-dead", records[0]["payload"]["run_id"])

    def test_acquire_lock_takeover_reclaims_legacy_hostname_less_lock(self) -> None:
        # Locks predating the hostname field are the only route back for the
        # zombies that already exist on disk.
        self.write_unfinished_state(run_id="run-legacy")
        self.write_lock(pid=self.dead_pid(), hostname=None)

        state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(
            state_abandonment.ABANDONED_REASON, self.persisted_state()["reason"]
        )
        records = read_jsonl(self.events_path)
        self.assertEqual(["run-legacy"], [r["payload"]["run_id"] for r in records])

    def test_acquire_lock_foreign_hostname_never_marks_abandoned(self) -> None:
        self.write_unfinished_state(run_id="run-elsewhere")
        self.write_lock(pid=self.dead_pid(), hostname="some-other-host")
        # Dead in *this* host's process table, which is not the table that
        # owns the pid.
        self.assertNotIn("is not running", self.read_lock_info().reason)
        lock_bytes = self.lock_path.read_bytes()

        with self.assertRaises(state.StateError) as raised:
            state_locks.acquire_lock(self.plan_dir, self.state_dir)

        message = str(raised.exception)
        # The prefix is what `run_outcome._state_error_reason` reads as
        # lock-contention rather than an unrecoverable state error.
        self.assertIn("plan is locked", message)
        self.assertIn("some-other-host", message)
        self.assertEqual(lock_bytes, self.lock_path.read_bytes())
        self.assertUntouched()

    def test_acquire_lock_reclaims_pidless_lock_without_marking_abandoned(self) -> None:
        # An unparseable lock is garbage worth clearing, but it names no pid,
        # so it says nothing about whether the run that wrote it still lives.
        self.write_unfinished_state()
        self.lock_path.write_text("not json", encoding="utf-8")

        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(os.getpid(), lock_info.pid)
        self.assertUntouched()

    def test_acquire_lock_without_takeover_leaves_unfinished_state_alone(self) -> None:
        # A fresh acquire and a post-takeover acquire share the same return
        # path, so only a stolen lock with a proven-dead owner can terminalize.
        self.write_unfinished_state(
            run_id="run-booting",
            run_started_at=datetime.now(UTC).isoformat(),
        )

        state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertUntouched()

    def test_abandonment_records_the_audit_the_dead_run_still_owed(self) -> None:
        self.write_unfinished_state(
            run_id="run-owing",
            assurance={"run_id": "run-owing", "state": "pending", "eligible": True},
            run_config={"harness": "claude", "audit_harness": "codex"},
        )

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )

        record = self.pending_record("run-owing")
        self.assertEqual(
            state_assurance_pending.ASSURANCE_PENDING_SCHEMA_VERSION,
            record["schema_version"],
        )
        self.assertEqual("run-owing", record["run_id"])
        self.assertIsNone(record["skip"])
        self.assertEqual(events.RUN_OUTCOME_ABANDONED, record["outcome"])
        # The record carries its own copy of what the audit needs, because the
        # next run of this plan overwrites the `state.json` it came from.
        self.assertEqual("codex", record["run_config"]["audit_harness"])
        self.assertEqual(
            str(plans_files.canonicalize_path(self.plan_dir)), record["plan_dir"]
        )
        self.assertEqual(
            str(plans_files.canonicalize_path(self.plan_dir)), record["workspace_root"]
        )

        persisted = self.persisted_state()
        self.assertEqual("run-owing", persisted["assurance_pending"]["run_id"])
        self.assertEqual(
            persisted["run_finished_at"],
            persisted["assurance_pending"]["recorded_at"],
        )
        records = read_jsonl(self.events_path)
        self.assertEqual(
            persisted["assurance_pending"],
            records[0]["payload"]["assurance_pending"],
        )

    def test_run_that_predates_auditing_is_recorded_as_such(self) -> None:
        self.write_unfinished_state(run_id="run-old")

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )

        self.assertEqual(
            state_assurance_pending.ASSURANCE_SKIP_PREDATES,
            self.pending_record("run-old")["skip"],
        )

    def test_settled_assurance_is_recorded_as_owing_nothing(self) -> None:
        for label, assurance in (
            ("verdict-reached", {"state": "all-clear", "eligible": True}),
            ("opted-out", {"state": "pending", "eligible": False}),
        ):
            with self.subTest(label):
                self.write_unfinished_state(run_id=label, assurance=assurance)

                self.assertTrue(
                    state_abandonment.mark_run_abandoned(
                        self.plan_state, lock_info=None, trigger="test"
                    )
                )

                self.assertEqual(
                    state_assurance_pending.ASSURANCE_SKIP_SETTLED,
                    self.pending_record(label)["skip"],
                )

    def test_abandonment_records_the_debt_without_launching_a_harness(self) -> None:
        # `asl status` and lock acquisition both reach this code, and neither
        # may block on a harness turn.
        self.write_unfinished_state(
            run_id="run-quiet",
            assurance={"state": "pending", "eligible": True},
            run_config={"harness": "claude", "audit_harness": "codex"},
        )

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=AssertionError("abandonment launched a harness"),
        ):
            self.assertTrue(
                state_abandonment.mark_run_abandoned(
                    self.plan_state, lock_info=None, trigger="test"
                )
            )

        self.assertIsNone(self.pending_record("run-quiet")["skip"])

    def test_unwritable_pending_record_never_blocks_terminalization(self) -> None:
        self.write_unfinished_state(run_id="run-blocked")
        runs_dir = self.plan_state / state.RUNS_DIR_NAME
        runs_dir.mkdir(parents=True)
        (runs_dir / "run-blocked").write_text("not a directory", encoding="utf-8")

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )

        persisted = self.persisted_state()
        self.assertEqual(state_abandonment.ABANDONED_REASON, persisted["reason"])
        self.assertNotIn("assurance_pending", persisted)
        self.assertEqual(1, len(read_jsonl(self.events_path)))

    def test_an_unwritable_terminal_state_declines_and_says_so(self) -> None:
        self.write_unfinished_state(run_id="run-unwritable")
        stderr = io.StringIO()
        with (
            mock.patch.object(
                state_abandonment.artifacts,
                "atomic_write_bytes",
                side_effect=PermissionError(13, "Permission denied"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertFalse(
                state_abandonment.mark_run_abandoned(
                    self.plan_state, lock_info=None, trigger="test"
                )
            )

        message = stderr.getvalue()
        self.assertIn("could not record the abandoned run", message)
        self.assertIn("PermissionError", message)
        self.assertNotIn("Traceback", message)
        self.assertIsNone(self.persisted_state()["run_finished_at"])

    def test_the_atomic_writers_own_refusal_is_an_expected_failure(self) -> None:
        # What an unwritable state directory actually raises: the writer wraps
        # the OSError in its own error type, which is not a defect.
        self.write_unfinished_state(run_id="run-artifact-error")
        stderr = io.StringIO()
        with (
            mock.patch.object(
                state_abandonment.artifacts,
                "atomic_write_bytes",
                side_effect=state_abandonment.artifacts.ArtifactError(
                    "cannot write state.json: Permission denied"
                ),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertFalse(
                state_abandonment.mark_run_abandoned(
                    self.plan_state, lock_info=None, trigger="test"
                )
            )

        message = stderr.getvalue()
        self.assertIn("could not record the abandoned run", message)
        self.assertNotIn("unexpected error", message)
        self.assertNotIn("Traceback", message)

    def test_a_defect_still_fails_open_but_is_reported_with_its_traceback(
        self,
    ) -> None:
        self.write_unfinished_state(run_id="run-defect")
        stderr = io.StringIO()
        with (
            mock.patch.object(
                state_abandonment,
                "_write_assurance_pending_record",
                side_effect=KeyError("a bug"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertFalse(
                state_abandonment.mark_run_abandoned(
                    self.plan_state, lock_info=None, trigger="test"
                )
            )

        message = stderr.getvalue()
        self.assertIn("unexpected error: KeyError", message)
        self.assertIn("Traceback", message)
