"""Shared fixtures for the overseer discovery tests: a state corpus and a seeded run."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    overseer_discovery,
    plans_files,
    plans_snapshot,
    run_payloads,
    run_selection,
    state,
)

from .support import write_plan

#: accident, and late enough to be an ordinary run rather than an obvious
#: sentinel.
PAUSED_AT = "2026-06-19T14:00:00+00:00"
STARTED_AT = "2026-06-19T13:00:00+00:00"
FINISHED_AT = "2026-06-19T14:30:00+00:00"


def _repositories(root: Path) -> list[dict[str, str]]:
    """The repository projection `run_prompts._resolved_repositories_payload` writes."""
    return [
        {
            "alias": "scripts",
            "path": "scripts",
            "origin": "github.com/sglavoie/scripts",
            "resolved_root": str(root / "scripts"),
        }
    ]


class _Corpus:
    """An ASL state root plus the plan directories its records point at.

    Plan directories are created but never populated with anything discovery is
    allowed to read, except where a test deliberately stages a file it must not
    open. A plan is addressed by the same `state.plan_state_dir` the runner
    uses, so a record lands where a real run would have written it.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # `plans_files.canonicalize_path` resolves the macOS `/var` symlink, and the
        # state directory name is a hash of the canonical path, so the fixture
        # has to agree with it before anything is written.
        self.root = plans_files.canonicalize_path(tmp.name)
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.state_root = self.root / "state"

    def plan_dir(self, name: str) -> Path:
        plan_dir = self.plans_root / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plans_files.canonicalize_path(plan_dir)

    def published_record(self, **overrides: Any) -> dict[str, Any]:
        """A record shaped the way a run born after this session publishes one."""
        record: dict[str, Any] = {
            "run_id": "20260619T130000Z-1",
            "harness": "claude",
            "ok": True,
            "paused": False,
            "dry_run": False,
            "run_started_at": STARTED_AT,
            "run_finished_at": None,
            "workspace_root": str(self.root),
            "registry_digest": "d" * 64,
            "repositories": _repositories(self.root),
            overseer_discovery.PAUSED_AT_KEY: None,
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            ),
        }
        record.update(overrides)
        return record

    def write_run(self, name: str, **overrides: Any) -> Path:
        plan_dir = self.plan_dir(name)
        record = self.published_record(**overrides)
        record.setdefault("plan_dir", str(plan_dir))
        self.write_raw_record(plan_dir, json.dumps(record, sort_keys=True))
        return plan_dir

    def write_raw_record(self, plan_dir: Path, text: str) -> Path:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        state_path.write_text(text, encoding="utf-8")
        return state_path

    def write_lock(self, plan_dir: Path, **fields: Any) -> Path:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        payload: dict[str, Any] = {
            "version": 3,
            "owner": "agent-session-loop",
            "plan_dir": str(plan_dir),
            "created_at": STARTED_AT,
            **fields,
        }
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return lock_path

    def inventory(self) -> overseer_discovery.Inventory:
        return overseer_discovery.build_inventory(state_dir=self.state_root)

    def only(self) -> overseer_discovery.Candidate:
        inventory = self.inventory()
        if len(inventory.candidates) != 1:
            raise AssertionError(
                f"expected one candidate, found {len(inventory.candidates)}"
            )
        return inventory.candidates[0]


def _initial_payload(case: unittest.TestCase) -> dict[str, Any]:
    """The run-state document `run_payloads` seeds, built from a real plan.

    Assembled here rather than imported from `tests/test_asl_run_payloads.py`:
    the claim under test is what a *born* run publishes, so the seed has to come
    from the real builder with the real arguments a run start hands it.
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    plan_dir = root / "plan"
    write_plan(plan_dir, ("pending", "pending"))
    state_dir = root / "state"
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    run_id = "20260619T130000Z-1"
    from agent_session_loop import cli

    args = cli.build_parser().parse_args(
        ["run", str(plan_dir), "--state-dir", str(state_dir)]
    )
    return run_payloads._initial_payload(
        args=args,
        snapshot=plans_snapshot.inspect_plan(plan_dir),
        state_dir=plan_state,
        run_dir=plan_state / "runs" / run_id,
        event_path=plan_state / events.EVENTS_FILE_NAME,
        state_path=plan_state / events.STATE_FILE_NAME,
        lock_info=state.LockInfo(
            path=plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans_files.canonicalize_path(plan_dir)),
            created_at=STARTED_AT,
            stale=False,
            reason="active",
            raw={},
        ),
        cwd=root,
        run_id=run_id,
        commit_mode=None,
        mailbox_activation=run_selection._mailbox_activation_payload(args, plan_dir),
    )
