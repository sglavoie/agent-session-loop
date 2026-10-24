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
    overseer_store_ledger,
    overseer_store_models,
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
    reconciliations: list[overseer_store_models.ActionRecord] = field(
        default_factory=list
    )
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
        self, record: overseer_store_models.ActionRecord
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
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
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
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)

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


__all__ = [
    "EXTRA_REPOSITORY",
    "PLAN",
    "QUESTION",
    "REPOSITORIES",
    "RUN",
    "WATCHER",
    "_Adapter",
    "_DispatchCase",
    "_candidate",
    "_decision",
    "_facts",
    "_grant",
    "_inventory",
    "assert_no_runner_import",
    "dataclass",
    "intervention",
    "overseer_discovery",
    "overseer_dispatch",
    "overseer_policy_client",
    "replace",
    "unittest",
]
