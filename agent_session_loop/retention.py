"""Project which persisted run directories an age cutoff would reclaim.

Nothing has ever deleted a ``runs/<run-id>/`` directory, and the per-turn
``stderr.log`` files inside them dominate the state corpus. ``compact_state``
rewrites JSON records but never touches those logs, which is why it projects a
rounding error against gigabytes of growth.

The projection functions compute the deletion *plan* only and never remove
anything; :func:`run_gc` is the sole caller that deletes, and only when its
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

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import assurance_ledger, assurance_report, plans, session_overview, state

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
    "PlanGC",
    "GCReport",
    "live_run_keys",
    "pinned_run_ids",
    "lock_blocker",
    "plan_retention",
    "corpus_retention",
    "remove_run_directory",
    "format_bytes",
    "run_gc",
    "gc_from_args",
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
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    return _plan_report(
        plan_dir=canonical_plan_dir,
        plan_state=state.plan_state_dir(canonical_plan_dir, state_dir),
        lock_info=state.inspect_lock_info(canonical_plan_dir, state_dir),
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
            canonical = plans.canonicalize_path(candidate)
        except OSError, ValueError:
            continue
        if plans.path_sha256(canonical) == plan_state.name:
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
                lock_info=state.inspect_lock_info(plan_dir, root),
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


@dataclass(frozen=True)
class PlanGC:
    """One plan's projection plus whatever ``--apply`` actually removed."""

    retention: PlanRetention
    removed_run_ids: tuple[str, ...]
    reclaimed_bytes: int
    failures: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        payload = self.retention.to_dict()
        payload.update(
            {
                "removed_run_count": len(self.removed_run_ids),
                "removed_run_ids": list(self.removed_run_ids),
                "reclaimed_bytes": self.reclaimed_bytes,
                "failed_removals": [
                    {"run_id": run_id, "error": error}
                    for run_id, error in self.failures
                ],
            }
        )
        return payload


@dataclass(frozen=True)
class GCReport:
    """Outcome of one ``asl gc`` invocation over a plan or a whole state root."""

    scope: str
    applied: bool
    state_root: str
    max_age_days: float
    cutoff_at: str
    plans: tuple[PlanGC, ...]
    unresolved_state_dirs: tuple[str, ...]
    assurance: dict[str, Any] = field(default_factory=dict)

    @property
    def run_count(self) -> int:
        return sum(len(entry.retention.runs) for entry in self.plans)

    @property
    def reclaimable_run_count(self) -> int:
        return sum(len(entry.retention.reclaimable_runs) for entry in self.plans)

    @property
    def reclaimable_bytes(self) -> int:
        return sum(entry.retention.reclaimable_bytes for entry in self.plans)

    @property
    def total_bytes(self) -> int:
        return sum(entry.retention.total_bytes for entry in self.plans)

    @property
    def removed_run_count(self) -> int:
        return sum(len(entry.removed_run_ids) for entry in self.plans)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(entry.reclaimed_bytes for entry in self.plans)

    @property
    def failure_count(self) -> int:
        return sum(len(entry.failures) for entry in self.plans)

    def retained_by_reason(self) -> dict[str, dict[str, int]]:
        """Aggregate retained runs and bytes per retain reason."""
        totals: dict[str, dict[str, int]] = {}
        for entry in self.plans:
            for run in entry.retention.runs:
                if run.reclaimable:
                    continue
                bucket = totals.setdefault(run.reason, {"run_count": 0, "bytes": 0})
                bucket["run_count"] += 1
                bucket["bytes"] += run.size_bytes
        return dict(sorted(totals.items()))

    def locked_plans(self) -> tuple[tuple[str, str], ...]:
        """Return ``(plan_dir, detail)`` for plans refused because of a lock."""
        refused: list[tuple[str, str]] = []
        for entry in self.plans:
            for run in entry.retention.runs:
                if run.reason == RETAIN_PLAN_LOCKED:
                    refused.append((entry.retention.plan_dir, run.detail))
                    break
        return tuple(refused)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "gc",
            "scope": self.scope,
            "applied": self.applied,
            "state_root": self.state_root,
            "max_age_days": self.max_age_days,
            "cutoff_at": self.cutoff_at,
            "plan_count": len(self.plans),
            "run_count": self.run_count,
            "reclaimable_run_count": self.reclaimable_run_count,
            "reclaimable_bytes": self.reclaimable_bytes,
            "total_bytes": self.total_bytes,
            "removed_run_count": self.removed_run_count,
            "reclaimed_bytes": self.reclaimed_bytes,
            "failed_removal_count": self.failure_count,
            "retained_by_reason": self.retained_by_reason(),
            "unresolved_state_dirs": list(self.unresolved_state_dirs),
            "assurance": dict(self.assurance),
            "plans": [entry.to_dict() for entry in self.plans],
        }


def remove_run_directory(path: Path) -> str:
    """Delete one run directory, returning ``""`` or the failure message.

    The parent check is a last stop before an unrecoverable delete: only a
    ``runs/<run-id>/`` directory is ever removed, whatever a caller passes.
    Public because ``asl maintenance`` deletes on the same terms as ``asl gc``
    and must go through this check rather than around it.
    """
    if path.parent.name != state.RUNS_DIR_NAME:
        return f"{path} is not a runs/<run-id> directory"
    try:
        shutil.rmtree(path)
    except OSError as error:
        return str(error)
    return ""


def _sweep_plan(report: PlanRetention, *, apply: bool) -> PlanGC:
    if not apply:
        return PlanGC(
            retention=report,
            removed_run_ids=(),
            reclaimed_bytes=0,
            failures=(),
        )
    removed: list[str] = []
    failures: list[tuple[str, str]] = []
    reclaimed = 0
    for candidate in report.reclaimable_runs:
        error = remove_run_directory(candidate.path)
        if error:
            failures.append((candidate.run_id, error))
            continue
        removed.append(candidate.run_id)
        reclaimed += candidate.size_bytes
    return PlanGC(
        retention=report,
        removed_run_ids=tuple(removed),
        reclaimed_bytes=reclaimed,
        failures=tuple(failures),
    )


def _present_run_ids(root: Path) -> set[str]:
    """Return the run IDs that still have a directory somewhere under ``root``."""
    present: set[str] = set()
    for plan_state in session_overview._iter_state_plan_dirs(root):
        for run_dir in session_overview._iter_run_dirs(plan_state):
            present.add(run_dir.name)
    return present


def _assurance_payload(
    pinned: assurance_report.OpenIssueRuns, *, root: Path
) -> dict[str, Any]:
    """Report what the inbox pinned and which of its references are already gone.

    Missing references are always resolved against the whole state root, so a
    plan-scoped sweep does not report another plan's runs as lost. A reference
    that no longer resolves is stated rather than hidden: the ledger keeps the
    issue's history whether or not the artifacts survive, and an operator
    following the trail deserves to learn that up front.

    ``collection_blocked`` states the fail-closed consequence of an unreadable
    ledger up front, so a JSON consumer never has to infer it from an
    ``available: false`` beside a pin count of zero.
    """
    payload: dict[str, Any] = {
        # Structural, not a policy: the ledger sits directly under the state
        # root and the sweep only ever removes ``runs/<run-id>/`` directories.
        "ledger_path": str(assurance_ledger.ledger_path(root)),
        "ledger_excluded": True,
        "available": pinned.available,
        "collection_blocked": not pinned.available,
        "error": pinned.error,
        "open_issue_count": pinned.open_issue_count,
        "pinned_run_count": len(pinned.run_ids),
        "missing_run_references": [],
    }
    if pinned.run_ids:
        payload["missing_run_references"] = sorted(
            pinned.run_ids - _present_run_ids(root)
        )
    return payload


def run_gc(
    plan_dir: str | Path | None = None,
    *,
    state_dir: str | Path | None = None,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    apply: bool = False,
    now: datetime | None = None,
) -> GCReport:
    """Project — and under ``apply``, delete — the reclaimable run directories.

    ``plan_dir`` restricts the sweep to one plan; omitting it judges every plan
    state directory under the resolved state root, which is the aggregate the
    corpus-wide dry run reports.
    """
    root_path = state.state_root(state_dir)
    pinned = pinned_run_ids(root_path)
    if plan_dir is None:
        corpus = corpus_retention(
            state_dir,
            max_age_days=max_age_days,
            now=now,
            pinned=pinned,
        )
        scope = "corpus"
        root = corpus.state_root
        cutoff_at = corpus.cutoff_at
        reports: tuple[PlanRetention, ...] = corpus.plans
        unresolved = corpus.unresolved_state_dirs
    else:
        report = plan_retention(
            plan_dir,
            state_dir=state_dir,
            max_age_days=max_age_days,
            now=now,
            pinned=pinned,
        )
        scope = "plan"
        root = str(root_path)
        cutoff_at = report.cutoff_at
        reports = (report,)
        unresolved = ()
    return GCReport(
        scope=scope,
        applied=apply,
        state_root=root,
        max_age_days=max_age_days,
        cutoff_at=cutoff_at,
        plans=tuple(_sweep_plan(report, apply=apply) for report in reports),
        unresolved_state_dirs=unresolved,
        assurance=_assurance_payload(pinned, root=root_path),
    )


def format_bytes(count: int) -> str:
    """Render a byte count the way ``asl metrics`` renders its corpus totals."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(count)} B"


def _print_report(report: GCReport) -> None:
    mode = "applied" if report.applied else "dry-run"
    if report.scope == "plan":
        target = report.plans[0].retention.plan_dir
    else:
        target = f"state root {report.state_root}"
    print(f"Retention gc ({mode}) for {target}")
    print(f"Cutoff: {report.cutoff_at} (max age {report.max_age_days:g} days)")
    unresolved = len(report.unresolved_state_dirs)
    plan_line = f"Plans: {len(report.plans)} judged"
    if unresolved:
        plan_line += f", {unresolved} state dir(s) unresolved and not judged"
    print(plan_line)
    print(f"Runs: {report.reclaimable_run_count} reclaimable of {report.run_count}")
    print(
        f"Reclaimable: {format_bytes(report.reclaimable_bytes)} of "
        f"{format_bytes(report.total_bytes)} measured"
    )

    if report.scope == "plan":
        for run in report.plans[0].retention.runs:
            marker = "reclaim" if run.reclaimable else run.reason
            detail = f": {run.detail}" if run.detail else ""
            print(f"  {run.run_id}  {format_bytes(run.size_bytes)}  {marker}{detail}")
    else:
        reclaiming = sorted(
            (entry for entry in report.plans if entry.retention.reclaimable_runs),
            key=lambda entry: entry.retention.reclaimable_bytes,
            reverse=True,
        )
        if reclaiming:
            print("Reclaimable by plan:")
            for entry in reclaiming:
                print(
                    f"  {format_bytes(entry.retention.reclaimable_bytes)}  "
                    f"{len(entry.retention.reclaimable_runs)} run(s)  "
                    f"{entry.retention.plan_dir}"
                )

    retained = report.retained_by_reason()
    if retained:
        print("Retained by reason:")
        for reason, totals in retained.items():
            print(
                f"  {reason}: {totals['run_count']} run(s), "
                f"{format_bytes(totals['bytes'])}"
            )
    locked = report.locked_plans()
    if locked:
        print("Plans refused for a lock:")
        for plan_path, detail in locked:
            print(f"  {plan_path}: {detail}")

    assurance = report.assurance
    if assurance:
        if not assurance["available"]:
            print(
                f"Assurance inbox: unreadable ({assurance['error']}); "
                "collection is blocked and every run is retained under "
                f"`{RETAIN_PIN_UNKNOWN}`"
            )
        else:
            print(
                f"Assurance inbox: {assurance['open_issue_count']} open issue(s) "
                f"pinning {assurance['pinned_run_count']} run(s); ledger kept at "
                f"{assurance['ledger_path']}"
            )
        missing = assurance["missing_run_references"]
        if missing:
            print(
                f"Assurance inbox: {len(missing)} pinned run(s) no longer on disk: "
                f"{', '.join(missing[:5])}"
                f"{' …' if len(missing) > 5 else ''}"
            )

    if report.applied:
        print(
            f"Removed: {report.removed_run_count} run(s), "
            f"{format_bytes(report.reclaimed_bytes)} reclaimed"
        )
        for entry in report.plans:
            for run_id, error in entry.failures:
                print(
                    f"  failed to remove {run_id} ({entry.retention.plan_dir}): {error}"
                )
    elif report.reclaimable_run_count:
        print("Re-run with --apply to delete the reclaimable runs.")


def gc_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl gc``."""
    try:
        report = run_gc(
            getattr(args, "plan_dir", None),
            state_dir=getattr(args, "state_dir", None),
            max_age_days=getattr(args, "max_age_days", DEFAULT_MAX_AGE_DAYS),
            apply=bool(getattr(args, "apply", False)),
        )
    except (OSError, ValueError) as error:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {"ok": False, "command": "gc", "error": str(error)},
                    sort_keys=True,
                )
            )
        else:
            print(f"gc failed: {error}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        _print_report(report)
    return 1 if report.failure_count else 0
