"""Focused tests for the overseer's attributable-changes checkpoint adapter.

``commit_message`` already guarantees that a scoped commit carries the index and
not the worktree. What this module adds is everything a *fleet* supervisor needs
before it is allowed to make one, and the fixtures are grouped by the way each
of those additions can go wrong.

*A safe checkpoint.* Every dirty path is one the run's own artifacts recorded, so
the commit is made, its sha is the receipt's locator, it carries the derived
checkpoint trailer, and the worktree it came from is clean afterwards.

*An already-clean repository.* Nothing to checkpoint is refused as
``nothing_to_commit`` before anything is staged, because a checkpoint of nothing
is not a checkpoint and must not read as one.

*Attribution refusal.* A dirty path the run's records do not account for stops
the whole checkpoint, and stops it in ``prepare`` --- so nothing is staged,
nothing is committed, and the other worker's file is exactly where it was. The
same goes for a HEAD that moved out from under the run's recorded baseline and
for a run whose records name no baseline at all: neither is somewhere ownership
can be established from, and neither is guessed past.

*Concurrent edits and snapshot mismatch.* Between preparation and execution an
owned file can be edited on disk or re-staged, and the two are told apart:
``worktree_drifted`` and ``snapshot_mismatch``. Both refuse without committing,
and both leave the edit that caused them alone.

*Crash reconciliation after a successful commit.* A process that committed and
then died leaves a row nobody settled. Reconciliation answers "did this happen?"
from that row alone, by the derived checkpoint id, and says ``uncertain`` rather
than ``failed`` when the repository cannot be read --- because a repository
nobody could open is not evidence that a commit is absent.

*Preserved work and staging.* Staging is where an unrelated actor's work is
easiest to destroy, so the refusals are checked for what they left behind as
well as for what they said: an entry somebody else staged is still staged after
a refusal, and work the checkpoint never owned is still on disk after one.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from agent_session_loop import (
    commit_message,
    overseer_checkpoint,
    overseer_decisions,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
    state,
)
from tests import support
from tests.support import assert_no_runner_import

PROJECT = "/home/dev/scripts"
RUN = "run-1"
ALIAS = "scripts"
FINGERPRINT = "fp-first"
MOMENT = "2026-01-01T00:00:00+00:00"
MESSAGE = "chore: checkpoint the work in progress"


def _ruling(
    *,
    plan_dir: str,
    incident_id: int,
    operation: str = "commit",
    run_id: str = RUN,
    project: str = PROJECT,
    decision_id: str = "d-1",
    stance: str = "allow",
    at: str = MOMENT,
    authorized: bool = True,
    refusal: str | None = None,
    with_decision: bool = True,
    settlements: tuple[overseer_decisions.Settlement, ...] = (),
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
        text="check the work in",
    )
    return overseer_decisions.Ruling(
        operation=operation,
        scope=scope,
        authorized=authorized,
        refusal=refusal,  # type: ignore[arg-type]
        decision=decision if with_decision else None,
        settlements=settlements,
    )


class _CheckpointCase(unittest.TestCase):
    """A real state root, a real ledger, a real git repository, one run."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-checkpoint-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._close)
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.plan = self.new_plan("plan-a")
        self.repo = self.root / "repos" / ALIAS
        support.init_git_repo(self.repo)
        self.repository = overseer_policy_client.Repository(
            alias=ALIAS, path=str(self.repo)
        )
        self.incident_id = self.ledger.open_incident(
            grant=None,
            plan_dir=self.plan,
            run_id=RUN,
            kind="run_paused",
            summary="paused with work in progress",
        ).incident_id
        self.rulings: list[overseer_decisions.Ruling | None] = []

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    # -- the plan side -----------------------------------------------------

    def new_plan(self, name: str) -> str:
        """A real plan directory, canonical, so every read agrees on its name."""
        path = self.root / "plans" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def run_dir(self, plan_dir: str | None = None) -> Path:
        return (
            state.plan_state_dir(plan_dir or self.plan, self.root)
            / state.RUNS_DIR_NAME
            / RUN
        )

    def record_turn(
        self,
        *,
        paths: tuple[str, ...] = (),
        head: str | None = None,
        alias: str = ALIAS,
        verification: dict[str, Any] | None = None,
        index: int = 1,
        guard: bool = True,
    ) -> None:
        """Publish one turn artifact shaped the way the write guard writes it."""
        observed = self.head() if head is None else head
        record: dict[str, Any] = {"turn_index": index, "session_id": "1"}
        if guard:
            record["workspace_write_guard"] = {
                "changed": {
                    alias: {
                        "head_before": observed,
                        "head_after": observed,
                        "qualified_paths": [f"{alias}:{path}" for path in paths],
                    }
                }
            }
        if verification is not None:
            record["verification"] = verification
        self.write_artifact(f"turn-{index:04d}", record)

    def record_commit(
        self,
        *,
        paths: tuple[str, ...] = (),
        head_after: str | None = None,
        index: int = 1,
    ) -> None:
        """Publish one commit artifact shaped the way the runner writes it."""
        self.write_artifact(
            f"commit-{index:04d}",
            {
                "commit_index": index,
                "repository_alias": ALIAS,
                "head_before": "0" * 40,
                "head_after": self.head() if head_after is None else head_after,
                "qualified_paths": [f"{ALIAS}:{path}" for path in paths],
            },
        )

    def write_artifact(self, name: str, record: dict[str, Any]) -> None:
        directory = self.run_dir() / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / state.RESULT_FILE_NAME).write_text(
            json.dumps(record), encoding="utf-8"
        )

    # -- the repository side -----------------------------------------------

    def head(self) -> str:
        return support.git_stdout(self.repo, "rev-parse", "HEAD")

    def write(self, name: str, text: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
        )

    def porcelain(self) -> list[str]:
        return [
            line
            for line in support.git_stdout(
                self.repo, "status", "--porcelain", "--untracked-files=all"
            ).splitlines()
            if line
        ]

    def commit_body(self, revision: str = "HEAD") -> str:
        return support.git_stdout(self.repo, "log", "-1", "--format=%B", revision)

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
        self,
        *rulings: overseer_decisions.Ruling | None,
    ) -> overseer_checkpoint.Authority:
        """An authority seam that answers each call from a scripted queue.

        Scripted rather than constant so a decision withdrawn *between*
        preparation and execution --- the only window this adapter can be
        surprised in --- is expressible.
        """
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
        repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
    ) -> overseer_checkpoint.CheckpointAdapter:
        declared = (self.repository,) if repositories is None else repositories

        def resolve(
            plan_dir: str, *, state_dir: Any = None
        ) -> tuple[overseer_policy_client.Repository, ...]:
            return declared

        return overseer_checkpoint.CheckpointAdapter(
            authority=self.authority(*rulings),
            state_dir=self.root,
            repositories=resolve,
        )

    def action(
        self,
        *,
        message: str = MESSAGE,
        repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
        plan_dir: str | None = None,
    ) -> overseer_dispatch.Action:
        return overseer_dispatch.Action(
            plan_dir=plan_dir or self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=overseer_dispatch.Commit(message=message),
            token="token-1",
            scope="sha256:scope",
            repositories=(self.repository,) if repositories is None else repositories,
        )

    def attribute(self, **kwargs: Any) -> overseer_checkpoint.Attribution:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "run_id": RUN,
            "repositories": (self.repository,),
            "state_dir": self.root,
        }
        arguments.update(kwargs)
        return overseer_checkpoint.attribute(**arguments)

    def attempted(
        self,
        action: overseer_dispatch.Action,
    ) -> overseer_store_models.ActionRecord:
        """The ledger row a dispatch writes before the adapter is called."""
        return self.ledger.attempt_action(
            grant=self.grant(),
            plan_dir=action.plan_dir,
            operation="commit",
            target=action.target,
            incident_id=action.incident_id,
        )


class ClosedVocabulary(_CheckpointCase):
    """The refusal words are a consumer contract, not a set of strings."""

    def test_refusals_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_checkpoint.REFUSALS),
            set(get_args(overseer_checkpoint.Refusal)),
        )
        self.assertEqual(
            len(overseer_checkpoint.REFUSALS),
            len(set(overseer_checkpoint.REFUSALS)),
        )

    def test_verification_states_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_checkpoint.VERIFICATION_STATES),
            set(get_args(overseer_checkpoint.VerificationState)),
        )

    def test_the_registry_holds_the_commit_operation_only(self) -> None:
        registry = overseer_checkpoint.adapters(
            authority=self.authority(), state_dir=self.root
        )
        self.assertTrue(registry.supports("commit"))
        for operation in ("reply", "guidance", "verify", "resume"):
            self.assertFalse(registry.supports(operation), msg=operation)

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_checkpoint)


class DerivedIdentity(_CheckpointCase):
    """The checkpoint id is a function of the row, so a crash is answerable."""

    def test_identity_is_the_same_for_the_same_three_fields(self) -> None:
        base = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "target": "t-1",
        }
        self.assertEqual(
            overseer_checkpoint.checkpoint_id(**base),
            overseer_checkpoint.checkpoint_id(**base),
        )

    def test_identity_moves_with_every_field_it_is_derived_from(self) -> None:
        base: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "target": "t-1",
        }
        identity = overseer_checkpoint.checkpoint_id(**base)
        for name, value in (
            ("plan_dir", self.new_plan("plan-other")),
            ("incident_id", self.incident_id + 1),
            ("target", "t-2"),
        ):
            moved = dict(base)
            moved[name] = value
            self.assertNotEqual(
                identity, overseer_checkpoint.checkpoint_id(**moved), msg=name
            )

    def test_the_message_carries_the_trailer_and_keeps_the_subject(self) -> None:
        message = overseer_checkpoint.checkpoint_message(MESSAGE, "id-1")
        self.assertTrue(message.startswith(MESSAGE))
        self.assertIn(f"{overseer_checkpoint.CHECKPOINT_TRAILER}: id-1", message)


class Attribution(_CheckpointCase):
    """Ownership is established from the run's own evidence, or not at all."""

    def test_a_recorded_path_is_owned(self) -> None:
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertIsNone(attribution.refusal)
        self.assertTrue(attribution.safe)
        self.assertEqual(attribution.owned, ("notes.md",))
        self.assertEqual(attribution.unattributed, ())
        self.assertEqual(attribution.baseline, self.head())

    def test_a_commit_artifact_supplies_the_baseline_and_the_paths(self) -> None:
        self.record_commit(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertIsNone(attribution.refusal)
        self.assertEqual(attribution.owned, ("notes.md",))

    def test_an_unrecorded_path_is_unattributed(self) -> None:
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")
        self.write("somebody-else.md", "not this run\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "changes_unattributed")
        self.assertEqual(attribution.unattributed, ("somebody-else.md",))
        self.assertIn("somebody-else.md", attribution.detail)

    def test_a_path_qualified_by_another_repository_is_not_owned(self) -> None:
        self.record_turn(paths=("notes.md",), alias="other")
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "baseline_unknown")
        self.assertEqual(attribution.activity, ())

    def test_a_single_repository_run_may_qualify_with_a_dot(self) -> None:
        self.record_turn(paths=("notes.md",), alias=".")
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertIsNone(attribution.refusal)
        self.assertEqual(attribution.owned, ("notes.md",))

    def test_a_clean_repository_has_nothing_to_checkpoint(self) -> None:
        self.record_turn(paths=("notes.md",))

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "nothing_to_commit")
        self.assertTrue(attribution.clean)
        self.assertEqual(attribution.dirty, ())

    def test_a_run_that_recorded_no_head_establishes_no_baseline(self) -> None:
        self.record_turn(guard=False)
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "baseline_unknown")

    def test_a_head_that_moved_is_not_a_baseline_to_measure_from(self) -> None:
        stale = self.head()
        self.record_turn(paths=("notes.md",), head=stale)
        self.write("elsewhere.md", "another actor\n")
        self.git("add", "elsewhere.md")
        self.git("commit", "-m", "somebody else committed")
        self.write("notes.md", "work in progress\n")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "baseline_moved")
        self.assertIn(stale, attribution.detail)

    def test_two_repositories_cannot_be_isolated_together(self) -> None:
        other = overseer_policy_client.Repository(alias="other", path=str(self.repo))

        attribution = self.attribute(repositories=(self.repository, other))

        self.assertEqual(attribution.refusal, "repository_ambiguous")

    def test_no_repository_is_refused_rather_than_defaulted(self) -> None:
        self.assertEqual(
            self.attribute(repositories=()).refusal, "repository_ambiguous"
        )

    def test_a_directory_that_is_not_a_worktree_is_unreadable(self) -> None:
        outside = self.root / "not-a-repo"
        outside.mkdir()
        elsewhere = overseer_policy_client.Repository(alias=ALIAS, path=str(outside))

        self.assertEqual(
            self.attribute(repositories=(elsewhere,)).refusal, "repository_unreadable"
        )

    def test_both_ends_of_a_rename_must_be_owned(self) -> None:
        self.write("notes.md", "work in progress\n")
        self.git("add", "notes.md")
        self.git("commit", "-m", "add notes")
        self.record_turn(paths=("renamed.md",))
        self.git("mv", "notes.md", "renamed.md")

        attribution = self.attribute()

        self.assertEqual(attribution.refusal, "changes_unattributed")
        self.assertIn("notes.md", attribution.unattributed)


class VerificationState(_CheckpointCase):
    """A checkpoint reports the checks the run recorded, and never more."""

    def test_no_recorded_verification_is_unrecorded(self) -> None:
        self.record_turn(paths=("notes.md",))

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "unrecorded")
        self.assertFalse(recorded.satisfied)

    def test_a_failing_verification_is_reported_as_failed(self) -> None:
        self.record_turn(
            paths=("notes.md",),
            verification={
                "session": "1",
                "passed": False,
                "reason": "check-1 failed",
                "failures": ["check-1"],
            },
        )

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "failed")
        self.assertEqual(recorded.failures, ("check-1",))
        self.assertFalse(recorded.satisfied)

    def test_an_unresolved_verification_is_not_a_pass(self) -> None:
        self.record_turn(
            paths=("notes.md",),
            verification={"session": "1", "passed": None, "reason": "interrupted"},
        )

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "unresolved")
        self.assertFalse(recorded.satisfied)

    def test_the_newest_recorded_verification_wins(self) -> None:
        self.record_turn(
            index=1,
            paths=("notes.md",),
            verification={"session": "1", "passed": True},
        )
        self.record_turn(
            index=2,
            paths=("notes.md",),
            verification={"session": "1", "passed": False},
        )

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "failed")

    def test_a_turn_that_verified_nothing_does_not_erase_the_last_one(self) -> None:
        self.record_turn(
            index=1,
            paths=("notes.md",),
            verification={"session": "1", "passed": True},
        )
        self.record_turn(index=2, paths=("notes.md",))

        recorded = overseer_checkpoint.read_verification(
            plan_dir=self.plan, run_id=RUN, state_dir=self.root
        )

        self.assertEqual(recorded.state, "passed")


class SafeCheckpoint(_CheckpointCase):
    """The whole path, when every change belongs to the run."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(
            paths=("notes.md",),
            verification={"session": "1", "passed": False, "failures": ["check-1"]},
        )
        self.write("notes.md", "work in progress\n")

    def test_a_prepared_checkpoint_commits_the_owned_set(self) -> None:
        adapter = self.adapter()
        action = self.action()
        before = self.head()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, self.head())
        self.assertNotEqual(self.head(), before)
        self.assertEqual(self.porcelain(), [])
        self.assertIn("notes.md", support.git_stdout(self.repo, "show", "--name-only"))

    def test_the_commit_carries_the_derived_checkpoint_trailer(self) -> None:
        adapter = self.adapter()
        action = self.action()

        adapter.prepare(action)
        adapter.execute(action)

        identity = overseer_checkpoint.checkpoint_id(
            plan_dir=self.plan,
            incident_id=self.incident_id,
            target=action.target,
        )
        body = self.commit_body()
        self.assertIn(MESSAGE, body)
        self.assertIn(f"{overseer_checkpoint.CHECKPOINT_TRAILER}: {identity}", body)

    def test_the_receipt_reports_the_verification_the_run_recorded(self) -> None:
        adapter = self.adapter()
        action = self.action()

        adapter.prepare(action)
        receipt = adapter.execute(action)

        self.assertIn("verification=failed", receipt.detail)
        self.assertNotIn("verification=passed", receipt.detail)

    def test_somebody_elses_staged_entry_is_left_staged(self) -> None:
        # Staging is where an unrelated actor's work is most easily destroyed:
        # a checkpoint that reset the index to make room for itself would erase
        # it silently. This one refuses instead, and the entry is still there.
        self.write("somebody-else.md", "not this run\n")
        self.git("add", "somebody-else.md")

        declined = self.adapter().prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("changes_unattributed", declined.detail)
        self.assertIn(
            "A  somebody-else.md",
            support.git_stdout(self.repo, "status", "--porcelain"),
        )

    def test_work_outside_the_repository_index_is_untouched(self) -> None:
        # An ignored scratch file is unrelated work that attribution never sees
        # and the commit must never carry away.
        self.write(".gitignore", "scratch/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore scratch")
        self.write("scratch/notes.txt", "somebody else's scratch\n")
        self.record_turn(index=2, paths=("notes.md",))

        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertNotIn(
            "scratch", support.git_stdout(self.repo, "show", "--name-only")
        )
        self.assertEqual(
            (self.repo / "scratch" / "notes.txt").read_text(encoding="utf-8"),
            "somebody else's scratch\n",
        )


class AlreadyClean(_CheckpointCase):
    """Nothing to checkpoint is refused, and refused before anything is staged."""

    def test_a_clean_repository_declines_preparation(self) -> None:
        self.record_turn(paths=("notes.md",))
        adapter = self.adapter()
        before = self.head()

        declined = adapter.prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("nothing_to_commit", declined.detail)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.porcelain(), [])

    def test_execution_without_preparation_commits_nothing(self) -> None:
        self.record_turn(paths=("notes.md",))
        adapter = self.adapter()
        before = self.head()

        receipt = adapter.execute(self.action())

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("not_prepared", receipt.detail)
        self.assertEqual(self.head(), before)


class AttributionRefusal(_CheckpointCase):
    """A change this run cannot be shown to own stops the whole checkpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")
        self.write("somebody-else.md", "not this run\n")

    def test_preparation_declines_and_leaves_the_worktree_alone(self) -> None:
        adapter = self.adapter()
        before = self.head()

        declined = adapter.prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("changes_unattributed", declined.detail)
        self.assertEqual(self.head(), before)
        self.assertEqual(
            sorted(self.porcelain()), ["?? notes.md", "?? somebody-else.md"]
        )

    def test_a_declined_preparation_leaves_nothing_to_execute(self) -> None:
        adapter = self.adapter()
        action = self.action()

        adapter.prepare(action)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("not_prepared", receipt.detail)


class ConcurrentEdits(_CheckpointCase):
    """The owned set moving between preparation and execution refuses, twice over."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

    def test_an_edit_after_staging_is_worktree_drift(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        before = self.head()

        self.write("notes.md", "the worker kept going\n")
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("worktree_drifted", receipt.detail)
        self.assertEqual(self.head(), before)
        self.assertEqual(
            (self.repo / "notes.md").read_text(encoding="utf-8"),
            "the worker kept going\n",
        )

    def test_a_restage_after_staging_is_a_snapshot_mismatch(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)
        before = self.head()

        self.write("notes.md", "the worker kept going\n")
        self.git("add", "notes.md")
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("snapshot_mismatch", receipt.detail)
        self.assertEqual(self.head(), before)

    def test_a_commit_landing_in_the_window_moves_the_baseline(self) -> None:
        adapter = self.adapter()
        action = self.action()
        self.assertTrue(adapter.prepare(action).ready)

        self.write("elsewhere.md", "another actor\n")
        self.git("add", "elsewhere.md")
        self.git("commit", "-m", "somebody else committed")
        landed = self.head()

        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("baseline_moved", receipt.detail)
        self.assertEqual(self.head(), landed)


class RevokedAuthority(_CheckpointCase):
    """No ruling, the wrong ruling, or a ruling withdrawn mid-flight commits nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

    def test_no_ruling_declines_before_anything_is_staged(self) -> None:
        declined = self.adapter(None).prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_absent", declined.detail)
        self.assertEqual(self.porcelain(), ["?? notes.md"])

    def test_a_ruling_about_another_operation_is_silent_here(self) -> None:
        adapter = self.adapter(self.ruling(operation="guidance"))
        declined = adapter.prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_silent", declined.detail)

    def test_a_ruling_about_another_plan_is_unscoped(self) -> None:
        other = self.new_plan("plan-other")
        declined = self.adapter(self.ruling(plan_dir=other)).prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_unscoped", declined.detail)

    def test_a_refusing_ruling_declines(self) -> None:
        declined = self.adapter(
            self.ruling(authorized=False, refusal="no_decision")
        ).prepare(self.action())

        self.assertFalse(declined.ready)
        self.assertIn("authority_refused", declined.detail)

    def test_a_decision_replaced_between_prepare_and_execute_refuses(self) -> None:
        adapter = self.adapter(self.ruling(), self.ruling(decision_id="d-2"))
        action = self.action()
        before = self.head()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("authority_superseded", receipt.detail)
        self.assertEqual(self.head(), before)

    def test_a_decision_withdrawn_between_prepare_and_execute_refuses(self) -> None:
        adapter = self.adapter(self.ruling(), None)
        action = self.action()
        before = self.head()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("authority_absent", receipt.detail)
        self.assertEqual(self.head(), before)

    def test_an_empty_message_is_not_a_checkpoint(self) -> None:
        declined = self.adapter().prepare(self.action(message="   "))

        self.assertFalse(declined.ready)
        self.assertIn("message_empty", declined.detail)


class CrashReconciliation(_CheckpointCase):
    """A row a dead process left behind, answered from the row alone."""

    def setUp(self) -> None:
        super().setUp()
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")

    def commit_then_forget(
        self,
    ) -> tuple[overseer_store_models.ActionRecord, overseer_dispatch.Action, str]:
        """Make a real checkpoint, then keep only what the ledger persisted."""
        adapter = self.adapter()
        action = self.action()
        record = self.attempted(action)
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed")
        return (record, action, receipt.locator)

    def test_a_committed_checkpoint_reconciles_as_executed(self) -> None:
        record, _, sha = self.commit_then_forget()

        # A new adapter, as a restarted process would build: it never saw the
        # commit and holds no preparation for it.
        receipt = self.adapter().reconcile(record)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, sha)
        self.assertTrue(record.in_doubt)

    def test_an_uncommitted_checkpoint_reconciles_as_failed(self) -> None:
        record = self.attempted(self.action())

        receipt = self.adapter().reconcile(record)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("commit_absent", receipt.detail)

    def test_an_unreadable_repository_is_uncertain_rather_than_absent(self) -> None:
        record = self.attempted(self.action())
        shutil.rmtree(self.repo)

        receipt = self.adapter().reconcile(record)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("repository_unreadable", receipt.detail)

    def test_an_ambiguous_repository_set_is_uncertain(self) -> None:
        record = self.attempted(self.action())

        receipt = self.adapter(repositories=()).reconcile(record)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("repository_ambiguous", receipt.detail)

    def test_another_plans_checkpoint_is_not_read_as_ours(self) -> None:
        record, _, _ = self.commit_then_forget()
        elsewhere = overseer_store_models.ActionRecord(
            action_id=record.action_id,
            plan_dir=self.new_plan("plan-other"),
            incident_id=record.incident_id,
            operation=record.operation,
            target=record.target,
            status=record.status,
            epoch=record.epoch,
            config_fingerprint=record.config_fingerprint,
            request_digest=record.request_digest,
            progress_token=record.progress_token,
            attempted_at=record.attempted_at,
            settled_at=record.settled_at,
            receipt=record.receipt,
        )

        receipt = self.adapter().reconcile(elsewhere)

        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("commit_absent", receipt.detail)

    def test_a_row_for_another_operation_is_never_answered_here(self) -> None:
        record = self.attempted(self.action())
        other = overseer_store_models.ActionRecord(
            action_id=record.action_id,
            plan_dir=record.plan_dir,
            incident_id=record.incident_id,
            operation="resume",
            target=record.target,
            status=record.status,
            epoch=record.epoch,
            config_fingerprint=record.config_fingerprint,
            request_digest=record.request_digest,
            progress_token=record.progress_token,
            attempted_at=record.attempted_at,
            settled_at=record.settled_at,
            receipt=record.receipt,
        )

        receipt = self.adapter().reconcile(other)

        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("payload_mismatch", receipt.detail)

    def test_a_retried_checkpoint_finds_the_first_commit(self) -> None:
        record, action, sha = self.commit_then_forget()

        # The retry re-attributes from scratch: the worktree is clean now, so
        # the second preparation declines rather than committing twice.
        adapter = self.adapter()
        declined = adapter.prepare(action)

        self.assertFalse(declined.ready)
        self.assertIn("nothing_to_commit", declined.detail)
        self.assertEqual(
            overseer_checkpoint.find_checkpoint(
                self.repo,
                overseer_checkpoint.checkpoint_id(
                    plan_dir=record.plan_dir,
                    incident_id=record.incident_id,
                    target=record.target,
                ),
            ),
            sha,
        )


class PorcelainReading(_CheckpointCase):
    """The dirty set is read exactly, because it is what ownership is tested on."""

    def test_a_deleted_path_is_dirty_and_can_be_owned(self) -> None:
        self.record_turn(paths=("README.md",))
        (self.repo / "README.md").unlink()

        attribution = self.attribute()

        self.assertEqual(attribution.owned, ("README.md",))

    def test_a_checkpoint_can_commit_a_deletion(self) -> None:
        self.record_turn(paths=("README.md",))
        (self.repo / "README.md").unlink()
        adapter = self.adapter()
        action = self.action()

        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)

        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(self.porcelain(), [])
        self.assertNotIn("README.md", support.git_stdout(self.repo, "ls-files"))

    def test_an_unreadable_status_is_never_read_as_clean(self) -> None:
        missing = overseer_policy_client.Repository(
            alias=ALIAS, path=str(self.root / "absent")
        )

        attribution = self.attribute(repositories=(missing,))

        self.assertEqual(attribution.refusal, "repository_unreadable")
        self.assertFalse(attribution.safe)


class ReusedPrimitives(_CheckpointCase):
    """The commit surface is the one `asl commit` already publishes."""

    def test_the_snapshot_is_the_commit_modules_own_token(self) -> None:
        self.record_turn(paths=("notes.md",))
        self.write("notes.md", "work in progress\n")
        adapter = self.adapter()
        action = self.action()

        self.assertTrue(adapter.prepare(action).ready)
        prepared = adapter.prepared[next(iter(adapter.prepared))]

        self.assertEqual(
            prepared.snapshot,
            commit_message.pathspec_snapshot(self.repo, prepared.paths),
        )
        self.assertEqual(len(prepared.snapshot), commit_message.SNAPSHOT_TOKEN_LENGTH)


if __name__ == "__main__":
    unittest.main()
