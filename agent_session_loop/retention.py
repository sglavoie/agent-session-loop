"""Delete — or, by default, only report — the run directories retention reclaims.

:func:`run_gc` is the sole caller that deletes a ``runs/<run-id>/`` directory,
and only when its ``apply`` argument is set; the plan it deletes against comes
from :mod:`agent_session_loop.retention_projection`, which owns the retention
rules and never removes anything. This module also owns the ``asl gc`` report
and CLI handler. Nothing here removes a lock in either mode; clearing one stays
a deliberate ``asl unlock``.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    assurance_ledger,
    assurance_report,
    plans_errors,
    plans_snapshot,
    responses,
    session_overview,
    state,
    workbench_archive_storage,
    workbench_archive_workflows,
)
from .cli_defaults import program_name
from .cli_parser_support import NON_NEGATIVE_NUMBER, flag_refusal
from .retention_projection import (
    DEFAULT_MAX_AGE_DAYS,
    RETAIN_PIN_UNKNOWN,
    RETAIN_PLAN_LOCKED,
    PlanRetention,
    _dir_size_bytes,
    corpus_retention,
    pinned_run_ids,
    plan_retention,
)
from .workbench_archive_commands import ArchiveRefused

__all__ = [
    "PlanGC",
    "GCReport",
    "remove_run_directory",
    "format_bytes",
    "run_gc",
    "gc_from_args",
]


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
    #: The Workbench archive under the same state root, which gc never judges
    #: or deletes; ``None`` for a plan-scoped sweep. See :func:`_archive_payload`.
    archive: dict[str, Any] | None = None

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
            "archive": None if self.archive is None else dict(self.archive),
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


#: How an operator empties Workbench archive entries; gc never does.
ARCHIVE_PURGE_HINT = (
    "purge them from the Workbench Archive view (`asl workbench serve`)"
)


def _archive_payload(
    state_dir: str | Path | None,
    *,
    now: datetime | None,
    registry_path: str | Path | None = None,
) -> dict[str, Any]:
    """Measure the Workbench archive that gc leaves alone, deleting nothing.

    Archive entries sit under ``<state_root>/archive/`` beside the plan state
    directories gc judges, and can hold most of the state root's bytes. A
    report that said "0 B reclaimable of N measured" without them implied the
    root held N. ``past_retention_count`` is the Archive view's own
    classification against the live retention window, so the two surfaces never
    disagree on what an operator may empty; it is ``None`` when the registry
    holding that window cannot be read.
    """
    root = workbench_archive_storage.archive_root(state_dir)
    payload: dict[str, Any] = {
        "path": str(root),
        "managed_by_gc": False,
        "entry_count": 0,
        "bytes": 0,
        "retention_days": None,
        "past_retention_count": None,
        "past_retention_bytes": None,
        "error": None,
        "purge_hint": ARCHIVE_PURGE_HINT,
    }
    if not root.is_dir():
        payload.update(past_retention_count=0, past_retention_bytes=0)
        return payload
    payload["bytes"] = _dir_size_bytes(root)
    try:
        view = workbench_archive_workflows.review_archive(
            state_dir=state_dir, registry_path=registry_path, now=now
        )
    except ArchiveRefused as error:
        payload["entry_count"] = len(
            workbench_archive_storage.list_archive_entries(state_dir).entries
        )
        payload["error"] = str(error)
        return payload
    candidates = set(view.review.candidate_ids)
    past_bytes = 0
    for entry in view.entries:
        if entry.archive_id in candidates:
            entry_dir = (
                entry.archive_dir
                or workbench_archive_storage.archive_entry_dir(
                    entry.archive_id, state_dir
                )
            )
            past_bytes += _dir_size_bytes(entry_dir)
    payload["entry_count"] = len(view.entries)
    payload["retention_days"] = view.retention_days
    payload["past_retention_count"] = len(candidates)
    payload["past_retention_bytes"] = past_bytes
    return payload


def run_gc(
    plan_dir: str | Path | None = None,
    *,
    state_dir: str | Path | None = None,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    apply: bool = False,
    now: datetime | None = None,
    measure_archive: bool = False,
    registry_path: str | Path | None = None,
) -> GCReport:
    """Project — and under ``apply``, delete — the reclaimable run directories.

    ``plan_dir`` restricts the sweep to one plan; omitting it judges every plan
    state directory under the resolved state root, which is the aggregate the
    corpus-wide dry run reports. ``measure_archive`` adds the size of the
    Workbench archive to a corpus-wide report as context only; ``asl gc`` asks
    for it, while maintenance, which measures the whole root itself, does not
    pay for a second walk. ``registry_path`` names the Workbench registry whose
    archive retention window classifies the entries (default: the user's).
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
        archive = (
            _archive_payload(state_dir, now=now, registry_path=registry_path)
            if measure_archive
            else None
        )
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
        archive = None
    return GCReport(
        scope=scope,
        applied=apply,
        state_root=root,
        max_age_days=max_age_days,
        cutoff_at=cutoff_at,
        plans=tuple(_sweep_plan(report, apply=apply) for report in reports),
        unresolved_state_dirs=unresolved,
        assurance=_assurance_payload(pinned, root=root_path),
        archive=archive,
    )


def format_bytes(count: int) -> str:
    """Render a byte count the way ``asl metrics`` renders its corpus totals."""
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(count)} B"


def _print_archive(archive: dict[str, Any]) -> None:
    """Say what the Workbench archive holds, and that gc leaves it alone."""
    if not archive["entry_count"] and not archive["bytes"]:
        print("Archive (not managed by gc): empty")
        return
    line = (
        f"Archive (not managed by gc): {archive['entry_count']} entry(ies), "
        f"{format_bytes(archive['bytes'])} at {archive['path']}"
    )
    past = archive["past_retention_count"]
    if past is not None:
        window = archive["retention_days"]
        line += f"; {past} past the {window}-day retention window"
        if past:
            line += f" ({format_bytes(archive['past_retention_bytes'])})"
    elif archive["error"]:
        line += f"; retention window unreadable ({archive['error']})"
    print(line)
    print(f"  gc never deletes archive entries; {archive['purge_hint']}.")


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
        f"{format_bytes(report.total_bytes)} measured in run directories"
    )
    if report.archive is not None:
        _print_archive(report.archive)

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


def _print_gc_error(args: argparse.Namespace, *, reason: str, error: str) -> int:
    """Report one ``asl gc`` refusal in the common error envelope; returns 1."""
    if getattr(args, "json", False):
        plan_dir = getattr(args, "plan_dir", None)
        responses.print_json(
            responses.error_payload(
                "gc",
                reason=reason,
                error=error,
                paused=False,
                plan_dir=None if plan_dir is None else str(plan_dir),
            )
        )
    else:
        print(f"{program_name()} gc: {error}", file=sys.stderr)
    return 1


def gc_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl gc``."""
    plan_dir = getattr(args, "plan_dir", None)
    max_age_days = getattr(args, "max_age_days", DEFAULT_MAX_AGE_DAYS)
    # Checked here rather than by an argparse type so a refusal keeps its
    # JSON reason under --json.
    if not math.isfinite(max_age_days) or max_age_days < 0:
        return _print_gc_error(
            args,
            reason="invalid-max-age-days",
            error=flag_refusal("--max-age-days", NON_NEGATIVE_NUMBER, max_age_days),
        )
    try:
        if plan_dir is not None:
            # The validation `asl status` reports: a directory that is not a
            # split plan (no context.md / manifest.md) owns no runs to judge.
            plans_snapshot.require_split_plan_directory(plan_dir)
        report = run_gc(
            plan_dir,
            state_dir=getattr(args, "state_dir", None),
            max_age_days=max_age_days,
            apply=bool(getattr(args, "apply", False)),
            measure_archive=True,
        )
    except plans_errors.PlanError as error:
        return _print_gc_error(
            args,
            reason=(
                "plan-not-found"
                if isinstance(error, plans_errors.PlanNotFoundError)
                else "malformed-plan"
            ),
            error=str(error),
        )
    except OSError as error:
        return _print_gc_error(args, reason="os-error", error=str(error))
    except ValueError as error:
        return _print_gc_error(args, reason="gc-error", error=str(error))

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        _print_report(report)
    return 1 if report.failure_count else 0
