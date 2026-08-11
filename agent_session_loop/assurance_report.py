"""Read-only operator, metrics, and retention projections over assurance.

Every surface an operator reads — the end of ``asl run``, ``asl status``, ``asl
metrics``, ``asl gc``, the Workbench queue, and the Autopilot supervision
result — needs to answer the same question: did the last run find anything, and
what is still open. This module is the single place that answers it, and it is
deliberately unable to do anything else:

* **It cannot launch an audit.** Its only siblings are the assurance ledger,
  its models, and its projection. Nothing here can reach a harness, a
  subprocess, or the runner, so an observer that imports it stays an observer.
  ``asl status`` depends on that structurally rather than on a rule.
* **It never writes.** The ledger is append-only and is read here under the
  shared lock the projection already takes; the disposable index is not touched
  at all.
* **An unreadable ledger is reported, not raised.** Every entry point that
  needs ledger truth degrades to an explicit ``available: False`` with the
  reason, because a status command that dies on a corrupt inbox is worse than
  one that says the inbox could not be read.
* **Bounded output stays bounded.** The summaries carried through the Workbench
  and Autopilot cap their issue lists and flag the truncation, so a run that
  opened two hundred issues cannot inflate a queue payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import statistics
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_models as models
from . import assurance_projection as projection


__all__ = [
    "LIST_COMMAND",
    "MAX_BOUNDED_ISSUE_IDS",
    "RunAssuranceReport",
    "bounded_summary",
    "build_run_report",
    "collect_assurance_metrics",
    "open_issue_run_ids",
    "report_lines",
    "resolve_report_path",
    "status_line",
]


#: The command that lists the shared inbox. Printed beside every count so an
#: operator never has to remember where the issue IDs lead.
LIST_COMMAND = "asl assurance list"

#: Issue IDs carried into a bounded consumer payload before truncation. The
#: authoritative list is the run's own assurance object and the inbox.
MAX_BOUNDED_ISSUE_IDS = 8

#: Evidence locators are anchored to a named root; audit artifacts live under
#: the run directory, so this prefix maps a locator back onto the filesystem.
_RUN_ANCHOR_PREFIX = "run/"


# ---------------------------------------------------------------------------
# Reading one run's persisted assurance object
# ---------------------------------------------------------------------------


def _format_timeout(value: Any) -> str:
    if isinstance(value, (int, float)) and float(value).is_integer():
        return f"{int(value)}s"
    return f"{value}s"


def status_line(assurance: Mapping[str, Any] | None) -> str | None:
    """Render the one-line operator summary of a run's assurance object."""
    if not isinstance(assurance, Mapping):
        return None
    audit_state = assurance.get("state")
    if not audit_state:
        return None
    role = assurance.get("role")
    if isinstance(role, Mapping) and role.get("harness"):
        detail = (
            f" ({role.get('harness')} via {role.get('source')} role, "
            f"{_format_timeout(role.get('timeout_seconds'))} per attempt)"
        )
    elif assurance.get("reason"):
        detail = f" ({assurance.get('reason')})"
    else:
        detail = ""
    return f"Supervisor audit: {audit_state}{detail}"


def _issue_ids(assurance: Mapping[str, Any]) -> tuple[str, ...]:
    raw = assurance.get("issue_ids")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(str(item) for item in raw)


def _count(assurance: Mapping[str, Any], key: str) -> int:
    value = assurance.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def resolve_report_path(
    assurance: Mapping[str, Any], run_dir: str | Path | None
) -> str | None:
    """Return the audit report an operator can open, or ``None``.

    The locator stored on the artifact is run-relative by design, so a caller
    that knows the run directory gets an absolute path and one that does not
    still gets the locator rather than nothing.
    """
    artifacts = assurance.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        return None
    locator: str | None = None
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        candidate = artifact.get("locator")
        if not isinstance(candidate, str):
            continue
        if candidate.rsplit("/", 1)[-1] == models.AUDIT_REPORT_FILENAME:
            locator = candidate
            break
    if locator is None:
        return None
    if run_dir is None or not locator.startswith(_RUN_ANCHOR_PREFIX):
        return locator
    return str(Path(run_dir) / locator[len(_RUN_ANCHOR_PREFIX) :])


def _is_partial(assurance: Mapping[str, Any], issue_ids: Sequence[str]) -> bool:
    """Say whether these issue IDs are a partial verdict rather than a whole one.

    ``unavailable`` is the only state that can carry findings without claiming
    full rubric coverage: the deterministic half ran, the agent half did not.
    Labelling those IDs partial is the difference between "these are the
    problems" and "these are the problems we managed to see".
    """
    return bool(issue_ids) and assurance.get("state") == "unavailable"


@dataclass(frozen=True)
class RunAssuranceReport:
    """The additive block every run and status surface prints and emits."""

    run_id: str | None
    state: str
    eligible: bool
    reason: str | None
    issue_ids: tuple[str, ...]
    new_issue_count: int
    recurring_issue_count: int
    partial: bool
    report_path: str | None
    open_issue_count: int | None
    open_issue_error: str | None
    list_command: str = LIST_COMMAND

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "eligible": self.eligible,
            "reason": self.reason,
            "issue_ids": list(self.issue_ids),
            "issue_count": len(self.issue_ids),
            "new_issue_count": self.new_issue_count,
            "recurring_issue_count": self.recurring_issue_count,
            "partial": self.partial,
            "report_path": self.report_path,
            "open_issue_count": self.open_issue_count,
            "open_issue_error": self.open_issue_error,
            "list_command": self.list_command,
        }


def _open_issue_count(state_dir: str | Path | None) -> tuple[int | None, str | None]:
    try:
        return len(projection.read_projection(state_dir=state_dir).open_issues), None
    except (OSError, models.AssuranceError) as error:
        return None, str(error)


def build_run_report(
    assurance: Mapping[str, Any] | None,
    *,
    run_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    open_issues: bool = True,
) -> dict[str, Any] | None:
    """Project one run's assurance object into the additive operator block.

    ``open_issues`` reads the shared ledger for the corpus-wide open count.
    Callers that must not touch the ledger pass ``False`` and get an explicit
    ``open_issue_count: null`` instead of a number nobody computed.
    """
    if not isinstance(assurance, Mapping) or not assurance.get("state"):
        return None
    issue_ids = _issue_ids(assurance)
    if open_issues:
        open_count, open_error = _open_issue_count(state_dir)
    else:
        open_count, open_error = None, None
    return RunAssuranceReport(
        run_id=(
            str(assurance.get("run_id"))
            if assurance.get("run_id") is not None
            else None
        ),
        state=str(assurance.get("state")),
        eligible=bool(assurance.get("eligible")),
        reason=(
            str(assurance.get("reason"))
            if assurance.get("reason") is not None
            else None
        ),
        issue_ids=issue_ids,
        new_issue_count=_count(assurance, "new_issue_count"),
        recurring_issue_count=_count(assurance, "recurring_issue_count"),
        partial=_is_partial(assurance, issue_ids),
        report_path=resolve_report_path(assurance, run_dir),
        open_issue_count=open_count,
        open_issue_error=open_error,
    ).to_dict()


def report_lines(report: Mapping[str, Any] | None) -> list[str]:
    """Render the concise operator text for an additive run report."""
    if not isinstance(report, Mapping) or not report.get("state"):
        return []
    issue_ids = report.get("issue_ids")
    listed = (
        ", ".join(str(item) for item in issue_ids)
        if isinstance(issue_ids, Sequence)
        and not isinstance(issue_ids, (str, bytes))
        and issue_ids
        else "none"
    )
    label = "Assurance issues (partial)" if report.get("partial") else "Assurance issues"
    lines = [
        f"{label}: {report.get('new_issue_count', 0)} new, "
        f"{report.get('recurring_issue_count', 0)} recurring; {listed}"
    ]
    open_count = report.get("open_issue_count")
    if isinstance(open_count, int):
        lines.append(f"Assurance inbox: {open_count} open; `{LIST_COMMAND}`")
    else:
        detail = report.get("open_issue_error") or "not read"
        lines.append(
            f"Assurance inbox: open count unavailable ({detail}); `{LIST_COMMAND}`"
        )
    report_path = report.get("report_path")
    if report_path:
        lines.append(f"Assurance report: {report_path}")
    return lines


def bounded_summary(
    assurance: Mapping[str, Any] | None,
    *,
    max_issue_ids: int = MAX_BOUNDED_ISSUE_IDS,
) -> dict[str, Any]:
    """Project a run's assurance object for a consumer that only displays it.

    Deliberately derived from the run's own persisted object and nothing else:
    no ledger read, no path resolution, no command. A consumer holding this can
    tell an operator what the audit found and where to look it up, and has no
    field with which to configure, launch, retry, or dispose of anything.
    """
    if not isinstance(assurance, Mapping) or not assurance.get("state"):
        return {}
    issue_ids = _issue_ids(assurance)
    shown = issue_ids[: max(0, max_issue_ids)]
    return {
        "run_id": (
            str(assurance.get("run_id"))
            if assurance.get("run_id") is not None
            else None
        ),
        "state": str(assurance.get("state")),
        "eligible": bool(assurance.get("eligible")),
        "reason": (
            str(assurance.get("reason"))
            if assurance.get("reason") is not None
            else None
        ),
        "issue_ids": list(shown),
        "issue_count": len(issue_ids),
        "issue_ids_truncated": len(shown) < len(issue_ids),
        "new_issue_count": _count(assurance, "new_issue_count"),
        "recurring_issue_count": _count(assurance, "recurring_issue_count"),
        "partial": _is_partial(assurance, issue_ids),
        "status_line": status_line(assurance),
        "list_command": LIST_COMMAND,
    }


# ---------------------------------------------------------------------------
# Retention pinning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenIssueRuns:
    """The run IDs an open issue still needs the artifacts of."""

    run_ids: frozenset[str]
    open_issue_count: int
    available: bool
    error: str | None = None


def open_issue_run_ids(state_dir: str | Path | None = None) -> OpenIssueRuns:
    """Return the run IDs referenced by every currently open issue.

    Pinning is by run ID alone because that is all an occurrence records. Two
    plans cannot mint the same ``<utc-timestamp>-<pid>`` run ID in practice, and
    the failure mode of a collision is retaining one extra run directory rather
    than deleting evidence an open issue points at.

    A ledger that cannot be read yields ``available: False`` and an empty set.
    The caller decides what that means; nothing here silently claims there is
    nothing to pin.
    """
    try:
        inbox = projection.read_projection(state_dir=state_dir)
    except (OSError, models.AssuranceError) as error:
        return OpenIssueRuns(
            run_ids=frozenset(),
            open_issue_count=0,
            available=False,
            error=str(error),
        )
    open_issues = inbox.open_issues
    return OpenIssueRuns(
        run_ids=frozenset(
            run_id for issue in open_issues for run_id in issue.run_ids
        ),
        open_issue_count=len(open_issues),
        available=True,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


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
        "audit_states": {audit_state: 0 for audit_state in models.AUDIT_STATES},
        "issues": {
            "observations": 0,
            "new": 0,
            "recurring": 0,
            "by_severity": {},
            "by_category": {},
            "open_total": 0,
            "recurrence_after_resolution": 0,
            "issues_recurring_after_resolution": 0,
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
        "integrity": {"anomalies": 0, "rejected_records": 0},
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
) -> tuple[int, int]:
    """Count in-window sightings of a defect a human had already resolved.

    Replayed rather than read off the projection's ``reopened_count`` so the
    answer can be windowed: the projection reports the lifetime total, and a
    reopen from a year ago says nothing about the last seven days.
    """
    resolved: set[str] = set()
    events_in_window = 0
    issues_in_window: set[str] = set()
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
    return events_in_window, len(issues_in_window)


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
    except (OSError, models.AssuranceError) as error:
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
            if audit.state in models.TERMINAL_AUDIT_STATES:
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
    recurrences, recurring_issues = _recurrence_after_resolution(view, inbox, since)
    report["issues"] = {
        "observations": observations,
        "new": sum(
            audit.summary.new_issue_count for audit in windowed_runs.values()
        ),
        "recurring": sum(
            audit.summary.recurring_issue_count for audit in windowed_runs.values()
        ),
        "by_severity": dict(sorted(by_severity.items())),
        "by_category": dict(sorted(by_category.items())),
        "open_total": len(inbox.open_issues),
        "recurrence_after_resolution": recurrences,
        "issues_recurring_after_resolution": recurring_issues,
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
        "accepted_risk_rate": _rate(
            by_kind.get("accept-risk", 0), disposition_total
        ),
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
        audit = inbox.run(run_id)
        closeout = audit.closeout if audit is not None else None
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

    report["integrity"] = {
        "anomalies": len(inbox.anomalies),
        "rejected_records": len(inbox.rejected),
    }
    return report
