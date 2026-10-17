"""Shared fixtures for support asl overseer resume."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    intervention,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_resume,
    overseer_store_ledger,
    overseer_store_models,
    pause_policy,
    state,
    supervision_liveness,
)

PROJECT = "/home/dev/scripts"


RUN = "run-1"


ALIAS = "scripts"


FINGERPRINT = "fp-first"


MOMENT = "2026-01-01T00:00:00+00:00"


HOST = "workstation"


ENVIRONMENTAL_PAUSE = "commit-preflight"


def _ruling(
    *,
    plan_dir: str,
    incident_id: int,
    operation: str = "resume",
    run_id: str = RUN,
    project: str = PROJECT,
    decision_id: str = "d-1",
    stance: str = "allow",
    at: str = MOMENT,
    authorized: bool = True,
    refusal: str | None = None,
    with_decision: bool = True,
) -> overseer_decisions.Ruling:
    """A ruling that authorizes one operation on one plan, run and incident."""
    scope = overseer_decisions.Scope(
        project=project,
        plan_dir=plan_dir,
        run_id=run_id,
        incident=str(incident_id),
    )
    decision = overseer_decisions.Decision(
        decision_id=decision_id,
        statement_id="s-1",
        topic="operations",
        stance=stance,  # type: ignore[arg-type]
        scope=scope,
        operations=(operation,),  # type: ignore[arg-type]
        at=at,
        moment=datetime.fromisoformat(at),
        sources=("s-1",),
        text="put it back on its feet",
    )
    return overseer_decisions.Ruling(
        operation=operation,
        scope=scope,
        authorized=authorized,
        refusal=refusal,  # type: ignore[arg-type]
        decision=decision if with_decision else None,
    )


class _ResumeCase(unittest.TestCase):
    """A real state root, a real ledger, one paused run, injected processes.

    Everything that reads a *process* --- the plan lock's owner, the helpers'
    liveness, the launch itself, the clock --- is a seam here, because the
    failure this adapter exists to prevent only happens when two of them are
    running at once and that is not reproducible with real ones.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-resume-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._close)
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.plan = self.new_plan("plan-a")
        self.repo = self.root / "repos" / ALIAS
        self.repo.mkdir(parents=True, exist_ok=True)
        self.repository = overseer_policy_client.Repository(
            alias=ALIAS, path=str(self.repo)
        )
        self.incident_id = self.ledger.open_incident(
            grant=None,
            plan_dir=self.plan,
            run_id=RUN,
            kind="run_paused",
            summary="paused, and the environment has since been repaired",
        ).incident_id

        # The run, as its own state file describes it.
        self.runner_state: dict[str, Any] = self.paused_state()
        self.unanswered: tuple[str, ...] = ()
        self.readable = True

        # Who else is on this plan.
        self.ownership = overseer_discovery.Ownership.unowned()
        self.record_observed = True
        self.helper_ids: dict[str, str] = {}
        self.verdicts: dict[str, str] = {}

        # The launch, and the clock the observation runs on.
        self.launches: list[overseer_resume.LaunchContext] = []
        self.launch_error: Exception | None = None
        self.moves_after: int | None = 0
        self.launched_at: int | None = None
        self.inspections = 0
        self.elapsed = 0.0
        self.slept: list[float] = []
        self.rulings: list[overseer_decisions.Ruling | None] = []

        self.write_run_config()

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    # -- the plan side -----------------------------------------------------

    def new_plan(self, name: str) -> str:
        path = self.root / "plans" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def plan_state(self, plan_dir: str | None = None) -> Path:
        return state.plan_state_dir(plan_dir or self.plan, self.root)

    def paused_state(self, reason: str = ENVIRONMENTAL_PAUSE) -> dict[str, Any]:
        """The runner state block a paused run publishes, as ``state`` shapes it."""
        return {
            "present": True,
            "run_id": RUN,
            "ok": True,
            "paused": True,
            "reason": reason,
            "pause_policy": pause_policy.classify_pause(reason),
            "run_started_at": MOMENT,
            "run_finished_at": None,
            "active_turn": {"session_id": "15", "turn_index": 3},
        }

    def run_config(self, **overrides: Any) -> dict[str, Any]:
        """A ``run_config`` block shaped the way ``run_payloads`` persists one."""
        config: dict[str, Any] = {
            "harness": "codex",
            "harness_model": "gpt-5-codex",
            "effort": "high",
            "timeout": 5400,
            "review_harness": "claude",
            "review_harness_model": "claude-opus-5",
            "commit_mode": "per-session",
            "unsafe_bypass": False,
            "allow_not_loop_ready": False,
            "note": "the run's own note",
            "max_review_fix_cycles": 2,
            "max_review_fix_cycles_ceiling": 4,
            "max_sessions": 18,
            "max_pending_sessions": 18,
            "max_repair_group_attempts": 2,
            "cwd": str(self.repo),
            "state_dir_override": str(self.root),
            "workspace_root": str(self.root / "workspace"),
            "registry_digest": "sha256:registry",
            "repositories": [{"alias": ALIAS, "path": str(self.repo)}],
        }
        config.update(overrides)
        return config

    def write_run_config(
        self,
        *,
        plan_dir: str | None = None,
        config: dict[str, Any] | None = None,
        omit: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> Path:
        """Publish this plan's ``state.json``, with or without its launch block."""
        directory = self.plan_state(plan_dir)
        directory.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = dict(payload or {"run_id": RUN, "paused": True})
        if not omit:
            record["run_config"] = self.run_config() if config is None else config
        path = directory / events.STATE_FILE_NAME
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def helper_id(self, name: str) -> str:
        """A stable supervision id for a readable name.

        Helper records are named by uuid, which is right for the product and
        unreadable in a test, so each fixture name gets one derived id and the
        assertions can say ``sup-1``.
        """
        if name not in self.helper_ids:
            self.helper_ids[name] = str(uuid.uuid5(uuid.NAMESPACE_URL, name))
        return self.helper_ids[name]

    def write_helper(
        self,
        name: str,
        *,
        verdict: str = supervision_liveness.DEAD,
        pid: int = 9001,
        host: str = HOST,
        plan_dir: str | None = None,
    ) -> Path:
        """A detached helper's liveness record, and what a probe proves about it."""

        def fake_ps(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no ps in this fixture")

        supervision_id = self.helper_id(name)
        self.verdicts[supervision_id] = verdict
        return supervision_liveness.write_record(
            plan_dir or self.plan,
            supervision_id,
            pid=pid,
            host=host,
            argv=("asl", "outbox", "supervise"),
            state_dir=self.root,
            run_command=fake_ps,
        )

    def corrupt_helper(self, name: str) -> Path:
        """A helper record that exists and cannot be read. Not a dead helper."""
        path = self.write_helper(name)
        path.write_text("{ this is not json", encoding="utf-8")
        return path

    # -- the seams ---------------------------------------------------------

    def facts(self) -> intervention.RunFacts:
        return intervention.build_run_facts(
            Path(self.plan),
            runner_state=self.runner_state,
            lock_active=bool(self.ownership.lock_present),
            unanswered_ids=self.unanswered,
        )

    def inspect_run(
        self, plan_dir: str | Path, *, state_dir: Any = None
    ) -> intervention.RunFacts:
        """Read the run, and let the fixture decide when it starts moving.

        ``moves_after`` counts polls after the launch rather than seconds, so
        "the run moved on the third poll" is expressible without a real clock
        and without the outcome depending on how fast the machine is. Before
        the launch the run never moves on its own: a test that wants it to (a
        concurrent manual resume) says so by editing ``runner_state``.
        """
        self.inspections += 1
        if not self.readable:
            raise intervention.InterventionError("run-unreadable", str(plan_dir))
        if (
            self.moves_after is not None
            and self.launched_at is not None
            and self.inspections > self.launched_at + self.moves_after
        ):
            moved = dict(self.runner_state)
            moved["reason"] = "runner-resumed"
            return dataclasses.replace(self.facts(), runner_state=moved)
        return self.facts()

    def build_inventory(self, *, state_dir: Any = None) -> overseer_discovery.Inventory:
        return overseer_discovery.Inventory(
            state_root=self.root,
            candidates=(self.candidate(),),
            warnings=(),
            complete=True,
        )

    def candidate(self) -> overseer_discovery.Candidate:
        return overseer_discovery.Candidate(
            state_dir=self.plan_state(),
            plan_dir=self.plan,
            run_id=RUN,
            workspace_root=str(self.root / "workspace"),
            registry_digest="sha256:registry",
            repositories=((ALIAS, str(self.repo)),),
            run_started_at=MOMENT,
            run_finished_at=None,
            paused=True,
            pause_reason=ENVIRONMENTAL_PAUSE,
            paused_at=MOMENT,
            contract_version=1,
            ownership=self.ownership,
            lifecycle="paused",
            record_observed=self.record_observed,
            ineligible_reasons=(),
        )

    def probe(
        self, record: supervision_liveness.LivenessRecord
    ) -> supervision_liveness.ProbeVerdict:
        verdict = self.verdicts.get(record.supervision_id, supervision_liveness.DEAD)
        return verdict  # type: ignore[return-value]

    def launch(
        self, context: overseer_resume.LaunchContext
    ) -> overseer_resume.Launched:
        self.launches.append(context)
        if self.launch_error is not None:
            raise self.launch_error
        self.launched_at = self.inspections
        return overseer_resume.Launched(
            pid=4242, argv=("asl", "run", self.plan, "--resume")
        )

    def clock(self) -> float:
        return self.elapsed

    def rest(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.elapsed += seconds

    # -- ownership shorthands ----------------------------------------------

    def own(self, **fields: Any) -> None:
        """Set the plan lock's ownership, one named fact at a time."""
        self.ownership = dataclasses.replace(self.ownership, **fields)

    def live_lock(self, pid: int = 5150) -> None:
        self.own(lock_present=True, pid=pid, hostname=HOST, alive=True)

    def dead_lock(self, pid: int = 5150) -> None:
        self.own(lock_present=True, pid=pid, hostname=HOST, alive=False)

    # -- the overseer side -------------------------------------------------

    def grant(self) -> overseer_policy_client.Grant:
        return overseer_policy_client.Grant(
            plan_dir=self.plan,
            access=overseer_policy_client.ACCESS_ACTION,
            request_digest="rd-1",
            config_fingerprint=FINGERPRINT,
        )

    def ruling(self, **kwargs: Any) -> overseer_decisions.Ruling:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
        }
        arguments.update(kwargs)
        return _ruling(**arguments)

    def authority(
        self, *rulings: overseer_decisions.Ruling | None
    ) -> overseer_resume.Authority:
        """An authority seam answering from a queue, so a withdrawal is expressible."""
        answers = list(rulings) or [self.ruling()]
        self.rulings = answers

        def establish(
            action: overseer_dispatch.Action,
        ) -> overseer_decisions.Ruling | None:
            return answers.pop(0) if len(answers) > 1 else answers[0]

        return establish

    def adapter(
        self,
        *rulings: overseer_decisions.Ruling | None,
        timeout_seconds: float = 10.0,
    ) -> overseer_resume.ResumeAdapter:
        return overseer_resume.ResumeAdapter(
            authority=self.authority(*rulings),
            state_dir=self.root,
            build_inventory=self.build_inventory,
            inspect_run=self.inspect_run,
            probe=self.probe,
            launch=self.launch,
            now=self.clock,
            sleep=self.rest,
            progress_timeout_seconds=timeout_seconds,
            progress_poll_seconds=1.0,
        )

    def action(
        self,
        *,
        session_id: str = "",
        payload: Any = None,
        plan_dir: str | None = None,
        repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
    ) -> overseer_dispatch.Action:
        return overseer_dispatch.Action(
            plan_dir=plan_dir or self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=payload or overseer_dispatch.Resume(session_id=session_id),
            token="token-1",
            scope="sha256:scope",
            repositories=(self.repository,) if repositories is None else repositories,
        )

    def attempted(
        self,
        action: overseer_dispatch.Action,
        *,
        progress_token: str | None = None,
    ) -> overseer_store_models.ActionRecord:
        """The ledger row the dispatcher writes before the adapter is called."""
        token = (
            overseer_dispatch.progress_token(self.facts())
            if progress_token is None
            else progress_token
        )
        return self.ledger.attempt_action(
            grant=self.grant(),
            plan_dir=action.plan_dir,
            operation="resume",
            target=action.target,
            incident_id=action.incident_id,
            progress_token=token,
        )

    def dispatch(
        self,
        adapter: overseer_resume.ResumeAdapter | None = None,
        *,
        action: overseer_dispatch.Action | None = None,
    ) -> tuple[overseer_dispatch.Preparation, overseer_dispatch.Receipt | None]:
        """Prepare, and execute only if preparation allowed it."""
        adapter = adapter or self.adapter()
        action = action or self.action()
        preparation = adapter.prepare(action)
        if not preparation.ready:
            return preparation, None
        return preparation, adapter.execute(action)

    # -- assertions --------------------------------------------------------

    def assertRefused(self, detail: str, refusal: str) -> None:
        self.assertTrue(
            detail.startswith(f"{refusal}:") or detail == refusal,
            msg=f"expected {refusal!r}, got {detail!r}",
        )
