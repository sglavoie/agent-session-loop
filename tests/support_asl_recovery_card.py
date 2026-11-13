"""Shared fixtures for test_asl_recovery_card."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    recovery_card_build,
    recovery_card_models,
    state,
)
from tests.support import (
    _discovered,
    _make_summary_plan,
    _write_lock_file,
    _write_state_payload,
)


class RecoveryCardCase(unittest.TestCase):
    def _recovery_for_payload(
        self,
        tmp_path: Path,
        payload: dict[str, object] | None,
        *,
        plan_name: str,
        lock_pid: int | None = None,
        pid_running=None,
    ) -> recovery_card_models.RecoveryCard:
        plan_dir = _make_summary_plan(tmp_path, plan_name)
        state_root = tmp_path / "state"
        if payload is not None:
            _write_state_payload(plan_dir, state_root, payload)
        if lock_pid is not None:
            _write_lock_file(plan_dir, state_root, pid=lock_pid)
        discovered = _discovered(plan_dir)
        if pid_running is None:
            return recovery_card_build.build_recovery_card(
                discovered, state_dir=state_root
            )
        with mock.patch.object(state, "is_pid_running", side_effect=pid_running):
            return recovery_card_build.build_recovery_card(
                discovered, state_dir=state_root
            )
