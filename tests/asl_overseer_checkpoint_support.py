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


__all__ = [
    "ALIAS",
    "Any",
    "MESSAGE",
    "RUN",
    "_CheckpointCase",
    "assert_no_runner_import",
    "commit_message",
    "get_args",
    "overseer_checkpoint",
    "overseer_dispatch",
    "overseer_policy_client",
    "overseer_store_models",
    "shutil",
    "support",
    "unittest",
]
