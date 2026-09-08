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
    overseer_store,
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
        self.ledgers: list[overseer_store.Ledger] = []
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

    def open_ledger(self) -> overseer_store.Ledger:
        """A ledger handle of its own, so a restart can open the same file."""
        ledger = overseer_store.open_ledger(state_dir=self.root)
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
        ledger: overseer_store.Ledger | None = None,
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
        ledger: overseer_store.Ledger | None = None,
    ) -> overseer_store.CheckpointGroupRecord | None:
        return (ledger or self.ledger).checkpoint_group(self.key(action))

    def attempted(
        self, action: overseer_dispatch.Action
    ) -> overseer_store.ActionRecord:
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


class ClosedVocabulary(_GroupCase):
    """The refusal words and the phases are a contract, not a set of strings."""

    def test_refusals_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_checkpoint_groups.REFUSALS),
            set(get_args(overseer_checkpoint_groups.Refusal)),
        )
        self.assertEqual(
            len(overseer_checkpoint_groups.REFUSALS),
            len(set(overseer_checkpoint_groups.REFUSALS)),
        )

    def test_every_single_repository_refusal_is_sayable_here_too(self) -> None:
        self.assertLessEqual(
            set(overseer_checkpoint.REFUSALS),
            set(overseer_checkpoint_groups.REFUSALS),
        )

    def test_the_registry_holds_the_commit_operation_only(self) -> None:
        registry = overseer_checkpoint_groups.adapters(
            authority=lambda _action: self.ruling(),
            ledger=self.ledger,
            state_dir=self.root,
        )
        self.assertTrue(registry.supports("commit"))
        for operation in ("reply", "guidance", "verify", "resume"):
            self.assertFalse(registry.supports(operation), msg=operation)

    def test_groups_are_written_under_a_scope_commit_groups_knows(self) -> None:
        self.assertIn(
            overseer_checkpoint_groups.GROUP_SCOPE, commit_groups.GROUP_SCOPES
        )
        self.assertNotIn(
            overseer_checkpoint_groups.GROUP_SCOPE, commit_groups.SESSION_GROUP_SCOPES
        )

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_checkpoint_groups)


class DerivedIdentity(_GroupCase):
    """Group and repository identities are functions of the row, not of a run."""

    def test_the_group_key_is_the_same_for_the_same_three_fields(self) -> None:
        base = {"plan_dir": self.plan, "incident_id": self.incident_id, "target": "t-1"}
        self.assertEqual(
            overseer_checkpoint_groups.group_key(**base),
            overseer_checkpoint_groups.group_key(**base),
        )

    def test_the_group_key_moves_with_every_field_it_is_derived_from(self) -> None:
        base: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "target": "t-1",
        }
        original = overseer_checkpoint_groups.group_key(**base)
        for name, value in (
            ("plan_dir", self.new_plan("plan-other")),
            ("incident_id", self.incident_id + 1),
            ("target", "t-2"),
        ):
            moved = dict(base)
            moved[name] = value
            self.assertNotEqual(
                original, overseer_checkpoint_groups.group_key(**moved), msg=name
            )

    def test_each_repository_gets_an_identity_of_its_own(self) -> None:
        action = self.action()
        identities = {alias: self.identity(action, alias) for alias in ALIASES}
        self.assertEqual(len(set(identities.values())), len(ALIASES))

    def test_a_group_commit_is_never_found_by_a_lone_checkpoint_id(self) -> None:
        action = self.action()
        lone = overseer_checkpoint.checkpoint_id(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
        )
        for alias in ALIASES:
            self.assertNotEqual(lone, self.identity(action, alias), msg=alias)


class CommitsTheWholeRegistry(_GroupCase):
    """The ordinary case: three attributed repositories, one group, one pass."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_every_repository_is_committed_once_under_its_own_identity(self) -> None:
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed")
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))), 1, msg=alias
            )
            self.assertEqual(self.porcelain(alias), [], msg=alias)

    def test_the_receipt_locates_the_durable_group(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.locator, self.key(action))

    def test_the_commit_says_what_the_action_asked_and_carries_the_trailer(
        self,
    ) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        body = self.body("alpha")
        self.assertIn(MESSAGE, body)
        self.assertIn(
            f"{overseer_checkpoint.CHECKPOINT_TRAILER}: "
            f"{self.identity(action, 'alpha')}",
            body,
        )

    def test_the_durable_group_is_settled_and_owes_nothing(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        stored = self.group(action)
        assert stored is not None
        self.assertTrue(stored.settled)
        self.assertEqual(stored.phase, "complete")
        self.assertEqual(stored.outstanding, ())
        self.assertEqual(set(stored.committed), set(ALIASES))

    def test_preparation_opens_the_group_before_anything_is_committed(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        stored = self.group(action)
        assert stored is not None
        self.assertEqual(stored.phase, "prepared")
        self.assertFalse(stored.settled)
        self.assertEqual(stored.outstanding, ALIASES)
        self.assertEqual(stored.committed, ())
        for alias in ALIASES:
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])

    def test_the_recorded_phase_is_always_one_the_module_publishes(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        stored = self.group(action)
        assert stored is not None
        self.assertIn(stored.phase, overseer_checkpoint_groups.GROUP_PHASES)

    def test_the_receipt_reports_the_verification_the_run_recorded(self) -> None:
        self.publish(verification={"passed": False, "session": "1", "failures": ["x"]})
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertIn("verification=failed", receipt.detail)

    def test_a_clean_repository_is_skipped_rather_than_committed(self) -> None:
        self.changes.clear()
        self.turn_index = 0
        shutil.rmtree(self.run_dir(), ignore_errors=True)
        (self.repos["gamma"] / "work.txt").unlink()
        self.prime("alpha", "beta")
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(self.carrying("gamma", self.identity(action, "gamma")), [])
        stored = self.group(action)
        assert stored is not None
        self.assertEqual(set(stored.committed), {"alpha", "beta"})

    def test_a_registry_with_nothing_dirty_is_refused_before_staging(self) -> None:
        for alias in ALIASES:
            (self.repos[alias] / "work.txt").unlink()
        declined = self.adapter().prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("nothing_to_commit", declined.detail)
        self.assertIsNone(self.group(self.action()))

    def test_an_action_naming_no_repository_is_refused(self) -> None:
        declined = self.adapter().prepare(self.action(repositories=()))
        self.assertFalse(declined.ready)
        self.assertIn("registry_empty", declined.detail)


class MixedPermission(_GroupCase):
    """A registry AgentMux permits only part of is refused as a whole."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_one_excluded_repository_refuses_the_whole_group(self) -> None:
        adapter = self.adapter(decisions=(self.decision(refused=("beta",)),))
        declined = adapter.prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("repository_excluded", declined.detail)
        self.assertIn("beta", declined.detail)

    def test_a_mixed_answer_stages_and_commits_nothing_anywhere(self) -> None:
        action = self.action()
        adapter = self.adapter(decisions=(self.decision(refused=("beta",)),))
        adapter.prepare(action)
        for alias in ALIASES:
            self.assertEqual(self.staged(alias), [], msg=alias)
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])
        self.assertIsNone(self.group(action))

    def test_the_permitted_repositories_are_left_exactly_as_they_were(self) -> None:
        heads = {alias: self.head(alias) for alias in ALIASES}
        adapter = self.adapter(decisions=(self.decision(refused=("gamma",)),))
        adapter.prepare(self.action())
        for alias in ALIASES:
            self.assertEqual(self.head(alias), heads[alias], msg=alias)
            self.assertEqual(self.porcelain(alias), ["?? work.txt"], msg=alias)

    def test_a_policy_that_cannot_be_asked_is_not_a_permission(self) -> None:
        adapter = self.adapter(decisions=(self.decision(available=False),))
        declined = adapter.prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("policy_unavailable", declined.detail)

    def test_the_whole_registry_is_the_question_that_gets_asked(self) -> None:
        adapter = self.adapter()
        adapter.prepare(self.action())
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(
            tuple(repository.alias for repository in self.asked[0]["repositories"]),
            ALIASES,
        )
        self.assertEqual(self.asked[0]["access"], overseer_policy_client.ACCESS_ACTION)


class PartialStaging(_GroupCase):
    """Staging stops partway, and says so rather than hiding what it did."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_a_repository_that_cannot_be_staged_refuses_the_group(self) -> None:
        self.lock_index("beta")
        declined = self.adapter().prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("staging_failed", declined.detail)
        self.assertIn("beta", declined.detail)

    def test_the_refusal_names_the_repositories_already_staged(self) -> None:
        self.lock_index("beta")
        declined = self.adapter().prepare(self.action())
        self.assertIn("already staged alpha", declined.detail)
        self.assertEqual(self.staged("alpha"), ["work.txt"])

    def test_a_partly_staged_registry_opens_no_durable_group(self) -> None:
        self.lock_index("beta")
        action = self.action()
        self.adapter().prepare(action)
        self.assertIsNone(self.group(action))

    def test_nothing_is_committed_after_staging_stops(self) -> None:
        self.lock_index("beta")
        action = self.action()
        heads = {alias: self.head(alias) for alias in ALIASES}
        self.adapter().prepare(action)
        for alias in ALIASES:
            self.assertEqual(self.head(alias), heads[alias], msg=alias)
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])

    def test_a_repository_after_the_failure_is_never_touched(self) -> None:
        self.lock_index("beta")
        self.adapter().prepare(self.action())
        self.assertEqual(self.staged("gamma"), [])

    def test_execution_without_a_preparation_refuses(self) -> None:
        self.lock_index("beta")
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("not_prepared", receipt.detail)


class AFailedSecondCommit(_GroupCase):
    """The first commit stands, the failure is reported, nothing is undone."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()
        self.action_under_test = self.action()
        self.adapter_under_test = self.adapter()
        self.assertTrue(self.adapter_under_test.prepare(self.action_under_test).ready)
        # Move beta out from under what preparation measured, which is the
        # ordinary way a middle repository refuses: somebody is still editing an
        # owned file while the group is being made.
        (self.repos["beta"] / "work.txt").write_text(
            "somebody else is still editing\n", encoding="utf-8"
        )
        self.receipt = self.adapter_under_test.execute(self.action_under_test)

    def test_the_group_reports_a_failure_rather_than_an_execution(self) -> None:
        self.assertEqual(self.receipt.outcome, "failed")
        self.assertIn("worktree_drifted", self.receipt.detail)
        self.assertIn("beta", self.receipt.detail)

    def test_the_first_repository_keeps_the_commit_it_earned(self) -> None:
        self.assertEqual(
            len(self.carrying("alpha", self.identity(self.action_under_test, "alpha"))),
            1,
        )
        self.assertEqual(self.porcelain("alpha"), [])

    def test_the_receipt_reports_the_partial_result(self) -> None:
        self.assertIn("committed alpha", self.receipt.detail)
        self.assertIn("outstanding", self.receipt.detail)
        self.assertIn("gamma", self.receipt.detail)

    def test_the_repository_after_the_failure_is_neither_committed_nor_cleaned(
        self,
    ) -> None:
        self.assertEqual(
            self.carrying("gamma", self.identity(self.action_under_test, "gamma")), []
        )
        self.assertEqual(self.porcelain("gamma"), ["A  work.txt"])

    def test_the_durable_group_keeps_the_success_and_names_what_is_owed(self) -> None:
        stored = self.group(self.action_under_test)
        assert stored is not None
        self.assertFalse(stored.settled)
        self.assertEqual(stored.phase, "repository-failed")
        self.assertEqual(stored.committed, ("alpha",))
        self.assertEqual(stored.outstanding, ("beta", "gamma"))

    def test_the_failed_repository_is_named_in_the_durable_group(self) -> None:
        stored = self.group(self.action_under_test)
        assert stored is not None
        self.assertEqual(stored.record["failed_alias"], "beta")

    def test_a_retry_finishes_the_group_without_committing_alpha_again(self) -> None:
        # Re-attribute beta from where it now is, which is what a supervisor
        # would do after reconciling the repository somebody else moved.
        self.publish()
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed", msg=receipt.detail)
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))), 1, msg=alias
            )


class RevocationBetweenCommits(_GroupCase):
    """Permission is asked again per commit, and its withdrawal costs no record."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def _revoked(self) -> tuple[overseer_dispatch.Action, overseer_dispatch.Receipt]:
        """Prepare under a full grant, then withdraw beta before its commit."""
        action = self.action()
        adapter = self.adapter(
            decisions=(
                self.decision(),
                self.decision(),
                self.decision(refused=("beta",)),
            )
        )
        self.assertTrue(adapter.prepare(action).ready)
        return action, adapter.execute(action)

    def test_the_withdrawn_repository_is_not_committed(self) -> None:
        action, receipt = self._revoked()
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("repository_excluded", receipt.detail)
        self.assertEqual(self.carrying("beta", self.identity(action, "beta")), [])

    def test_the_commit_made_before_the_revocation_is_not_rolled_back(self) -> None:
        action, _ = self._revoked()
        self.assertEqual(len(self.carrying("alpha", self.identity(action, "alpha"))), 1)

    def test_the_ledger_still_holds_the_evidence_of_that_commit(self) -> None:
        action, _ = self._revoked()
        stored = self.group(action)
        assert stored is not None
        self.assertEqual(stored.committed, ("alpha",))
        self.assertEqual(stored.outstanding, ("beta", "gamma"))
        self.assertEqual(stored.phase, "repository-failed")

    def test_permission_is_asked_once_per_commit_and_once_to_prepare(self) -> None:
        self._revoked()
        self.assertEqual(len(self.asked), 3)

    def test_a_policy_that_moves_mid_group_stops_the_next_commit(self) -> None:
        action = self.action()
        adapter = self.adapter(
            decisions=(
                self.decision(),
                self.decision(),
                self.decision(fingerprint="fp-second"),
            )
        )
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("policy_superseded", receipt.detail)
        self.assertEqual(len(self.carrying("alpha", self.identity(action, "alpha"))), 1)
        self.assertEqual(self.carrying("gamma", self.identity(action, "gamma")), [])

    def test_a_decision_replaced_between_preparation_and_execution_refuses(
        self,
    ) -> None:
        action = self.action()
        adapter = self.adapter(
            rulings=(self.ruling(), self.ruling(decision_id="d-2")),
        )
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("authority_superseded", receipt.detail)
        for alias in ALIASES:
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])

    def test_an_authority_that_refuses_never_prepares(self) -> None:
        for name, kwargs in (
            ("authority_absent", {"with_decision": False}),
            ("authority_refused", {"authorized": False}),
            ("authority_held", {"stance": "deny"}),
        ):
            with self.subTest(name):
                adapter = self.adapter(rulings=(self.ruling(**kwargs),))
                declined = adapter.prepare(self.action())
                self.assertFalse(declined.ready)
                self.assertIn(name, declined.detail)


class RestartAtEachBoundary(_GroupCase):
    """A crash at every persistence point is recovered without duplication."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def _crash_after(self, boundaries: int) -> None:
        """Let the group persist ``boundaries`` times, then kill the process."""
        real = self.ledger.save_checkpoint_group
        seen = 0

        def guarded(**kwargs: Any) -> Any:
            nonlocal seen
            seen += 1
            if seen > boundaries:
                raise _Crash(str(kwargs.get("phase")))
            return real(**kwargs)

        self.ledger.save_checkpoint_group = guarded  # type: ignore[method-assign]

    def _restart_after(self, boundaries: int) -> None:
        """Crash at one boundary, then finish the group in a fresh process."""
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        self._crash_after(boundaries)
        with self.assertRaises(_Crash):
            adapter.execute(action)

        successor = self.adapter(ledger=self.open_ledger())
        receipt = successor.execute(action)
        self.assertEqual(receipt.outcome, "executed", msg=receipt.detail)
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))),
                1,
                msg=f"{alias} after {boundaries} boundaries",
            )
            self.assertEqual(self.porcelain(alias), [], msg=alias)
        stored = self.group(action, self.ledgers[-1])
        assert stored is not None
        self.assertTrue(stored.settled)
        self.assertEqual(set(stored.committed), set(ALIASES))

    def test_restart_before_any_repository_is_reached(self) -> None:
        self._restart_after(0)

    def test_restart_after_the_group_was_resumed(self) -> None:
        self._restart_after(1)

    def test_restart_after_the_first_repository_committed(self) -> None:
        self._restart_after(2)

    def test_restart_after_the_second_repository_committed(self) -> None:
        self._restart_after(3)

    def test_restart_after_the_last_repository_committed(self) -> None:
        self._restart_after(4)

    def test_a_full_pass_persists_at_every_boundary_it_publishes(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        phases: list[str] = []
        real = self.ledger.save_checkpoint_group

        def watched(**kwargs: Any) -> Any:
            phases.append(str(kwargs["phase"]))
            return real(**kwargs)

        self.ledger.save_checkpoint_group = watched  # type: ignore[method-assign]
        adapter.execute(action)
        self.assertEqual(len(phases), FULL_RUN_BOUNDARIES)
        self.assertLessEqual(set(phases), set(overseer_checkpoint_groups.GROUP_PHASES))

    def test_a_restart_that_re_prepares_recovers_rather_than_repeats(self) -> None:
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        self._crash_after(2)
        with self.assertRaises(_Crash):
            adapter.execute(action)
        self.assertEqual(len(self.carrying("alpha", self.identity(action, "alpha"))), 1)

        successor = self.adapter(ledger=self.open_ledger())
        self.assertTrue(successor.prepare(action).ready)
        receipt = successor.execute(action)
        self.assertEqual(receipt.outcome, "executed", msg=receipt.detail)
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))), 1, msg=alias
            )
        stored = self.group(action, self.ledgers[-1])
        assert stored is not None
        self.assertEqual(set(stored.committed), set(ALIASES))

    def test_a_settled_group_is_never_prepared_a_second_time(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        declined = self.adapter().prepare(action)
        self.assertFalse(declined.ready)
        self.assertIn("group_settled", declined.detail)

    def test_executing_a_settled_group_again_commits_nothing_more(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        heads = {alias: self.head(alias) for alias in ALIASES}
        receipt = self.adapter(ledger=self.open_ledger()).execute(action)
        self.assertEqual(receipt.outcome, "executed")
        for alias in ALIASES:
            self.assertEqual(self.head(alias), heads[alias], msg=alias)

    def test_a_registry_that_changed_since_preparation_is_refused(self) -> None:
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        narrowed = self.repositories[:2]
        declined = self.adapter(repositories=narrowed).prepare(
            self.action(repositories=narrowed)
        )
        self.assertFalse(declined.ready)
        self.assertIn("registry_changed", declined.detail)


class Reconciliation(_GroupCase):
    """What a dead process did is answered from the repositories themselves."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_a_finished_group_reconciles_as_executed(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        record = self.attempted(action)
        receipt = self.adapter(ledger=self.open_ledger()).reconcile(record)
        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, self.key(action))

    def test_a_group_nobody_started_reconciles_as_absent(self) -> None:
        action = self.action()
        record = self.attempted(action)
        receipt = self.adapter().reconcile(record)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("commit_absent", receipt.detail)

    def test_a_partial_group_names_what_landed_and_what_did_not(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        (self.repos["beta"] / "work.txt").write_text(
            "somebody else is still editing\n", encoding="utf-8"
        )
        adapter.execute(action)
        receipt = self.adapter().reconcile(self.attempted(action))
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("committed alpha", receipt.detail)
        self.assertIn("outstanding", receipt.detail)
        self.assertIn("beta", receipt.detail)

    def test_a_repository_that_cannot_be_read_is_uncertain_not_absent(self) -> None:
        action = self.action()
        self.adapter().prepare(action)
        shutil.rmtree(self.repos["gamma"])
        receipt = self.adapter().reconcile(self.attempted(action))
        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("repository_unreadable", receipt.detail)

    def test_a_settled_group_is_answered_from_its_own_record(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        # First-hand evidence outlives the repository it was made in: the group
        # recorded the commits it made, so a repository nobody can open any more
        # cannot turn a finished checkpoint back into an open question.
        shutil.rmtree(self.repos["gamma"])
        receipt = self.adapter().reconcile(self.attempted(action))
        self.assertEqual(receipt.outcome, "executed")

    def test_reconciling_another_operation_is_uncertain(self) -> None:
        action = self.action()
        record = self.attempted(action)
        adapter = self.adapter()
        receipt = adapter.reconcile(
            overseer_store.ActionRecord(
                **{**record.__dict__, "operation": "resume"},
            )
        )
        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("payload_mismatch", receipt.detail)

    def test_reconciliation_writes_nothing_into_the_durable_group(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        before = self.group(action)
        assert before is not None
        self.adapter().reconcile(self.attempted(action))
        after = self.group(action)
        assert after is not None
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.phase, before.phase)


class PayloadAndMessage(_GroupCase):
    """The two things every checkpoint needs before any of this can start."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_a_payload_that_is_not_a_commit_is_refused(self) -> None:
        action = overseer_dispatch.Action(
            plan_dir=self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=overseer_dispatch.Resume(),
            token="token-1",
            scope="sha256:scope",
            repositories=self.repositories,
        )
        adapter = self.adapter()
        self.assertIn("payload_mismatch", adapter.prepare(action).detail)
        self.assertIn("payload_mismatch", adapter.execute(action).detail)

    def test_a_message_of_whitespace_is_not_a_message(self) -> None:
        declined = self.adapter().prepare(self.action(message="   \n"))
        self.assertFalse(declined.ready)
        self.assertIn("message_empty", declined.detail)


if __name__ == "__main__":
    unittest.main()
