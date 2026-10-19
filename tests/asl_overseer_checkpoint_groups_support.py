"""Focused tests for the overseer's multi-repository checkpoint groups.

``overseer_checkpoint`` already guarantees that one repository is only
committed from evidence the run itself published. What this module adds is
everything that becomes possible once a checkpoint spans a *registry*, and the
fixtures are grouped by the way each of those additions can go wrong.

*Mixed permission.* A registry AgentMux permits only part of is refused whole.
Preparation stops before anything is staged, no durable group is opened, and
the refusal names the repository that was excluded --- because committing the
permitted two would publish a claim about the run's work that the third
contradicts. The same refusal covers a policy that could not be asked at all.

*Partial staging.* Staging is repository by repository and can stop partway. A
repository whose index is locked by another git process fails to stage, and the
refusal names both it and the repositories staged before it, so the partial
state is reported rather than left to be discovered. Nothing is committed and
no group is opened.

*A failed second commit.* When the second of three repositories refuses ---
here because somebody is still editing a file the group was about to commit ---
the first repository's commit stays exactly where it is. The receipt reports the partial
result, the durable group keeps the successful commit and still names the two
outstanding repositories, and the third repository is neither committed nor
cleaned.

*Revocation between commits.* Permission is asked again before every single
commit, so a repository withdrawn after the first commit stops the second. The
evidence of the first survives the withdrawal in the ledger, which is the whole
reason a group is saved without a grant: a revocation must cost the next
commit, never the record of the last one. A policy fingerprint that moves
mid-group is the same fact arriving a different way.

*Restart at each transaction boundary.* The durable group is rewritten at every
boundary, and a crash at each of them in turn is recovered by a fresh process
with a fresh ledger handle. In every case each repository ends with exactly one
commit carrying its own checkpoint trailer: recovery reconciles the commits a
dead process made instead of repeating them, and never rolls one back.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, get_args

from agent_session_loop import (
    commit_groups,
    overseer_checkpoint,
    overseer_checkpoint_groups,
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
ALIASES = ("alpha", "beta", "gamma")
FINGERPRINT = "fp-first"
DIGEST = "rd-1"
MOMENT = "2026-01-01T00:00:00+00:00"
MESSAGE = "chore: checkpoint the work in progress"

#: How many times a three-repository group with nothing to recover persists.
#: One resume, one per repository committed, and one completion.
FULL_RUN_BOUNDARIES = 5


class _Crash(RuntimeError):
    """The process dying at a transaction boundary, on purpose."""


class _GroupCase(unittest.TestCase):
    """A real state root, a real ledger, three real git repositories, one run."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-groups-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._close)
        self.ledgers: list[overseer_store_ledger.Ledger] = []
        self.ledger = self.open_ledger()
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.plan = self.new_plan("plan-a")
        self.repos: dict[str, Path] = {}
        for alias in ALIASES:
            path = self.root / "repos" / alias
            support.init_git_repo(path)
            self.repos[alias] = path
        self.repositories = tuple(
            overseer_policy_client.Repository(alias=alias, path=str(path))
            for alias, path in self.repos.items()
        )
        self.incident_id = self.ledger.open_incident(
            grant=None,
            plan_dir=self.plan,
            run_id=RUN,
            kind="run_paused",
            summary="paused with work in progress",
        ).incident_id
        self.changes: dict[str, list[str]] = {}
        self.asked: list[dict[str, Any]] = []
        self.turn_index = 0

    def open_ledger(self) -> overseer_store_ledger.Ledger:
        """A ledger handle of its own, so a restart can open the same file."""
        ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        self.ledgers.append(ledger)
        return ledger

    def _close(self) -> None:
        for ledger in self.ledgers:
            with contextlib.suppress(sqlite3.ProgrammingError):
                ledger.close()

    # -- the plan side -----------------------------------------------------

    def new_plan(self, name: str) -> str:
        path = self.root / "plans" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def run_dir(self) -> Path:
        return state.plan_state_dir(self.plan, self.root) / state.RUNS_DIR_NAME / RUN

    def change(self, alias: str, name: str = "work.txt", text: str = "wip\n") -> None:
        """Dirty one repository, remembering the path for the next artifact."""
        path = self.repos[alias] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.changes.setdefault(alias, []).append(name)

    def publish(self, verification: dict[str, Any] | None = None) -> None:
        """Publish one turn artifact attributing every remembered change."""
        self.turn_index += 1
        record: dict[str, Any] = {
            "turn_index": self.turn_index,
            "session_id": "1",
            "workspace_write_guard": {
                "changed": {
                    alias: {
                        "head_before": self.head(alias),
                        "head_after": self.head(alias),
                        "qualified_paths": [
                            f"{alias}:{name}" for name in dict.fromkeys(names)
                        ],
                    }
                    for alias, names in self.changes.items()
                }
            },
        }
        if verification is not None:
            record["verification"] = verification
        directory = self.run_dir() / f"turn-{self.turn_index:04d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / state.RESULT_FILE_NAME).write_text(
            json.dumps(record), encoding="utf-8"
        )

    def prime(self, *aliases: str) -> None:
        """Give every named repository one attributed change, ready to commit."""
        for alias in aliases or ALIASES:
            self.change(alias)
        self.publish()

    # -- the repository side -----------------------------------------------

    def head(self, alias: str) -> str:
        return support.git_stdout(self.repos[alias], "rev-parse", "HEAD")

    def porcelain(self, alias: str) -> list[str]:
        return [
            line
            for line in support.git_stdout(
                self.repos[alias], "status", "--porcelain", "--untracked-files=all"
            ).splitlines()
            if line
        ]

    def staged(self, alias: str) -> list[str]:
        """Every path currently in this repository's index but not in HEAD."""
        return [
            line.split("\t", 1)[-1]
            for line in support.git_stdout(
                self.repos[alias], "diff", "--cached", "--name-only"
            ).splitlines()
            if line
        ]

    def carrying(self, alias: str, identity: str) -> list[str]:
        """Every commit in ``alias`` carrying ``identity``, newest first."""
        return [
            line
            for line in support.git_stdout(
                self.repos[alias],
                "log",
                "--format=%H",
                "--fixed-strings",
                f"--grep={overseer_checkpoint.CHECKPOINT_TRAILER}: {identity}",
            ).splitlines()
            if line
        ]

    def body(self, alias: str) -> str:
        return support.git_stdout(self.repos[alias], "log", "-1", "--format=%B")

    def lock_index(self, alias: str) -> Path:
        """Hold this repository's index the way a concurrent git process would."""
        lock = self.repos[alias] / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        return lock

    # -- the overseer side -------------------------------------------------

    def ruling(self, **kwargs: Any) -> overseer_decisions.Ruling:
        arguments: dict[str, Any] = {
            "operation": "commit",
            "decision_id": "d-1",
            "stance": "allow",
            "at": MOMENT,
            "authorized": True,
            "refusal": None,
            "with_decision": True,
        }
        arguments.update(kwargs)
        scope = overseer_decisions.Scope(
            project=PROJECT,
            plan_dir=self.plan,
            run_id=RUN,
            incident=str(self.incident_id),
        )
        decision = overseer_decisions.Decision(
            decision_id=arguments["decision_id"],
            statement_id="s-1",
            topic="operations",
            stance=arguments["stance"],
            scope=scope,
            operations=(arguments["operation"],),
            at=arguments["at"],
            moment=datetime.fromisoformat(arguments["at"]),
            sources=("s-1",),
            text="check the work in",
        )
        return overseer_decisions.Ruling(
            operation=arguments["operation"],
            scope=scope,
            authorized=arguments["authorized"],
            refusal=arguments["refusal"],
            decision=decision if arguments["with_decision"] else None,
        )

    def decision(
        self,
        *,
        allowed: bool = True,
        available: bool = True,
        refused: tuple[str, ...] = (),
        fingerprint: str = FINGERPRINT,
        digest: str = DIGEST,
    ) -> overseer_policy_client.Decision:
        """One AgentMux answer about the whole registry."""
        permitted = allowed and available and not refused
        return overseer_policy_client.Decision(
            plan_dir=self.plan,
            access=overseer_policy_client.ACCESS_ACTION,
            repositories=self.repositories,
            allowed=permitted,
            available=available,
            reason="opted_in" if permitted else "excluded",
            failure=None,
            summary=f"{len(self.repositories)} repositories",
            config_fingerprint=fingerprint if available else "",
            request_digest=digest if available else "",
            contract_version=1 if available else None,
            exit_status=0 if permitted else 1,
            outcomes=tuple(
                overseer_policy_client.RepositoryOutcome(
                    alias=repository.alias,
                    path=repository.path,
                    resolved_path=repository.path,
                    allowed=repository.alias not in refused,
                    reason="excluded" if repository.alias in refused else "opted_in",
                    detail="the operator withdrew this repository"
                    if repository.alias in refused
                    else "",
                )
                for repository in self.repositories
            ),
        )

    def policy(
        self, *decisions: overseer_policy_client.Decision
    ) -> overseer_checkpoint_groups.Authorize:
        """An authorize seam answering each call from a scripted queue.

        Scripted rather than constant because the window this adapter exists to
        notice is *between* two commits, and that window can only be described
        by an answer that changes while the group is running.
        """
        answers = list(decisions) or [self.decision()]

        def ask(**kwargs: Any) -> overseer_policy_client.Decision:
            self.asked.append(kwargs)
            return answers.pop(0) if len(answers) > 1 else answers[0]

        return ask

    def adapter(
        self,
        *,
        rulings: tuple[overseer_decisions.Ruling | None, ...] = (),
        decisions: tuple[overseer_policy_client.Decision, ...] = (),
        repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
        ledger: overseer_store_ledger.Ledger | None = None,
    ) -> overseer_checkpoint_groups.GroupAdapter:
        answers = list(rulings) or [self.ruling()]

        def establish(
            _action: overseer_dispatch.Action,
        ) -> overseer_decisions.Ruling | None:
            return answers.pop(0) if len(answers) > 1 else answers[0]

        declared = self.repositories if repositories is None else repositories

        def resolve(
            _plan_dir: str, *, state_dir: Any = None
        ) -> tuple[overseer_policy_client.Repository, ...]:
            return declared

        return overseer_checkpoint_groups.GroupAdapter(
            authority=establish,
            ledger=ledger or self.ledger,
            state_dir=self.root,
            repositories=resolve,
            authorize=self.policy(*decisions),
        )

    def action(
        self,
        *,
        message: str = MESSAGE,
        repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
    ) -> overseer_dispatch.Action:
        return overseer_dispatch.Action(
            plan_dir=self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=overseer_dispatch.Commit(message=message),
            token="token-1",
            scope="sha256:scope",
            repositories=self.repositories if repositories is None else repositories,
        )

    def key(self, action: overseer_dispatch.Action) -> str:
        return overseer_checkpoint_groups.group_key(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
        )

    def identity(self, action: overseer_dispatch.Action, alias: str) -> str:
        return overseer_checkpoint_groups.repository_identity(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
            alias=alias,
        )

    def group(
        self,
        action: overseer_dispatch.Action,
        ledger: overseer_store_ledger.Ledger | None = None,
    ) -> overseer_store_models.CheckpointGroupRecord | None:
        return (ledger or self.ledger).checkpoint_group(self.key(action))

    def attempted(
        self, action: overseer_dispatch.Action
    ) -> overseer_store_models.ActionRecord:
        """The ledger row a dispatch writes before the adapter is called."""
        return self.ledger.attempt_action(
            grant=overseer_policy_client.Grant(
                plan_dir=self.plan,
                access=overseer_policy_client.ACCESS_ACTION,
                request_digest=DIGEST,
                config_fingerprint=FINGERPRINT,
            ),
            plan_dir=action.plan_dir,
            operation="commit",
            target=action.target,
            incident_id=action.incident_id,
        )


__all__ = ["contextlib", "json", "shutil", "sqlite3", "tempfile", "unittest", "datetime", "Path", "Any", "get_args", "commit_groups", "overseer_checkpoint", "overseer_checkpoint_groups", "overseer_decisions", "overseer_dispatch", "overseer_policy_client", "overseer_store_ledger", "overseer_store_models", "state", "support", "assert_no_runner_import", "PROJECT", "RUN", "ALIASES", "FINGERPRINT", "DIGEST", "MOMENT", "MESSAGE", "FULL_RUN_BOUNDARIES", "_Crash", "_GroupCase"]  # fmt: skip
