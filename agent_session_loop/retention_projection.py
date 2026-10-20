"""Project which persisted run directories an age cutoff would reclaim.

Nothing has ever deleted a ``runs/<run-id>/`` directory, and the per-turn
``stderr.log`` files inside them dominate the state corpus. ``compact_state``
rewrites JSON records but never touches those logs, which is why it projects a
rounding error against gigabytes of growth.

The projection functions compute the deletion *plan* only and never remove
anything; :func:`agent_session_loop.retention.run_gc` is the sole caller that
deletes, and only when its
``apply`` argument is set. A run is reclaimable solely when its finish time can
be read from its own turn artifacts and is at least ``max_age_days`` old, its
plan directory carries no lock at all, it is not reported running by the scan
``asl sessions`` uses, it is not the newest run of its plan, and the assurance
ledger could be read well enough to say whether an open issue pins it. Every run
that cannot be dated is retained: a run whose age is unknown is never old
enough. An unreadable ledger retains every run for the same reason — an empty
pin set read off a ledger nobody could parse is an absence of knowledge, not an
absence of pins, and deleting against it is unrecoverable.

Every lock here is inspected through :func:`state.inspect_lock_info` and the
running scan is asked not to reclaim, because the ordinary read path removes a
lock this host has proven dead. Under that read a dry run would write, and the
two scopes would disagree: corpus scope runs the whole-corpus liveness pass
before it judges any plan, so a lock plan scope still sees would already be
gone. Nothing in this module removes a lock in either mode; clearing one stays
a deliberate ``asl unlock``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    assurance_report,
    plans_files,
    session_overview,
    state,
    state_locks,
)

__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "RETAIN_PLAN_LOCKED",
    "RETAIN_LIVE_RUN",
    "RETAIN_PIN_UNKNOWN",
    "RETAIN_OPEN_ISSUE",
    "RETAIN_UNFINISHED",
    "RETAIN_UNDATED",
    "RETAIN_NEWEST_RUN",
    "RETAIN_WITHIN_CUTOFF",
    "RunCandidate",
    "PlanRetention",
    "CorpusRetention",
    "live_run_keys",
    "pinned_run_ids",
    "lock_blocker",
    "plan_retention",
    "corpus_retention",
]


DEFAULT_MAX_AGE_DAYS = 30.0

# Why a run was kept. Evaluated in this order, so the first true statement is
# the one reported.
RETAIN_PLAN_LOCKED = "plan-locked"
RETAIN_LIVE_RUN = "live-run"
RETAIN_PIN_UNKNOWN = "pin-unknown"
RETAIN_OPEN_ISSUE = "open-issue"
RETAIN_UNFINISHED = "unfinished"
RETAIN_UNDATED = "undated"
RETAIN_NEWEST_RUN = "newest-run"
RETAIN_WITHIN_CUTOFF = "within-cutoff"


@dataclass(frozen=True)
class RunCandidate:
    """One ``runs/<run-id>/`` directory judged against the age cutoff."""

    run_id: str
    path: Path
    size_bytes: int
    finished_at: str | None
    age_seconds: float | None
    reclaimable: bool
    reason: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "size_bytes": self.size_bytes,
            "finished_at": self.finished_at,
            "age_seconds": self.age_seconds,
            "reclaimable": self.reclaimable,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PlanRetention:
    """Retention projection for the runs of a single plan directory."""

    plan_dir: str
    state_dir: str
    max_age_days: float
    cutoff_at: str
    runs: tuple[RunCandidate, ...]

    @property
    def reclaimable_runs(self) -> tuple[RunCandidate, ...]:
        return tuple(run for run in self.runs if run.reclaimable)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(run.size_bytes for run in self.runs if run.reclaimable)

    @property
    def retained_bytes(self) -> int:
        return sum(run.size_bytes for run in self.runs if not run.reclaimable)

    @property
    def total_bytes(self) -> int:
        return sum(run.size_bytes for run in self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "state_dir": self.state_dir,
            "max_age_days": self.max_age_days,
            "cutoff_at": self.cutoff_at,
            "run_count": len(self.runs),
            "reclaimable_run_count": len(self.reclaimable_runs),
            "reclaimable_bytes": self.reclaimable_bytes,
            "retained_bytes": self.retained_bytes,
            "total_bytes": self.total_bytes,
            "runs": [run.to_dict() for run in self.runs],
        }


@dataclass(frozen=True)
class CorpusRetention:
    """Retention projection across every plan state directory in a state root."""

    state_root: str
    max_age_days: float
    cutoff_at: str
    plans: tuple[PlanRetention, ...]
    unresolved_state_dirs: tuple[str, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(report.reclaimable_bytes for report in self.plans)

    @property
    def total_bytes(self) -> int:
        return sum(report.total_bytes for report in self.plans)

    @property
    def reclaimable_run_count(self) -> int:
        return sum(len(report.reclaimable_runs) for report in self.plans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_root": self.state_root,
            "max_age_days": self.max_age_days,
            "cutoff_at": self.cutoff_at,
            "plan_count": len(self.plans),
            "reclaimable_run_count": self.reclaimable_run_count,
            "reclaimable_bytes": self.reclaimable_bytes,
            "total_bytes": self.total_bytes,
            "plans": [report.to_dict() for report in self.plans],
            "unresolved_state_dirs": list(self.unresolved_state_dirs),
        }


def _dir_size_bytes(path: Path) -> int:
    """Sum the apparent size of every file under ``path``, symlinks not followed."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def lock_blocker(lock_info: state.LockInfo | None) -> str:
    """Return why a lock forbids reclaiming, or ``""`` when the plan has none.

    Any lock at all blocks. An active one owns the plan outright; a stale one
    records a run that ended without releasing its claim, and nothing has
    judged that plan yet. Clearing it is a deliberate `asl unlock`, never a
    side effect of reclaiming disk — including under ``--apply``, which removes
    run directories and nothing else.
    """
    if lock_info is None:
        return ""
    if lock_info.active:
        return f"plan lock is active: {lock_info.reason}"
    return f"plan lock is present but stale: {lock_info.reason}"


def _run_finished_at(run_dir: Path) -> tuple[datetime | None, tuple[str, str]]:
    """Resolve a run's finish time from its turn artifacts.

    Returns ``(finished_at, problem)`` where ``problem`` is ``("", "")`` once a
    finish time is resolved. A single unreadable or undated artifact makes the
    whole run unresolvable rather than being skipped, because the artifacts that
    remain cannot bound the age of the one that is missing.
    """
    latest: datetime | None = None
    for result_path in session_overview._iter_turn_result_paths(run_dir):
        label = f"{result_path.parent.name}/{result_path.name}"
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return None, (RETAIN_UNDATED, f"{label} is unreadable: {error}")
        if not isinstance(payload, dict):
            return None, (RETAIN_UNDATED, f"{label} does not hold a JSON object")
        raw = payload.get("finished_at")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None, (RETAIN_UNFINISHED, f"{label} has no finished_at")
        finished = session_overview.parse_iso_timestamp(raw)
        if finished is None:
            return None, (RETAIN_UNDATED, f"{label} has an unparseable finished_at")
        if latest is None or finished > latest:
            latest = finished
    if latest is None:
        return None, (RETAIN_UNFINISHED, "run has no turn result artifacts")
    return latest, ("", "")


def _newest_run_name(
    entries: list[tuple[Path, datetime | None, tuple[str, str]]],
) -> str:
    """Return the directory name of the plan's newest run, or ``""`` when empty.

    Undated runs fall back to their directory name, which sorts chronologically
    for the ``<utc-timestamp>-<pid>`` ids the runner mints.
    """
    if not entries:
        return ""
    newest = max(
        entries,
        key=lambda entry: (
            entry[1].timestamp() if entry[1] is not None else float("-inf"),
            entry[0].name,
        ),
    )
    return newest[0].name


def live_run_keys(state_dir: str | Path | None = None) -> frozenset[tuple[str, str]]:
    """Return ``(state_dir, run_id)`` pairs for every run reported running.

    Derived from :func:`session_overview.scan_running_rows`, so retention's idea
    of a live run is the one ``asl sessions`` prints rather than a second
    definition that could disagree with it. The scan runs with ``reclaim=False``
    so this pre-pass cannot decide a later retain question by removing the lock
    that would have answered it.
    """
    rows, _stale_locks, _skipped = session_overview.scan_running_rows(
        state_dir, reclaim=False
    )
    return frozenset((str(row.state_dir), row.run_id) for row in rows if row.run_id)


def pinned_run_ids(
    state_dir: str | Path | None = None,
) -> assurance_report.OpenIssueRuns:
    """Return the run IDs an open assurance issue still needs the artifacts of.

    An open issue points a human at the audit report that opened it, so
    reclaiming that run's directory would leave the inbox naming evidence that
    no longer exists. Closing the issue releases the pin; the ledger entry
    survives either way, because the ledger is not a run artifact.
    """
    return assurance_report.open_issue_run_ids(state_dir)


def _plan_report(
    *,
    plan_dir: Path,
    plan_state: Path,
    lock_info: state.LockInfo | None,
    max_age_days: float,
    now: datetime,
    live_runs: frozenset[tuple[str, str]],
    pinned: assurance_report.OpenIssueRuns,
) -> PlanRetention:
    cutoff = now - timedelta(days=max_age_days)
    locked = lock_blocker(lock_info)
    entries: list[tuple[Path, datetime | None, tuple[str, str]]] = []
    for run_dir in session_overview._iter_run_dirs(plan_state):
        finished, problem = _run_finished_at(run_dir)
        entries.append((run_dir, finished, problem))
    newest_name = _newest_run_name(entries)
    runs: list[RunCandidate] = []
    for run_dir, finished, problem in sorted(entries, key=lambda e: e[0].name):
        if locked:
            reason, detail = RETAIN_PLAN_LOCKED, locked
        elif (str(plan_state), run_dir.name) in live_runs:
            reason, detail = RETAIN_LIVE_RUN, "run is currently executing"
        elif not pinned.available:
            reason = RETAIN_PIN_UNKNOWN
            detail = (
                "assurance ledger is unreadable, so no run can be shown free of "
                f"an open issue: {pinned.error}"
            )
        elif run_dir.name in pinned.run_ids:
            reason = RETAIN_OPEN_ISSUE
            detail = "audit artifacts are referenced by an open assurance issue"
        elif problem[0]:
            reason, detail = problem
        elif run_dir.name == newest_name:
            reason, detail = RETAIN_NEWEST_RUN, "newest run of this plan"
        elif finished is not None and finished > cutoff:
            reason = RETAIN_WITHIN_CUTOFF
            detail = f"finished after the {max_age_days:g}-day cutoff"
        else:
            reason, detail = "", ""
        age_seconds = (now - finished).total_seconds() if finished is not None else None
        runs.append(
            RunCandidate(
                run_id=run_dir.name,
                path=run_dir,
                size_bytes=_dir_size_bytes(run_dir),
                finished_at=finished.isoformat() if finished is not None else None,
                age_seconds=age_seconds,
                reclaimable=not reason,
                reason=reason,
                detail=detail,
            )
        )
    return PlanRetention(
        plan_dir=str(plan_dir),
        state_dir=str(plan_state),
        max_age_days=max_age_days,
        cutoff_at=cutoff.isoformat(),
        runs=tuple(runs),
    )


def plan_retention(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
    live_runs: frozenset[tuple[str, str]] | None = None,
    pinned: assurance_report.OpenIssueRuns | None = None,
) -> PlanRetention:
    """Project which of one plan's run directories the age cutoff would reclaim.

    ``now`` must be timezone-aware and defaults to the current UTC instant; a
    run is old enough when it finished at or before ``now - max_age_days``.
    ``live_runs`` and ``pinned`` are computed from :func:`live_run_keys` and
    :func:`pinned_run_ids` when omitted; callers scanning many plans pass one
    shared value of each so the running scan and the ledger read happen once.
    ``pinned`` is the whole :class:`~assurance_report.OpenIssueRuns` rather than
    its run IDs, so a caller cannot hand over the pins without the fact that
    reading them succeeded.
    """
    if max_age_days < 0:
        raise ValueError("max_age_days must be non-negative")
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    return _plan_report(
        plan_dir=canonical_plan_dir,
        plan_state=state.plan_state_dir(canonical_plan_dir, state_dir),
        lock_info=state_locks.inspect_lock_info(canonical_plan_dir, state_dir),
        max_age_days=max_age_days,
        now=now if now is not None else datetime.now(UTC),
        live_runs=live_runs if live_runs is not None else live_run_keys(state_dir),
        pinned=pinned if pinned is not None else pinned_run_ids(state_dir),
    )


def _recover_plan_dir(plan_state: Path) -> Path | None:
    """Recover the plan directory a state directory belongs to, or ``None``."""
    state_plan_dir, lock_plan_dir = session_overview._read_state_dir_plan_dir_fallbacks(
        plan_state
    )
    for candidate in (state_plan_dir, lock_plan_dir):
        if not candidate:
            continue
        try:
            canonical = plans_files.canonicalize_path(candidate)
        except OSError, ValueError:
            continue
        if plans_files.path_sha256(canonical) == plan_state.name:
            return canonical
    return None


def corpus_retention(
    state_dir: str | Path | None = None,
    *,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
    pinned: assurance_report.OpenIssueRuns | None = None,
) -> CorpusRetention:
    """Project the age cutoff across every plan state directory in a state root.

    State directories whose plan directory cannot be recovered (or that hash to
    a different plan than they claim) are reported in ``unresolved_state_dirs``
    rather than judged, since neither their lock nor their identity can be
    trusted.
    """
    if max_age_days < 0:
        raise ValueError("max_age_days must be non-negative")
    root = state.state_root(state_dir)
    moment = now if now is not None else datetime.now(UTC)
    live = live_run_keys(root)
    pins = pinned if pinned is not None else pinned_run_ids(root)
    reports: list[PlanRetention] = []
    unresolved: list[str] = []
    for plan_state in session_overview._iter_state_plan_dirs(root):
        plan_dir = _recover_plan_dir(plan_state)
        if plan_dir is None:
            unresolved.append(str(plan_state))
            continue
        reports.append(
            _plan_report(
                plan_dir=plan_dir,
                plan_state=plan_state,
                lock_info=state_locks.inspect_lock_info(plan_dir, root),
                max_age_days=max_age_days,
                now=moment,
                live_runs=live,
                pinned=pins,
            )
        )
    reports.sort(key=lambda report: report.plan_dir)
    return CorpusRetention(
        state_root=str(root),
        max_age_days=max_age_days,
        cutoff_at=(moment - timedelta(days=max_age_days)).isoformat(),
        plans=tuple(reports),
        unresolved_state_dirs=tuple(sorted(unresolved)),
    )
