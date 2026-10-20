"""The seams and the outcome type of the completion pass.

:mod:`run_completion` decides what a plan with no pending session owes; this
module holds the vocabulary that decision is stated in, so the sibling
``run_completion_*`` modules that carry its branches can name the same context,
soft-fail seam and outcome without importing the entry point back.

* :class:`RunContext` is the structural view of the loop's own context object:
  the ``CoordinationPersistence``/``LockedRun`` pattern applied to the run
  loop, declared here so nothing in the completion boundary depends on
  ``run_loop``, which owns the dataclass.
* :class:`SoftFailCommit` is the commit-failure recorder that stays in
  ``run_loop`` because the owed-group recovery above the loop shares it.
* :class:`CompletionOutcome` is what the loop's ``break`` and ``continue``
  became: the next move paired with the plan image it is made against.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from . import (
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
    """One locked run's own state, as the completion pass needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members a completion
    pass actually reaches are named, which is what makes this a boundary rather
    than a second copy of the run's shape: everything the loop carries for its
    turns — the counters, the idle baseline, the per-turn details — is absent
    here because nothing on this path may touch it.

    ``commit_at_end_attempted`` and ``review_appended`` are declared mutable
    because this is the boundary that spends them: each marks a piece of
    end-of-run work as owed once and never again, and the loop reads them
    nowhere else.
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
    def verification_context(self) -> verification_models.VerificationContext: ...

    @property
    def commit_generation(self) -> run_commit._CommitGeneration: ...

    @property
    def control_payload(self) -> dict[str, Any]: ...

    @property
    def seen_external_command_ids(self) -> set[str]: ...

    commit_at_end_attempted: bool
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
    the top of the loop shares it with the end-of-run commit here, and because
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

#: The two moves a completion pass can hand back, spelled as the loop
#: statements they replace. Each constant is annotated with its own literal
#: rather than the union so a comparison against it narrows at the call site.
CompletionAction = Literal["continue", "break"]

#: Go round the loop again: this pass grew the plan rather than ending it.
ACTION_CONTINUE: Literal["continue"] = "continue"

#: Stop the loop: this pass either settled the run or paused it.
ACTION_BREAK: Literal["break"] = "break"


@dataclasses.dataclass(frozen=True)
class CompletionOutcome:
    """The loop's next move, paired with the plan image it makes it against.

    The pass rebinds its snapshot every time it grows the plan, so returning
    the action alone would leave the caller selecting out of a stale image.
    Both constructors take the snapshot for that reason: there is no way to
    state an outcome here without stating which plan it is an outcome for.
    """

    action: CompletionAction
    snapshot: plans_models.PlanSnapshot

    @classmethod
    def again(cls, snapshot: plans_models.PlanSnapshot) -> CompletionOutcome:
        """Grow the run: the loop takes another pass over this plan."""
        return cls(action=ACTION_CONTINUE, snapshot=snapshot)

    @classmethod
    def stop(cls, snapshot: plans_models.PlanSnapshot) -> CompletionOutcome:
        """End the run: the loop leaves and terminalization takes over."""
        return cls(action=ACTION_BREAK, snapshot=snapshot)
