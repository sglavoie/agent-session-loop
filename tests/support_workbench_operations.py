"""Journal fixtures for Workbench operation tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from agent_session_loop import plans_files, workbench_operations
from asl_workbench import operations
from tests.support import write_plan

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A pid this host can prove is not running, so "the owner is gone" is a fact
#: the test establishes rather than a race it hopes for.
DEAD_PID = 4_242_424

_HOSTNAME = workbench_operations._hostname()


def _target(
    plan_key: str = "plan-abc", selector: str = ""
) -> operations.OperationTarget:
    return operations.OperationTarget(plan_key=plan_key, selector=selector)


def _record(**overrides: Any) -> dict[str, Any]:
    record = operations.new_record(
        operation_id="op-1",
        method=operations.METHOD_LAUNCH,
        target=_target(),
        now=1000.0,
        pid=os.getpid(),
        hostname=_HOSTNAME,
    )
    record.update(overrides)
    return record


class JournalFixture:
    """A plan directory and an isolated state root for one journal."""

    addCleanup: Any

    def install_journal(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.plan_dir = root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.canonical = plans_files.canonicalize_path(self.plan_dir)
        self.journal_path = workbench_operations.journal_path_for_plan(
            self.canonical, self.state_dir
        )

    def admit(self, operation_id: str, **kwargs: Any) -> Any:
        parameters: dict[str, Any] = {
            "operation_id": operation_id,
            "method": operations.METHOD_LAUNCH,
            "state_dir": self.state_dir,
        }
        parameters.update(kwargs)
        return workbench_operations.admit_operation(self.canonical, **parameters)

    def rows(self) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            self.canonical, state_dir=self.state_dir
        )

    def row(self, operation_id: str) -> dict[str, Any]:
        record = workbench_operations.read_operation(
            self.canonical, operation_id, state_dir=self.state_dir
        )
        assert record is not None
        return record
