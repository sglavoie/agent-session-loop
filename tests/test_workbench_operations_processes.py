"""Concurrent operation admission across processes and killed writers."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
import unittest
from typing import Any

from agent_session_loop import workbench_operations
from asl_workbench import operations

from .support_workbench_operations import REPO_ROOT, JournalFixture

ASL_TEST_LANE = "integration"

_CHILD_ADMIT = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from agent_session_loop import workbench_operations
from asl_workbench import operations

plan_dir, state_dir, operation_id, start_at, mode = sys.argv[2:7]
while time.time() < float(start_at):
    time.sleep(0.001)
try:
    admission = workbench_operations.admit_operation(
        plan_dir,
        operation_id=operation_id,
        method=operations.METHOD_LAUNCH,
        state_dir=state_dir,
    )
except operations.OperationError as error:
    print(json.dumps({"decision": "error", "reason": error.reason}))
    raise SystemExit(0)
if admission.admitted and mode != "admit-only":
    workbench_operations.mark_dispatched(
        plan_dir, operation_id, state_dir=state_dir
    )
    if mode == "hang":
        time.sleep(600)
print(
    json.dumps({"decision": admission.decision, "reason": admission.reason}),
    flush=True,
)
if mode == "admit-only":
    sys.stdin.readline()
"""


class CrossProcessTests(JournalFixture, unittest.TestCase):
    """Two processes submitting at once produce one attempt, not two."""

    def setUp(self) -> None:
        self.install_journal()

    def _race(self, ids: list[str]) -> list[dict[str, Any]]:
        start_at = time.time() + 1.0
        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CHILD_ADMIT,
                    str(REPO_ROOT),
                    str(self.canonical),
                    str(self.state_dir),
                    identifier,
                    str(start_at),
                    "admit-only",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            for identifier in ids
        ]
        results: list[dict[str, Any]] = []
        # Keep every owner alive until all contenders have decided. Otherwise
        # a slow contender legitimately expires an exited owner's pending row
        # and admits again, turning scheduling delays into a false lock failure.
        try:
            with selectors.DefaultSelector() as ready:
                for child in children:
                    assert child.stdout is not None
                    ready.register(child.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 120
                while ready.get_map():
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "admission children timed out")
                    for key, _ in ready.select(timeout=remaining):
                        results.append(json.loads(key.fileobj.readline()))
                        ready.unregister(key.fileobj)
        finally:
            for child in children:
                if child.stdin is not None:
                    child.stdin.close()
                    child.stdin = None
            for child in children:
                _, stderr = child.communicate(timeout=120)
                self.assertEqual(0, child.returncode, stderr)
        return results

    def test_eight_processes_resending_one_id_admit_once(self) -> None:
        results = self._race(["op-same"] * 8)
        decisions = [result["decision"] for result in results]
        self.assertEqual(1, decisions.count(operations.DECISION_ADMIT))
        self.assertEqual(7, decisions.count(operations.DECISION_REPLAY))
        # One row, so one attempt: the resends found the first one's record
        # rather than starting anything.
        self.assertEqual(1, len(self.rows()))

    def test_eight_processes_with_different_ids_admit_once(self) -> None:
        results = self._race([f"op-{index}" for index in range(8)])
        decisions = [result["decision"] for result in results]
        self.assertEqual(1, decisions.count(operations.DECISION_ADMIT))
        self.assertEqual(7, decisions.count("refuse"))
        reasons = {result["reason"] for result in results if result["reason"]}
        self.assertEqual({operations.REASON_OPERATION_OUTSTANDING}, reasons)
        # One row, because the seven refusals were decided before anything was
        # written: a refused submission leaves no record to account for.
        self.assertEqual(1, len(self.rows()))

    def test_process_exit_during_compaction_keeps_duplicate_protection(self) -> None:
        self.admit("op-1")
        before = self.rows()
        inode = self.journal_path.stat().st_ino
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from agent_session_loop import workbench_operation_journal as journal

path = Path(sys.argv[2])
with journal._locked_journal(path) as (fd, replay):
    truncate = os.ftruncate
    def crash_after_truncate(target, size):
        truncate(target, size)
        os._exit(73)
    with mock.patch.object(os, "ftruncate", side_effect=crash_after_truncate):
        journal._compact_locked(fd, replay, journal_path=path)
""",
                str(REPO_ROOT),
                str(self.journal_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(73, child.returncode, child.stderr)
        self.assertEqual(0, self.journal_path.stat().st_size)
        recovery = self.journal_path.with_name("operations.jsonl.compact")
        self.assertEqual(0o600, recovery.stat().st_mode & 0o777)
        self.assertEqual(before, self.rows())
        # Reads do not mutate recovery state; the next writer repairs the inode
        # before deciding whether this is a replay or a new admission.
        self.assertEqual(0, self.journal_path.stat().st_size)
        results = self._race([f"op-contender-{index}" for index in range(4)])
        self.assertEqual(
            [operations.REASON_OPERATION_OUTSTANDING] * 4,
            [result["reason"] for result in results],
        )
        self.assertTrue(self.admit("op-1").replayed)
        self.assertFalse(recovery.exists())
        self.assertEqual(before, self.rows())
        self.assertEqual(inode, self.journal_path.stat().st_ino)

    def test_a_killed_child_leaves_an_accountable_record(self) -> None:
        start_at = time.time() + 0.2
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_ADMIT,
                str(REPO_ROOT),
                str(self.canonical),
                str(self.state_dir),
                "op-killed",
                str(start_at),
                "hang",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.addCleanup(child.kill)
        deadline = time.time() + 60
        while time.time() < deadline:
            record = workbench_operations.read_operation(
                self.canonical, "op-killed", state_dir=self.state_dir
            )
            if record is not None and record["status"] == (
                operations.OPERATION_DISPATCHED
            ):
                break
            time.sleep(0.05)
        else:  # pragma: no cover - only reached if the child never got there
            self.fail("the child never recorded dispatch")
        child.kill()
        child.wait(timeout=30)
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        # The owner is gone and the mutation was already dispatched: the honest
        # answer is that nobody can say, and the journal says exactly that.
        self.assertEqual(
            operations.OPERATION_UNCONFIRMED, self.row("op-killed")["status"]
        )


if __name__ == "__main__":
    unittest.main()
