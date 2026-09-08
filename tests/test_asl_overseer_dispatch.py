"""Focused tests for the guarded action dispatcher.

Seven properties, and every one of them is a refusal, because a dispatcher that
performs the right action is unremarkable and a dispatcher that performs the
wrong one is the whole risk.

*Stale actions.* A token built against a run that has since moved is refused,
and so is a run that was replaced under a still-matching plan -- including when
the move happens *during* preparation, which is the case a single up-front
check cannot see.

*Revoked permissions.* AgentMux answering no refuses, AgentMux being
unreachable refuses under a different name, and permission withdrawn between
the two asks refuses even though the first ask said yes.

*Permission before content.* Every read of the run is preceded by an
authorization question, on both passes, and a refused or unreachable answer
opens nothing at all. Asserted as an interleaving rather than as two counts,
because "both happened" is exactly what a dispatcher reading first and asking
afterwards would also satisfy. ``reconcile`` is in the same claim: it takes a
grant it verifies, so there is no way to reach an adapter's ``reconcile``
without one.

*Scope expansion.* An action naming a repository the run does not declare is
refused, and so is one whose scope digest no longer matches the run's -- the
two ways a permission for one set of repositories turns into work on another.

*Duplicate dispatches.* The same operation against the same target in the same
situation runs once. Including when the first one is only discovered to have
run by reconciling it, which happens after the first guard pass.

*Unsupported operations.* A registry cannot be built holding an operation
outside the scope, an action whose payload is not one of the five typed
payloads is refused before anything else happens, and no payload has a field an
argv could be smuggled through.

*Evading the recovery limit.* Two failures against one incident escalate it,
and a third attempt is refused -- after the clock moves, after the question is
regenerated under a new id, and after the whole process restarts on a fresh
ledger. Real progress, and only real progress, buys more attempts.
"""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent_session_loop import (
    intervention,
    overseer_context,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store,
)
from tests.support import assert_no_runner_import

PLAN = "/state/agent-session-loop/plan-abc"
RUN = "run-1"
WATCHER = "watcher-1"
FINGERPRINT = "fp-first"
DIGEST = "rd-1"
QUESTION = "q-1"

REPOSITORIES = (
    overseer_discovery.RepositoryDeclaration(
        alias="scripts",
        path="scripts",
        origin="workspace",
        resolved_root="/home/dev/scripts",
    ),
)
EXTRA_REPOSITORY = overseer_policy_client.Repository(
    alias="secrets", path="/home/dev/secrets"
)


def _candidate(
    *,
    plan_dir: str = PLAN,
    run_id: str = RUN,
    repositories: tuple[overseer_discovery.RepositoryDeclaration, ...] = REPOSITORIES,
    record_observed: bool = True,
    ineligible: tuple[overseer_discovery.Ineligibility, ...] = (),
) -> overseer_discovery.Candidate:
    return overseer_discovery.Candidate(
        state_dir=Path("/state/agent-session-loop/plan-abc"),
        plan_dir=plan_dir,
        run_id=run_id,
        workspace_root="/home/dev",
        registry_digest="sha256:registry",
        repositories=repositories,
        run_started_at="2026-01-01T00:00:00+00:00",
        run_finished_at=None,
        paused=True,
        pause_reason="question_pending",
        paused_at="2026-01-01T00:05:00+00:00",
        contract_version=1,
        ownership=overseer_discovery.Ownership.unowned(),
        lifecycle="paused",
        record_observed=record_observed,
        ineligible_reasons=ineligible,
    )


def _inventory(
    *candidates: overseer_discovery.Candidate,
) -> overseer_discovery.Inventory:
    return overseer_discovery.Inventory(
        state_root=Path("/state/agent-session-loop"),
        candidates=candidates,
        warnings=(),
        complete=True,
    )


def _facts(
    *,
    run_id: str = RUN,
    question_id: str | None = QUESTION,
    unanswered: tuple[str, ...] = (QUESTION,),
    turn_index: int = 3,
    session_id: str = "session-7",
    last_activity_at: str = "2026-01-01T00:05:00+00:00",
) -> intervention.RunFacts:
    """Run facts for a paused run waiting on one question.

    ``last_activity_at`` and ``turn_index`` are separate knobs on purpose: the
    recovery-limit tests move one without the other to show which of them the
    progress token notices.
    """
    return intervention.RunFacts(
        plan_dir=Path(PLAN),
        run_id=run_id,
        lock_active=True,
        run_active=True,
        paused=False,
        turn_active=True,
        context_kind="waiting",
        question_id=question_id,
        unanswered_ids=unanswered,
        runner_state={
            "run_id": run_id,
            "paused": False,
            "reason": None,
            "run_started_at": "2026-01-01T00:00:00+00:00",
            "run_finished_at": None,
            "active_turn": {
                "session_id": session_id,
                "turn_index": turn_index,
                "last_activity_at": last_activity_at,
            },
        },
    )


def _decision(
    *,
    allowed: bool = True,
    available: bool = True,
    fingerprint: str = FINGERPRINT,
    digest: str = DIGEST,
    repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
    failure: Any = None,
) -> overseer_policy_client.Decision:
    asked = (
        repositories
        if repositories is not None
        else overseer_policy_client.repositories_for(REPOSITORIES)
    )
    return overseer_policy_client.Decision(
        plan_dir=PLAN,
        access=overseer_policy_client.ACCESS_ACTION,
        repositories=asked,
        allowed=allowed and available,
        available=available,
        reason="opted_in" if allowed else "excluded",
        failure=failure,
        summary="1 repository",
        config_fingerprint=fingerprint if available else "",
        request_digest=digest if available else "",
        contract_version=1 if available else None,
        exit_status=0 if allowed and available else 1,
        outcomes=()
        if allowed and available
        else tuple(
            overseer_policy_client.RepositoryOutcome(
                alias=repository.alias,
                path=repository.path,
                resolved_path=repository.path,
                allowed=False,
                reason="excluded",
                detail="the operator withdrew this repository",
            )
            for repository in asked
        ),
    )


def _grant(**kwargs: Any) -> overseer_policy_client.Grant:
    """The permission a caller of ``reconcile`` has to be holding.

    Built through :meth:`overseer_policy_client.Decision.grant` rather than
    constructed, because that is the only way one is ever made and a test that
    assembles one by hand would be testing a shape the client cannot produce.
    """
    grant = _decision(**kwargs).grant()
    assert grant is not None
    return grant


@dataclass
class _World:
    """The corpus the injected seams read, mutable between dispatches."""

    inventory: overseer_discovery.Inventory
    facts: intervention.RunFacts
    decisions: list[overseer_policy_client.Decision]
    asked: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    on_prepare: Any = None

    def build_inventory(self, **_: Any) -> overseer_discovery.Inventory:
        return self.inventory

    def inspect_run(self, _plan_dir: Any, **__: Any) -> intervention.RunFacts:
        # `inspect_run` opens the run's state file and its mailbox, so a call
        # recorded here is content access. `events` interleaves it with the
        # authorization questions, which is the only way to show which came
        # first rather than that both happened.
        self.events.append("read")
        return self.facts

    def authorize(self, **kwargs: Any) -> overseer_policy_client.Decision:
        self.asked.append(kwargs)
        self.events.append("ask")
        if len(self.decisions) > 1:
            return self.decisions.pop(0)
        return self.decisions[0]


@dataclass
class _Adapter:
    """One operation's stand-in, scripted per call."""

    world: _World
    executions: list[overseer_dispatch.Action] = field(default_factory=list)
    reconciliations: list[overseer_store.ActionRecord] = field(default_factory=list)
    receipts: list[overseer_dispatch.Receipt] = field(default_factory=list)
    reconciled: list[overseer_dispatch.Receipt] = field(default_factory=list)
    raises: BaseException | None = None

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        if self.world.on_prepare is not None:
            outcome = self.world.on_prepare(action)
            if isinstance(outcome, overseer_dispatch.Preparation):
                return outcome
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        self.executions.append(action)
        if self.raises is not None:
            raise self.raises
        if self.receipts:
            return self.receipts.pop(0)
        return overseer_dispatch.Receipt.executed("done", locator="msg-1")

    def reconcile(
        self, record: overseer_store.ActionRecord
    ) -> overseer_dispatch.Receipt:
        self.reconciliations.append(record)
        if self.reconciled:
            return self.reconciled.pop(0)
        return overseer_dispatch.Receipt.uncertain("cannot tell")


class _DispatchCase(unittest.TestCase):
    """A real ledger, a held lease, an open incident, and a scripted world."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-dispatch-"))
        self.addCleanup(self._close)
        self.ledger = overseer_store.open_ledger(state_dir=self.root)
        self.now = 1000.0
        self.world = _World(
            inventory=_inventory(_candidate()),
            facts=_facts(),
            decisions=[_decision()],
        )
        self.adapter = _Adapter(world=self.world)
        self.registry = overseer_dispatch.registry({"reply": self.adapter})
        self._enroll()
        self.incident_id = self.ledger.open_incident(
            grant=None,
            plan_dir=PLAN,
            run_id=RUN,
            kind="question_pending",
            summary="waiting on a reply",
        ).incident_id

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    def _enroll(self) -> None:
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.ledger.record_enrollment(_decision())
        self.ledger.claim_watch(
            plan_dir=PLAN,
            watcher_id=WATCHER,
            run_id=RUN,
            host="host-1",
            pid=4242,
            now=self.now,
            lease_seconds=300.0,
        )

    def reopen(self) -> None:
        """Close and open again over the same path: the restart under test."""
        self.ledger.close()
        self.ledger = overseer_store.open_ledger(state_dir=self.root)

    def action(
        self,
        payload: Any = None,
        *,
        run_id: str = RUN,
        repositories: tuple[overseer_policy_client.Repository, ...] | None = None,
        token: str | None = None,
        scope: str | None = None,
        incident_id: int | None = None,
    ) -> overseer_dispatch.Action:
        """An action built against the world as it currently stands."""
        candidate = self.world.inventory.candidates[0]
        return overseer_dispatch.Action(
            plan_dir=PLAN,
            run_id=run_id,
            incident_id=self.incident_id if incident_id is None else incident_id,
            payload=payload
            if payload is not None
            else overseer_dispatch.Reply(question_id=QUESTION, text="carry on"),
            token=token
            if token is not None
            else intervention.action_token(self.world.facts),
            scope=scope
            if scope is not None
            else overseer_context.scope_digest(candidate),
            repositories=repositories
            if repositories is not None
            else overseer_policy_client.repositories_for(candidate.repositories),
        )

    def dispatch(
        self, action: overseer_dispatch.Action | None = None, **overrides: Any
    ) -> overseer_dispatch.Dispatch:
        kwargs: dict[str, Any] = {
            "action": action if action is not None else self.action(),
            "watcher_id": WATCHER,
            "ledger": self.ledger,
            "adapters": self.registry,
            "authorize": self.world.authorize,
            "build_inventory": self.world.build_inventory,
            "inspect_run": self.world.inspect_run,
            "now": lambda: self.now,
        }
        kwargs.update(overrides)
        return overseer_dispatch.dispatch(**kwargs)


class BaselineTests(_DispatchCase):
    """What a permitted dispatch looks like, so the refusals mean something."""

    def test_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_dispatch)

    def test_a_permitted_action_is_attempted_then_confirmed(self) -> None:
        result = self.dispatch()

        self.assertTrue(result.performed, result.detail)
        self.assertIsNone(result.refusal)
        self.assertEqual(len(self.adapter.executions), 1)
        assert result.record is not None
        self.assertEqual(result.record.status, "executed")
        self.assertTrue(result.record.confirmed)
        self.assertEqual(result.record.target, QUESTION)
        self.assertEqual(result.record.progress_token, self.expected_progress())
        assert result.record.receipt is not None
        self.assertEqual(result.record.receipt.locator, "msg-1")

    def test_permission_is_asked_twice_and_under_the_action_category(self) -> None:
        """Once before preparation and once after, per the requirement that a
        decision made before a wait is not evidence about the moment after."""
        self.dispatch()

        self.assertEqual(len(self.world.asked), 2)
        for question in self.world.asked:
            self.assertEqual(question["access"], overseer_policy_client.ACCESS_ACTION)
            self.assertEqual(
                question["repositories"],
                overseer_policy_client.repositories_for(REPOSITORIES),
            )

    def test_the_attempt_is_written_before_the_adapter_is_called(self) -> None:
        """A crash inside the adapter has to leave a durable trace, so the row
        must already exist when the adapter runs."""
        seen: list[int] = []

        def execute(action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
            seen.append(len(self.ledger.actions(plan_dir=PLAN)))
            return overseer_dispatch.Receipt.executed()

        self.adapter.execute = execute  # type: ignore[method-assign]
        self.dispatch()

        self.assertEqual(seen, [1])

    def expected_progress(self) -> str:
        return overseer_dispatch.progress_token(self.world.facts)


class StaleActionTests(_DispatchCase):
    """A picture of the run that the run has moved on from."""

    def test_a_token_from_an_earlier_snapshot_is_refused(self) -> None:
        action = self.action(token="sha256:stale")

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "stale_action")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_replaced_run_is_refused_even_with_a_matching_plan(self) -> None:
        self.world.inventory = _inventory(_candidate(run_id="run-2"))

        result = self.dispatch()

        self.assertEqual(result.refusal, "run_replaced")
        self.assertEqual(self.adapter.executions, [])

    def test_a_run_that_moves_during_preparation_is_refused(self) -> None:
        """The whole reason there is a second pass: everything checked before
        preparation was true before a wait, not after it."""

        def move(_action: overseer_dispatch.Action) -> None:
            self.world.facts = _facts(turn_index=4)

        self.world.on_prepare = move

        result = self.dispatch()

        self.assertEqual(result.refusal, "stale_action")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_lost_lease_is_refused(self) -> None:
        self.now += 400.0

        result = self.dispatch()

        self.assertEqual(result.refusal, "ownership_lost")

    def test_another_watchers_lease_is_refused(self) -> None:
        result = self.dispatch(watcher_id="watcher-2")

        self.assertEqual(result.refusal, "ownership_lost")

    def test_a_question_that_is_no_longer_open_is_refused(self) -> None:
        self.world.facts = _facts(question_id="q-2", unanswered=("q-2",))
        action = self.action(token=intervention.action_token(self.world.facts))

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "question_unknown")
        self.assertEqual(self.adapter.executions, [])

    def test_an_incident_that_is_not_open_is_refused(self) -> None:
        self.ledger.settle_incident(incident_id=self.incident_id, status="resolved")

        result = self.dispatch()

        self.assertEqual(result.refusal, "incident_unknown")

    def test_an_unobservable_plan_is_refused(self) -> None:
        self.world.inventory = _inventory(_candidate(record_observed=False))

        result = self.dispatch()

        self.assertEqual(result.refusal, "plan_unobserved")


class RevokedPermissionTests(_DispatchCase):
    """Fail closed, every time, and name which kind of no it was."""

    def test_a_refusal_from_agentmux_stops_the_dispatch(self) -> None:
        self.world.decisions = [_decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_an_unreachable_agentmux_is_a_fault_not_a_refusal(self) -> None:
        self.world.decisions = [
            _decision(allowed=False, available=False, failure="client_missing")
        ]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_unavailable")
        self.assertEqual(self.adapter.executions, [])

    def test_permission_withdrawn_during_preparation_is_refused(self) -> None:
        """The first answer was yes. It is the second one that governs."""
        self.world.decisions = [_decision(), _decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_policy_that_moves_during_preparation_is_refused(self) -> None:
        self.world.decisions = [_decision(), _decision(fingerprint="fp-second")]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_superseded")
        self.assertEqual(self.adapter.executions, [])

    def test_every_answer_is_recorded_including_the_refusals(self) -> None:
        self.world.decisions = [_decision(allowed=False)]

        self.dispatch()

        recorded = self.ledger.decisions(plan_dir=PLAN)
        self.assertTrue(any(not row.allowed for row in recorded))


class PermissionBeforeContentTests(_DispatchCase):
    """A no from AgentMux costs a discovery walk, and nothing is opened.

    "Fail closed" is about what a refused dispatch does not *do*; this is about
    what a refused dispatch does not *see*. A revoked opt-in that is discovered
    after the run's state and mailbox have already been read has protected
    nothing, however correct the eventual refusal is, so every assertion here
    is about ordering rather than about the outcome.
    """

    def test_permission_precedes_every_read_of_the_run(self) -> None:
        self.dispatch()

        self.assertEqual(["ask", "read", "ask", "read"], self.world.events)

    def test_a_refused_policy_opens_nothing(self) -> None:
        self.world.decisions = [_decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(["ask"], self.world.events)

    def test_an_unavailable_policy_opens_nothing(self) -> None:
        """Fail closed reaches the reads too: no answer is not a quiet yes."""
        self.world.decisions = [
            _decision(allowed=False, available=False, failure="client_missing")
        ]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_unavailable")
        self.assertEqual(["ask"], self.world.events)

    def test_permission_withdrawn_during_preparation_stops_the_second_read(
        self,
    ) -> None:
        """The recheck is the pass a mid-wait revocation lands in, so it is the
        pass that must not read first and ask afterwards."""
        self.world.decisions = [_decision(), _decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(["ask", "read", "ask"], self.world.events)

    def test_a_policy_that_moves_during_preparation_stops_the_second_read(
        self,
    ) -> None:
        self.world.decisions = [_decision(), _decision(fingerprint="fp-second")]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_superseded")
        self.assertEqual(["ask", "read", "ask"], self.world.events)

    def test_a_refusal_leaves_an_already_dispatched_action_recorded(self) -> None:
        """Revocation stops what is queued; it does not unmake what happened."""
        self.dispatch()
        self.world.facts = _facts(turn_index=4)
        self.world.decisions = [_decision(allowed=False)]

        result = self.dispatch(
            self.action(token=intervention.action_token(_facts(turn_index=4)))
        )

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(len(self.adapter.executions), 1)
        self.assertEqual(
            [row.status for row in self.ledger.actions(plan_dir=PLAN)], ["executed"]
        )

    def test_reconciliation_cannot_be_run_without_a_grant_for_this_plan(self) -> None:
        elsewhere = _decision().grant()
        assert elsewhere is not None

        with self.assertRaises(overseer_dispatch.DispatchError) as caught:
            overseer_dispatch.reconcile(
                plan_dir="/state/agent-session-loop/plan-other",
                ledger=self.ledger,
                adapters=self.registry,
                grant=elsewhere,
            )

        self.assertIn("plan-other", str(caught.exception))

    def test_reconciliation_cannot_be_run_under_a_context_grant(self) -> None:
        """The easier question, answered instead of the harder one, is how a
        read-only permission turns into an action."""
        context = replace(
            _decision(), access=overseer_policy_client.ACCESS_CONTEXT
        ).grant()
        assert context is not None

        with self.assertRaises(overseer_dispatch.DispatchError) as caught:
            overseer_dispatch.reconcile(
                plan_dir=PLAN,
                ledger=self.ledger,
                adapters=self.registry,
                grant=context,
            )

        self.assertIn(overseer_policy_client.ACCESS_ACTION, str(caught.exception))


class ScopeExpansionTests(_DispatchCase):
    """A permission for these repositories is not a permission for those."""

    def test_an_action_naming_an_undeclared_repository_is_refused(self) -> None:
        declared = overseer_policy_client.repositories_for(REPOSITORIES)
        action = self.action(repositories=declared + (EXTRA_REPOSITORY,))

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "scope_expanded")
        self.assertIn("secrets", result.detail)
        self.assertEqual(self.world.asked, [])

    def test_a_scope_digest_that_no_longer_matches_is_refused(self) -> None:
        action = self.action(scope="sha256:elsewhere")

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "scope_expanded")
        self.assertEqual(self.adapter.executions, [])

    def test_a_repository_added_to_the_run_invalidates_the_action(self) -> None:
        """Scope grows under a live action: the digest the action carries was
        taken against the smaller set, so it stops matching."""
        grown = REPOSITORIES + (
            overseer_discovery.RepositoryDeclaration(
                alias="secrets",
                path="secrets",
                origin="workspace",
                resolved_root="/home/dev/secrets",
            ),
        )
        action = self.action()
        self.world.inventory = _inventory(_candidate(repositories=grown))

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "scope_expanded")
        self.assertEqual(self.adapter.executions, [])


class DuplicateDispatchTests(_DispatchCase):
    """The same thing, in the same circumstances, happens once."""

    def test_the_same_action_twice_in_the_same_situation_runs_once(self) -> None:
        first = self.dispatch()
        second = self.dispatch()

        self.assertTrue(first.performed)
        self.assertEqual(second.refusal, "duplicate_dispatch")
        self.assertEqual(len(self.adapter.executions), 1)

    def test_a_duplicate_is_refused_after_a_restart(self) -> None:
        self.dispatch()
        self.reopen()

        result = self.dispatch()

        self.assertEqual(result.refusal, "duplicate_dispatch")
        self.assertEqual(len(self.adapter.executions), 1)

    def test_the_same_operation_in_a_new_situation_is_not_a_duplicate(self) -> None:
        """A run that pauses again is asking again, not repeating itself."""
        self.dispatch()
        self.world.facts = _facts(turn_index=4)
        action = self.action(token=intervention.action_token(self.world.facts))

        result = self.dispatch(action)

        self.assertTrue(result.performed, result.detail)
        self.assertEqual(len(self.adapter.executions), 2)

    def test_a_dispatch_left_in_doubt_blocks_the_retry(self) -> None:
        self.adapter.receipts = [overseer_dispatch.Receipt.uncertain("no answer")]
        first = self.dispatch()

        self.assertEqual(first.refusal, "outcome_uncertain")
        assert first.record is not None
        self.assertTrue(first.record.in_doubt)

        second = self.dispatch()

        self.assertEqual(second.refusal, "outcome_uncertain")
        self.assertEqual(len(self.adapter.executions), 1)

    def test_an_adapter_that_raises_leaves_the_action_in_doubt(self) -> None:
        self.adapter.raises = RuntimeError("connection reset")

        result = self.dispatch()

        self.assertEqual(result.refusal, "outcome_uncertain")
        in_doubt = self.ledger.in_doubt_actions(plan_dir=PLAN)
        self.assertEqual(len(in_doubt), 1)
        self.assertEqual(in_doubt[0].status, "attempted")

    def test_reconciling_an_in_doubt_action_can_reveal_a_duplicate(self) -> None:
        """The case only the second guard pass can catch: the row that makes
        this a duplicate does not say ``executed`` until reconciliation runs,
        and reconciliation runs after the first pass."""
        self.adapter.receipts = [overseer_dispatch.Receipt.uncertain("no answer")]
        self.dispatch()
        self.adapter.reconciled = [overseer_dispatch.Receipt.executed("it landed")]

        result = self.dispatch()

        self.assertEqual(result.refusal, "duplicate_dispatch")
        self.assertEqual(len(self.adapter.executions), 1)
        settled = self.ledger.actions(plan_dir=PLAN)
        self.assertEqual([row.status for row in settled], ["executed"])

    def test_reconciliation_never_guesses_at_an_unknown_outcome(self) -> None:
        self.adapter.receipts = [overseer_dispatch.Receipt.uncertain("no answer")]
        self.dispatch()

        results = overseer_dispatch.reconcile(
            plan_dir=PLAN,
            ledger=self.ledger,
            adapters=self.registry,
            grant=_grant(),
        )

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].settled)
        self.assertEqual(
            self.ledger.in_doubt_actions(plan_dir=PLAN)[0].status, "attempted"
        )


class UnsupportedOperationTests(_DispatchCase):
    """The operational scope is closed, and nothing in it carries a command."""

    def test_a_registry_cannot_be_built_for_an_operation_outside_the_scope(
        self,
    ) -> None:
        with self.assertRaises(overseer_dispatch.DispatchError) as caught:
            overseer_dispatch.registry({"shell": self.adapter})

        self.assertIn("shell", str(caught.exception))

    def test_an_operation_with_no_adapter_is_refused(self) -> None:
        result = self.dispatch(self.action(overseer_dispatch.Commit("checkpoint")))

        self.assertEqual(result.refusal, "operation_unsupported")
        self.assertEqual(self.world.asked, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_payload_that_is_not_one_of_the_five_types_is_refused(self) -> None:
        """The shape a smuggled command would have to arrive in."""

        @dataclass(frozen=True)
        class _Shell:
            argv: tuple[str, ...]
            operation: str = "reply"

            def target(self) -> str:
                return " ".join(self.argv)

        result = self.dispatch(self.action(_Shell(argv=("rm", "-rf", "/"))))

        self.assertEqual(result.refusal, "operation_unsupported")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.world.asked, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_no_payload_has_a_field_a_command_could_ride_in(self) -> None:
        """A structural check, not a spelling one: if a future payload grows a
        field named like a command, this is what notices."""
        forbidden = {"argv", "command", "cmd", "shell", "env", "script", "executable"}
        for payload in overseer_dispatch.PAYLOAD_TYPES:
            with self.subTest(payload=payload.__name__):
                names = set(payload.__dataclass_fields__)
                self.assertEqual(names & forbidden, set())

    def test_the_operational_scope_is_the_one_decisions_authorize(self) -> None:
        self.assertEqual(
            overseer_dispatch.OPERATIONS,
            ("reply", "guidance", "verify", "commit", "resume"),
        )

    def test_the_registry_is_closed_after_construction(self) -> None:
        built = overseer_dispatch.registry({"reply": self.adapter})

        with self.assertRaises(TypeError):
            built.adapters["commit"] = self.adapter  # type: ignore[index]

        self.assertFalse(built.supports("commit"))


class RecoveryLimitTests(_DispatchCase):
    """Two recoveries without progress, and no way to buy a third."""

    def fail_once(self) -> overseer_dispatch.Dispatch:
        self.adapter.receipts = [overseer_dispatch.Receipt.failed("delivery refused")]
        return self.dispatch(self.action(overseer_dispatch.Guidance("try again")))

    def setUp(self) -> None:
        super().setUp()
        self.registry = overseer_dispatch.registry(
            {"reply": self.adapter, "guidance": self.adapter}
        )

    def test_two_failures_escalate_the_incident(self) -> None:
        first = self.fail_once()
        second = self.fail_once()

        self.assertIsNone(first.refusal)
        self.assertFalse(first.performed)
        self.assertFalse(first.escalated)
        self.assertTrue(second.escalated)
        incident = self.ledger.incident(self.incident_id)
        assert incident is not None
        self.assertEqual(incident.status, "escalated")

    def test_a_third_attempt_is_refused(self) -> None:
        self.fail_once()
        self.fail_once()

        result = self.fail_once()

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(result.recoveries, 2)
        self.assertEqual(len(self.adapter.executions), 2)

    def test_moving_the_clock_does_not_reset_the_limit(self) -> None:
        """Waiting is not progress, so a timestamp is not in the token."""
        self.fail_once()
        self.fail_once()
        self.world.facts = _facts(last_activity_at="2026-01-01T09:99:99+00:00")
        self.now += 60.0
        self.ledger.claim_watch(
            plan_dir=PLAN,
            watcher_id=WATCHER,
            run_id=RUN,
            host="host-1",
            pid=4242,
            now=self.now,
            lease_seconds=300.0,
        )

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                token=intervention.action_token(self.world.facts),
            )
        )

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(len(self.adapter.executions), 2)

    def test_regenerating_the_question_does_not_reset_the_limit(self) -> None:
        """A new id for the same trouble is a new question identity and an
        unchanged situation. The action token notices; the progress token must
        not, or asking again is how a caller buys attempts."""
        self.fail_once()
        self.fail_once()
        before = overseer_dispatch.progress_token(self.world.facts)
        self.world.facts = _facts(question_id="q-2", unanswered=("q-2",))

        self.assertNotEqual(
            intervention.action_token(self.world.facts),
            intervention.action_token(_facts()),
        )
        self.assertEqual(overseer_dispatch.progress_token(self.world.facts), before)

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                token=intervention.action_token(self.world.facts),
            )
        )

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(len(self.adapter.executions), 2)

    def test_restarting_the_process_does_not_reset_the_limit(self) -> None:
        """The count is rows on disk, so a fresh ledger and a fresh registry
        find exactly the same two failures."""
        self.fail_once()
        self.fail_once()
        self.reopen()
        self.adapter = _Adapter(world=self.world)
        self.registry = overseer_dispatch.registry(
            {"reply": self.adapter, "guidance": self.adapter}
        )

        result = self.fail_once()

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(self.adapter.executions, [])

    def test_real_progress_resets_the_limit(self) -> None:
        """The run moving on is the one thing that buys another attempt, and it
        buys it by being real rather than by anyone deciding it counts.

        The fresh incident is not what does it -- the test above shows a fresh
        incident alone changing nothing. It is here only because the second
        failure escalated the first one, and an escalated incident is closed.
        """
        self.fail_once()
        self.fail_once()
        moved = self.ledger.open_incident(
            grant=None,
            plan_dir=PLAN,
            run_id=RUN,
            kind="question_pending",
            summary="still waiting",
        )
        self.world.facts = _facts(turn_index=4)

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                token=intervention.action_token(self.world.facts),
                incident_id=moved.incident_id,
            )
        )

        self.assertTrue(result.performed, result.detail)
        self.assertEqual(len(self.adapter.executions), 3)

    def test_refiling_the_trouble_as_a_new_incident_does_not_reset_the_limit(
        self,
    ) -> None:
        """Opening a fresh incident row for the same trouble is something the
        overseer does for itself, so a count keyed by incident would reset
        without anybody trying. The count is keyed by the situation instead."""
        self.fail_once()
        self.fail_once()
        refiled = self.ledger.open_incident(
            grant=None,
            plan_dir=PLAN,
            run_id=RUN,
            kind="question_pending",
            summary="same trouble, new row",
        )
        before = overseer_dispatch.progress_token(self.world.facts)

        result = self.dispatch(
            self.action(
                overseer_dispatch.Guidance("try again"),
                incident_id=refiled.incident_id,
            )
        )

        self.assertEqual(result.refusal, "recovery_exhausted")
        self.assertEqual(len(self.adapter.executions), 2)
        self.assertEqual(
            [row.progress_token for row in self.ledger.actions(plan_dir=PLAN)],
            [before, before],
        )


class ProgressTokenTests(unittest.TestCase):
    """What the token notices, stated as equalities rather than as prose."""

    def test_timestamps_are_not_in_the_token(self) -> None:
        self.assertEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(
                _facts(last_activity_at="2027-06-06T06:06:06+00:00")
            ),
        )

    def test_question_identity_is_not_in_the_token(self) -> None:
        self.assertEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(
                _facts(question_id="q-99", unanswered=("q-99",))
            ),
        )

    def test_a_new_turn_is_in_the_token(self) -> None:
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(_facts(turn_index=4)),
        )

    def test_a_new_session_is_in_the_token(self) -> None:
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(_facts(session_id="session-8")),
        )

    def test_a_question_appearing_is_in_the_token(self) -> None:
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(_facts(unanswered=(QUESTION, "q-2"))),
        )

    def test_a_pause_is_in_the_token(self) -> None:
        paused = replace(_facts(), paused=True, run_active=False)
        self.assertNotEqual(
            overseer_dispatch.progress_token(_facts()),
            overseer_dispatch.progress_token(paused),
        )


if __name__ == "__main__":
    unittest.main()
