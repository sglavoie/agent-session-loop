"""MarkRunAbandonedTests.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    plans,
    state,
    state_abandonment,
    state_assurance_pending,
    state_locks,
)
from tests.support import (
    read_jsonl,
    write_plan,
)

# Takes the abandonment claim in a genuinely separate process, announces it,
# then holds it until stdin closes — so the exclusion under test is the
# cross-process one, not an in-process bookkeeping flag.
_HOLD_ABANDON_CLAIM_SOURCE = """
import fcntl, os, sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


class MarkRunAbandonedTests(unittest.TestCase):
    """Proof-of-death guards around the synthetic terminal run record."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True)
        self.state_path = self.plan_state / events.STATE_FILE_NAME
        self.events_path = self.plan_state / events.EVENTS_FILE_NAME
        self.lock_path = self.plan_state / state.LOCK_FILE_NAME

    def dead_pid(self) -> int:
        """A pid that has exited and been reaped, so it is provably gone."""
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        return process.pid

    def write_unfinished_state(
        self,
        *,
        run_id: str = "20260728T123131255926Z-77262",
        run_started_at: str = "2026-06-19T12:00:00+00:00",
        assurance: dict[str, object] | None = None,
        run_config: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "ok": True,
            "paused": False,
            "run_id": run_id,
            "harness": "claude",
            "reason": "",
            "run_started_at": run_started_at,
            "run_finished_at": None,
            "duration_seconds": None,
            "active_turn": {"session": 1, "turn_index": 1},
            "active_verification": {"session": 1, "turn_index": 1},
        }
        if assurance is not None:
            payload["assurance"] = assurance
        if run_config is not None:
            # Shaped like `run_payloads._initial_payload`: the plan directory
            # is a top-level field and the workspace binding lives in
            # `run_config`.
            payload["plan_dir"] = str(plans.canonicalize_path(self.plan_dir))
            payload["run_config"] = {
                "workspace_root": str(plans.canonicalize_path(self.plan_dir)),
                **run_config,
            }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def write_lock(self, *, pid: int, hostname: str | None) -> None:
        payload: dict[str, object] = {
            "version": 1,
            "owner": "agent-session-loop",
            "pid": pid,
            "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
            "created_at": "2026-06-19T12:00:00+00:00",
        }
        if hostname is not None:
            payload["hostname"] = hostname
        self.lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def read_lock_info(self) -> state.LockInfo:
        lock_info = state_locks.read_lock_info(self.plan_dir, self.state_dir)
        assert lock_info is not None
        return lock_info

    def persisted_state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def assertUntouched(self) -> None:
        persisted = self.persisted_state()
        self.assertIsNone(persisted["run_finished_at"])
        self.assertNotIn("abandoned", persisted)
        self.assertFalse(self.events_path.exists())

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

    def test_acquire_lock_takeover_marks_abandoned_and_records_hostname(self) -> None:
        self.write_unfinished_state(run_id="run-dead")
        self.write_lock(pid=self.dead_pid(), hostname=socket.gethostname())

        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual(os.getpid(), lock_info.pid)
        self.assertEqual(socket.gethostname(), lock_info.raw["hostname"])
        self.assertEqual(4, lock_info.raw["version"])

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

    def pending_record(self, run_id: str) -> dict[str, object]:
        path = (
            self.plan_state
            / state.RUNS_DIR_NAME
            / run_id
            / state_assurance_pending.ASSURANCE_PENDING_FILE_NAME
        )
        return json.loads(path.read_text(encoding="utf-8"))

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
            str(plans.canonicalize_path(self.plan_dir)), record["plan_dir"]
        )
        self.assertEqual(
            str(plans.canonicalize_path(self.plan_dir)), record["workspace_root"]
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
