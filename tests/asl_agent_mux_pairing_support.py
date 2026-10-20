"""Shared fixtures for the ``asl agent-mux pairing`` test modules."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any

from agent_session_loop import (
    agent_mux_pairing,
    agent_mux_projection,
    events,
    plans_files,
    state,
    state_locks,
)
from tests.support import write_plan

CURRENT = agent_mux_projection.STATE_SCHEMA_VERSION


class PairingTestBase(unittest.TestCase):
    """A throwaway state root that plans can be seeded into one at a time."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = plans_files.canonicalize_path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir(parents=True)

    def _seed(
        self,
        name: str,
        *,
        version: Any = 2,
        identity: bool = True,
        raw: str | None = None,
        locked: bool = False,
    ) -> Path:
        """Create a plan and its state directory, and return the plan directory.

        ``version`` is stamped verbatim so a record can claim a version this
        build does not understand. ``identity`` off drops the three fields the
        projection contract requires. ``raw`` writes the given bytes instead of
        a JSON object. ``locked`` takes a real lock held by this live process,
        which is the only honest way to produce an active run.
        """
        plan_dir = plans_files.canonicalize_path(self.tmp_path / "plans" / name)
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        if raw is not None:
            state_path.write_text(raw, encoding="utf-8")
        else:
            payload: dict[str, Any] = {"status": "paused"}
            if version is not None:
                payload["version"] = version
            if identity:
                payload["run_id"] = f"20260101T000000000000Z-{name}"
                payload["plan_dir"] = str(plan_dir)
                payload["run_started_at"] = "2026-01-01T00:00:00+00:00"
            state_path.write_text(
                json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
        if locked:
            state_locks.acquire_lock(plan_dir, self.state_dir)
            self.addCleanup(self._release, plan_dir)
        return plan_dir

    def _release(self, plan_dir: Path) -> None:
        with suppress(state.StateError):
            state_locks.release_lock(plan_dir, self.state_dir)

    def _survey(self, **kwargs: Any) -> dict[str, Any]:
        return agent_mux_pairing.survey(state_dir=self.state_dir, **kwargs)

    def _classification_of(self, report: dict[str, Any], plan_dir: Path) -> str:
        plan_state = str(state.plan_state_dir(plan_dir, self.state_dir))
        for run in report["active_incompatible_runs"]:
            if run["state_dir"] == plan_state:
                return agent_mux_pairing.CLASS_ACTIVE_INCOMPATIBLE
        for issue in report["issues"]:
            if issue["state_dir"] == plan_state:
                return issue["classification"]
        return ""

    def _state_bytes(self, plan_dir: Path) -> bytes:
        path = state.plan_state_dir(plan_dir, self.state_dir) / events.STATE_FILE_NAME
        return path.read_bytes()

    def _state_object(self, plan_dir: Path) -> dict[str, Any]:
        return json.loads(self._state_bytes(plan_dir).decode("utf-8"))
