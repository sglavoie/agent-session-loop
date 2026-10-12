"""Shared fixtures for the agent-mux notification test modules."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from agent_session_loop import (
    agent_mux_delivery,
)
from tests.support import (
    agent_mux_notify_enabled,
)


class AgentMuxNotificationIntegrationTestsBase(unittest.TestCase):
    """Notifier-specific coverage for AgentMux hook delivery.

    The suite-wide default (``tests/support.py``) forces
    ``ASL_AGENT_MUX_NOTIFY=off`` so no other test can reach a live daemon;
    every test here that expects delivery re-enables it explicitly and
    substitutes the executable lookup / subprocess layer with fakes.
    """

    def setUp(self) -> None:
        self.enterContext(agent_mux_notify_enabled())
        agent_mux_delivery.reset_legacy_cache()
        self.addCleanup(agent_mux_delivery.reset_legacy_cache)

    @staticmethod
    def accepted_delivery(
        trace_id: str = "trace-test",
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            ["/opt/amx"],
            0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "status": "accepted",
                    "retryable": False,
                    "reason": "accepted",
                    "trace_id": trace_id,
                }
            ),
            stderr="",
        )

    def finish_payload(self, tmp_dir: Path) -> dict[str, object]:
        return {
            "run_id": "run-123",
            "plan_dir": str(tmp_dir / "plan"),
            "cwd": str(tmp_dir / "repo"),
            "harness": "codex",
            "reason": "all-complete",
            "run_started_at": "2026-06-11T12:00:00Z",
            "run_finished_at": "2026-06-11T12:00:03Z",
            "state_path": str(tmp_dir / "state.json"),
            "kitty_window_id": 42,
        }
