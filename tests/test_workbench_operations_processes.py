"""Concurrent operation admission across processes and killed writers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from typing import Any

from agent_session_loop import workbench_operations
from asl_workbench import operations

from .support_workbench_operations import REPO_ROOT, JournalFixture

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
print(json.dumps({"decision": admission.decision, "reason": admission.reason}))
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            for identifier in ids
        ]
        results: list[dict[str, Any]] = []
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            self.assertEqual(0, child.returncode, stderr)
            results.append(json.loads(stdout.strip()))
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
