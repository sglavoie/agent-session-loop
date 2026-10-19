"""Shared fixtures for test_asl_workbench_recovery."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    state,
    workbench_recovery_build,
    workbench_recovery_models,
)
from tests.support import (
    _discovered,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
)


class WorkbenchRecoveryCase(unittest.TestCase):
    def _recovery_for_payload(
        self,
        tmp_path: Path,
        payload: dict[str, object] | None,
        *,
        plan_name: str,
        lock_pid: int | None = None,
        pid_running=None,
    ) -> workbench_recovery_models.WorkbenchRecovery:
        plan_dir = _make_workbench_plan(tmp_path, plan_name)
        state_root = tmp_path / "state"
        if payload is not None:
            _write_state_payload(plan_dir, state_root, payload)
        if lock_pid is not None:
            _write_lock_file(plan_dir, state_root, pid=lock_pid)
        discovered = _discovered(plan_dir)
        if pid_running is None:
            return workbench_recovery_build.build_workbench_recovery(
                discovered, state_dir=state_root
            )
        with mock.patch.object(state, "is_pid_running", side_effect=pid_running):
            return workbench_recovery_build.build_workbench_recovery(
                discovered, state_dir=state_root
            )
