from __future__ import annotations

import json
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    clock,
    events,
    harnesses,
    plans_payloads,
    plans_snapshot,
    process_tree,
    run_persistence,
    run_reporting,
    turn_execution_argv,
    turn_execution_artifacts,
    turn_execution_commit,
    turn_execution_constants,
    turn_execution_executor,
    turn_execution_hang_sentinel,
    turn_execution_hang_settings,
    turn_execution_live,
    turn_execution_protocols,
    turn_execution_signals,
)

from .support import assert_no_runner_import, write_plan


class RecordingPersistence:
    """A :class:`turn_execution_protocols.TurnPersistence` that keeps what it was told.

    Events are kept in order and state writes are kept as snapshots, so a test
    can assert on sequence without reading files back. The lock is real: the
    heartbeat thread takes it concurrently with the calling thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        self.states.append(json.loads(json.dumps(payload, default=str)))

    def names(self) -> list[str]:
        return [name for name, _payload in self.events]

    def payloads(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == name]


class RecordingProgress:
    """A :class:`turn_execution_protocols.TurnProgress` that reports to a list."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def turn_started(self, active_turn: dict[str, Any]) -> None:
        self.calls.append("turn_started")

    def turn_finished(self, turn_payload: dict[str, Any]) -> None:
        self.calls.append("turn_finished")

    def heartbeat(self, active_turn: dict[str, Any]) -> None:
        self.calls.append("heartbeat")

    def action_required(self, signal: dict[str, Any]) -> None:
        self.calls.append("action_required")


@contextmanager
def silent_delivery() -> Iterator[None]:
    """Suppress AgentMux delivery: it is a separate seam with its own tests."""
    with mock.patch.object(agent_mux_delivery, "notify_loop", return_value=None):
        yield


class TurnExecutorHarness:
    """One plan, one run directory, and one executor bound to turn ``index``."""

    def __init__(self, root: Path, *, turn_index: int = 1) -> None:
        self.root = root
        self.plan_dir = root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.before = plans_snapshot.inspect_plan(self.plan_dir)
        self.persistence = RecordingPersistence()
        self.progress = RecordingProgress()
        self.plan_cache = plans_snapshot.PlanSnapshotCache()
        self.event_path = root / "events.jsonl"
        self.turn_index = turn_index
        self.paths = turn_execution_artifacts.turn_paths(
            root / "run", turn_index=turn_index, session_id="1"
        )
        self.payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_started_at": clock.utc_iso_now(),
            "active_turn": None,
            "turns": [],
        }
        self.executor = turn_execution_executor.TurnExecutor(
            identity=turn_execution_executor.TurnIdentity(
                run_id="run-1", turn_index=turn_index, session_id="1"
            ),
            paths=self.paths,
            event_path=self.event_path,
            persistence=self.persistence,
            progress=self.progress,
            plan_cache=self.plan_cache,
        )

    def names(self) -> list[str]:
        return self.persistence.names()

    def active_turn(self) -> dict[str, Any]:
        started_at = clock.utc_iso_now()
        return {
            "run_id": "run-1",
            "turn_index": self.turn_index,
            **plans_payloads.session_identity_payload("1"),
            "session_kind": plans_payloads.SESSION_KIND_IMPLEMENTATION,
            "total_sessions": 1,
            "artifact_dir": str(self.paths["artifact_dir"]),
            "prompt": "do the work",
            "harness": "codex",
            "model": None,
            "model_source": None,
            "observed_model": None,
            "observed_model_source": None,
            "effort": None,
            "effort_source": None,
            "spark_route": None,
            "stdout_path": str(self.paths["stdout"]),
            "stderr_path": str(self.paths["stderr"]),
            "last_message_path": str(self.paths["last_message"]),
            "result_path": str(self.paths["result"]),
            "started_at": started_at,
            "finished_at": None,
            "duration_seconds": None,
            "idle_seconds_since_previous_turn": None,
            "last_activity_at": started_at,
            "last_activity_stream": None,
            "last_activity_preview": "",
            "last_heartbeat_at": None,
            "elapsed_seconds": 0.0,
            "idle_seconds": 0.0,
            "heartbeat_count": 0,
            "stdout_chars": 0,
            "stderr_chars": 0,
            "action_required": None,
        }

    def live_turn(self, active_turn: dict[str, Any]) -> turn_execution_live.LiveTurn:
        return turn_execution_live.LiveTurn(
            payload=self.payload,
            event_path=self.event_path,
            progress=self.progress,
            active_turn=active_turn,
            persistence=self.persistence,
            # Far enough out that no heartbeat fires inside a test; the
            # heartbeat cadence has its own coverage in the control suite.
            heartbeat_interval=3600.0,
        )

    def launch_spec(self) -> turn_execution_executor.TurnLaunch:
        invocation = harnesses.HarnessInvocation(
            name="codex",
            argv=["codex", "exec", "do the work"],
            cwd=self.root,
        )
        return turn_execution_executor.TurnLaunch(
            harness="codex",
            cwd=self.root,
            plan_dir=self.plan_dir,
            prompt="do the work",
            unsafe_bypass=False,
            timeout=None,
            model=None,
            effort=None,
            abort=harnesses.AbortSignal(),
            live_delivery=None,
            stream_callback=None,
            build_invocation=lambda: invocation,
        )

    def run(
        self, result: harnesses.HarnessResult | BaseException
    ) -> turn_execution_executor.TurnExecution:
        """Launch a turn whose harness produces ``result`` (or raises it)."""
        active_turn = self.active_turn()
        live_turn = self.live_turn(active_turn)
        side_effect: Any = (
            result if isinstance(result, BaseException) else lambda *a, **k: result
        )
        with (
            silent_delivery(),
            mock.patch.object(harnesses, "run_harness", side_effect=side_effect),
        ):
            return self.executor.launch(
                self.launch_spec(),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=self.payload,
            )


def harness_result(**overrides: Any) -> harnesses.HarnessResult:
    fields: dict[str, Any] = {
        "name": "codex",
        "argv": ["codex", "exec", "do the work"],
        "returncode": 0,
        "stdout": "working\n",
        "stderr": "",
        "last_message": "done\n",
    }
    fields.update(overrides)
    return harnesses.HarnessResult(**fields)
