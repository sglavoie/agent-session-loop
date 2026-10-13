"""Budgeted, ASL-owned storage maintenance for the operational state corpus.

``asl gc`` is a manual, age-only sweep an operator has to remember to run. This
module is the scheduled counterpart: a daily launchd job ASL installs, owns, and
uninstalls, which does nothing at all until the corpus is actually too big and
then reclaims only what policy permits it to reclaim.

Three rules govern everything here.

**Thresholds gate the work.** Maintenance runs only when the corpus exceeds
:data:`CORPUS_BUDGET_BYTES` or the filesystem holding it has less than
:data:`MIN_FREE_FRACTION` free. Under both thresholds the run is a measured
no-op, so the daily job costs one stat walk on a healthy machine.

**Protection is retention's classifier, unchanged.** A run directory is
reclaimable here exactly when :mod:`agent_session_loop.retention` says it is:
the plan carries no lock, the run is not executing, the assurance ledger was
readable and pins nothing to it, it finished, it is dateable, it is not its
plan's newest run, and it is older than the cutoff. Every fail-closed fence in
that module — an unreadable ledger retains everything, an undated run is never
old enough — is inherited rather than restated. Maintenance adds one further
lane and no further permissions: the *newest* run of a plan is never deleted,
but once it is terminal and older than :data:`COMPACTION_AGE_DAYS` its
reproducible log bodies may be compressed, and the bytes above
:data:`LOG_BODY_BUDGET_BYTES` removed. Authoritative records — ``state.json``,
``events.jsonl``, every ``result.json`` — are never touched by maintenance in
any lane.

**Nothing is deleted on stale knowledge.** The whole mutating pass runs under a
single state-root-wide ``flock``, so two maintenance runs can never interleave.
That claim bounds only other maintenance passes, though — a run lock, a live
runner, and an assurance pin are all written by processes that never take it —
so every fact this module deletes on is re-read at the moment it is used rather
than carried forward. The thresholds are re-measured once the claim is held,
and a pass that finds the pressure already relieved by whoever it waited for
returns without mutating unless ``force`` was asked for explicitly. Liveness,
pins, and plan locks are re-read under the claim to narrow the plan, and then
again at every ``unlink`` rather than merely in front of the operation that
contains one: compressing a multi-gigabyte body and digesting one both take real
time, so a fence taken before that work has gone stale by the moment the bytes
actually go. A protection that lands during the preparation therefore still
wins, and what it leaves on disk is true — a staged gzip nobody published is
discarded, a published gzip and its sidecar describe a body that is still there
and say so, and a tombstone written for a removal that then did not happen is
withdrawn. Every removal writes its metadata durably before the bytes go, so an
interrupted pass can leave a tombstone with nothing removed but never removed
bytes with no tombstone. Byte accounting is measured after the fact, never
assumed: a failed removal reports the bytes that are still allocated instead of
counting them as reclaimed.

When protected data alone still exceeds the budget, maintenance deletes nothing
outside policy and raises a strict health finding instead. That is the one
outcome a human has to resolve, and it is exactly the outcome an automatic
sweeper must never resolve on its own.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import gzip
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import durability, retention, state, state_locks

__all__ = [
    "CORPUS_BUDGET_BYTES",
    "MIN_FREE_FRACTION",
    "COMPACTION_AGE_DAYS",
    "LOG_BODY_BUDGET_BYTES",
    "DEFAULT_MAX_RUNTIME_SECONDS",
    "JOB_LABEL",
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "LOCK_FILE_NAME",
    "LOG_BODY_NAMES",
    "OUTCOME_UNDER_THRESHOLD",
    "OUTCOME_LOCK_UNAVAILABLE",
    "OUTCOME_PRESSURE_RELIEVED",
    "OUTCOME_PLANNED",
    "OUTCOME_APPLIED",
    "LANE_RECLAIM",
    "LANE_COMPACTION",
    "STAGE_REVALIDATION",
    "STAGE_MUTATION",
    "TRIGGER_NONE",
    "TRIGGER_FORCED",
    "TRIGGER_CORPUS_OVER_BUDGET",
    "TRIGGER_LOW_FREE_SPACE",
    "HEALTH_PROTECTED_OVER_BUDGET",
    "HEALTH_PARTIAL_FAILURE",
    "HEALTH_DEADLINE_EXCEEDED",
    "REASON_LOG_BODY_BUDGET",
    "MaintenanceError",
    "CorpusUsage",
    "HealthFinding",
    "MaintenanceFailure",
    "LogBodyAction",
    "RunCompaction",
    "MaintenancePlan",
    "MaintenanceResult",
    "JobStatus",
    "measure_corpus",
    "manifest_path",
    "lock_path",
    "plan_maintenance",
    "run_maintenance",
    "read_manifest",
    "render_plist",
    "plist_path",
    "install_job",
    "uninstall_job",
    "job_status",
    "install_from_args",
    "run_from_args",
    "status_from_args",
    "uninstall_from_args",
]


#: Corpus size above which maintenance is permitted to reclaim.
CORPUS_BUDGET_BYTES = 8 * 1024**3

#: Filesystem free fraction below which maintenance is permitted to reclaim,
#: even when the corpus itself is comfortably under budget: a full volume is an
#: operational emergency whoever filled it.
MIN_FREE_FRACTION = 0.15

#: Age at which a terminal newest run's reproducible detail becomes compactable.
COMPACTION_AGE_DAYS = 90.0

#: Retained log-body bytes permitted per compactable run, measured on disk after
#: compression. Bytes above this are removed largest-first, leaving a tombstone.
LOG_BODY_BUDGET_BYTES = 64 * 1024**2

#: Wall-clock ceiling on one maintenance pass. launchd cannot bound a job, so
#: the bound lives here: work stops cleanly at the deadline and the remainder is
#: reported rather than abandoned silently.
DEFAULT_MAX_RUNTIME_SECONDS = 900.0

#: How long to wait for the global maintenance claim before declining. A daily
#: job that finds another pass running has nothing useful to do.
LOCK_WAIT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.05

#: The reproducible per-turn bodies. Every one of these can be regenerated by
#: re-running the turn, and none of them is the authoritative record of what the
#: turn decided — that is ``result.json``, which maintenance never touches.
LOG_BODY_NAMES = ("stdout.log", "stderr.log", "last-message.txt")

#: Suffix of the durable ledger written beside every body maintenance touches.
BODY_LEDGER_SUFFIX = ".body.json"
BODY_LEDGER_SCHEMA_VERSION = 1

JOB_LABEL = "com.sglavoie.asl-maintenance"
DEFAULT_HOUR = 3
DEFAULT_MINUTE = 30
LAUNCH_AGENTS_RELATIVE_DIR = "Library/LaunchAgents"
LAUNCHCTL_TIMEOUT_SECONDS = 30

MANIFEST_FILE_NAME = "maintenance.json"
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_HISTORY_LIMIT = 10
LOCK_FILE_NAME = "maintenance.lock"
JOB_LOG_FILE_NAME = "maintenance.log"

TRIGGER_NONE = "none"
TRIGGER_FORCED = "forced"
TRIGGER_CORPUS_OVER_BUDGET = "corpus-over-budget"
TRIGGER_LOW_FREE_SPACE = "filesystem-low-free-space"

OUTCOME_UNDER_THRESHOLD = "under-threshold"
OUTCOME_LOCK_UNAVAILABLE = "lock-unavailable"
#: A pass that waited for the claim and found the corpus healthy by the time it
#: was granted. The pressure that admitted it was relieved by whoever it waited
#: for, so there is nothing left for it to justify deleting.
OUTCOME_PRESSURE_RELIEVED = "pressure-relieved"
OUTCOME_PLANNED = "planned"
OUTCOME_APPLIED = "applied"

#: The two lanes a run can be dropped from, and when the drop was decided.
LANE_RECLAIM = "reclaim"
LANE_COMPACTION = "compaction"
STAGE_REVALIDATION = "revalidation"
STAGE_MUTATION = "mutation"

HEALTH_PROTECTED_OVER_BUDGET = "protected-over-budget"
HEALTH_PARTIAL_FAILURE = "partial-failure"
HEALTH_DEADLINE_EXCEEDED = "deadline-exceeded"

#: Why a body was removed. One value today, named rather than inlined because a
#: tombstone that does not say why it exists is not evidence.
REASON_LOG_BODY_BUDGET = "log-body-budget"

FAILURE_RUN_REMOVAL = "run-removal"
FAILURE_BODY_COMPRESS = "body-compress"
FAILURE_BODY_REMOVE = "body-remove"
FAILURE_MANIFEST_WRITE = "manifest-write"

_CHUNK_BYTES = 1024 * 1024


class MaintenanceError(Exception):
    """Raised when maintenance cannot be planned, applied, or installed."""


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _file_size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


def _tree_bytes(path: Path) -> int:
    """Sum the apparent size of every file under ``path``, symlinks not followed."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


@dataclass(frozen=True)
class CorpusUsage:
    """What the state corpus costs, and whether that crosses either threshold."""

    state_root: str
    measured_at: str
    corpus_bytes: int
    budget_bytes: int
    filesystem_total_bytes: int
    filesystem_free_bytes: int
    free_fraction: float
    min_free_fraction: float

    @property
    def over_budget(self) -> bool:
        return self.corpus_bytes > self.budget_bytes

    @property
    def low_free_space(self) -> bool:
        return self.free_fraction < self.min_free_fraction

    @property
    def triggered(self) -> bool:
        return self.over_budget or self.low_free_space

    @property
    def trigger(self) -> str:
        """The first crossed threshold, or :data:`TRIGGER_NONE`."""
        if self.over_budget:
            return TRIGGER_CORPUS_OVER_BUDGET
        if self.low_free_space:
            return TRIGGER_LOW_FREE_SPACE
        return TRIGGER_NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_root": self.state_root,
            "measured_at": self.measured_at,
            "corpus_bytes": self.corpus_bytes,
            "budget_bytes": self.budget_bytes,
            "filesystem_total_bytes": self.filesystem_total_bytes,
            "filesystem_free_bytes": self.filesystem_free_bytes,
            "free_fraction": self.free_fraction,
            "min_free_fraction": self.min_free_fraction,
            "over_budget": self.over_budget,
            "low_free_space": self.low_free_space,
            "triggered": self.triggered,
            "trigger": self.trigger,
        }


def measure_corpus(
    state_dir: str | Path | None = None,
    *,
    now: datetime | None = None,
    budget_bytes: int = CORPUS_BUDGET_BYTES,
    min_free_fraction: float = MIN_FREE_FRACTION,
) -> CorpusUsage:
    """Measure the corpus and the volume holding it against both thresholds.

    A state root that does not exist yet measures zero bytes rather than
    raising: an installation with no runs is under budget, not broken. The free
    fraction is read from the nearest existing ancestor so the same is true of
    the volume probe.
    """
    root = state.state_root(state_dir)
    corpus_bytes = _tree_bytes(root) if root.is_dir() else 0
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        stats = os.statvfs(probe)
    except OSError as error:
        raise MaintenanceError(
            f"could not measure free space on the volume holding {root}: {error}"
        ) from error
    total = stats.f_frsize * stats.f_blocks
    free = stats.f_frsize * stats.f_bavail
    fraction = (free / total) if total > 0 else 1.0
    return CorpusUsage(
        state_root=str(root),
        measured_at=_now(now).isoformat(),
        corpus_bytes=corpus_bytes,
        budget_bytes=budget_bytes,
        filesystem_total_bytes=total,
        filesystem_free_bytes=free,
        free_fraction=fraction,
        min_free_fraction=min_free_fraction,
    )


def manifest_path(state_dir: str | Path | None = None) -> Path:
    """Path of the durable maintenance manifest for a state root."""
    return state.state_root(state_dir) / MANIFEST_FILE_NAME


def lock_path(state_dir: str | Path | None = None) -> Path:
    """Path of the state-root-wide maintenance claim."""
    return state.state_root(state_dir) / LOCK_FILE_NAME


def job_log_path(state_dir: str | Path | None = None) -> Path:
    """Path launchd is told to append the job's own output to."""
    return state.state_root(state_dir) / JOB_LOG_FILE_NAME


@dataclass(frozen=True)
class HealthFinding:
    """One thing about this corpus a human may need to act on."""

    code: str
    strict: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "strict": self.strict, "detail": self.detail}


@dataclass(frozen=True)
class MaintenanceFailure:
    """One mutation that did not land, and the bytes it left allocated."""

    kind: str
    target: str
    error: str
    bytes_still_allocated: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "error": self.error,
            "bytes_still_allocated": self.bytes_still_allocated,
        }


@dataclass(frozen=True)
class LogBodyAction:
    """What maintenance did — or projected doing — to one reproducible body."""

    path: str
    action: str
    byte_count: int
    stored_bytes: int
    sha256: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "byte_count": self.byte_count,
            "stored_bytes": self.stored_bytes,
            "sha256": self.sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RunCompaction:
    """One terminal newest run judged against the log-body budget."""

    plan_dir: str
    run_id: str
    run_path: str
    finished_at: str | None
    age_seconds: float | None
    body_bytes_before: int
    body_bytes_after: int
    removable_bytes: int
    actions: tuple[LogBodyAction, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "run_id": self.run_id,
            "run_path": self.run_path,
            "finished_at": self.finished_at,
            "age_seconds": self.age_seconds,
            "body_bytes_before": self.body_bytes_before,
            "body_bytes_after": self.body_bytes_after,
            "removable_bytes": self.removable_bytes,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class MaintenancePlan:
    """Everything policy permits on this corpus, before anything is mutated."""

    usage: CorpusUsage
    forced: bool
    reclaim: retention.GCReport
    compactions: tuple[RunCompaction, ...]
    compaction_age_days: float
    log_body_budget_bytes: int

    @property
    def trigger(self) -> str:
        if self.usage.triggered:
            return self.usage.trigger
        return TRIGGER_FORCED if self.forced else TRIGGER_NONE

    @property
    def triggered(self) -> bool:
        return self.usage.triggered or self.forced

    @property
    def reclaimable_bytes(self) -> int:
        return self.reclaim.reclaimable_bytes

    @property
    def removable_body_bytes(self) -> int:
        return sum(entry.removable_bytes for entry in self.compactions)

    @property
    def projected_protected_bytes(self) -> int:
        """Corpus bytes this policy has no permission to remove.

        Compression is deliberately not counted here. A compressed body is
        still retained evidence, so shrinking it does not make it removable;
        counting the ratio would let a favourable compression hide a corpus
        that policy genuinely cannot bring under budget.
        """
        return max(
            0,
            self.usage.corpus_bytes
            - self.reclaimable_bytes
            - self.removable_body_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "usage": self.usage.to_dict(),
            "forced": self.forced,
            "trigger": self.trigger,
            "triggered": self.triggered,
            "compaction_age_days": self.compaction_age_days,
            "log_body_budget_bytes": self.log_body_budget_bytes,
            "reclaimable_run_count": self.reclaim.reclaimable_run_count,
            "reclaimable_bytes": self.reclaimable_bytes,
            "removable_body_bytes": self.removable_body_bytes,
            "projected_protected_bytes": self.projected_protected_bytes,
            "retained_by_reason": self.reclaim.retained_by_reason(),
            "compactions": [entry.to_dict() for entry in self.compactions],
        }


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


class _Deadline:
    """The wall-clock bound on one pass, checked between units of work."""

    def __init__(self, seconds: float) -> None:
        self._seconds = max(0.0, seconds)
        self._started = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self._started) >= self._seconds

    def elapsed(self) -> float:
        return round(time.monotonic() - self._started, 3)


@contextlib.contextmanager
def _maintenance_claim(path: Path, *, wait_seconds: float) -> Iterator[bool]:
    """Hold the state-root-wide exclusive claim on mutating this corpus.

    ``flock`` and not a lock *file* with a payload: the claim has to be released
    by the kernel when a maintenance process dies, or an interrupted pass would
    disable every later one. The sidecar is never unlinked, so its identity is
    stable for everyone queuing behind it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    held = False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_SECONDS)
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _iter_turn_dirs(run_dir: Path) -> list[Path]:
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return []
    return sorted(
        (child for child in children if child.is_dir() and not child.is_symlink()),
        key=lambda child: child.name,
    )


@dataclass(frozen=True)
class _Body:
    """One reproducible body on disk, in whichever form it currently has."""

    source: Path
    stored: Path
    compressed: bool
    stored_bytes: int


@dataclass(frozen=True)
class _BodyOutcome:
    """What one compression left behind: a compacted body, or why it stopped.

    ``body`` is the compacted replacement when the whole operation landed.
    ``blocked`` is non-empty when protection arrived mid-flight, which is not a
    failure — nothing is broken and nothing is lost — but does end this run's
    compaction. Both empty means the compression failed and said so in
    ``failures``.
    """

    body: _Body | None
    blocked: str


def _iter_bodies(run_dir: Path) -> list[_Body]:
    """Return every reproducible body of ``run_dir`` in a deterministic order.

    A body already removed by an earlier pass is skipped: its ledger records it
    and there is nothing left to act on.
    """
    bodies: list[_Body] = []
    for turn_dir in _iter_turn_dirs(run_dir):
        for name in LOG_BODY_NAMES:
            raw = turn_dir / name
            packed = turn_dir / f"{name}.gz"
            if raw.is_file() and not raw.is_symlink():
                bodies.append(
                    _Body(
                        source=raw,
                        stored=raw,
                        compressed=False,
                        stored_bytes=_file_size(raw),
                    )
                )
            elif packed.is_file() and not packed.is_symlink():
                bodies.append(
                    _Body(
                        source=raw,
                        stored=packed,
                        compressed=True,
                        stored_bytes=_file_size(packed),
                    )
                )
    return sorted(bodies, key=lambda body: str(body.source))


def _ledger_path(source: Path) -> Path:
    return source.with_name(f"{source.name}{BODY_LEDGER_SUFFIX}")


def _read_ledger(source: Path) -> dict[str, Any]:
    path = _ledger_path(source)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_ledger(source: Path, payload: dict[str, Any]) -> None:
    """Persist a body's metadata durably, before the body itself can go.

    Durable and not merely atomic: the ordering that makes an interrupted pass
    safe is "metadata is on the platter, then bytes are removed", and only a
    completed fsync of both the file and its directory establishes that.
    """
    durability.write_json_file(_ledger_path(source), payload, durable=True)


def _over_budget_selection(
    bodies: list[_Body], *, budget_bytes: int
) -> tuple[list[_Body], int]:
    """Pick the bodies to remove so the rest fit ``budget_bytes``.

    Largest first, ties broken by path, so the selection is a pure function of
    the sizes on disk: the same corpus plans the same removals on every host and
    on every repeat, and the fewest bodies are touched to reach the budget.
    """
    ordered = sorted(bodies, key=lambda body: (-body.stored_bytes, str(body.source)))
    retained = sum(body.stored_bytes for body in bodies)
    selected: list[_Body] = []
    removable = 0
    for body in ordered:
        if retained <= budget_bytes:
            break
        selected.append(body)
        retained -= body.stored_bytes
        removable += body.stored_bytes
    return selected, removable


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


@dataclass(frozen=True)
class MaintenanceResult:
    """One maintenance pass: what it was allowed to do and what it did."""

    ok: bool
    schema_version: int
    outcome: str
    trigger: str
    applied: bool
    state_root: str
    started_at: str
    finished_at: str
    duration_seconds: float
    usage: CorpusUsage
    usage_at_claim: CorpusUsage | None
    usage_after: CorpusUsage | None
    plan: MaintenancePlan | None
    removed_run_ids: tuple[str, ...]
    reclaimed_bytes: int
    compressed_body_count: int
    removed_body_count: int
    compaction_reclaimed_bytes: int
    revalidation_drops: tuple[dict[str, str], ...]
    failures: tuple[MaintenanceFailure, ...]
    health: tuple[HealthFinding, ...]
    manifest_path: str

    @property
    def strict_findings(self) -> tuple[HealthFinding, ...]:
        return tuple(finding for finding in self.health if finding.strict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": "maintenance run",
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "trigger": self.trigger,
            "applied": self.applied,
            "state_root": self.state_root,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "usage": self.usage.to_dict(),
            "usage_at_claim": (
                self.usage_at_claim.to_dict()
                if self.usage_at_claim is not None
                else None
            ),
            "usage_after": (
                self.usage_after.to_dict() if self.usage_after is not None else None
            ),
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "removed_run_count": len(self.removed_run_ids),
            "removed_run_ids": list(self.removed_run_ids),
            "reclaimed_bytes": self.reclaimed_bytes,
            "compressed_body_count": self.compressed_body_count,
            "removed_body_count": self.removed_body_count,
            "compaction_reclaimed_bytes": self.compaction_reclaimed_bytes,
            "revalidation_drops": [dict(drop) for drop in self.revalidation_drops],
            "failure_count": len(self.failures),
            "failures": [failure.to_dict() for failure in self.failures],
            "health": [finding.to_dict() for finding in self.health],
            "manifest_path": self.manifest_path,
        }

    def summary(self) -> dict[str, Any]:
        """The bounded record the manifest keeps in its history."""
        return {
            "ok": self.ok,
            "outcome": self.outcome,
            "trigger": self.trigger,
            "applied": self.applied,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "corpus_bytes": self.usage.corpus_bytes,
            "removed_run_count": len(self.removed_run_ids),
            "reclaimed_bytes": self.reclaimed_bytes,
            "compressed_body_count": self.compressed_body_count,
            "removed_body_count": self.removed_body_count,
            "compaction_reclaimed_bytes": self.compaction_reclaimed_bytes,
            "failure_count": len(self.failures),
            "health_codes": [finding.code for finding in self.health],
        }


class _Mutator:
    """Applies one plan under the claim, accounting only for bytes truly freed.

    Every method here that destroys something asks ``fence`` first, so the plan
    is a list of *candidates* rather than a list of permissions: what the plan
    said was safe is re-established against current state at the instant each
    action is taken, and a candidate the fence refuses is dropped and reported
    instead of deleted.

    "The instant each action is taken" means the ``unlink``, not the call that
    leads to one. Compression and budgeted removal both read a whole body before
    they destroy anything, so they carry the fence inside and re-take it at that
    boundary; a body they were told to stop touching keeps its bytes, and any
    metadata they had already published about it is corrected rather than left
    describing a removal that did not happen.
    """

    def __init__(self, *, deadline: _Deadline, now: datetime, fence: _Fence) -> None:
        self.deadline = deadline
        self.now = now
        self.fence = fence
        self.removed_run_ids: list[str] = []
        self.reclaimed_bytes = 0
        self.compressed_body_count = 0
        self.removed_body_count = 0
        self.compaction_reclaimed_bytes = 0
        self.failures: list[MaintenanceFailure] = []
        self.drops: list[dict[str, str]] = []
        self.deadline_hit = False

    def out_of_time(self) -> bool:
        if self.deadline.expired():
            self.deadline_hit = True
            return True
        return False

    def _fenced_out(
        self, *, lane: str, plan_dir: str, plan_state_dir: str, run_id: str
    ) -> bool:
        """True when current state forbids touching this run, recording why."""
        reason = self.fence(plan_dir, plan_state_dir, run_id)
        if not reason:
            return False
        self.drops.append(
            _drop(
                lane=lane,
                stage=STAGE_MUTATION,
                plan_dir=plan_dir,
                run_id=run_id,
                reason=reason,
            )
        )
        return True

    def remove_run(self, target: _Removal) -> None:
        """Delete one run directory, counting only the bytes that actually went."""
        if self._fenced_out(
            lane=LANE_RECLAIM,
            plan_dir=target.plan_dir,
            plan_state_dir=target.plan_state_dir,
            run_id=target.run_id,
        ):
            return
        candidate = target.candidate
        error = retention.remove_run_directory(candidate.path)
        if not error:
            self.removed_run_ids.append(candidate.run_id)
            self.reclaimed_bytes += candidate.size_bytes
            return
        # A partial rmtree leaves an unknown remainder, so measure it rather
        # than assume either extreme: the difference is what was really freed.
        still_allocated = _tree_bytes(candidate.path) if candidate.path.exists() else 0
        self.reclaimed_bytes += max(0, candidate.size_bytes - still_allocated)
        self.failures.append(
            MaintenanceFailure(
                kind=FAILURE_RUN_REMOVAL,
                target=str(candidate.path),
                error=error,
                bytes_still_allocated=still_allocated,
            )
        )

    def compress_body(self, body: _Body, *, blocker: _Blocker) -> _BodyOutcome:
        """Replace one raw body with its gzip, recording the original first.

        The fence in front of this call proves only that the body was
        unprotected when compression *started*, and compressing a body is the
        slowest thing maintenance does. ``blocker`` is therefore re-read twice
        more: once with the gzip merely staged, where a refusal costs a
        temporary file and changes nothing else, and once immediately before the
        original is unlinked, which is the only destructive instruction here.
        """
        target = body.source.with_name(f"{body.source.name}.gz")

        def failed(error: str) -> _BodyOutcome:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_COMPRESS,
                    target=str(body.source),
                    error=error,
                    bytes_still_allocated=_file_size(body.source),
                )
            )
            return _BodyOutcome(body=None, blocked="")

        try:
            staged, digest, byte_count = _stage_compressed(body.source, target)
        except OSError as error:
            return failed(str(error))
        reason = blocker()
        if reason:
            # Nothing has been published, so withdrawing is exact: the staged
            # copy goes and the corpus is byte-for-byte what it was before.
            _discard_staged(staged)
            return _BodyOutcome(body=None, blocked=reason)
        try:
            os.replace(staged, target)
        except OSError as error:
            _discard_staged(staged)
            return failed(str(error))
        stored_bytes = _file_size(target)
        ledger = {
            "schema_version": BODY_LEDGER_SCHEMA_VERSION,
            "path": str(body.source),
            "byte_count": byte_count,
            "sha256": digest,
            "compressed_path": str(target),
            "compressed_bytes": stored_bytes,
            "compressed_at": self.now.isoformat(),
            "removed": False,
            "removed_at": None,
            "reason": "",
        }
        try:
            _write_ledger(body.source, ledger)
        except OSError as error:
            return failed(f"could not record the body ledger: {error}")
        reason = blocker()
        if reason:
            # The gzip and its ledger are published, and both are true: the
            # ledger says the body is not removed, and it is not. This is the
            # same recoverable shape an interrupted pass leaves — original plus
            # gzip plus sidecar — which the next unprotected pass converges.
            return _BodyOutcome(body=None, blocked=reason)
        try:
            body.source.unlink()
        except OSError as error:
            return failed(str(error))
        self.compressed_body_count += 1
        self.compaction_reclaimed_bytes += max(0, byte_count - stored_bytes)
        return _BodyOutcome(
            body=_Body(
                source=body.source,
                stored=target,
                compressed=True,
                stored_bytes=stored_bytes,
            ),
            blocked="",
        )

    def remove_body(self, body: _Body, *, blocker: _Blocker) -> str:
        """Remove one over-budget body, leaving its identity behind on disk.

        Returns the reason protection forbade the removal, or ``""``. Digesting
        a body that has no ledger yet reads every byte of it, so ``blocker`` is
        re-read after that preparation and again immediately before the unlink;
        a tombstone published in between is withdrawn rather than left standing
        over a body that is still there.
        """
        prepared = _read_ledger(body.source)
        if not prepared.get("sha256"):
            try:
                digest, byte_count = _digest_file(body.stored)
            except OSError as error:
                self.failures.append(
                    MaintenanceFailure(
                        kind=FAILURE_BODY_REMOVE,
                        target=str(body.stored),
                        error=str(error),
                        bytes_still_allocated=body.stored_bytes,
                    )
                )
                return ""
            prepared = {
                "schema_version": BODY_LEDGER_SCHEMA_VERSION,
                "path": str(body.source),
                "byte_count": byte_count,
                "sha256": digest,
                "compressed_path": str(body.stored) if body.compressed else None,
                "compressed_bytes": body.stored_bytes if body.compressed else None,
                "compressed_at": None,
                "removed": False,
                "removed_at": None,
                "reason": "",
            }
        reason = blocker()
        if reason:
            # Caught before the tombstone was published, so nothing on disk has
            # to be walked back at all.
            return reason
        ledger = {
            **prepared,
            "removed": True,
            "removed_at": self.now.isoformat(),
            "reason": REASON_LOG_BODY_BUDGET,
            "removed_path": str(body.stored),
            "removed_bytes": body.stored_bytes,
        }
        try:
            _write_ledger(body.source, ledger)
        except OSError as error:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_REMOVE,
                    target=str(body.stored),
                    error=f"could not record the body ledger: {error}",
                    bytes_still_allocated=body.stored_bytes,
                )
            )
            return ""
        reason = blocker()
        if reason:
            self._withdraw_tombstone(body, prepared)
            return reason
        try:
            body.stored.unlink()
        except OSError as error:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_REMOVE,
                    target=str(body.stored),
                    error=str(error),
                    bytes_still_allocated=_file_size(body.stored),
                )
            )
            return ""
        self.removed_body_count += 1
        self.compaction_reclaimed_bytes += body.stored_bytes
        return ""

    def _withdraw_tombstone(self, body: _Body, prepared: dict[str, Any]) -> None:
        """Undo a published tombstone for a body protection saved from removal.

        A ledger saying ``removed`` over a body that is still on disk is a lie
        an operator would act on, so it is rewritten to the pre-removal record —
        which keeps the digest the preparation just paid for. A rewrite that
        fails is a real partial failure: the bytes are safe, but the metadata
        beside them is wrong until a human or a later pass fixes it.
        """
        retracted = {
            key: value
            for key, value in prepared.items()
            if key not in ("removed_path", "removed_bytes")
        }
        retracted.update(removed=False, removed_at=None, reason="")
        try:
            _write_ledger(body.source, retracted)
        except OSError as error:
            self.failures.append(
                MaintenanceFailure(
                    kind=FAILURE_BODY_REMOVE,
                    target=str(body.stored),
                    error=f"could not withdraw the body ledger: {error}",
                    bytes_still_allocated=body.stored_bytes,
                )
            )

    def compact_run(self, target: _Compaction, *, log_body_budget_bytes: int) -> bool:
        """Compress then budget one run's bodies. False when the deadline hit.

        Compression is destructive too — the original is unlinked once the gzip
        and its ledger are durable — so the fence guards it as well as removal,
        and a run that acquires protection midway keeps every body it still has.
        The fence is handed down into both operations rather than only checked
        in front of them, because each one spends real time preparing before it
        destroys anything, and a reading that old is a memory, not a fence.
        """

        def blocker() -> str:
            return self.fence(target.plan_dir, target.plan_state_dir, target.run_id)

        def dropped(reason: str) -> None:
            self.drops.append(
                _drop(
                    lane=LANE_COMPACTION,
                    stage=STAGE_MUTATION,
                    plan_dir=target.plan_dir,
                    run_id=target.run_id,
                    reason=reason,
                )
            )

        bodies: list[_Body] = []
        for body in _iter_bodies(target.run_path):
            if self.out_of_time():
                return False
            if body.compressed:
                bodies.append(body)
                continue
            reason = blocker()
            if reason:
                dropped(reason)
                return True
            outcome = self.compress_body(body, blocker=blocker)
            if outcome.blocked:
                dropped(outcome.blocked)
                return True
            bodies.append(outcome.body if outcome.body is not None else body)
        selected, _removable = _over_budget_selection(
            bodies, budget_bytes=log_body_budget_bytes
        )
        for body in selected:
            if self.out_of_time():
                return False
            reason = blocker()
            if reason:
                dropped(reason)
                return True
            reason = self.remove_body(body, blocker=blocker)
            if reason:
                dropped(reason)
                return True
        return True


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _stage_compressed(source: Path, target: Path) -> tuple[Path, str, int]:
    """Stream ``source`` into a deterministic gzip staged beside ``target``.

    Streaming and not ``gzip.compress``: a multi-gigabyte ``stderr.log`` is
    exactly the case this exists for, and reading one into memory to save it
    would be the wrong kind of maintenance. The digest is taken on the same
    pass, so the original is read once. ``mtime=0`` keeps identical input
    byte-identical output, which is what makes a repeated pass a no-op.

    Staging stops one file short of publication on purpose. Compressing a
    multi-gigabyte body is the longest thing this module does, and it is exactly
    the window in which a plan lock, a run, or an assurance pin can appear, so
    the caller gets the chance to re-read protection while the only thing on
    disk is a dot-prefixed temporary it can discard.
    """
    temporary = durability.staging_path(target)
    digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as raw, temporary.open("wb") as staged:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=staged, mtime=0
            ) as compressed:
                while True:
                    chunk = raw.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    compressed.write(chunk)
            staged.flush()
            durability.fullsync(staged.fileno())
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    return temporary, digest.hexdigest(), total


def _discard_staged(temporary: Path) -> None:
    """Drop a staged gzip that was never published, leaving the corpus as it was."""
    with contextlib.suppress(OSError):
        temporary.unlink()


def _compress_file(source: Path, target: Path) -> tuple[str, int, int]:
    """Stage ``source``'s gzip and publish it at ``target`` in one step."""
    temporary, digest, total = _stage_compressed(source, target)
    try:
        os.replace(temporary, target)
    except OSError:
        _discard_staged(temporary)
        raise
    return digest, total, _file_size(target)


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


def _health(
    *,
    plan: MaintenancePlan | None,
    usage_after: CorpusUsage | None,
    failures: list[MaintenanceFailure],
    deadline_hit: bool,
    applied: bool,
) -> list[HealthFinding]:
    findings: list[HealthFinding] = []
    if plan is not None:
        if applied and usage_after is not None:
            protected = usage_after.corpus_bytes
            budget = usage_after.budget_bytes
            measured = "after this pass"
        else:
            protected = plan.projected_protected_bytes
            budget = plan.usage.budget_bytes
            measured = "as projected"
        if protected > budget and not failures:
            findings.append(
                HealthFinding(
                    code=HEALTH_PROTECTED_OVER_BUDGET,
                    strict=True,
                    detail=(
                        f"protected evidence alone is {protected} bytes {measured}, "
                        f"above the {budget}-byte budget; maintenance deleted "
                        "nothing outside policy and this needs a human decision "
                        "(close an open assurance issue, `asl unlock` a stale "
                        "plan lock, or raise the budget)"
                    ),
                )
            )
    if failures:
        allocated = sum(failure.bytes_still_allocated for failure in failures)
        findings.append(
            HealthFinding(
                code=HEALTH_PARTIAL_FAILURE,
                strict=True,
                detail=(
                    f"{len(failures)} mutation(s) failed, leaving {allocated} bytes "
                    "allocated that were not counted as reclaimed"
                ),
            )
        )
    if deadline_hit:
        findings.append(
            HealthFinding(
                code=HEALTH_DEADLINE_EXCEEDED,
                strict=False,
                detail=(
                    "the runtime bound expired before the plan was exhausted; the "
                    "remainder is left for the next scheduled pass"
                ),
            )
        )
    return findings


def _record_manifest(
    result: MaintenanceResult, *, state_dir: str | Path | None
) -> MaintenanceFailure | None:
    """Fsync the manifest, returning the failure when it could not be written."""
    path = manifest_path(state_dir)
    existing = read_manifest(state_dir)
    history = existing.get("history")
    entries = list(history) if isinstance(history, list) else []
    entries.append(result.summary())
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "state_root": result.state_root,
        "last_run": result.to_dict(),
        "history": entries[-MANIFEST_HISTORY_LIMIT:],
    }
    try:
        durability.write_json_file(path, payload, durable=True)
    except OSError as error:
        return MaintenanceFailure(
            kind=FAILURE_MANIFEST_WRITE,
            target=str(path),
            error=str(error),
            bytes_still_allocated=0,
        )
    return None


def read_manifest(state_dir: str | Path | None = None) -> dict[str, Any]:
    """Return the durable maintenance manifest, or ``{}`` when there is none."""
    try:
        payload = json.loads(manifest_path(state_dir).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def run_maintenance(
    state_dir: str | Path | None = None,
    *,
    apply: bool = False,
    force: bool = False,
    now: datetime | None = None,
    max_age_days: float = retention.DEFAULT_MAX_AGE_DAYS,
    compaction_age_days: float = COMPACTION_AGE_DAYS,
    log_body_budget_bytes: int = LOG_BODY_BUDGET_BYTES,
    budget_bytes: int = CORPUS_BUDGET_BYTES,
    min_free_fraction: float = MIN_FREE_FRACTION,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
    lock_wait_seconds: float = LOCK_WAIT_SECONDS,
    record: bool = True,
) -> MaintenanceResult:
    """Measure, plan and — under ``apply`` — perform one maintenance pass.

    The pass is a no-op unless a threshold is crossed or ``force`` is set, and
    the thresholds are measured twice: once to decide whether to queue for the
    claim, and again once it is held, because waiting is exactly the situation
    in which the reason for deleting can expire. Every mutation happens under
    that claim, and each one re-reads liveness, pins, and plan locks for itself
    immediately before it acts. ``record`` writes the durable manifest and is
    only turned off by a caller that is measuring, not maintaining.
    """
    moment = _now(now)
    deadline = _Deadline(max_runtime_seconds)
    root = state.state_root(state_dir)
    usage = measure_corpus(
        state_dir,
        now=moment,
        budget_bytes=budget_bytes,
        min_free_fraction=min_free_fraction,
    )

    def _finish(
        *,
        outcome: str,
        plan: MaintenancePlan | None,
        applied: bool,
        usage_at_claim: CorpusUsage | None = None,
        usage_after: CorpusUsage | None = None,
        mutator: _Mutator | None = None,
        drops: tuple[dict[str, str], ...] = (),
    ) -> MaintenanceResult:
        failures = list(mutator.failures) if mutator is not None else []
        findings = _health(
            plan=plan,
            usage_after=usage_after,
            failures=failures,
            deadline_hit=bool(mutator is not None and mutator.deadline_hit),
            applied=applied,
        )
        result = MaintenanceResult(
            ok=not failures and not any(finding.strict for finding in findings),
            schema_version=MANIFEST_SCHEMA_VERSION,
            outcome=outcome,
            trigger=(
                plan.trigger
                if plan is not None
                else (usage_at_claim if usage_at_claim is not None else usage).trigger
            ),
            applied=applied,
            state_root=str(root),
            started_at=moment.isoformat(),
            finished_at=_now(now).isoformat(),
            duration_seconds=deadline.elapsed(),
            usage=usage,
            usage_at_claim=usage_at_claim,
            usage_after=usage_after,
            plan=plan,
            removed_run_ids=tuple(mutator.removed_run_ids) if mutator else (),
            reclaimed_bytes=mutator.reclaimed_bytes if mutator else 0,
            compressed_body_count=mutator.compressed_body_count if mutator else 0,
            removed_body_count=mutator.removed_body_count if mutator else 0,
            compaction_reclaimed_bytes=(
                mutator.compaction_reclaimed_bytes if mutator else 0
            ),
            revalidation_drops=drops,
            failures=tuple(failures),
            health=tuple(findings),
            manifest_path=str(manifest_path(state_dir)),
        )
        if record:
            manifest_failure = _record_manifest(result, state_dir=state_dir)
            if manifest_failure is not None:
                failures.append(manifest_failure)
                findings = _health(
                    plan=plan,
                    usage_after=usage_after,
                    failures=failures,
                    deadline_hit=bool(mutator is not None and mutator.deadline_hit),
                    applied=applied,
                )
                result = dataclasses.replace(
                    result,
                    ok=False,
                    failures=tuple(failures),
                    health=tuple(findings),
                )
        return result

    if not usage.triggered and not force:
        return _finish(outcome=OUTCOME_UNDER_THRESHOLD, plan=None, applied=False)

    if not apply:
        plan = plan_maintenance(
            state_dir,
            now=moment,
            force=force,
            max_age_days=max_age_days,
            compaction_age_days=compaction_age_days,
            log_body_budget_bytes=log_body_budget_bytes,
            usage=usage,
        )
        return _finish(outcome=OUTCOME_PLANNED, plan=plan, applied=False)

    claim = _maintenance_claim(lock_path(state_dir), wait_seconds=lock_wait_seconds)
    with claim as held:
        if not held:
            return _finish(outcome=OUTCOME_LOCK_UNAVAILABLE, plan=None, applied=False)
        # The pressure that admitted this pass was measured before the claim,
        # and a pass that queued behind another one waited for exactly the pass
        # most likely to have relieved it. Deleting evidence to answer pressure
        # that no longer exists is deleting for no reason, so the thresholds are
        # re-read here and only an explicit `--force` proceeds past a corpus
        # that is now healthy.
        at_claim = measure_corpus(
            state_dir,
            now=moment,
            budget_bytes=budget_bytes,
            min_free_fraction=min_free_fraction,
        )
        if not at_claim.triggered and not force:
            return _finish(
                outcome=OUTCOME_PRESSURE_RELIEVED,
                plan=None,
                applied=False,
                usage_at_claim=at_claim,
            )
        plan = plan_maintenance(
            state_dir,
            now=moment,
            force=force,
            max_age_days=max_age_days,
            compaction_age_days=compaction_age_days,
            log_body_budget_bytes=log_body_budget_bytes,
            usage=at_claim,
        )
        removals, compactions, drops = _revalidate(plan, state_dir=state_dir)

        def _fence(plan_dir: str, plan_state_dir: str, run_id: str) -> str:
            """Read the safety facts now, not when the plan was computed."""
            return _SafetyReading(root).blocker(
                plan_dir=plan_dir, plan_state_dir=plan_state_dir, run_id=run_id
            )

        mutator = _Mutator(deadline=deadline, now=moment, fence=_fence)
        # Whole-run reclaim first: it frees the most bytes per unit of work, so
        # a pass that runs out of time has already done the cheapest thing.
        # Compaction only reaches runs the reclaim lane is forbidden to touch.
        for removal in removals:
            if mutator.out_of_time():
                break
            mutator.remove_run(removal)
        for compaction in compactions:
            if mutator.out_of_time():
                break
            if not mutator.compact_run(
                compaction, log_body_budget_bytes=log_body_budget_bytes
            ):
                break
        usage_after = measure_corpus(
            state_dir,
            now=moment,
            budget_bytes=budget_bytes,
            min_free_fraction=min_free_fraction,
        )
        return _finish(
            outcome=OUTCOME_APPLIED,
            plan=plan,
            applied=True,
            usage_at_claim=at_claim,
            usage_after=usage_after,
            mutator=mutator,
            drops=tuple([*drops, *mutator.drops]),
        )


# --------------------------------------------------------------------------
# launchd job ownership
# --------------------------------------------------------------------------


def plist_path(home: str | Path | None = None) -> Path:
    """Path of the ASL-owned launchd job definition."""
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / LAUNCH_AGENTS_RELATIVE_DIR / f"{JOB_LABEL}.plist"


def job_argv(
    executable: str,
    *,
    state_dir: str | Path | None = None,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
) -> list[str]:
    """The exact command launchd runs. Bounded and machine-readable by design."""
    argv = [
        executable,
        "maintenance",
        "run",
        "--apply",
        "--max-runtime-seconds",
        f"{max_runtime_seconds:g}",
        "--json",
    ]
    if state_dir is not None:
        argv.extend(["--state-dir", str(state_dir)])
    return argv


def render_plist(
    executable: str,
    *,
    state_dir: str | Path | None = None,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
) -> bytes:
    """Render the daily job definition.

    ``RunAtLoad`` is deliberately false: an install must not start reclaiming
    disk as a side effect of being installed. The job is a background,
    low-priority process because it competes with real work for I/O, and its
    own output is appended to the state root so `status` can point at it.
    """
    log_path = job_log_path(state_dir)
    payload: dict[str, Any] = {
        "Label": JOB_LABEL,
        "ProgramArguments": job_argv(
            executable,
            state_dir=state_dir,
            max_runtime_seconds=max_runtime_seconds,
        ),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 5,
        "ExitTimeOut": int(max_runtime_seconds) + 60,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(payload, sort_keys=True)


def _launchctl(
    arguments: list[str], *, runner: Any = subprocess.run
) -> tuple[int, str]:
    try:
        completed = runner(
            ["launchctl", *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    detail = (completed.stderr or "").strip() or (completed.stdout or "").strip()
    return completed.returncode, detail


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _resolve_executable(executable: str | None) -> str:
    """Return the absolute ``asl`` launchd should run.

    Absolute and not a bare name: a launchd job inherits no shell environment,
    so a ``PATH`` lookup at run time is exactly the failure mode this avoids.
    """
    if executable:
        candidate = Path(executable).expanduser()
        if not candidate.is_absolute():
            candidate = candidate.resolve()
        if not candidate.exists():
            raise MaintenanceError(f"{candidate} does not exist")
        return str(candidate)
    found = shutil.which("asl")
    if found is None:
        raise MaintenanceError(
            "asl was not found on PATH; pass --executable PATH with the absolute "
            "path launchd should run"
        )
    return str(Path(found).absolute())


@dataclass(frozen=True)
class JobStatus:
    """Everything install verification needs to judge the scheduled job."""

    ok: bool
    label: str
    supported: bool
    plist_path: str
    plist_present: bool
    plist_current: bool
    loaded: bool
    load_detail: str
    executable: str
    state_root: str
    manifest_path: str
    last_run: dict[str, Any]
    history: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "command": "maintenance status",
            "label": self.label,
            "supported": self.supported,
            "plist_path": self.plist_path,
            "plist_present": self.plist_present,
            "plist_current": self.plist_current,
            "loaded": self.loaded,
            "load_detail": self.load_detail,
            "executable": self.executable,
            "state_root": self.state_root,
            "manifest_path": self.manifest_path,
            "last_run": dict(self.last_run),
            "history": [dict(entry) for entry in self.history],
        }


def _platform_supported(platform: str) -> bool:
    return platform == "darwin"


def install_job(
    *,
    executable: str | None = None,
    home: str | Path | None = None,
    state_dir: str | Path | None = None,
    hour: int = DEFAULT_HOUR,
    minute: int = DEFAULT_MINUTE,
    max_runtime_seconds: float = DEFAULT_MAX_RUNTIME_SECONDS,
    bootstrap: bool = True,
    platform: str = sys.platform,
    launchctl: Any = subprocess.run,
) -> dict[str, Any]:
    """Write and load the ASL-owned daily maintenance job."""
    if not _platform_supported(platform):
        raise MaintenanceError(
            f"scheduled maintenance is a launchd job and {platform} has no "
            "launchd; run `asl maintenance run --apply` from this platform's own "
            "scheduler instead"
        )
    resolved = _resolve_executable(executable)
    document = render_plist(
        resolved,
        state_dir=state_dir,
        hour=hour,
        minute=minute,
        max_runtime_seconds=max_runtime_seconds,
    )
    target = plist_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        durability.write_bytes_file(target, document)
    except OSError as error:
        raise MaintenanceError(f"could not write {target}: {error}") from error
    state.state_root(state_dir).mkdir(parents=True, exist_ok=True)

    loaded = False
    detail = "not bootstrapped (--no-bootstrap)"
    if bootstrap:
        _launchctl(["bootout", f"{_domain()}/{JOB_LABEL}"], runner=launchctl)
        code, detail = _launchctl(
            ["bootstrap", _domain(), str(target)], runner=launchctl
        )
        loaded = code == 0
        if not loaded and not detail:
            detail = f"launchctl bootstrap exited {code}"
    return {
        "ok": loaded or not bootstrap,
        "command": "maintenance install",
        "label": JOB_LABEL,
        "plist_path": str(target),
        "executable": resolved,
        "argv": job_argv(
            resolved, state_dir=state_dir, max_runtime_seconds=max_runtime_seconds
        ),
        "hour": int(hour),
        "minute": int(minute),
        "max_runtime_seconds": max_runtime_seconds,
        "state_root": str(state.state_root(state_dir)),
        "log_path": str(job_log_path(state_dir)),
        "bootstrapped": loaded,
        "detail": detail,
    }


def uninstall_job(
    *,
    home: str | Path | None = None,
    state_dir: str | Path | None = None,
    bootstrap: bool = True,
    platform: str = sys.platform,
    launchctl: Any = subprocess.run,
) -> dict[str, Any]:
    """Unload and remove the job, leaving every byte of evidence in place.

    Uninstalling stops the schedule and nothing else. The manifest and the job
    log stay where they are, because the record of what maintenance did to a
    corpus outlives the decision to stop maintaining it automatically.
    """
    target = plist_path(home)
    detail = "not booted out (--no-bootstrap)"
    booted_out = False
    if bootstrap and _platform_supported(platform):
        code, detail = _launchctl(
            ["bootout", f"{_domain()}/{JOB_LABEL}"], runner=launchctl
        )
        booted_out = code == 0
    removed = False
    error = ""
    try:
        target.unlink()
        removed = True
    except FileNotFoundError:
        error = "no job definition was installed"
    except OSError as os_error:
        error = str(os_error)
    return {
        "ok": not error or error == "no job definition was installed",
        "command": "maintenance uninstall",
        "label": JOB_LABEL,
        "plist_path": str(target),
        "plist_removed": removed,
        "booted_out": booted_out,
        "detail": detail,
        "error": error,
        "manifest_path": str(manifest_path(state_dir)),
    }


def job_status(
    *,
    home: str | Path | None = None,
    state_dir: str | Path | None = None,
    executable: str | None = None,
    probe: bool = True,
    platform: str = sys.platform,
    launchctl: Any = subprocess.run,
) -> JobStatus:
    """Report job presence, the last run, reclaimed bytes, and failures."""
    target = plist_path(home)
    present = target.is_file()
    resolved = ""
    with contextlib.suppress(MaintenanceError):
        resolved = _resolve_executable(executable)
    current = False
    if present and resolved:
        with contextlib.suppress(OSError):
            current = target.read_bytes() == render_plist(resolved, state_dir=state_dir)
    loaded = False
    detail = "launchctl was not probed"
    supported = _platform_supported(platform)
    if probe and supported:
        code, detail = _launchctl(
            ["print", f"{_domain()}/{JOB_LABEL}"], runner=launchctl
        )
        loaded = code == 0
        if loaded:
            detail = f"{JOB_LABEL} is loaded in {_domain()}"
    elif not supported:
        detail = f"{platform} has no launchd; no scheduled job is possible here"

    manifest = read_manifest(state_dir)
    last_run = manifest.get("last_run")
    history = manifest.get("history")
    return JobStatus(
        # A machine with no job installed is reported, not failed: install is an
        # operator decision and `status` is how they check it, not a gate.
        ok=not _status_failures(last_run),
        label=JOB_LABEL,
        supported=supported,
        plist_path=str(target),
        plist_present=present,
        plist_current=current,
        loaded=loaded,
        load_detail=detail,
        executable=resolved,
        state_root=str(state.state_root(state_dir)),
        manifest_path=str(manifest_path(state_dir)),
        last_run=last_run if isinstance(last_run, dict) else {},
        history=tuple(entry for entry in history if isinstance(entry, dict))
        if isinstance(history, list)
        else (),
    )


def _status_failures(last_run: Any) -> bool:
    if not isinstance(last_run, dict):
        return False
    return not last_run.get("ok", True)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def format_bytes(count: int) -> str:
    """Render a byte count the way `asl gc` and `asl metrics` render theirs."""
    return retention.format_bytes(count)


def _print_run(result: MaintenanceResult) -> None:
    print(f"Maintenance ({result.outcome}) for {result.state_root}")
    usage = result.usage
    print(
        f"Corpus: {format_bytes(usage.corpus_bytes)} of "
        f"{format_bytes(usage.budget_bytes)} budget; volume "
        f"{usage.free_fraction * 100:.1f}% free "
        f"(floor {usage.min_free_fraction * 100:.0f}%)"
    )
    print(f"Trigger: {result.trigger}")
    if result.outcome == OUTCOME_UNDER_THRESHOLD:
        print("Both thresholds are satisfied; nothing was planned or touched.")
        return
    if result.outcome == OUTCOME_LOCK_UNAVAILABLE:
        print("Another maintenance pass holds the claim; this one declined.")
        return
    if result.outcome == OUTCOME_PRESSURE_RELIEVED:
        at_claim = result.usage_at_claim
        if at_claim is not None:
            print(
                f"Under the claim the corpus measured "
                f"{format_bytes(at_claim.corpus_bytes)} with "
                f"{at_claim.free_fraction * 100:.1f}% free."
            )
        print(
            "Both thresholds were satisfied by the time this pass held the "
            "claim; nothing was touched. Use --force to maintain anyway."
        )
        return
    plan = result.plan
    if plan is not None:
        print(
            f"Reclaimable: {plan.reclaim.reclaimable_run_count} run(s), "
            f"{format_bytes(plan.reclaimable_bytes)}"
        )
        print(
            f"Compactable: {len(plan.compactions)} terminal newest run(s) older "
            f"than {plan.compaction_age_days:g} days, "
            f"{format_bytes(plan.removable_body_bytes)} of log body above the "
            f"{format_bytes(plan.log_body_budget_bytes)} per-run budget"
        )
        retained = plan.reclaim.retained_by_reason()
        if retained:
            print("Retained by reason:")
            for reason, totals in retained.items():
                print(
                    f"  {reason}: {totals['run_count']} run(s), "
                    f"{format_bytes(totals['bytes'])}"
                )
    if not result.applied:
        print("Re-run with --apply to perform the planned maintenance.")
    else:
        print(
            f"Removed: {len(result.removed_run_ids)} run(s), "
            f"{format_bytes(result.reclaimed_bytes)} reclaimed"
        )
        print(
            f"Compaction: {result.compressed_body_count} body(ies) compressed, "
            f"{result.removed_body_count} removed, "
            f"{format_bytes(result.compaction_reclaimed_bytes)} reclaimed"
        )
    for drop in result.revalidation_drops:
        print(
            f"  dropped {drop['run_id']} ({drop['lane']}) under the claim: "
            f"{drop['reason']}"
        )
    for failure in result.failures:
        print(
            f"  FAILED {failure.kind} {failure.target}: {failure.error} "
            f"({format_bytes(failure.bytes_still_allocated)} still allocated)"
        )
    for finding in result.health:
        prefix = "HEALTH" if finding.strict else "note"
        print(f"{prefix} {finding.code}: {finding.detail}")


def _print_status(status: JobStatus) -> None:
    print(f"Maintenance job: {status.label}")
    if not status.plist_present:
        freshness = "absent"
    else:
        freshness = "present, current" if status.plist_current else "present, stale"
    print(f"Definition: {status.plist_path} ({freshness})")
    print(f"Loaded: {status.loaded} — {status.load_detail}")
    print(f"State root: {status.state_root}")
    print(f"Manifest: {status.manifest_path}")
    last_run = status.last_run
    if not last_run:
        print("Last run: never (no manifest has been written)")
        return
    print(
        f"Last run: {last_run.get('finished_at')} — {last_run.get('outcome')} "
        f"(trigger {last_run.get('trigger')}, ok={last_run.get('ok')})"
    )
    print(
        f"Reclaimed: {format_bytes(int(last_run.get('reclaimed_bytes') or 0))} from "
        f"{last_run.get('removed_run_count') or 0} run(s), plus "
        f"{format_bytes(int(last_run.get('compaction_reclaimed_bytes') or 0))} "
        "from compaction"
    )
    failures = last_run.get("failures")
    if isinstance(failures, list) and failures:
        print(f"Failures: {len(failures)}")
        for failure in failures:
            if isinstance(failure, dict):
                print(
                    f"  {failure.get('kind')} {failure.get('target')}: "
                    f"{failure.get('error')}"
                )
    else:
        print("Failures: 0")
    health = last_run.get("health")
    if isinstance(health, list):
        for finding in health:
            if isinstance(finding, dict):
                prefix = "HEALTH" if finding.get("strict") else "note"
                print(f"{prefix} {finding.get('code')}: {finding.get('detail')}")


def _fail(command: str, error: Exception, args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"ok": False, "command": command, "error": str(error)}, sort_keys=True
            )
        )
    else:
        print(f"{command} failed: {error}", file=sys.stderr)
    return 1


def run_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance run``."""
    try:
        result = run_maintenance(
            getattr(args, "state_dir", None),
            apply=bool(getattr(args, "apply", False)),
            force=bool(getattr(args, "force", False)),
            max_age_days=getattr(args, "max_age_days", retention.DEFAULT_MAX_AGE_DAYS),
            max_runtime_seconds=getattr(
                args, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS
            ),
            # Stated rather than defaulted: the shipped policy is what this
            # command applies, and naming it keeps the constants the single
            # place it changes.
            compaction_age_days=COMPACTION_AGE_DAYS,
            log_body_budget_bytes=LOG_BODY_BUDGET_BYTES,
            budget_bytes=CORPUS_BUDGET_BYTES,
            min_free_fraction=MIN_FREE_FRACTION,
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail("maintenance run", error, args)
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        _print_run(result)
    return 0 if result.ok else 1


def install_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance install``."""
    try:
        payload = install_job(
            executable=getattr(args, "executable", None),
            state_dir=getattr(args, "state_dir", None),
            hour=getattr(args, "hour", DEFAULT_HOUR),
            minute=getattr(args, "minute", DEFAULT_MINUTE),
            max_runtime_seconds=getattr(
                args, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS
            ),
            bootstrap=not getattr(args, "no_bootstrap", False),
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail("maintenance install", error, args)
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Installed {payload['label']} at {payload['plist_path']}")
        print(
            f"Runs daily at {payload['hour']:02d}:{payload['minute']:02d}: "
            f"{' '.join(payload['argv'])}"
        )
        print(f"Output: {payload['log_path']}")
        print(f"Bootstrapped: {payload['bootstrapped']} — {payload['detail']}")
    return 0 if payload["ok"] else 1


def uninstall_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance uninstall``."""
    try:
        payload = uninstall_job(
            state_dir=getattr(args, "state_dir", None),
            bootstrap=not getattr(args, "no_bootstrap", False),
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail("maintenance uninstall", error, args)
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"Removed: {payload['plist_removed']} ({payload['plist_path']})")
        print(f"Booted out: {payload['booted_out']} — {payload['detail']}")
        if payload["error"]:
            print(payload["error"])
        print(
            f"Evidence is untouched; the manifest stays at {payload['manifest_path']}"
        )
    return 0 if payload["ok"] else 1


def status_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl maintenance status``."""
    try:
        status = job_status(
            state_dir=getattr(args, "state_dir", None),
            executable=getattr(args, "executable", None),
            probe=not getattr(args, "no_probe", False),
        )
    except (MaintenanceError, OSError, ValueError) as error:
        return _fail("maintenance status", error, args)
    if getattr(args, "json", False):
        print(json.dumps(status.to_dict(), sort_keys=True))
    else:
        _print_status(status)
    return 0 if status.ok else 1
