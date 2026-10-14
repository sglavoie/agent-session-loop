"""Value types for one maintenance pass: usage, plan, failures, and result."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import retention, state
from .maintenance_policy import (
    CORPUS_BUDGET_BYTES,
    MIN_FREE_FRACTION,
    TRIGGER_CORPUS_OVER_BUDGET,
    TRIGGER_FORCED,
    TRIGGER_LOW_FREE_SPACE,
    TRIGGER_NONE,
    MaintenanceError,
    _now,
    _tree_bytes,
)


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
