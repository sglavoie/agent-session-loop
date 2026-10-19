"""Focused implementation extracted from :mod:`.assurance_cli`."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_projection as projection
from . import responses
from .assurance_models_primitives import AssuranceError, issue_id_number

"""Operator surface over the assurance inbox.

``asl assurance list`` and ``asl assurance show`` are how a person reads the
inbox, and they read it the way every other consumer does: through
:mod:`agent_session_loop.assurance_projection`, which replays the append-only
ledger. The browse commands append nothing, mint no issue ID, and repair no
malformed line, so browsing a corrupt inbox reports the corruption rather than
quietly rewriting it.

``resolve``, ``dismiss``, ``accept-risk``, ``merge``, and ``closeout`` manage
the inbox the only way an append-only ledger can be managed: by adding a record
that says what a human decided. Nothing is edited and nothing is deleted, so
the history stays complete no matter how many times a decision is revised. Each
of them writes through the projection's lifecycle functions, which validate
against the ledger inside the append lock and name the issue that owns the
history today, and each maps the domain's own refusal reasons straight into the
error envelope rather than inventing its own vocabulary.

Two properties make the printed IDs usable. Every command accepts the canonical
``ASI-42`` and the documented bare ``42`` shorthand, and always emits the
canonical form, so an ID printed anywhere pastes into another command
unchanged. And one payload builder feeds the text and JSON renderers, so the
two surfaces cannot disagree about what an issue is.

``reconcile`` repairs identity itself rather than an issue's lifecycle, and it
splits into a read and a write for that reason. The preview reads the inbox and
writes nothing, including no file; a person turns its suggestions into explicit
decisions; ``--apply MANIFEST`` replays only those decisions, as one append,
against the catalog the reviewer saw. No suggestion is ever acted on
automatically, because every one of them rests on text an agent wrote.

``process-pending`` and ``plan`` are the two commands here that run a harness,
and both defer that import into the handler so browsing never loads it.
``process-pending`` audits the runs whose own process died before it could audit
itself, which is work no observational command may do: abandonment is detected
inside ``asl status`` and inside lock acquisition, so the detection side only
records the debt and this side pays it, out of band and only when a person asks.
``plan`` turns an issue into a Markdown proposal a human reads and decides on;
it produces a document and a ledger record, and reaches nothing that could act
on either.

Unreadable ledger lines are reported as warnings on stderr rather than as
failures: the rest of the file is still history a human needs, and hiding the
rejected lines would hide the only evidence that the ledger is damaged.
"""

COMMAND_ASSURANCE_LIST = "assurance-list"

COMMAND_ASSURANCE_SHOW = "assurance-show"

COMMAND_ASSURANCE_MERGE = "assurance-merge"

COMMAND_ASSURANCE_CLOSEOUT = "assurance-closeout"

COMMAND_ASSURANCE_PROCESS_PENDING = "assurance-process-pending"

COMMAND_ASSURANCE_PLAN = "assurance-plan"

COMMAND_ASSURANCE_RECONCILE = "assurance-reconcile"

COMMAND_ASSURANCE_RECONCILE_APPLY = "assurance-reconcile-apply"

TEXT_LABEL_LIST = "assurance list"

TEXT_LABEL_SHOW = "assurance show"

TEXT_LABEL_MERGE = "assurance merge"

TEXT_LABEL_CLOSEOUT = "assurance closeout"

TEXT_LABEL_PROCESS_PENDING = "assurance process-pending"

TEXT_LABEL_PLAN = "assurance plan"

TEXT_LABEL_RECONCILE = "assurance reconcile"

DISPOSITION_CONSEQUENCE: dict[str, str] = {
    "resolve": "a later recurrence will reopen it",
    "dismiss": "a later recurrence will be recorded without reopening it",
    "accept-risk": "a later recurrence will be recorded without reopening it",
}

DEFAULT_LIST_LIMIT = 50

DEFAULT_LIST_STATE = "open"

MAX_RENDERED_OCCURRENCES = 20

MAX_RENDERED_ARTIFACTS = 20

MAX_RENDERED_WARNINGS = 20

SUMMARY_PREVIEW_CHARS = 100

MAX_RENDERED_RECONCILE_ROWS = 20

REASON_INPUT_ERROR = "input-error"

REASON_INVALID_ISSUE_ID = "invalid-issue-id"

REASON_UNKNOWN_ISSUE = "unknown-issue"

REASON_LEDGER_READ_FAILED = ledger.ERROR_LEDGER_READ

REASON_LEDGER_WRITE_FAILED = ledger.ERROR_LEDGER_WRITE

REASON_UNKNOWN_RUN = projection.UNKNOWN_RUN

REASON_AMBIGUOUS_RUN = "ambiguous-run"

REASON_AMBIGUOUS_PLAN = "ambiguous-plan"

REASON_CANCELLED = "cancelled"

REASON_MANIFEST_READ_FAILED = "manifest-read-failed"


def _print_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    reason: str,
    error: str,
    details: dict[str, Any] | None = None,
    hint: str | None = None,
    **fields: Any,
) -> int:
    """Report one failure in whichever form the caller asked for."""
    if args.json:
        responses.print_json(
            responses.error_payload(
                command_name,
                reason=reason,
                error=error,
                paused=False,
                details=details,
                **fields,
            )
        )
        return 1
    message = f"agent-session-loop {text_label}: {error}"
    if hint is not None:
        message = f"{message}; {hint}"
    print(message, file=sys.stderr)
    return 1


def _read_inbox(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> projection.AssuranceProjection | None:
    """Return the projected inbox, or ``None`` after reporting why it is unreadable."""
    try:
        return projection.read_projection(state_dir=args.state_dir)
    except ledger.LedgerError as error:
        _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=error.reason,
            error=str(error),
            details=None if error.path is None else {"path": str(error.path)},
        )
    except (AssuranceError, OSError) as error:
        _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=REASON_LEDGER_READ_FAILED,
            error=str(error),
        )
    return None


def _warning_payloads(
    inbox: projection.AssuranceProjection,
) -> list[dict[str, Any]]:
    """Describe every stored record the projection could not apply.

    The raw text of a rejected line is deliberately left out: a warning's job
    is to name the line and the reason, and the bytes themselves are arbitrary
    content that belongs in the ledger rather than on an operator's terminal.
    """
    warnings: list[dict[str, Any]] = [
        {
            "kind": "rejected-line",
            "seq": record.seq,
            "record_kind": None,
            "reason": record.reason,
            "detail": record.detail,
        }
        for record in inbox.rejected
    ]
    warnings.extend(
        {
            "kind": "skipped-record",
            "seq": anomaly.seq,
            "record_kind": anomaly.kind,
            "reason": anomaly.reason,
            "detail": anomaly.detail,
        }
        for anomaly in inbox.anomalies
    )
    warnings.sort(key=lambda item: item["seq"])
    return warnings


def _warning_fields(inbox: projection.AssuranceProjection) -> dict[str, Any]:
    warnings = _warning_payloads(inbox)
    shown = warnings[:MAX_RENDERED_WARNINGS]
    return {
        "warnings": shown,
        "warning_count": len(warnings),
        "warnings_omitted": len(warnings) - len(shown),
        "rejected_count": len(inbox.rejected),
        "anomaly_count": len(inbox.anomalies),
    }


def _print_warnings(fields: dict[str, Any]) -> None:
    for warning in fields["warnings"]:
        if warning["kind"] == "rejected-line":
            head = f"ledger line {warning['seq']} was rejected ({warning['reason']})"
        else:
            head = (
                f"ledger record {warning['seq']} ({warning['record_kind']}) was "
                f"skipped ({warning['reason']})"
            )
        detail = warning["detail"]
        body = head if detail is None else f"{head}: {detail}"
        print(f"agent-session-loop assurance: warning: {body}", file=sys.stderr)
    omitted = fields["warnings_omitted"]
    if omitted:
        print(
            f"agent-session-loop assurance: warning: {omitted} further "
            "unreadable ledger record(s) not shown",
            file=sys.stderr,
        )


def _preview(text: str, *, limit: int = SUMMARY_PREVIEW_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def _issue_row(issue: projection.Issue) -> dict[str, Any]:
    return {
        "issue_id": issue.issue_id,
        "severity": issue.finding.severity,
        "state": issue.state,
        "occurrence_count": issue.occurrence_count,
        "first_seen_at": issue.first_seen_at,
        "last_seen_at": issue.last_seen_at,
        "component": issue.finding.component,
        "summary": issue.finding.summary,
        "primary_identity_key": issue.primary_identity_key,
    }


def _occurrence_payload(
    occurrence: projection.IssueOccurrence,
) -> dict[str, Any]:
    return {
        "seq": occurrence.seq,
        "run_id": occurrence.run_id,
        "observed_at": occurrence.observed_at,
        "fingerprint": occurrence.fingerprint,
        "severity": occurrence.severity,
        "audit_artifact": occurrence.audit_artifact,
    }


def _run_payload(inbox: projection.AssuranceProjection, run_id: str) -> dict[str, Any]:
    audit = inbox.run(run_id)
    if audit is None:
        # An observation names the run that saw the defect, and only an audit
        # record creates a run. A hand-written observation can therefore point
        # at a run the ledger never audited.
        return {
            "run_id": run_id,
            "state": None,
            "eligible": None,
            "artifacts": [],
            "artifacts_omitted": 0,
            "closeout": None,
        }
    artifacts = audit.summary.artifacts[:MAX_RENDERED_ARTIFACTS]
    closeout = audit.closeout
    return {
        "run_id": run_id,
        "state": audit.state,
        "eligible": audit.summary.eligible,
        "artifacts": [artifact.to_dict() for artifact in artifacts],
        "artifacts_omitted": len(audit.summary.artifacts) - len(artifacts),
        "closeout": None if closeout is None else closeout.to_dict(),
    }


def _issue_detail(
    inbox: projection.AssuranceProjection, issue: projection.Issue
) -> dict[str, Any]:
    occurrences = issue.occurrences[-MAX_RENDERED_OCCURRENCES:]
    finding = issue.finding
    return {
        "issue_id": issue.issue_id,
        "state": issue.state,
        "severity": finding.severity,
        "reopened_count": issue.reopened_count,
        "occurrence_count": issue.occurrence_count,
        "first_seen_at": issue.first_seen_at,
        "last_seen_at": issue.last_seen_at,
        "fingerprint": issue.fingerprint,
        "fingerprints": list(issue.fingerprints),
        "primary_identity_key": issue.primary_identity_key,
        "identity_keys": list(issue.identity_keys),
        "schema_version": finding.schema_version,
        "category": finding.category,
        "component": finding.component,
        "invariant_key": finding.invariant_key,
        "lane": finding.lane,
        "summary": finding.summary,
        "recommendation": finding.recommendation,
        "evidence": list(finding.evidence),
        "merged_from": list(issue.merged_from),
        "occurrences": [_occurrence_payload(item) for item in occurrences],
        "occurrences_omitted": issue.occurrence_count - len(occurrences),
        "dispositions": [
            {
                "kind": item.kind,
                "result_state": item.result_state,
                "recorded_at": item.recorded_at,
                "note": item.note,
            }
            for item in issue.dispositions
        ],
        "plans": [item.to_dict() for item in issue.plans],
        "runs": [_run_payload(inbox, run_id) for run_id in issue.run_ids],
    }


def _observed_order(issue: projection.Issue) -> tuple[datetime, int]:
    return (
        datetime.fromisoformat(issue.last_seen_at),
        issue_id_number(issue.issue_id),
    )


def _matching_issues(
    inbox: projection.AssuranceProjection,
    *,
    state: str | None,
    severity: str | None,
) -> tuple[projection.Issue, ...]:
    """Return matching issues, most recently observed first."""
    matched = [
        issue
        for issue in inbox.issues
        if (state is None or issue.state == state)
        and (severity is None or issue.finding.severity == severity)
    ]
    matched.sort(key=_observed_order, reverse=True)
    return tuple(matched)


def _list_text_lines(payload: dict[str, Any]) -> list[str]:
    filters = payload["filters"]
    limit = filters["limit"]
    lines = [
        f"Ledger: {payload['ledger_path']}",
        (
            f"Filters: state={filters['state'] or 'any'} "
            f"severity={filters['severity'] or 'any'} "
            f"limit={'none' if limit is None else limit}"
        ),
        (
            f"Issues: {payload['issue_count']} shown of "
            f"{payload['matching_count']} matching "
            f"({payload['total_count']} in inbox)"
        ),
    ]
    if not payload["issues"]:
        lines.append("  No matching issues.")
        return lines
    for row in payload["issues"]:
        lines.append(
            f"  {row['issue_id']:<9}"
            f"  {row['severity']:<8}"
            f"  {row['state']:<13}"
            f"  x{row['occurrence_count']:<4}"
            f"  last {row['last_seen_at']}"
            f"  {row['component']}"
            f"  {_preview(row['summary'])}"
        )
    if payload["omitted_count"]:
        lines.append(
            f"  ... {payload['omitted_count']} more matching issue(s) not "
            "shown; raise --limit or pass --all"
        )
    return lines
