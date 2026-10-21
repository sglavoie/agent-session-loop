from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_settlement,
    pause_policy,
    run_entry,
    run_loop,
    run_loop_commit_settlement,
    run_loop_context,
    run_loop_control_redirects,
    run_loop_terminal,
    run_selection,
    run_termination,
    runner,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import

#: Everything Session 37 carried out of ``runner.py``, with the ``run_loop*``
#: module that owns it now. Each has to exist at its owner and be absent from
#: the runner: an alias left behind would let a caller keep the old
#: spelling and would make the move reversible by accident.
MOVED_SYMBOLS = (
    ("run_locked", run_loop),
    ("RunContext", run_loop_context),
    ("MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED", run_loop_context),
    ("_finish_run_locked", run_loop_terminal),
    ("_terminalize", run_loop_terminal),
    ("_soft_fail_commit", run_loop_commit_settlement),
    ("_settle_owed_commit_group", run_loop_commit_settlement),
    ("_handle_run_control_action", run_loop_control_redirects),
    ("_handle_run_control_status", run_loop_control_redirects),
    ("_expire_run_guards", run_loop_terminal),
    ("_abandon_turn", run_loop_control_redirects),
)

#: The names the loop resolves at their owning module instead of through a
#: runner alias. Session 36 and earlier spelled these through the runner; the
#: loop no longer has a runner to spell them through, and a caller patching the
#: owner is only heard while the loop reads the owner.
OWNED_ELSEWHERE = (
    "run_coordination_mailbox_poll.MailboxPoll",
    "run_coordination_reply_state.ReplyStatePreserver",
    "run_coordination_terminal.TerminalAuditContext",
    "run_coordination_control_drain.drain_external_control_queue",
    "turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS",
    "turn_execution_artifacts.turn_paths",
    "clock.utc_now",
    "plans_payloads.snapshot_payload",
)


def _loop_tree() -> ast.Module:
    return ast.parse(run_loop_view.assembled_source())


#: The three non-recoverable reasons the gate has to refuse, one per way a run
#: can be blocked on its environment rather than on a retry:
#: ``guard-protected-path-changed`` is the gate whose whole purpose is to leave
#: the changes on disk for a human, and the other two are what the relaunch
#: itself trips over. All three are in
#: ``pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS``.
NON_RECOVERABLE_REASONS = (
    "guard-protected-path-changed",
    "commit-preflight",
    "not-loop-ready",
)


class RecordingPersistence:
    """The run's write seam, reduced to the lock and the two writes."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:  # pragma: no cover - the recorder below appends none
            pass

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))


class RecordingProgress:
    """The one reporter method the adapter calls, and nothing else.

    Anything else the adapter reached for would raise here rather than be
    silently absorbed, which is the point: the settlement's only output to a
    terminal is the line it prints about what the relaunch will find.
    """

    def __init__(self) -> None:
        self.paused_work: list[Mapping[str, Any]] = []

    def paused_work_committed(self, record: Mapping[str, Any]) -> None:
        self.paused_work.append(record)


class FakeRunContext:
    """A :class:`run_loop.RunContext` built by hand rather than by a run.

    Only the members the settlement reaches are populated. The collaborators it
    merely forwards are left as ``None``: the recorder standing in for
    ``commit_settlement._settle_paused_retained_work`` never calls them, so a
    ``None`` here is the assertion that the gate does not read them either.
    ``progress`` is the exception, because the adapter reports the record
    itself rather than forwarding the reporter.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = "run-20260904-gate"
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.progress = RecordingProgress()
        self.verification_context = None
        self.commit_generation = None
        self.owed_commit_group: dict[str, Any] | None = None
        self.owed_commit_group_attempts: dict[str, int] = {}
        self.retained_work: commit_settlement.RetainedWorkContext | None = None


class RecordingCommitProgress:
    """The one reporter method a soft-failed commit calls, and nothing else."""

    def __init__(self) -> None:
        self.failures: list[Mapping[str, Any]] = []

    def commit_failed(self, record: Mapping[str, Any]) -> None:
        self.failures.append(record)


class BarrierRunContext:
    """A context reduced to what the commit-group barrier reads and writes.

    The barrier's collaborators are the two the failure already used -- the
    write seam and the reporter -- plus the two terminal writes it takes,
    which are recorded here rather than performed so the assertion is about
    what the barrier decided rather than about how a run ends.
    """

    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.progress = RecordingCommitProgress()
        self.owed_commit_group: dict[str, Any] | None = None
        self.owed_commit_group_attempts: dict[str, int] = {}
        self.unsettled_commit_group: dict[str, Any] | None = None
        self.final_snapshots: list[Any] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: Any) -> None:
        self.final_snapshots.append(snapshot)

    def pause(
        self, *, reason: str, message: str, details: dict[str, Any] | None
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


#: One durable group, the shape ``_commit_failure_record`` reads its identity
#: out of. Only the four keys the record names are populated, because a group
#: this run cannot settle is identified rather than replayed.
FAILING_GROUP: dict[str, Any] = {
    "group_id": "group-1",
    "scope": "session",
    "session_id": "5",
    "failed_alias": "app",
}
