"""Locking cases for MarkRunAbandonedTests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from datetime import UTC, datetime
from unittest import mock

from agent_session_loop import (
    events,
    plans_files,
    state_abandonment,
    state_locks,
)
from tests.support import (
    read_jsonl,
)

from .asl_core_mark_run_abandoned_support import (
    _HOLD_ABANDON_CLAIM_SOURCE,
    MarkRunAbandonedTestsBase,
)


class MarkRunAbandonedTests(MarkRunAbandonedTestsBase):
    def test_dead_pid_lock_marks_abandoned_exactly_once(self) -> None:
        pid = self.dead_pid()
        self.write_unfinished_state()
        self.write_lock(pid=pid, hostname=socket.gethostname())
        lock_info = self.read_lock_info()
        self.assertTrue(lock_info.stale)

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )

        persisted = self.persisted_state()
        self.assertFalse(persisted["ok"])
        self.assertEqual(state_abandonment.ABANDONED_REASON, persisted["reason"])
        self.assertIsNotNone(persisted["run_finished_at"])
        self.assertIsNone(persisted["active_turn"])
        self.assertIsNone(persisted["active_verification"])
        self.assertEqual("test", persisted["abandoned"]["trigger"])
        self.assertEqual(os.getpid(), persisted["abandoned"]["detected_by_pid"])
        self.assertEqual(
            f"pid {pid} is not running", persisted["abandoned"]["lock_reason"]
        )

        records = read_jsonl(self.events_path)
        self.assertEqual([events.RUN_FINISHED_EVENT], [r["name"] for r in records])
        self.assertEqual(events.EVENT_SCHEMA_VERSION, records[0]["version"])
        self.assertTrue(records[0]["payload"]["synthetic"])
        self.assertEqual(events.RUN_OUTCOME_ABANDONED, records[0]["payload"]["outcome"])
        self.assertEqual(
            persisted["run_finished_at"], records[0]["payload"]["run_finished_at"]
        )

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertEqual(1, len(read_jsonl(self.events_path)))

    def test_reused_pid_lock_marks_abandoned(self) -> None:
        # The lock's pid is alive but belongs to a later process: the reader
        # already proved the owner gone, so abandonment must agree with it.
        self.write_unfinished_state()
        payload = {
            "version": 1,
            "owner": "agent-session-loop",
            "pid": os.getpid(),
            "plan_dir": str(plans_files.canonicalize_path(self.plan_dir)),
            "created_at": "2026-06-19T12:00:00+00:00",
            "hostname": socket.gethostname(),
            "process_start": "Fri Jun 19 11:59:59 2026",
        }
        self.lock_path.write_text(json.dumps(payload), encoding="utf-8")
        with mock.patch.object(
            state_locks.process_tree,
            "process_birth",
            return_value="Mon Oct 26 09:00:01 2026",
        ):
            lock_info = state_locks.inspect_lock_info(self.plan_dir, self.state_dir)
            assert lock_info is not None
            self.assertTrue(lock_info.stale)

            self.assertTrue(
                state_abandonment.mark_run_abandoned(
                    self.plan_state, lock_info=lock_info, trigger="test"
                )
            )

        self.assertIn("was reused", self.persisted_state()["abandoned"]["lock_reason"])

    def test_abandonment_records_the_reason_it_overwrote(self) -> None:
        # A runner killed while it finalized an operator's stop had already
        # recorded that stop; without keeping it, reclaiming the run erases the
        # only evidence that the ending was asked for.
        self.write_unfinished_state()
        persisted = self.persisted_state()
        persisted["reason"] = "stopped"
        persisted["active_finalization"] = {"phase": "assurance-audit"}
        self.state_path.write_text(json.dumps(persisted), encoding="utf-8")
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=self.read_lock_info(), trigger="test"
            )
        )

        reclaimed = self.persisted_state()
        self.assertEqual(state_abandonment.ABANDONED_REASON, reclaimed["reason"])
        self.assertEqual("stopped", reclaimed["abandoned"]["prior_reason"])
        self.assertIsNone(reclaimed["active_finalization"])

    def test_concurrent_abandonment_appends_exactly_one_terminal_event(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())
        lock_info = self.read_lock_info()

        # Releasing both callers together at the end of the proof check puts
        # each one step from the recheck, which is the interleaving that used
        # to let both of them publish a terminal record.
        barrier = threading.Barrier(2)
        real_is_proven = state_abandonment._abandonment_is_proven

        def barriered_proof(*args: object, **kwargs: object) -> bool:
            proven = real_is_proven(*args, **kwargs)
            barrier.wait(timeout=30)
            return proven

        results: list[bool] = []
        results_lock = threading.Lock()

        def probe() -> None:
            marked = state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
            with results_lock:
                results.append(marked)

        with mock.patch.object(
            state_abandonment, "_abandonment_is_proven", barriered_proof
        ):
            threads = [threading.Thread(target=probe) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
                self.assertFalse(thread.is_alive())

        self.assertEqual([False, True], sorted(results))
        persisted = self.persisted_state()
        self.assertEqual(state_abandonment.ABANDONED_REASON, persisted["reason"])
        records = read_jsonl(self.events_path)
        self.assertEqual([events.RUN_FINISHED_EVENT], [r["name"] for r in records])
        # The loser wrote neither a second event nor a competing timestamp.
        self.assertEqual(
            persisted["run_finished_at"], records[0]["payload"]["run_finished_at"]
        )
        self.assertEqual(
            persisted["run_finished_at"], persisted["abandoned"]["detected_at"]
        )

    def test_abandonment_declines_while_another_process_holds_the_claim(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())
        lock_info = self.read_lock_info()
        claim_path = self.plan_state / state_abandonment.ABANDON_CLAIM_FILE_NAME

        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_ABANDON_CLAIM_SOURCE, str(claim_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        assert holder.stdin is not None and holder.stdout is not None
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.assertEqual("held\n", holder.stdout.readline())

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertUntouched()

        holder.stdin.close()
        self.assertEqual(0, holder.wait(timeout=30))

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertEqual(1, len(read_jsonl(self.events_path)))

    def test_active_lock_never_marks_abandoned(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())
        lock_info = self.read_lock_info()
        self.assertTrue(lock_info.active)

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertUntouched()

    def test_foreign_hostname_never_marks_abandoned(self) -> None:
        # The pid is dead in *this* host's process table, which says nothing
        # about the host that actually recorded it.
        self.write_unfinished_state()
        self.write_lock(pid=self.dead_pid(), hostname="some-other-host")

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=self.read_lock_info(), trigger="test"
            )
        )
        self.assertUntouched()

    def test_ambiguous_stale_lock_without_pid_never_marks_abandoned(self) -> None:
        self.write_unfinished_state()
        self.lock_path.write_text("not json", encoding="utf-8")
        lock_info = self.read_lock_info()
        self.assertTrue(lock_info.stale)
        self.assertIsNone(lock_info.pid)

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=lock_info, trigger="test"
            )
        )
        self.assertUntouched()

    def test_missing_lock_marks_abandoned_only_after_the_grace_window(self) -> None:
        self.write_unfinished_state(run_started_at=datetime.now(UTC).isoformat())
        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )
        self.assertUntouched()

        self.write_unfinished_state()
        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )
        self.assertIsNone(self.persisted_state()["abandoned"]["lock_reason"])

    def test_missing_lock_defers_to_a_lock_that_reappeared(self) -> None:
        self.write_unfinished_state()
        self.write_lock(pid=os.getpid(), hostname=socket.gethostname())

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )
        self.assertUntouched()

    def test_finished_run_is_never_marked(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "run_id": "finished-run",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:30:00+00:00",
                }
            ),
            encoding="utf-8",
        )

        self.assertFalse(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )
        self.assertFalse(self.events_path.exists())

    def test_existing_events_are_preserved_as_a_prefix(self) -> None:
        events.append_event(
            self.events_path,
            events.Event(name="turn_started", payload={"run_id": "run-dead"}),
        )
        before = self.events_path.read_bytes()
        self.write_unfinished_state(run_id="run-dead")

        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )

        after = self.events_path.read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(2, len(read_jsonl(self.events_path)))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_read_only_events_file_is_tolerated(self) -> None:
        self.write_unfinished_state()
        self.events_path.write_text("", encoding="utf-8")
        self.events_path.chmod(0o444)
        self.addCleanup(self.events_path.chmod, 0o644)

        # The terminal state is durable even when the event cannot be appended.
        self.assertTrue(
            state_abandonment.mark_run_abandoned(
                self.plan_state, lock_info=None, trigger="test"
            )
        )
        self.assertEqual(
            state_abandonment.ABANDONED_REASON, self.persisted_state()["reason"]
        )
        self.assertEqual("", self.events_path.read_text(encoding="utf-8"))
