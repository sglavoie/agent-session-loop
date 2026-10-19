"""Run context and continuation values shared by terminating turn branches."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import (
    plans_models,
    plans_snapshot,
    redirect_continuation,
    run_persistence,
    run_prompts,
)


class RunContext(Protocol):
    """One locked run's own state, as the terminating branches need it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members these seven
    branches reach are named: the run payload and event log every record lands
    in, the persistence seam that serializes them, the plan cache a correction
    re-reads the plan from, the run and control identifiers the records are
    attributed to, the two idle-clock fields the reply wait owns, and the two
    ways a turn ends the run.

    The idle-clock fields are plain attributes rather than properties because
    the reply wait assigns both.
    """

    runner_work_finished_at: datetime | None
    idle_baseline_pinned: bool

    @property
    def run_id(self) -> str: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any]: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def plan_cache(self) -> plans_snapshot.PlanSnapshotCache: ...

    @property
    def control_payload(self) -> dict[str, Any]: ...

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What a surviving branch hands back
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MailboxContinuation:
    """The turn a mailbox answer turned a runner pause back into.

    Exactly one of the two continuation fields is set: a reply carries text the
    next turn resumes from, a resolution carries an operator's decision to
    continue without one. ``snapshot`` is the after-image the answer was given
    against, and the next pass starts from it rather than re-reading the plan.
    """

    snapshot: plans_models.PlanSnapshot
    pending_reply: run_prompts._PendingReply | None
    pending_resolution: run_prompts._PendingResolution | None


@dataclasses.dataclass(frozen=True)
class RedirectContinuation:
    """The same session again, carrying the correction that interrupted it.

    ``snapshot`` is ``None`` when the plan could not be inspected after the
    forced stop. The correction is not lost to that: the loop hands such a turn
    to the malformed-plan branch, which owns every ``after is None`` turn, and
    ``pending`` rides along into whichever attempt that repair produces.

    ``pending`` is never ``None`` -- a branch reached only because an acceptance
    was recorded always has one -- and it is what the next attempt resumes from.
    """

    snapshot: plans_models.PlanSnapshot | None
    pending: redirect_continuation.PendingRedirect


@dataclasses.dataclass(frozen=True)
class SparkRetry:
    """The same session, queued again on the baseline model.

    ``run_suppression`` is echoed rather than freshly decided: it outlives the
    turn, and a retry that could not write the shared availability ledger is
    the only thing that sets it. ``fallback_reason`` is per-turn, and the next
    pass reports it on the route it selects.
    """

    snapshot: plans_models.PlanSnapshot
    fallback_reason: str
    run_suppression: str | None
