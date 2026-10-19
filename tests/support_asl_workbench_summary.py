"""Shared fixtures for test_asl_workbench_summary."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import (
    _write_state_payload,
)


class WorkbenchSummaryCase(unittest.TestCase):
    def _write_audited_state(
        self, plan_dir: Path, state_root: Path, assurance: dict
    ) -> None:
        _write_state_payload(
            plan_dir,
            state_root,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260619T120000Z-9",
                "harness": "claude",
                "run_started_at": "2026-06-19T12:00:00+00:00",
                "run_finished_at": "2026-06-19T12:45:00+00:00",
                "active_turn": None,
                "active_verification": None,
                "assurance": assurance,
            },
        )
