"""Focused tests for the overseer's environmental-recovery resume adapter.

Resuming is the operation that follows every other recovery, and it is the one
with the most ways to be wrong that still look like success. The fixtures are
grouped by those ways.

*No invented answer.* The existing resume service takes reply text because the
pause it was built for is a question. This one takes none, and the test for that
is the spawned child's argv rather than a promise: ``--reply-stdin`` is absent,
nothing is written to the process, and the reply-oriented service still refuses
an empty reply exactly as it did before --- an addition beside it, not a
weakening of it.

*An outdated question.* A reply-shaped pause whose question is still open is
``answer_required``; the same pause with nothing open is recoverable, because
the question went away and there is no longer anybody's words to invent. That
distinction is the whole reason the check reads the mailbox instead of the
classification.

*Missing environment metadata.* A launch is restored from the run's own
``run_config`` or it is not restored at all: absent state, state that predates
the block, a truncated block, a conflicting state directory, and repositories
the action was not authorized for are each ``environment_unrecoverable`` with a
manual command attached. Nothing falls back to this process's defaults, because
a resume under different settings is a replacement reported as a recovery.

*Surviving workers and lost helpers.* A dead helper next to a live lock is
``worker_running`` --- the two are different processes and a lost supervisor is
not proof the worker died. A dead helper next to no lock clears the way. A lock
on another host, a lock with no pid, an unparsable record and an unverifiable
probe are all ``ownership_unknown``, never death.

*Checkpoint then resume.* The whole sequence: a paused run, a settled checkpoint
on its incident, a resume prepared and executed, and a receipt that names the
harness the run's own metadata recorded.

*A concurrent manual resume.* Detected two ways between preparation and
execution --- the situation fingerprint moving, or an owner appearing where the
plan was free --- and in both the launch never happens and nothing is killed.

*Progress, and only progress.* A spawned pid is not a resolution. Execution
reports ``executed`` only after the run's fingerprint moves, ``uncertain`` when
the deadline passes without it, and reconciliation answers the same question
later from the ledger row alone.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, get_args
from unittest import mock

from agent_session_loop import (
    events,
    intervention,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_resume,
    overseer_store,
    pause_policy,
    run_entry,
    state,
    supervision_liveness,
    workbench,
)
from tests.support import _FakePopen, assert_no_runner_import, write_plan

PROJECT = "/home/dev/scripts"
RUN = "run-1"
ALIAS = "scripts"
FINGERPRINT = "fp-first"
MOMENT = "2026-01-01T00:00:00+00:00"
HOST = "workstation"

#: A pause an operator repairs the environment for. Reply-shaped pauses are
#: spelled out where they are used, because there the shape is the point.
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
        self.ledger = overseer_store.open_ledger(state_dir=self.root)
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
    ) -> overseer_store.ActionRecord:
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


class ClosedVocabulary(_ResumeCase):
    """The refusal words are a consumer contract, not a set of strings."""

    def test_refusals_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_resume.REFUSALS),
            set(get_args(overseer_resume.Refusal)),
        )
        self.assertEqual(
            len(overseer_resume.REFUSALS), len(set(overseer_resume.REFUSALS))
        )

    def test_the_authority_words_are_the_checkpoint_boundary_words(self) -> None:
        # One authority check, shared, so the two boundaries cannot drift into
        # saying different things about the same refusal.
        from agent_session_loop import overseer_checkpoint

        self.assertTrue(
            set(overseer_checkpoint.AUTHORITY_REFUSALS) <= set(overseer_resume.REFUSALS)
        )

    def test_escalating_refusals_are_refusals(self) -> None:
        self.assertTrue(
            set(overseer_resume.ESCALATING_REFUSALS) <= set(overseer_resume.REFUSALS)
        )
        self.assertIn("environment_unrecoverable", overseer_resume.ESCALATING_REFUSALS)

    def test_the_registry_holds_the_resume_operation_only(self) -> None:
        registry = overseer_resume.adapters(
            authority=self.authority(), state_dir=self.root
        )
        self.assertTrue(registry.supports("resume"))
        for operation in ("reply", "guidance", "verify", "commit"):
            self.assertFalse(registry.supports(operation), msg=operation)

    def test_resume_operations_is_a_subset_of_the_operational_scope(self) -> None:
        self.assertTrue(
            set(overseer_resume.RESUME_OPERATIONS) < set(overseer_decisions.OPERATIONS)
        )

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_resume)


class LaunchRestoration(_ResumeCase):
    """The launch is read back from the run's metadata, or it is refused."""

    def test_the_profile_fields_are_the_launchers_own_fields(self) -> None:
        declared = {field.name for field in dataclasses.fields(workbench.LaunchProfile)}
        self.assertEqual(declared, set(overseer_resume.PROFILE_FIELDS))

    def test_every_restored_field_survives_the_resume_overlay(self) -> None:
        # ``--resume`` rehydrates by flag dest. A field this module restores or
        # records that the overlay drops would be silently reset by the child.
        restored = set(overseer_resume.PROFILE_FIELDS) | set(
            overseer_resume.LIMIT_FIELDS
        )
        self.assertTrue(restored <= set(run_entry._RESUME_OVERLAY_DESTS))

    def test_the_launch_is_rebuilt_from_the_runs_own_metadata(self) -> None:
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, repositories=(ALIAS,), state_dir=self.root
        )

        self.assertTrue(context.recovered)
        self.assertEqual("codex", context.profile["harness"])
        self.assertEqual("gpt-5-codex", context.profile["harness_model"])
        self.assertEqual("high", context.profile["effort"])
        self.assertEqual(5400, context.profile["timeout"])
        self.assertEqual("per-session", context.profile["commit_mode"])
        self.assertEqual("claude", context.profile["review_harness"])
        self.assertEqual(str(self.repo), context.profile["cwd"])
        self.assertEqual(str(self.root / "workspace"), context.workspace_root)
        self.assertEqual("sha256:registry", context.registry_digest)
        self.assertEqual((ALIAS,), context.repositories)

    def test_the_limits_are_read_for_the_record_and_never_launched(self) -> None:
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual(18, context.limits["max_sessions"])
        self.assertEqual(4, context.limits["max_review_fix_cycles_ceiling"])
        self.assertEqual(2, context.limits["max_repair_group_attempts"])
        # Only the launcher's own fields become a launch. The ceilings and the
        # attempt budgets have no flag here and reach the child only through
        # ``--resume`` reading the run's persisted config.
        for name in ("max_sessions", "max_repair_group_attempts"):
            self.assertNotIn(name, context.profile)
        for name in ("max_sessions", "max_review_fix_cycles_ceiling"):
            self.assertNotIn(name, overseer_resume.PROFILE_FIELDS)

    def test_the_adapter_exposes_no_limit_override_at_all(self) -> None:
        # The strongest form of "ASL's limits stay authoritative": not a
        # parameter this adapter promises not to pass, but no parameter.
        knobs = {
            field.name for field in dataclasses.fields(overseer_resume.ResumeAdapter)
        }
        self.assertTrue(knobs.isdisjoint(set(overseer_resume.LIMIT_FIELDS)))

    def test_a_plan_with_no_state_cannot_be_resumed(self) -> None:
        empty = self.new_plan("plan-empty")
        context = overseer_resume.recover_launch_context(
            plan_dir=empty, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("no readable runner state", context.detail)
        self.assertIn("asl run", context.manual_command)

    def test_state_that_predates_run_config_is_unrecoverable(self) -> None:
        self.write_run_config(omit=True)
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("predates run_config", context.detail)

    def test_a_truncated_launch_block_names_what_is_missing(self) -> None:
        config = self.run_config()
        config["harness"] = None
        config["workspace_root"] = ""
        self.write_run_config(config=config)

        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("harness", context.detail)
        self.assertIn("workspace_root", context.detail)

    def test_repositories_the_action_was_not_authorized_for_are_refused(self) -> None:
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, repositories=("other",), state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("authorized for", context.detail)

    def test_a_conflicting_state_directory_is_unrecoverable(self) -> None:
        config = self.run_config(state_dir_override=str(self.root / "elsewhere"))
        self.write_run_config(config=config)

        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("elsewhere", context.detail)

    def test_the_manual_command_carries_what_was_recorded_and_not_resume(self) -> None:
        self.write_run_config(omit=True)
        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        # ``--resume`` is what failed, so sending the operator back through it
        # would be sending them back into this refusal.
        self.assertNotIn("--resume", context.manual_command)
        self.assertIn(self.plan, context.manual_command)

    def test_the_manual_command_is_complete_when_the_metadata_is(self) -> None:
        config = self.run_config()
        config["repositories"] = []
        self.write_run_config(config=config)

        context = overseer_resume.recover_launch_context(
            plan_dir=self.plan, state_dir=self.root
        )

        self.assertEqual("environment_unrecoverable", context.refusal)
        self.assertIn("--harness codex", context.manual_command)
        self.assertIn("--harness-model gpt-5-codex", context.manual_command)
        self.assertIn("--effort high", context.manual_command)
        self.assertIn("--commit-per-session", context.manual_command)

    def test_preparation_refuses_and_hands_over_the_manual_command(self) -> None:
        self.write_run_config(omit=True)

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "environment_unrecoverable")
        self.assertIn("resume by hand with:", preparation.detail)
        self.assertIsNone(receipt)
        self.assertEqual([], self.launches)

    def test_the_recovered_launch_is_what_reaches_the_launcher(self) -> None:
        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)
        self.assertEqual(1, len(self.launches))
        context = self.launches[0]
        self.assertEqual(self.plan, context.plan_dir)
        self.assertEqual("codex", context.profile["harness"])
        self.assertIn("codex/gpt-5-codex/high", receipt.detail)


class OutdatedQuestion(_ResumeCase):
    """A pause that is a question is refused; a question that is gone is not."""

    def waiting(self, *, unanswered: tuple[str, ...]) -> None:
        self.runner_state = self.paused_state("awaiting-reply")
        self.unanswered = unanswered

    def test_an_open_question_is_the_operators_to_answer(self) -> None:
        self.waiting(unanswered=("q-1",))

        refusal = overseer_resume.recovery_refusal(self.facts())

        assert refusal is not None
        self.assertEqual("answer_required", refusal[0])
        self.assertIn("still open", refusal[1])

    def test_a_reply_shaped_pause_whose_question_is_gone_is_recoverable(self) -> None:
        # The case this whole path exists for: the run stopped on a question,
        # the question was resolved while it sat there, and what is left is an
        # outdated pause with nobody waiting on an answer.
        self.waiting(unanswered=())

        self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)

    def test_preparation_refuses_while_the_question_stands(self) -> None:
        self.waiting(unanswered=("q-1", "q-2"))

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "answer_required")
        self.assertIn("2 question(s)", preparation.detail)
        self.assertIsNone(receipt)
        self.assertEqual([], self.launches)

    def test_an_environmental_pause_needs_no_answer_even_with_noise(self) -> None:
        self.runner_state = self.paused_state(ENVIRONMENTAL_PAUSE)
        self.unanswered = ("q-1",)

        self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

    def test_a_recoverable_pause_needs_no_answer(self) -> None:
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.runner_state = self.paused_state(reason)
                self.unanswered = ("q-1",)
                self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

    def test_every_inspect_pause_is_environmentally_recoverable(self) -> None:
        for reason in pause_policy.INSPECT_PAUSE_REASONS:
            with self.subTest(reason=reason):
                self.runner_state = self.paused_state(reason)
                self.unanswered = ("q-1",)
                self.assertIsNone(overseer_resume.recovery_refusal(self.facts()))

    def test_the_runs_own_pause_policy_block_wins(self) -> None:
        # Classifying the reason again here would answer with today's policy
        # about yesterday's pause. The block the runner wrote beside the pause
        # is what that run was actually stopped under.
        self.runner_state = self.paused_state(ENVIRONMENTAL_PAUSE)
        self.runner_state["pause_policy"] = {
            "policy_version": 1,
            "reason": ENVIRONMENTAL_PAUSE,
            "user_action": pause_policy.USER_ACTION_REPLY,
        }
        self.unanswered = ("q-1",)

        policy = overseer_resume.pause_classification(self.facts())
        self.assertEqual(1, policy["policy_version"])

        refusal = overseer_resume.recovery_refusal(self.facts())
        assert refusal is not None
        self.assertEqual("answer_required", refusal[0])

    def test_a_state_file_without_the_block_falls_back_to_the_policy(self) -> None:
        self.runner_state = self.paused_state("awaiting-reply")
        del self.runner_state["pause_policy"]

        self.assertEqual(
            pause_policy.classify_pause("awaiting-reply"),
            dict(overseer_resume.pause_classification(self.facts())),
        )


class SurvivingWorkers(_ResumeCase):
    """A lost supervisor is not proof the worker died."""

    def census(self, plan_dir: str | None = None) -> overseer_resume.Census:
        return overseer_resume.census(
            plan_dir=plan_dir or self.plan,
            state_dir=self.root,
            build_inventory=self.build_inventory,
            probe=self.probe,
        )

    def test_a_live_lock_beside_a_dead_helper_is_a_running_worker(self) -> None:
        self.live_lock(pid=5150)
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        observed = self.census()

        self.assertFalse(observed.clear)
        self.assertEqual("worker_running", observed.refusal)
        self.assertIn("5150", observed.detail)
        self.assertIn("lost supervisor is not proof", observed.detail)
        # And the helper really was probed dead, so this is not the probe
        # failing to run.
        self.assertEqual(
            (supervision_liveness.DEAD,),
            tuple(helper.verdict for helper in observed.helpers),
        )

    def test_a_live_helper_stops_a_second_supervisor(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.ALIVE, pid=9001)

        observed = self.census()

        self.assertEqual("supervisor_live", observed.refusal)
        self.assertIn(self.helper_id("sup-1"), observed.detail)
        self.assertIn("9001", observed.detail)

    def test_a_lock_on_another_host_is_unknown_not_dead(self) -> None:
        self.own(
            lock_present=True,
            pid=5150,
            hostname="elsewhere",
            foreign_host=True,
            alive=None,
        )

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn("elsewhere", observed.detail)

    def test_a_lock_with_no_pid_is_unknown(self) -> None:
        self.own(lock_present=True, pid=None, hostname=HOST, alive=None)

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn("names no pid", observed.detail)

    def test_an_unverifiable_helper_fails_closed(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.UNVERIFIED)

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn(self.helper_id("sup-1"), observed.detail)

    def test_an_unreadable_helper_record_fails_closed(self) -> None:
        self.corrupt_helper("sup-broken")

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertEqual((self.helper_id("sup-broken"),), observed.unreadable)

    def test_a_plan_discovery_cannot_read_is_unknown(self) -> None:
        self.record_observed = False

        observed = self.census()

        self.assertEqual("ownership_unknown", observed.refusal)
        self.assertIn("nothing is known", observed.detail)

    def test_preparation_refuses_a_plan_somebody_is_running(self) -> None:
        self.live_lock()

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "worker_running")
        self.assertIsNone(receipt)
        self.assertEqual([], self.launches)

    def test_a_running_worker_is_checked_after_the_launch_is_recovered(self) -> None:
        # Ordering matters for the operator: a busy plan whose metadata is also
        # gone should say the thing a human has to act on.
        self.live_lock()
        self.write_run_config(omit=True)

        preparation, _ = self.dispatch()

        self.assertRefused(preparation.detail, "environment_unrecoverable")


class LostHelpers(_ResumeCase):
    """A helper that is provably gone clears out of the way, and only then."""

    def census(self) -> overseer_resume.Census:
        return overseer_resume.census(
            plan_dir=self.plan,
            state_dir=self.root,
            build_inventory=self.build_inventory,
            probe=self.probe,
        )

    def test_a_dead_helper_beside_no_lock_clears_the_plan(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        observed = self.census()

        self.assertTrue(observed.clear)
        self.assertIsNone(observed.refusal)
        self.assertEqual(
            (self.helper_id("sup-1"),),
            tuple(helper.supervision_id for helper in observed.helpers),
        )

    def test_a_dead_helper_beside_a_dead_lock_clears_the_plan(self) -> None:
        self.dead_lock()
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        self.assertTrue(self.census().clear)

    def test_no_helper_record_at_all_proves_nothing_either_way(self) -> None:
        observed = self.census()

        self.assertTrue(observed.clear)
        self.assertEqual((), observed.helpers)
        self.assertEqual((), observed.unreadable)

    def test_a_lost_helper_lets_the_resume_through(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)

    def test_one_live_helper_among_dead_ones_still_stops_the_resume(self) -> None:
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD, pid=9001)
        self.write_helper("sup-2", verdict=supervision_liveness.ALIVE, pid=9002)

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "supervisor_live")
        self.assertIn(self.helper_id("sup-2"), preparation.detail)
        self.assertIsNone(receipt)


class CheckpointThenResume(_ResumeCase):
    """The sequence this operation exists to complete."""

    def test_a_checkpointed_run_resumes_under_its_own_launch(self) -> None:
        # What a checkpoint leaves behind: a settled commit on this incident.
        commit = self.ledger.attempt_action(
            grant=self.grant(),
            plan_dir=self.plan,
            operation="commit",
            target="checkpoint",
            incident_id=self.incident_id,
        )
        self.ledger.confirm_action(
            action_id=commit.action_id,
            outcome="executed",
            locator="0" * 40,
        )

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)
        self.assertEqual("pid 4242", receipt.locator)
        self.assertIn("the run has moved since", receipt.detail)
        self.assertEqual(1, len(self.launches))
        self.assertEqual("codex", self.launches[0].profile["harness"])

    def test_a_run_that_is_not_paused_has_nothing_to_resume(self) -> None:
        self.runner_state["paused"] = False

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "not_paused")
        self.assertIsNone(receipt)

    def test_a_resume_does_not_choose_which_session_to_stop_on(self) -> None:
        preparation, receipt = self.dispatch(action=self.action(session_id="9"))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "session_mismatch")
        self.assertIn("'15'", preparation.detail)
        self.assertIsNone(receipt)

    def test_naming_the_session_the_run_stopped_on_is_allowed(self) -> None:
        preparation, receipt = self.dispatch(action=self.action(session_id="15"))

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)

    def test_a_run_that_cannot_be_read_is_never_read_as_gone(self) -> None:
        self.readable = False

        preparation, receipt = self.dispatch()

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "run_unreadable")
        self.assertIsNone(receipt)


class NoInventedReply(_ResumeCase):
    """The absence of a reply is a property of the spawned process."""

    def launchable_plan(self) -> Path:
        directory = self.root / "launchable"
        write_plan(directory, ("pending", "pending"))
        from agent_session_loop import plans

        return plans.canonicalize_path(directory)

    def spawn(self, **kwargs: Any) -> tuple[Any, list[_FakePopen]]:
        captured: list[_FakePopen] = []

        def factory(argv: Any, **popen_kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **popen_kwargs)
            captured.append(process)
            return process

        result = intervention.resume_recovered_run(
            self.launchable_plan(), popen_factory=factory, **kwargs
        )
        return result, captured

    def test_the_recovery_resume_opens_no_channel_for_a_reply(self) -> None:
        result, captured = self.spawn()

        self.assertEqual(1, len(captured))
        spawned = captured[0]
        self.assertIn("--resume", spawned.argv)
        self.assertNotIn("--reply-stdin", spawned.argv)
        self.assertNotIn("--reply", spawned.argv)
        self.assertFalse(result.reply_provided)
        self.assertIsNone(result.reply_origin)
        self.assertIsNone(result.reply_length)
        # Nothing was written to the child, because there was nothing to write
        # and no pipe to write it down.
        self.assertFalse(spawned.stdin_text)

    def test_the_restored_profile_becomes_flags_on_the_child(self) -> None:
        _, captured = self.spawn(
            profile_fields={
                "harness": "codex",
                "harness_model": "gpt-5-codex",
                "effort": "high",
                "commit_mode": "per-session",
            }
        )

        argv = captured[0].argv
        self.assertIn("--harness", argv)
        self.assertIn("gpt-5-codex", argv)
        self.assertIn("high", argv)
        self.assertIn("--commit-per-session", argv)

    def test_a_field_the_launcher_does_not_have_is_refused(self) -> None:
        with self.assertRaises(intervention.InterventionError) as caught:
            self.spawn(profile_fields={"max_sessions": 99})

        self.assertEqual(
            intervention.REASON_UNKNOWN_PROFILE_FIELD, caught.exception.reason
        )
        self.assertIn("max_sessions", str(caught.exception))

    def test_the_reply_oriented_service_is_untouched(self) -> None:
        # This path is an addition beside the manual interface, so the manual
        # interface must still refuse a reply that says nothing.
        captured: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            captured.append(process)
            return process

        with self.assertRaises(workbench.LaunchError):
            workbench.launch_plan(
                self.launchable_plan(),
                reply_text="   \n ",
                popen_factory=factory,
                resume=True,
            )
        self.assertEqual([], captured)

    def test_a_reply_still_travels_by_stdin_and_never_by_argv(self) -> None:
        captured: list[_FakePopen] = []

        def factory(argv: Any, **kwargs: Any) -> _FakePopen:
            process = _FakePopen(argv, **kwargs)
            captured.append(process)
            return process

        reply = "the answer is option B"
        result = workbench.launch_plan(
            self.launchable_plan(),
            reply_text=reply,
            popen_factory=factory,
            resume=True,
        )

        self.assertTrue(result.reply_provided)
        self.assertIn("--reply-stdin", captured[0].argv)
        self.assertEqual(reply, captured[0].stdin_text)


class ObservedProgress(_ResumeCase):
    """A spawned pid is not a resolution."""

    def test_resolution_is_reported_only_after_the_run_moves(self) -> None:
        self.moves_after = 4

        preparation, receipt = self.dispatch(self.adapter(timeout_seconds=30.0))

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)
        # It waited rather than answering from the launch alone.
        self.assertTrue(self.slept)

    def test_a_child_that_changes_nothing_leaves_the_row_in_doubt(self) -> None:
        self.moves_after = None

        preparation, receipt = self.dispatch(self.adapter(timeout_seconds=5.0))

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "progress_unobserved")
        self.assertEqual("pid 4242", receipt.locator)
        # The launch did happen; what is missing is the evidence it took hold.
        self.assertEqual(1, len(self.launches))

    def test_a_run_that_becomes_unreadable_mid_watch_is_not_progress(self) -> None:
        self.moves_after = None
        adapter = self.adapter(timeout_seconds=3.0)
        action = self.action()
        preparation = adapter.prepare(action)
        self.assertTrue(preparation.ready)

        original = self.inspect_run
        calls = {"n": 0}

        def flaky(plan_dir: Any, *, state_dir: Any = None) -> intervention.RunFacts:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("the state directory went away for a moment")
            return original(plan_dir, state_dir=state_dir)

        adapter = dataclasses.replace(adapter, inspect_run=flaky)
        adapter.prepared.update(self.adapter_memo(action))
        receipt = adapter.execute(action)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "progress_unobserved")

    def adapter_memo(self, action: overseer_dispatch.Action) -> dict[str, Any]:
        """Re-prepare on a fresh adapter, so a seam can be swapped in between."""
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        return dict(adapter.prepared)

    def test_a_launch_that_could_not_spawn_is_a_failure_not_a_wait(self) -> None:
        self.launch_error = OSError("no such executable")

        preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "launch_failed")
        self.assertEqual([], self.slept)

    def test_a_launcher_refusal_is_a_failure_too(self) -> None:
        self.launch_error = intervention.InterventionError(
            "invalid-profile", "the recorded cwd is not this plan's workspace"
        )

        _, receipt = self.dispatch()

        assert receipt is not None
        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "launch_failed")

    def test_execution_without_preparation_launches_nothing(self) -> None:
        adapter = self.adapter()

        receipt = adapter.execute(self.action())

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "not_prepared")
        self.assertEqual([], self.launches)

    def test_a_preparation_is_consumed_once(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        self.assertEqual("executed", adapter.execute(action).outcome)

        again = adapter.execute(action)

        self.assertEqual("failed", again.outcome)
        self.assertRefused(again.detail, "not_prepared")
        self.assertEqual(1, len(self.launches))


class ConcurrentManualResume(_ResumeCase):
    """Somebody else got there first, and they keep the run."""

    def test_a_run_that_moved_between_the_two_calls_is_refused(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        # An operator ran ``asl run --resume`` by hand in the meantime.
        self.runner_state = self.paused_state("no-status-change")

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "concurrent_resume")
        self.assertIn("already resumed it", receipt.detail)
        self.assertEqual([], self.launches)

    def test_an_owner_that_appeared_since_preparation_is_a_concurrent_resume(
        self,
    ) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        # The manual resume has taken the lock but has not moved the run yet.
        self.live_lock()

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "concurrent_resume")
        self.assertIn("was free when the resume was prepared", receipt.detail)
        self.assertEqual([], self.launches)

    def test_a_supervisor_that_appeared_since_preparation_is_the_same_thing(
        self,
    ) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        self.write_helper("sup-late", verdict=supervision_liveness.ALIVE)

        receipt = adapter.execute(action)

        self.assertRefused(receipt.detail, "concurrent_resume")
        self.assertIn(self.helper_id("sup-late"), receipt.detail)

    def test_ownership_that_became_unknown_is_reported_as_unknown(self) -> None:
        # Unknown is not evidence somebody resumed; it is evidence of nothing,
        # and renaming it would tell an operator a story.
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)

        self.corrupt_helper("sup-broken")

        receipt = adapter.execute(action)

        self.assertRefused(receipt.detail, "ownership_unknown")
        self.assertEqual([], self.launches)

    def test_nothing_is_ever_killed_to_make_room(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.moves_after = None
        self.assertTrue(adapter.prepare(action).ready)
        self.live_lock(pid=5150)
        helper = self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        adapter.execute(action)

        self.assertTrue(helper.exists())
        self.assertTrue(self.ownership.lock_present)
        self.assertEqual(5150, self.ownership.pid)


class AuthorityGuards(_ResumeCase):
    """A resume is an action, so it is authorized like one."""

    def test_a_payload_that_is_not_a_resume_is_refused(self) -> None:
        action = self.action(payload=overseer_dispatch.Commit(message="checkpoint"))
        adapter = self.adapter()

        preparation = adapter.prepare(action)

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "payload_mismatch")
        self.assertEqual("failed", adapter.execute(action).outcome)

    def test_no_ruling_is_no_authority(self) -> None:
        preparation, receipt = self.dispatch(self.adapter(None))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "authority_absent")
        self.assertIsNone(receipt)

    def test_a_hold_stops_the_resume(self) -> None:
        held = self.ruling(stance="hold")

        preparation, _ = self.dispatch(self.adapter(held))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "authority_held")

    def test_a_ruling_about_another_operation_says_nothing_about_this_one(
        self,
    ) -> None:
        other = self.ruling(operation="commit")

        preparation, _ = self.dispatch(self.adapter(other))

        self.assertFalse(preparation.ready)
        self.assertRefused(preparation.detail, "authority_silent")
        self.assertIn("'commit'", preparation.detail)

    def test_a_decision_replaced_between_the_two_calls_is_superseded(self) -> None:
        adapter = self.adapter(self.ruling(), self.ruling(decision_id="d-2"))
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "authority_superseded")
        self.assertIn("d-1", receipt.detail)
        self.assertIn("d-2", receipt.detail)
        self.assertEqual([], self.launches)

    def test_a_decision_withdrawn_between_the_two_calls_stops_the_launch(self) -> None:
        adapter = self.adapter(self.ruling(), None)
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)

        receipt = adapter.execute(action)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "authority_absent")
        self.assertEqual([], self.launches)


class Reconciliation(_ResumeCase):
    """A row a dead process left, answered from the row alone."""

    def test_a_run_that_moved_settles_as_executed(self) -> None:
        # The row was written while the run was in one situation; the run is in
        # another now. That comparison is the whole answer, and it needs
        # nothing the dispatching process remembered.
        action = self.action()
        self.moves_after = None
        record = self.attempted(action, progress_token="sha256:a-situation-since-left")

        receipt = self.adapter().reconcile(record)

        self.assertEqual("executed", receipt.outcome)
        self.assertIn("left the situation", receipt.detail)
        self.assertTrue(receipt.locator)

    def test_the_same_situation_with_nothing_holding_it_settles_as_failed(
        self,
    ) -> None:
        action = self.action()
        self.moves_after = None
        record = self.attempted(action)

        receipt = self.adapter().reconcile(record)

        self.assertEqual("failed", receipt.outcome)
        self.assertRefused(receipt.detail, "no_progress")

    def test_a_live_worker_leaves_the_row_in_doubt(self) -> None:
        self.moves_after = None
        record = self.attempted(self.action())
        self.live_lock()

        receipt = self.adapter().reconcile(record)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "progress_unobserved")

    def test_a_live_supervisor_leaves_the_row_in_doubt(self) -> None:
        self.moves_after = None
        record = self.attempted(self.action())
        self.write_helper("sup-1", verdict=supervision_liveness.ALIVE)

        receipt = self.adapter().reconcile(record)

        self.assertEqual("uncertain", receipt.outcome)

    def test_an_unreadable_run_is_evidence_of_nothing(self) -> None:
        record = self.attempted(self.action())
        self.readable = False

        receipt = self.adapter().reconcile(record)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "run_unreadable")
        self.assertEqual(record.progress_token, receipt.locator)

    def test_another_operations_row_is_not_this_adapters_to_settle(self) -> None:
        commit = self.ledger.attempt_action(
            grant=self.grant(),
            plan_dir=self.plan,
            operation="commit",
            target="checkpoint",
            incident_id=self.incident_id,
        )

        receipt = self.adapter().reconcile(commit)

        self.assertEqual("uncertain", receipt.outcome)
        self.assertRefused(receipt.detail, "payload_mismatch")

    def test_reconciliation_needs_nothing_the_executing_process_remembered(
        self,
    ) -> None:
        # The case this exists for: the process that dispatched is gone, so a
        # fresh adapter with an empty memo must still answer.
        self.moves_after = None
        record = self.attempted(self.action())
        fresh = self.adapter()

        self.assertEqual({}, fresh.prepared)
        self.assertEqual("failed", fresh.reconcile(record).outcome)


class Portability(_ResumeCase):
    """Nothing at this boundary shells out, which is the PORTABLE_MODULES claim."""

    def test_a_whole_resume_spawns_no_process_of_its_own(self) -> None:
        # The child this operation starts goes through the injected launch
        # seam. Everything else -- reading the run, probing the helpers,
        # watching for progress -- must reach no process at all, or the module
        # cannot honestly run on the portable lane.
        self.write_helper("sup-1", verdict=supervision_liveness.DEAD)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        with (
            mock.patch.object(subprocess, "Popen", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "call", refuse),
            mock.patch.object(subprocess, "check_call", refuse),
            mock.patch.object(subprocess, "check_output", refuse),
        ):
            preparation, receipt = self.dispatch()

        self.assertTrue(preparation.ready)
        assert receipt is not None
        self.assertEqual("executed", receipt.outcome)


if __name__ == "__main__":
    unittest.main()
