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

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import assurance_models_primitives, assurance_models_vocab
from . import assurance_projection as projection
from .assurance_report_metrics import (
    collect_assurance_metrics as collect_assurance_metrics,
)

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
    if isinstance(value, int):
        return f"{int(value)}s"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)}s"
    return f"{value}s"


def status_line(
    assurance: Mapping[str, Any] | None,
    *,
    report: Mapping[str, Any] | None = None,
) -> str | None:
    """Render the one-line operator summary of a run's assurance object.

    ``report`` is the block :func:`build_run_report` produced for the same run.
    When it names how much of the rubric was judged, that replaces the bare
    reason token: "partial" alone says less than which lanes had no evidence.
    """
    if not isinstance(assurance, Mapping):
        return None
    audit_state = assurance.get("state")
    if not audit_state:
        return None
    role = assurance.get("role")
    coverage = _coverage_detail(report)
    if coverage is not None:
        detail = f" ({coverage})"
    elif isinstance(role, Mapping) and role.get("harness"):
        detail = (
            f" ({role.get('harness')} via {role.get('source')} role, "
            f"{_format_timeout(role.get('timeout_seconds'))} per attempt)"
        )
    elif assurance.get("reason"):
        detail = f" ({assurance.get('reason')})"
    else:
        detail = ""
    return f"Supervisor audit: {audit_state}{detail}"


def _coverage_detail(report: Mapping[str, Any] | None) -> str | None:
    """Phrase a run report's lane coverage, or ``None`` when it is complete."""
    if not isinstance(report, Mapping):
        return None
    judged = report.get("lanes_judged")
    total = report.get("lanes_total")
    if not isinstance(judged, int) or not isinstance(total, int) or judged >= total:
        return None
    unjudged = report.get("lanes_without_evidence")
    listed = (
        ", ".join(str(item) for item in unjudged)
        if isinstance(unjudged, Sequence) and not isinstance(unjudged, (str, bytes))
        else ""
    )
    summary = f"{judged}/{total} lanes judged"
    return f"{summary}; no evidence: {listed}" if listed else summary


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
        if candidate.rsplit("/", 1)[-1] == assurance_models_vocab.AUDIT_REPORT_FILENAME:
            locator = candidate
            break
    if locator is None:
        return None
    if run_dir is None or not locator.startswith(_RUN_ANCHOR_PREFIX):
        return locator
    return str(Path(run_dir) / locator[len(_RUN_ANCHOR_PREFIX) :])


def _lane_coverage(
    report_path: str | None,
) -> tuple[int | None, int | None, tuple[str, ...]]:
    """Count the rubric lanes the stored audit judged on its own evidence.

    Derived on read rather than stored on the run's assurance object: the audit
    report is already the authoritative record of lane status, so recomputing
    here keeps the persisted assurance schema untouched.
    """
    if not report_path or not Path(report_path).is_absolute():
        return None, None, ()
    try:
        payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except OSError, ValueError, RecursionError:
        return None, None, ()
    lanes = payload.get("lanes") if isinstance(payload, Mapping) else None
    if not isinstance(lanes, Sequence) or isinstance(lanes, (str, bytes)) or not lanes:
        return None, None, ()
    absent = tuple(
        str(lane.get("lane"))
        for lane in lanes
        if isinstance(lane, Mapping) and lane.get("status") == "unavailable"
    )
    return len(lanes) - len(absent), len(lanes), absent


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
    lanes_judged: int | None = None
    lanes_total: int | None = None
    lanes_without_evidence: tuple[str, ...] = ()
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
            "lanes_judged": self.lanes_judged,
            "lanes_total": self.lanes_total,
            "lanes_without_evidence": list(self.lanes_without_evidence),
            "report_path": self.report_path,
            "open_issue_count": self.open_issue_count,
            "open_issue_error": self.open_issue_error,
            "list_command": self.list_command,
        }


def _open_issue_count(state_dir: str | Path | None) -> tuple[int | None, str | None]:
    try:
        return len(projection.read_projection(state_dir=state_dir).open_issues), None
    except (OSError, assurance_models_primitives.AssuranceError) as error:
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
    report_path = resolve_report_path(assurance, run_dir)
    lanes_judged, lanes_total, lanes_without_evidence = _lane_coverage(report_path)
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
        report_path=report_path,
        open_issue_count=open_count,
        open_issue_error=open_error,
        lanes_judged=lanes_judged,
        lanes_total=lanes_total,
        lanes_without_evidence=lanes_without_evidence,
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
    label = (
        "Assurance issues (partial)" if report.get("partial") else "Assurance issues"
    )
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
    except (OSError, assurance_models_primitives.AssuranceError) as error:
        return OpenIssueRuns(
            run_ids=frozenset(),
            open_issue_count=0,
            available=False,
            error=str(error),
        )
    open_issues = inbox.open_issues
    return OpenIssueRuns(
        run_ids=frozenset(run_id for issue in open_issues for run_id in issue.run_ids),
        open_issue_count=len(open_issues),
        available=True,
    )
