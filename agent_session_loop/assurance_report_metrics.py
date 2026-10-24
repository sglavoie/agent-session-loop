"""Run-level assurance metrics projected from the ledger (read-only).

Backs ``asl metrics``: coverage, issue counts, resolution latency, recurrence,
and identity anomalies. An unreadable ledger yields ``available: False``.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_models_primitives, assurance_models_vocab
from . import assurance_projection as projection

__all__ = ["collect_assurance_metrics"]


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _empty_metrics(path: Path, error: str | None) -> dict[str, Any]:
    return {
        "available": error is None,
        "error": error,
        "ledger_path": str(path),
        "runs_recorded": 0,
        "coverage": {
            "eligible_runs": 0,
            "ineligible_runs": 0,
            "audited_runs": 0,
            "coverage_rate": None,
        },
        "audit_states": {
            audit_state: 0 for audit_state in assurance_models_vocab.AUDIT_STATES
        },
        "issues": {
            "observations": 0,
            "new": 0,
            "recurring": 0,
            "by_severity": {},
            "by_category": {},
            "open_total": 0,
            "bound_total": 0,
            "unbound_total": 0,
            "recurrence_after_resolution": 0,
            "issues_recurring_after_resolution": 0,
            "recurrence_after_resolution_by_category": {},
        },
        "dispositions": {
            "total": 0,
            "by_kind": {},
            "dismissal_rate": None,
            "accepted_risk_rate": None,
            "time_to_disposition": {
                "count": 0,
                "mean_hours": None,
                "median_hours": None,
                "max_hours": None,
            },
        },
        "closeouts": {
            "total": 0,
            "by_outcome": {},
            "escaped_issues": 0,
            "rework_minutes": 0.0,
            "closeouts_reporting_rework": 0,
        },
        "integrity": {
            "anomalies": 0,
            "rejected_records": 0,
            "identity_key_conflicts": 0,
            "duplicate_identity_bindings": 0,
        },
    }


def _windowed_run_states(
    view: ledger.LedgerView,
    inbox: projection.AssuranceProjection,
    since: datetime,
) -> dict[str, projection.RunAudit]:
    """Return the projected audit of every run whose audit was recorded in window.

    A run is placed by its most recent audit record rather than by the summary's
    own timestamps, because a ``pending`` audit has no timestamps at all and
    would otherwise vanish from coverage exactly when coverage is the question.
    """
    latest: dict[str, datetime] = {}
    for record in view.records:
        if record.kind not in projection.AUDIT_RECORD_KINDS:
            continue
        recorded = _parse(record.recorded_at)
        run_id = getattr(record.payload, "run_id", None)
        if recorded is None or not isinstance(run_id, str):
            continue
        if run_id not in latest or recorded > latest[run_id]:
            latest[run_id] = recorded
    windowed: dict[str, projection.RunAudit] = {}
    for run_id, recorded in latest.items():
        if recorded < since:
            continue
        audit = inbox.run(run_id)
        if audit is not None:
            windowed[run_id] = audit
    return windowed


def _recurrence_after_resolution(
    view: ledger.LedgerView,
    inbox: projection.AssuranceProjection,
    since: datetime,
) -> tuple[int, int, dict[str, int]]:
    """Count in-window sightings of a defect a human had already resolved.

    Replayed rather than read off the projection's ``reopened_count`` so the
    answer can be windowed: the projection reports the lifetime total, and a
    reopen from a year ago says nothing about the last seven days.

    The sightings are also split by the recurring finding's category, because
    a total says only that fixes come back while a category says which class
    of defect the last fix did not close.
    """
    resolved: set[str] = set()
    events_in_window = 0
    issues_in_window: set[str] = set()
    by_category: dict[str, int] = {}
    for record in view.records:
        # Observations name their issue on the record, because the ID is minted
        # under the append lock; every later record names it on the payload.
        issue_id = record.issue_id or getattr(record.payload, "issue_id", None)
        if not isinstance(issue_id, str):
            continue
        canonical = inbox.resolve(issue_id)
        if record.kind == ledger.KIND_ISSUE_DISPOSITION:
            kind = getattr(record.payload, "kind", None)
            if kind == "resolve":
                resolved.add(canonical)
            else:
                resolved.discard(canonical)
        elif record.kind == ledger.KIND_ISSUE_OBSERVED:
            if canonical not in resolved:
                continue
            resolved.discard(canonical)
            recorded = _parse(record.recorded_at)
            if recorded is not None and recorded >= since:
                events_in_window += 1
                issues_in_window.add(canonical)
                finding = record.payload.finding
                by_category[finding.category] = by_category.get(finding.category, 0) + 1
    return (
        events_in_window,
        len(issues_in_window),
        dict(sorted(by_category.items())),
    )


def _first_seen(
    inbox: projection.AssuranceProjection, issue_id: str
) -> datetime | None:
    issue = inbox.issue(issue_id)
    if issue is None or not issue.occurrences:
        return None
    return _parse(issue.first_seen_at)


def collect_assurance_metrics(
    *,
    state_dir: str | Path | None = None,
    since: datetime,
) -> dict[str, Any]:
    """Aggregate the assurance ledger for the metrics window.

    Coverage and correctness are reported separately on purpose: the audit
    state counts say how many runs were graded at all, and the issue counts say
    what the graded ones were worth. A window with no audits and a window with
    no findings are different facts and must not render the same.
    """
    path = ledger.ledger_path(state_dir)
    try:
        view = ledger.read_ledger(state_dir=state_dir)
        inbox = projection.project(view)
    except (OSError, assurance_models_primitives.AssuranceError) as error:
        return _empty_metrics(path, str(error))

    report = _empty_metrics(path, None)
    windowed_runs = _windowed_run_states(view, inbox, since)
    report["runs_recorded"] = len(windowed_runs)

    eligible = ineligible = audited = 0
    for audit in windowed_runs.values():
        report["audit_states"][audit.state] = (
            report["audit_states"].get(audit.state, 0) + 1
        )
        if audit.summary.eligible:
            eligible += 1
            if audit.state in assurance_models_vocab.COMPLETED_AUDIT_STATES:
                audited += 1
        else:
            ineligible += 1
    report["coverage"] = {
        "eligible_runs": eligible,
        "ineligible_runs": ineligible,
        "audited_runs": audited,
        "coverage_rate": _rate(audited, eligible),
    }

    by_severity: dict[str, int] = {}
    by_category: dict[str, int] = {}
    observations = 0
    for record in view.records_of_kind(ledger.KIND_ISSUE_OBSERVED):
        recorded = _parse(record.recorded_at)
        if recorded is None or recorded < since:
            continue
        finding = getattr(record.payload, "finding", None)
        if finding is None:
            continue
        observations += 1
        by_severity[finding.severity] = by_severity.get(finding.severity, 0) + 1
        by_category[finding.category] = by_category.get(finding.category, 0) + 1
    recurrences, recurring_issues, recurrence_by_category = (
        _recurrence_after_resolution(view, inbox, since)
    )
    # Counted over canonical issues rather than over allocated IDs: a key an
    # issue absorbed through a merge names that issue now, so the question the
    # backlog asks is how many issues a machine can still only reach by number.
    bound_issues = sum(1 for issue in inbox.issues if issue.identity_keys)
    report["issues"] = {
        "observations": observations,
        "new": sum(audit.summary.new_issue_count for audit in windowed_runs.values()),
        "recurring": sum(
            audit.summary.recurring_issue_count for audit in windowed_runs.values()
        ),
        "by_severity": dict(sorted(by_severity.items())),
        "by_category": dict(sorted(by_category.items())),
        "open_total": len(inbox.open_issues),
        "bound_total": bound_issues,
        "unbound_total": len(inbox.issues) - bound_issues,
        "recurrence_after_resolution": recurrences,
        "issues_recurring_after_resolution": recurring_issues,
        "recurrence_after_resolution_by_category": recurrence_by_category,
    }

    by_kind: dict[str, int] = {}
    latencies: list[float] = []
    for record in view.records_of_kind(ledger.KIND_ISSUE_DISPOSITION):
        recorded = _parse(record.recorded_at)
        if recorded is None or recorded < since:
            continue
        disposition = record.payload
        by_kind[disposition.kind] = by_kind.get(disposition.kind, 0) + 1
        opened = _first_seen(inbox, disposition.issue_id)
        if opened is not None and recorded >= opened:
            latencies.append((recorded - opened).total_seconds() / 3600.0)
    disposition_total = sum(by_kind.values())
    report["dispositions"] = {
        "total": disposition_total,
        "by_kind": dict(sorted(by_kind.items())),
        "dismissal_rate": _rate(by_kind.get("dismiss", 0), disposition_total),
        "accepted_risk_rate": _rate(by_kind.get("accept-risk", 0), disposition_total),
        "time_to_disposition": {
            "count": len(latencies),
            "mean_hours": (
                round(statistics.fmean(latencies), 3) if latencies else None
            ),
            "median_hours": (
                round(statistics.median(latencies), 3) if latencies else None
            ),
            "max_hours": round(max(latencies), 3) if latencies else None,
        },
    }

    # Closeouts are counted per run through the projection, so a corrected
    # verdict replaces the one it corrects instead of being counted twice.
    closeout_runs: set[str] = set()
    for record in view.records_of_kind(ledger.KIND_HUMAN_CLOSEOUT):
        recorded = _parse(record.recorded_at)
        if recorded is None or recorded < since:
            continue
        closeout_runs.add(record.payload.run_id)
    by_outcome: dict[str, int] = {}
    escaped_issues: set[str] = set()
    rework_minutes = 0.0
    reporting_rework = 0
    for run_id in sorted(closeout_runs):
        closeout_audit = inbox.run(run_id)
        closeout = closeout_audit.closeout if closeout_audit is not None else None
        if closeout is None:
            continue
        by_outcome[closeout.outcome] = by_outcome.get(closeout.outcome, 0) + 1
        if closeout.outcome == "escaped":
            escaped_issues.update(
                inbox.resolve(issue_id) for issue_id in closeout.issue_ids
            )
        if closeout.rework_minutes is not None:
            rework_minutes += closeout.rework_minutes
            reporting_rework += 1
    report["closeouts"] = {
        "total": sum(by_outcome.values()),
        "by_outcome": dict(sorted(by_outcome.items())),
        "escaped_issues": len(escaped_issues),
        "rework_minutes": round(rework_minutes, 3),
        "closeouts_reporting_rework": reporting_rework,
    }

    identity_anomalies = {reason: 0 for reason in projection.IDENTITY_ANOMALY_REASONS}
    for anomaly in inbox.anomalies:
        if anomaly.reason in identity_anomalies:
            identity_anomalies[anomaly.reason] += 1
    report["integrity"] = {
        "anomalies": len(inbox.anomalies),
        "rejected_records": len(inbox.rejected),
        # Split because they mean different things: a conflict is a stored key
        # claiming a second issue, while a duplicate only restates a binding
        # already in force.
        "identity_key_conflicts": identity_anomalies[projection.IDENTITY_KEY_CONFLICT],
        "duplicate_identity_bindings": identity_anomalies[
            projection.DUPLICATE_IDENTITY_BINDING
        ],
    }
    return report
