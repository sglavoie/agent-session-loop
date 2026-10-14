"""Planning: what policy permits, and the fences that re-check it under the claim."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import retention, state, state_locks
from .maintenance_bodies import _iter_bodies, _over_budget_selection
from .maintenance_models import (
    CorpusUsage,
    LogBodyAction,
    MaintenancePlan,
    RunCompaction,
    measure_corpus,
)
from .maintenance_policy import (
    COMPACTION_AGE_DAYS,
    CORPUS_BUDGET_BYTES,
    LANE_COMPACTION,
    LANE_RECLAIM,
    LOG_BODY_BUDGET_BYTES,
    MIN_FREE_FRACTION,
    REASON_LOG_BODY_BUDGET,
    STAGE_REVALIDATION,
    _now,
)


class _SafetyReading:
    """One reading of the three facts that forbid touching a run's evidence.

    A plan lock, a live run, and an open assurance pin are each written by
    processes that know nothing about the maintenance claim, so holding that
    claim proves only that no *other maintenance pass* is running — never that
    the corpus stopped changing. The reading is therefore a value with a moment
    attached rather than a cache: whoever is about to delete something takes a
    fresh one, and the memo below lives only for the duration of this one.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._live = retention.live_run_keys(root)
        self._pinned = retention.pinned_run_ids(root)
        self._locks: dict[str, str] = {}

    def blocker(self, *, plan_dir: str, plan_state_dir: str, run_id: str) -> str:
        """Return why this run may not be touched now, or ``""`` when it may."""
        if plan_dir not in self._locks:
            info = state_locks.inspect_lock_info(Path(plan_dir), self._root)
            self._locks[plan_dir] = retention.lock_blocker(info)
        if self._locks[plan_dir]:
            return self._locks[plan_dir]
        if (plan_state_dir, run_id) in self._live:
            return "run became live after the plan was computed"
        if not self._pinned.available:
            return f"assurance ledger became unreadable: {self._pinned.error}"
        if run_id in self._pinned.run_ids:
            return "run acquired an open assurance pin"
        return ""


#: Reads current plan-lock, liveness, and pin state for one run, right now.
_Fence = Callable[[str, str, str], str]

#: The same reading, already bound to one run, so an operation deep inside a
#: body can re-take it at its own destructive boundary without knowing which run
#: it belongs to.
_Blocker = Callable[[], str]


def _drop(
    *, lane: str, stage: str, plan_dir: str, run_id: str, reason: str
) -> dict[str, str]:
    """One run this pass declined to touch, and the state that forbade it."""
    return {
        "lane": lane,
        "stage": stage,
        "plan_dir": plan_dir,
        "run_id": run_id,
        "reason": reason,
    }


@dataclass(frozen=True)
class _Removal:
    """One run the reclaim lane may delete, carrying what its fence needs."""

    plan_dir: str
    plan_state_dir: str
    candidate: retention.RunCandidate

    @property
    def run_id(self) -> str:
        return self.candidate.run_id


@dataclass(frozen=True)
class _Compaction:
    """One run the compaction lane may touch, carrying what its fence needs."""

    plan_dir: str
    plan_state_dir: str
    run_id: str
    run_path: Path


def _compactable_runs(
    report: retention.GCReport,
    *,
    now: datetime,
    compaction_age_days: float,
) -> list[tuple[str, retention.RunCandidate]]:
    """Return the ``(plan_dir, run)`` pairs eligible for detail compaction.

    Eligibility is read off retention's own ordered classification rather than
    recomputed. ``newest-run`` is the only reason that survives every safety
    fence ahead of it, so a run carrying it is by construction unlocked, not
    executing, unpinned against a *readable* ledger, finished, and dateable.
    """
    cutoff = now - timedelta(days=compaction_age_days)
    eligible: list[tuple[str, retention.RunCandidate]] = []
    for entry in report.plans:
        for run in entry.retention.runs:
            if run.reason != retention.RETAIN_NEWEST_RUN:
                continue
            if not run.finished_at:
                continue
            finished = datetime.fromisoformat(run.finished_at)
            if finished > cutoff:
                continue
            eligible.append((entry.retention.plan_dir, run))
    eligible.sort(key=lambda pair: (pair[0], pair[1].run_id))
    return eligible


def _project_compaction(
    plan_dir: str,
    run: retention.RunCandidate,
    *,
    now: datetime,
    log_body_budget_bytes: int,
) -> RunCompaction:
    bodies = _iter_bodies(run.path)
    before = sum(body.stored_bytes for body in bodies)
    selected, removable = _over_budget_selection(
        bodies, budget_bytes=log_body_budget_bytes
    )
    selected_paths = {str(body.source) for body in selected}
    actions = tuple(
        LogBodyAction(
            path=str(body.source),
            action="remove" if str(body.source) in selected_paths else "compress",
            byte_count=body.stored_bytes,
            stored_bytes=body.stored_bytes,
            sha256="",
            reason=(
                REASON_LOG_BODY_BUDGET if str(body.source) in selected_paths else ""
            ),
        )
        for body in bodies
        if not body.compressed or str(body.source) in selected_paths
    )
    return RunCompaction(
        plan_dir=plan_dir,
        run_id=run.run_id,
        run_path=str(run.path),
        finished_at=run.finished_at,
        age_seconds=run.age_seconds,
        body_bytes_before=before,
        body_bytes_after=before - removable,
        removable_bytes=removable,
        actions=actions,
    )


def plan_maintenance(
    state_dir: str | Path | None = None,
    *,
    now: datetime | None = None,
    force: bool = False,
    max_age_days: float = retention.DEFAULT_MAX_AGE_DAYS,
    compaction_age_days: float = COMPACTION_AGE_DAYS,
    log_body_budget_bytes: int = LOG_BODY_BUDGET_BYTES,
    budget_bytes: int = CORPUS_BUDGET_BYTES,
    min_free_fraction: float = MIN_FREE_FRACTION,
    usage: CorpusUsage | None = None,
) -> MaintenancePlan:
    """Project every action policy permits on this corpus, mutating nothing.

    ``usage`` is accepted so the threshold measurement taken before the claim is
    not repeated under it; omit it and the corpus is measured here.
    """
    moment = _now(now)
    measured = (
        usage
        if usage is not None
        else measure_corpus(
            state_dir,
            now=moment,
            budget_bytes=budget_bytes,
            min_free_fraction=min_free_fraction,
        )
    )
    reclaim = retention.run_gc(
        None,
        state_dir=state_dir,
        max_age_days=max_age_days,
        apply=False,
        now=moment,
    )
    compactions = tuple(
        _project_compaction(
            plan_dir,
            run,
            now=moment,
            log_body_budget_bytes=log_body_budget_bytes,
        )
        for plan_dir, run in _compactable_runs(
            reclaim, now=moment, compaction_age_days=compaction_age_days
        )
    )
    return MaintenancePlan(
        usage=measured,
        forced=force,
        reclaim=reclaim,
        compactions=compactions,
        compaction_age_days=compaction_age_days,
        log_body_budget_bytes=log_body_budget_bytes,
    )


def _revalidate(
    plan: MaintenancePlan, *, state_dir: str | Path | None
) -> tuple[list[_Removal], list[_Compaction], list[dict[str, str]]]:
    """Filter the plan against one reading taken under the claim.

    Everything in ``plan`` was judged against a snapshot taken before — possibly
    long before — the claim was granted. A run that came alive, acquired a pin,
    or whose plan acquired a lock in between is dropped here and reported.

    This narrows the plan; it does not authorize it. Nothing read here is
    carried into a deletion: :class:`_Mutator` fences every action against its
    own fresh reading, because the writers of these facts do not participate in
    the maintenance claim and a corpus can change again the moment after this.
    """
    root = state.state_root(state_dir)
    reading = _SafetyReading(root)
    drops: list[dict[str, str]] = []

    removals: list[_Removal] = []
    for entry in plan.reclaim.plans:
        plan_dir = entry.retention.plan_dir
        plan_state_dir = entry.retention.state_dir
        for run in entry.retention.reclaimable_runs:
            reason = reading.blocker(
                plan_dir=plan_dir, plan_state_dir=plan_state_dir, run_id=run.run_id
            )
            if reason:
                drops.append(
                    _drop(
                        lane=LANE_RECLAIM,
                        stage=STAGE_REVALIDATION,
                        plan_dir=plan_dir,
                        run_id=run.run_id,
                        reason=reason,
                    )
                )
                continue
            removals.append(
                _Removal(
                    plan_dir=plan_dir, plan_state_dir=plan_state_dir, candidate=run
                )
            )

    state_dirs = {
        entry.retention.plan_dir: entry.retention.state_dir
        for entry in plan.reclaim.plans
    }
    compactions: list[_Compaction] = []
    for candidate in plan.compactions:
        plan_state_dir = state_dirs.get(candidate.plan_dir, "")
        reason = reading.blocker(
            plan_dir=candidate.plan_dir,
            plan_state_dir=plan_state_dir,
            run_id=candidate.run_id,
        )
        if reason:
            drops.append(
                _drop(
                    lane=LANE_COMPACTION,
                    stage=STAGE_REVALIDATION,
                    plan_dir=candidate.plan_dir,
                    run_id=candidate.run_id,
                    reason=reason,
                )
            )
            continue
        compactions.append(
            _Compaction(
                plan_dir=candidate.plan_dir,
                plan_state_dir=plan_state_dir,
                run_id=candidate.run_id,
                run_path=Path(candidate.run_path),
            )
        )
    return removals, compactions, drops
