"""The seams and the result shape of the turn settlement pass.

``turn_settlement`` owns the pass itself; this module owns the structural
:class:`RunContext` and :class:`SoftFailCommit` Protocols it is handed and the
:class:`TurnSettlement` it hands back, so the sibling modules that carry part
of the pass can name them without importing the facade.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from . import (
    continuation_guards,
    guards,
    plans_models,
    plans_snapshot,
    run_commit,
    run_persistence,
    run_reporting,
    verification_models,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as the settlement pass needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members this pass
    reaches are named: the run payload and event log every record lands in, the
    persistence seam that serializes them, the progress reporter the commit
    group writes through, the plan cache a refusal re-reads the plan from, the
    verification context and commit machinery the per-session commit needs, and
    the two ways a turn ends the run.

    ``completed_count`` and ``review_appended`` are declared mutable because
    this is the boundary that advances them: the counter is what ``--max``
    reads, and clearing the review flag is how a cycle that materialized repair
    children tells the loop another review is owed once they finish.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def run_dir(self) -> Path: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any]: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def progress(self) -> run_reporting._ProgressReporter: ...

    @property
    def guard_controller(self) -> guards.GuardController: ...

    @property
    def plan_cache(self) -> plans_snapshot.PlanSnapshotCache: ...

    @property
    def session_attempts(self) -> continuation_guards.SessionAttempts: ...

    @property
    def verification_context(self) -> verification_models.VerificationContext: ...

    @property
    def commit_generation(self) -> run_commit._CommitGeneration: ...

    @property
    def control_payload(self) -> dict[str, Any]: ...

    @property
    def seen_external_command_ids(self) -> set[str]: ...

    completed_count: int
    review_appended: bool

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


class SoftFailCommit(Protocol):
    """Absorb a commit failure and keep the run going.

    The runner keeps the implementation because the owed-group settlement at
    the top of the loop shares it with the per-session commit here, and because
    the retry budget it spends lives on the run context rather than on this
    boundary. It never raises: a commit that cannot be made is a record and a
    warning, not an ending.
    """

    def __call__(
        self,
        *,
        scope: str,
        session_id: str | None,
        group: Mapping[str, Any] | None,
        commit: Mapping[str, Any] | None,
        error: str | None,
        message: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What the loop does next
# ---------------------------------------------------------------------------

#: The three moves a settlement pass can hand back. ``proceed`` is the pass
#: falling through to the loop's own control tail, ``retry`` is the loop's
#: `continue`, and ``stop`` is its `break`. Each constant is annotated with its
#: own literal rather than the union so a comparison against it narrows at the
#: call site.
SettlementAction = Literal["proceed", "retry", "stop"]
ACTION_PROCEED: Literal["proceed"] = "proceed"
ACTION_RETRY: Literal["retry"] = "retry"
ACTION_STOP: Literal["stop"] = "stop"


@dataclasses.dataclass(frozen=True)
class TurnSettlement:
    """What one settled turn leaves the loop holding.

    ``snapshot`` is the plan image the pass ended on, and it travels back on
    every action: a refusal hands back the image the loop already had, a retry
    hands back the one the retry was decided against, and a completed turn
    hands back the published one.

    ``turn_record`` is the published turn, and only :data:`ACTION_PROCEED`
    carries it: it is what the loop's control tail attributes its drain and its
    stop to, and no other action reaches that tail.
    """

    action: SettlementAction
    snapshot: plans_models.PlanSnapshot
    turn_record: Mapping[str, Any] | None
