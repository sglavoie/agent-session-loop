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

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_projection as projection
from . import assurance_reconcile as reconcile
from . import responses, state
from .assurance_models import (
    CLOSEOUT_OUTCOMES,
    DECISION_MERGE,
    ISSUE_STATES,
    SEVERITIES,
    AssuranceError,
    ReconciliationManifest,
    issue_id_number,
    parse_issue_id,
    parse_reconciliation_manifest,
)

COMMAND_ASSURANCE_LIST = "assurance-list"
COMMAND_ASSURANCE_SHOW = "assurance-show"
COMMAND_ASSURANCE_MERGE = "assurance-merge"
COMMAND_ASSURANCE_CLOSEOUT = "assurance-closeout"
COMMAND_ASSURANCE_PROCESS_PENDING = "assurance-process-pending"
COMMAND_ASSURANCE_PLAN = "assurance-plan"
#: Preview and apply are two commands rather than two modes of one, because
#: their payloads share no field: one describes an inbox and the other describes
#: a write. A consumer that switched on ``ok`` alone could not tell them apart.
COMMAND_ASSURANCE_RECONCILE = "assurance-reconcile"
COMMAND_ASSURANCE_RECONCILE_APPLY = "assurance-reconcile-apply"

TEXT_LABEL_LIST = "assurance list"
TEXT_LABEL_SHOW = "assurance show"
TEXT_LABEL_MERGE = "assurance merge"
TEXT_LABEL_CLOSEOUT = "assurance closeout"
TEXT_LABEL_PROCESS_PENDING = "assurance process-pending"
TEXT_LABEL_PLAN = "assurance plan"
TEXT_LABEL_RECONCILE = "assurance reconcile"

#: What each disposition claims about the future, which is the part an operator
#: needs to hear back. None of them removes the issue: a later recurrence is
#: always recorded, and only ``resolve`` lets that recurrence reopen it.
DISPOSITION_CONSEQUENCE: dict[str, str] = {
    "resolve": "a later recurrence will reopen it",
    "dismiss": "a later recurrence will be recorded without reopening it",
    "accept-risk": "a later recurrence will be recorded without reopening it",
}

#: Rows the default view prints. This is a view rather than a truncation of
#: the inbox: ``--limit`` widens it and ``--all`` removes it entirely.
DEFAULT_LIST_LIMIT = 50

#: The lifecycle state the default view answers for.
DEFAULT_LIST_STATE = "open"

#: ``Issue.occurrences`` grows without bound over a long-lived inbox, so
#: ``show`` caps its history the same way ``list`` caps its rows. The newest
#: occurrences are kept, because those are the ones a triage decision rests on.
MAX_RENDERED_OCCURRENCES = 20

#: Artifact references rendered per affected run.
MAX_RENDERED_ARTIFACTS = 20

#: Unreadable-record warnings rendered before the remainder is counted.
MAX_RENDERED_WARNINGS = 20

#: A finding summary is free text of up to 2000 characters; a list row is one
#: line, so the row carries a preview and ``show`` carries the whole thing.
SUMMARY_PREVIEW_CHARS = 100

#: Proposed bindings and suggested pairs printed by the text preview. The JSON
#: form always carries every one, because that is the form a manifest is cut
#: from; the text form is a summary a person reads before asking for the JSON.
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Payload builders shared by the text and JSON renderers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# asl assurance list
# ---------------------------------------------------------------------------


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


def assurance_list_from_args(args: argparse.Namespace) -> int:
    """List inbox issues newest sighting first, without touching the ledger."""
    show_all = bool(args.all)

    def refuse(error: str, hint: str | None = None) -> int:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_LIST,
            text_label=TEXT_LABEL_LIST,
            reason=REASON_INPUT_ERROR,
            error=error,
            hint=hint,
        )

    if show_all and (args.limit is not None or args.state is not None):
        return refuse(
            "--all cannot be combined with --limit or --state",
            "--all already means every state with no row cap",
        )
    if args.limit is not None and args.limit < 1:
        return refuse(f"--limit must be a positive integer; got {args.limit}")
    if args.state is not None and args.state not in ISSUE_STATES:
        return refuse(
            f"--state must be one of {', '.join(ISSUE_STATES)}; got {args.state!r}"
        )
    if args.severity is not None and args.severity not in SEVERITIES:
        return refuse(
            f"--severity must be one of {', '.join(SEVERITIES)}; got {args.severity!r}"
        )

    state_filter = None if show_all else (args.state or DEFAULT_LIST_STATE)
    limit = (
        None if show_all else (DEFAULT_LIST_LIMIT if args.limit is None else args.limit)
    )

    inbox = _read_inbox(
        args,
        command_name=COMMAND_ASSURANCE_LIST,
        text_label=TEXT_LABEL_LIST,
    )
    if inbox is None:
        return 1

    matched = _matching_issues(inbox, state=state_filter, severity=args.severity)
    shown = matched if limit is None else matched[:limit]
    warning_fields = _warning_fields(inbox)
    payload = responses.success_payload(
        COMMAND_ASSURANCE_LIST,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        filters={
            "state": state_filter,
            "severity": args.severity,
            "limit": limit,
            "all": show_all,
        },
        issues=[_issue_row(issue) for issue in shown],
        issue_count=len(shown),
        matching_count=len(matched),
        total_count=len(inbox.issues),
        omitted_count=len(matched) - len(shown),
        **warning_fields,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    _print_warnings(warning_fields)
    for line in _list_text_lines(payload):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# asl assurance show
# ---------------------------------------------------------------------------


def _show_text_lines(payload: dict[str, Any]) -> list[str]:
    issue = payload["issue"]
    lines = [
        f"Issue {issue['issue_id']}  state {issue['state']}  "
        f"severity {issue['severity']}"
    ]
    if payload["merged"]:
        lines.append(
            f"{payload['requested_issue_id']} was merged into {issue['issue_id']}."
        )
    lines.extend(
        (
            f"Ledger: {payload['ledger_path']}",
            "Fingerprint:",
            f"  fingerprint     {issue['fingerprint']}",
            f"  category        {issue['category']}",
            f"  component       {issue['component']}",
            f"  invariant key   {issue['invariant_key']}",
            f"  lane            {issue['lane']}",
            f"  schema version  {issue['schema_version']}",
        )
    )
    absorbed_fingerprints = issue["fingerprints"][1:]
    if absorbed_fingerprints:
        lines.append(f"  also observed as {', '.join(absorbed_fingerprints)}")
    lines.append(f"  identity key    {issue['primary_identity_key'] or 'unbound'}")
    absorbed_keys = [
        key for key in issue["identity_keys"] if key != issue["primary_identity_key"]
    ]
    if absorbed_keys:
        lines.append(f"  also named by   {', '.join(absorbed_keys)}")
    lines.extend(
        (
            f"History: {issue['occurrence_count']} occurrence(s)  "
            f"first {issue['first_seen_at']}  last {issue['last_seen_at']}  "
            f"reopened {issue['reopened_count']}",
            f"Summary: {issue['summary']}",
            f"Recommendation: {issue['recommendation']}",
            "Evidence:",
        )
    )
    lines.extend(f"  {locator}" for locator in issue["evidence"])
    if issue["merged_from"]:
        lines.append(f"Merged from: {', '.join(issue['merged_from'])}")

    lines.append(f"Observations ({issue['occurrence_count']}):")
    if issue["occurrences_omitted"]:
        lines.append(
            f"  ... {issue['occurrences_omitted']} older occurrence(s) not shown"
        )
    for occurrence in issue["occurrences"]:
        artifact = occurrence["audit_artifact"]
        lines.append(
            f"  seq {occurrence['seq']}  {occurrence['observed_at']}"
            f"  severity {occurrence['severity']}"
            f"  run {occurrence['run_id']}"
            + ("" if artifact is None else f"  artifact {artifact}")
        )

    lines.append(f"Dispositions ({len(issue['dispositions'])}):")
    for disposition in issue["dispositions"]:
        note = disposition["note"]
        lines.append(
            f"  {disposition['recorded_at']}  {disposition['kind']} -> "
            f"{disposition['result_state']}"
            + ("" if note is None else f"  note {note}")
        )

    lines.append(f"Affected runs ({len(issue['runs'])}):")
    for run in issue["runs"]:
        closeout = run["closeout"]
        lines.append(
            f"  {run['run_id']}  audit {run['state'] or 'no audit record'}"
            + ("" if closeout is None else f"  closeout {closeout['outcome']}")
        )
        for artifact in run["artifacts"]:
            digest = artifact["digest"]
            lines.append(
                f"    {artifact['kind']} {artifact['locator']}"
                + ("" if digest is None else f"  sha256 {digest}")
            )
        if run["artifacts_omitted"]:
            lines.append(
                f"    ... {run['artifacts_omitted']} more artifact "
                "reference(s) not shown"
            )

    if issue["plans"]:
        lines.append(f"Improvement plans ({len(issue['plans'])}):")
        for plan in issue["plans"]:
            lines.append(
                f"  {plan['drafted_at']}  {plan['harness']}  "
                f"{plan['output_path']}  sha256 {plan['output_sha256']}"
            )
    return lines


def assurance_show_from_args(args: argparse.Namespace) -> int:
    """Print everything the ledger records about one issue."""
    try:
        requested = parse_issue_id(args.issue)
    except AssuranceError as error:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_SHOW,
            text_label=TEXT_LABEL_SHOW,
            reason=REASON_INVALID_ISSUE_ID,
            error=str(error),
        )

    inbox = _read_inbox(
        args,
        command_name=COMMAND_ASSURANCE_SHOW,
        text_label=TEXT_LABEL_SHOW,
    )
    if inbox is None:
        return 1

    warning_fields = _warning_fields(inbox)
    issue = inbox.issue(requested)
    ledger_path = str(ledger.ledger_path(args.state_dir))
    if issue is None:
        # Warnings come first: a rejected line is often the reason an ID the
        # operator legitimately copied no longer names anything.
        if not args.json:
            _print_warnings(warning_fields)
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_SHOW,
            text_label=TEXT_LABEL_SHOW,
            reason=REASON_UNKNOWN_ISSUE,
            error=f"{requested} names no issue in the inbox",
            details={"issue_id": requested, "ledger_path": ledger_path},
            hint="run `asl assurance list --all` to see every issue",
            **warning_fields,
        )

    payload = responses.success_payload(
        COMMAND_ASSURANCE_SHOW,
        ledger_path=ledger_path,
        requested_issue_id=requested,
        issue_id=issue.issue_id,
        merged=requested != issue.issue_id,
        issue=_issue_detail(inbox, issue),
        **warning_fields,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    _print_warnings(warning_fields)
    for line in _show_text_lines(payload):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# Shared write-path helpers
# ---------------------------------------------------------------------------


def _report_write_failure(
    args: argparse.Namespace,
    error: Exception,
    *,
    command_name: str,
    text_label: str,
    hints: dict[str, str] | None = None,
) -> int:
    """Translate one failed append into the command's error envelope.

    Reasons come from the domain rather than from this module: every merge
    refusal is a member of ``projection.MERGE_REJECTIONS`` and every ledger
    failure carries its own reason, so a new refusal reaches operators without
    an edit here.
    """
    if isinstance(error, projection.ProjectionError):
        return _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=error.reason,
            error=error.detail or error.reason,
            hint=None if hints is None else hints.get(error.reason),
        )
    if isinstance(error, ledger.LedgerError):
        return _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=error.reason,
            error=str(error),
            details=None if error.path is None else {"path": str(error.path)},
        )
    if isinstance(error, AssuranceError):
        return _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=REASON_INPUT_ERROR,
            error=str(error),
        )
    return _print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason=REASON_LEDGER_WRITE_FAILED,
        error=str(error),
    )


def _confirm(args: argparse.Namespace, question: str) -> bool:
    """Ask before an irreversible act, when there is a person there to ask.

    Merging cannot be undone, so a human at a terminal is asked first. A piped
    or redirected invocation is a deliberate one and proceeds unprompted, which
    keeps the documented command shape scriptable.
    """
    if getattr(args, "yes", False) or args.json:
        return True
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        return True
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError, ValueError:
        return False


# ---------------------------------------------------------------------------
# asl assurance resolve | dismiss | accept-risk
# ---------------------------------------------------------------------------


def assurance_disposition_from_args(args: argparse.Namespace) -> int:
    """Append one human decision about an issue, never editing an old record."""
    kind = args.disposition_kind
    command_name = f"assurance-{kind}"
    text_label = f"assurance {kind}"
    try:
        requested = parse_issue_id(args.issue)
    except AssuranceError as error:
        return _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=REASON_INVALID_ISSUE_ID,
            error=str(error),
        )

    try:
        record = projection.record_disposition(
            requested, kind, note=args.note, state_dir=args.state_dir
        )
    except (AssuranceError, OSError) as error:
        return _report_write_failure(
            args,
            error,
            command_name=command_name,
            text_label=text_label,
            hints={
                REASON_UNKNOWN_ISSUE: (
                    "run `asl assurance list --all` to see every issue"
                )
            },
        )

    disposition = record.payload
    payload = responses.success_payload(
        command_name,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        seq=record.seq,
        requested_issue_id=requested,
        issue_id=disposition.issue_id,
        merged=requested != disposition.issue_id,
        kind=disposition.kind,
        result_state=disposition.result_state,
        recorded_at=disposition.recorded_at,
        note=disposition.note,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    if payload["merged"]:
        print(f"{requested} was merged into {disposition.issue_id}.")
    print(
        f"{disposition.issue_id} is now {disposition.result_state}; "
        f"{DISPOSITION_CONSEQUENCE[kind]}."
    )
    if disposition.note is not None:
        print(f"Note: {disposition.note}")
    print(f"Ledger: {payload['ledger_path']} line {record.seq}")
    return 0


# ---------------------------------------------------------------------------
# asl assurance merge
# ---------------------------------------------------------------------------


def assurance_merge_from_args(args: argparse.Namespace) -> int:
    """Redirect one issue's whole history into another, irreversibly."""

    def refuse_id(error: AssuranceError) -> int:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_MERGE,
            text_label=TEXT_LABEL_MERGE,
            reason=REASON_INVALID_ISSUE_ID,
            error=str(error),
        )

    try:
        source = parse_issue_id(args.issue)
        target = parse_issue_id(args.target)
    except AssuranceError as error:
        return refuse_id(error)

    if not _confirm(
        args,
        f"Merge {source} into {target}? This cannot be undone.",
    ):
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_MERGE,
            text_label=TEXT_LABEL_MERGE,
            reason=REASON_CANCELLED,
            error=f"merging {source} into {target} was cancelled",
        )

    try:
        record = projection.record_merge(
            source, target, note=args.note, state_dir=args.state_dir
        )
    except (AssuranceError, OSError) as error:
        return _report_write_failure(
            args,
            error,
            command_name=COMMAND_ASSURANCE_MERGE,
            text_label=TEXT_LABEL_MERGE,
            hints={
                projection.MERGE_SOURCE_ALREADY_MERGED: (
                    f"run `asl assurance show {source}` to see where its history went"
                ),
                projection.MERGE_TARGET_ALREADY_MERGED: (
                    f"run `asl assurance show {target}` to see which issue owns "
                    "its history"
                ),
            },
        )

    merge = record.payload
    payload = responses.success_payload(
        COMMAND_ASSURANCE_MERGE,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        seq=record.seq,
        source_issue_id=merge.source_issue_id,
        target_issue_id=merge.target_issue_id,
        recorded_at=merge.recorded_at,
        note=merge.note,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    print(
        f"{merge.source_issue_id} was merged into {merge.target_issue_id}; "
        "merging is irreversible."
    )
    print(
        f"{merge.source_issue_id} still resolves to "
        f"{merge.target_issue_id} everywhere it is used."
    )
    if merge.note is not None:
        print(f"Note: {merge.note}")
    print(f"Ledger: {payload['ledger_path']} line {record.seq}")
    return 0


# ---------------------------------------------------------------------------
# asl assurance closeout
# ---------------------------------------------------------------------------


def _resolve_run_location(
    args: argparse.Namespace, run_id: str
) -> tuple[state.RunLocation | None, int | None]:
    """Locate ``run_id`` in the state corpus, or explain why it cannot be.

    Returns the location and ``None`` on success, or ``None`` and an exit code
    when the run cannot be attributed to a single plan. A run the corpus does
    not know is not an error by itself: the ledger is what says whether the run
    was audited, and run directories are pruned long before their history is.
    """

    def refuse(reason: str, error: str, hint: str | None = None, **fields: Any) -> int:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_CLOSEOUT,
            text_label=TEXT_LABEL_CLOSEOUT,
            reason=reason,
            error=error,
            hint=hint,
            **fields,
        )

    plan_dir = None
    if args.plan is not None:
        try:
            plan_dir = state.resolve_plan_reference(
                args.plan, state.state_root(args.state_dir)
            )
        except state.AmbiguousPlanError as error:
            return None, refuse(
                REASON_AMBIGUOUS_PLAN,
                str(error),
                "pass a full plan directory path to disambiguate",
            )
        except state.StateError as error:
            return None, refuse(REASON_INPUT_ERROR, str(error))

    try:
        located = state.find_run_locations(
            run_id, state_dir=args.state_dir, plan_dir=plan_dir
        )
    except state.StateError as error:
        return None, refuse(REASON_INPUT_ERROR, str(error))

    if plan_dir is not None and not located:
        return None, refuse(
            REASON_UNKNOWN_RUN,
            f"{run_id} has no run directory under plan {plan_dir}",
            "omit --plan to record the closeout without plan attribution",
        )
    if len(located) > 1:
        return None, refuse(
            REASON_AMBIGUOUS_RUN,
            f"{run_id} names a run under {len(located)} plans",
            "pass --plan PLAN_DIR_OR_ID to say which one",
            candidates=[str(item.plan_dir or item.state_dir) for item in located],
        )
    return (located[0] if located else None), None


def assurance_closeout_from_args(args: argparse.Namespace) -> int:
    """Record what one audited run was worth to the human who reviewed it."""

    def refuse(reason: str, error: str, hint: str | None = None) -> int:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_CLOSEOUT,
            text_label=TEXT_LABEL_CLOSEOUT,
            reason=reason,
            error=error,
            hint=hint,
        )

    if args.outcome not in CLOSEOUT_OUTCOMES:
        return refuse(
            REASON_INPUT_ERROR,
            f"--outcome must be one of {', '.join(CLOSEOUT_OUTCOMES)}; "
            f"got {args.outcome!r}",
        )
    minutes = args.rework_minutes
    if minutes is not None and (not math.isfinite(minutes) or minutes < 0):
        return refuse(
            REASON_INPUT_ERROR,
            f"--rework-minutes must be a finite, non-negative number; got {minutes}",
        )
    try:
        issue_ids = tuple(parse_issue_id(item) for item in args.issue)
    except AssuranceError as error:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_CLOSEOUT,
            text_label=TEXT_LABEL_CLOSEOUT,
            reason=REASON_INVALID_ISSUE_ID,
            error=str(error),
        )

    location, failure = _resolve_run_location(args, args.run_id)
    if failure is not None:
        return failure

    try:
        record = projection.record_closeout(
            args.run_id,
            args.outcome,
            issue_ids=issue_ids,
            rework_minutes=minutes,
            note=args.note,
            plan_id=None if location is None else location.plan_id,
            replace=bool(args.replace),
            state_dir=args.state_dir,
        )
    except (AssuranceError, OSError) as error:
        return _report_write_failure(
            args,
            error,
            command_name=COMMAND_ASSURANCE_CLOSEOUT,
            text_label=TEXT_LABEL_CLOSEOUT,
            hints={
                projection.DUPLICATE_CLOSEOUT: (
                    "pass --replace to supersede it with a new record"
                ),
                REASON_UNKNOWN_RUN: (
                    "only a run the inbox has an audit record for can be closed out"
                ),
                REASON_UNKNOWN_ISSUE: (
                    "run `asl assurance list --all` to see every issue"
                ),
            },
        )

    closeout = record.payload
    payload = responses.success_payload(
        COMMAND_ASSURANCE_CLOSEOUT,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        seq=record.seq,
        run_id=closeout.run_id,
        outcome=closeout.outcome,
        rework_minutes=closeout.rework_minutes,
        issue_ids=list(closeout.issue_ids),
        note=closeout.note,
        plan_id=closeout.plan_id,
        plan_dir=None if location is None else str(location.plan_dir),
        replaces=closeout.replaces,
        recorded_at=closeout.recorded_at,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    print(f"Run {closeout.run_id} was closed out as {closeout.outcome}.")
    if closeout.rework_minutes is not None:
        print(f"Rework: {closeout.rework_minutes:g} minute(s)")
    if closeout.issue_ids:
        print(f"Linked issues: {', '.join(closeout.issue_ids)}")
    if closeout.plan_id is not None:
        print(f"Plan: {closeout.plan_id}")
    if closeout.replaces is not None:
        print(
            f"Supersedes the closeout on ledger line "
            f"{closeout.replaces.removeprefix(projection.CLOSEOUT_REFERENCE_PREFIX)}; "
            "that record is kept."
        )
    if closeout.note is not None:
        print(f"Note: {closeout.note}")
    print(f"Ledger: {payload['ledger_path']} line {record.seq}")
    return 0


# ---------------------------------------------------------------------------
# asl assurance process-pending
# ---------------------------------------------------------------------------


def _pending_result_line(result: dict[str, Any]) -> str:
    """Render one processed run as a line that says what became of it."""
    detail = result["state"] or ""
    if result["reason"]:
        detail = f"{detail} ({result['reason']})" if detail else result["reason"]
    if result["issue_ids"]:
        joined = ", ".join(result["issue_ids"])
        detail = f"{detail} {joined}" if detail else joined
    row = f"  {result['run_id']:<28}  {result['outcome']:<15}"
    return f"{row}  {detail}" if detail else row.rstrip()


def _process_pending_text_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Ledger: {payload['ledger_path']}",
        (
            f"Pending audits: {payload['candidate_count']} recorded, "
            f"{payload['audited_count']} audited (limit {payload['limit']})"
        ),
    ]
    if not payload["results"]:
        lines.append("  No run is recorded as owing an audit.")
        return lines
    lines.extend(_pending_result_line(item) for item in payload["results"])
    if payload["limit_reached"]:
        lines.append(
            f"  ... the --limit of {payload['limit']} was reached; run the "
            "command again to continue"
        )
    return lines


def assurance_process_pending_from_args(args: argparse.Namespace) -> int:
    """Audit the runs that died owing an audit, oldest sighting first."""
    # Imported here rather than at module scope because it pulls in the audit
    # harness machinery, and every other command in this module reads the
    # ledger without ever launching anything.
    from . import assurance_catchup as catchup

    def refuse(reason: str, error: str, hint: str | None = None, **fields: Any) -> int:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_PROCESS_PENDING,
            text_label=TEXT_LABEL_PROCESS_PENDING,
            reason=reason,
            error=error,
            hint=hint,
            **fields,
        )

    if args.limit is not None and args.limit < 1:
        return refuse(
            REASON_INPUT_ERROR,
            f"--limit must be a positive integer; got {args.limit}",
        )
    if args.audit_timeout is not None and args.audit_timeout < 1:
        return refuse(
            REASON_INPUT_ERROR,
            f"--audit-timeout must be a positive integer; got {args.audit_timeout}",
        )

    plan_dir = None
    if args.plan is not None:
        try:
            plan_dir = state.resolve_plan_reference(
                args.plan, state.state_root(args.state_dir)
            )
        except state.AmbiguousPlanError as error:
            return refuse(
                REASON_AMBIGUOUS_PLAN,
                str(error),
                "pass a full plan directory path to disambiguate",
            )
        except state.StateError as error:
            return refuse(REASON_INPUT_ERROR, str(error))

    try:
        report = catchup.process_pending(
            state_dir=args.state_dir,
            plan_dir=plan_dir,
            run_id=args.run_id,
            limit=(catchup.DEFAULT_PENDING_LIMIT if args.limit is None else args.limit),
            overrides=catchup.AuditOverrides(
                harness=args.audit_harness,
                model=args.audit_harness_model,
                effort=args.audit_effort,
                timeout_seconds=args.audit_timeout,
            ),
        )
    except catchup.CatchupError as error:
        return refuse(
            error.reason,
            str(error),
            {
                catchup.REASON_UNKNOWN_RUN: (
                    "only a run abandoned since auditing shipped records one; "
                    "older runs are never backfilled"
                ),
                catchup.REASON_AMBIGUOUS_RUN: (
                    "pass --plan PLAN_DIR_OR_ID to say which one"
                ),
            }.get(error.reason),
        )
    except (AssuranceError, OSError) as error:
        return refuse(REASON_LEDGER_READ_FAILED, str(error))

    payload = responses.success_payload(
        COMMAND_ASSURANCE_PROCESS_PENDING,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        limit=report.limit,
        limit_reached=report.limit_reached,
        candidate_count=report.candidate_count,
        audited_count=len(report.audited),
        results=[item.to_dict() for item in report.results],
    )
    if args.json:
        responses.print_json(payload)
        return 0
    for line in _process_pending_text_lines(payload):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# asl assurance plan
# ---------------------------------------------------------------------------


#: What an operator should do about each refusal. Drafting is the one command
#: here whose failures are usually recoverable by changing the request, so the
#: hint says which part of the request to change.
_PLAN_HINTS: dict[str, str] = {
    "output-exists": (
        "pass a path that does not exist yet, or move the existing proposal aside"
    ),
    "harness-unavailable": (
        "pick a harness that can run under the restricted read-only profile"
    ),
    "malformed-output": ("the reply is discarded, not saved; rerun to draft again"),
    "mutation-detected": (
        "the reply is discarded because the drafter wrote something under a "
        "read-only root; check that root before rerunning"
    ),
    "timeout": "raise --timeout, or narrow the issue before drafting again",
}


def _plan_text_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Issue: {payload['issue_id']}",
    ]
    if payload["requested_issue_id"] != payload["issue_id"]:
        lines.append(
            f"  {payload['requested_issue_id']} was merged into "
            f"{payload['issue_id']}; the proposal is recorded against the owner"
        )
    lines.extend(
        [
            f"Proposal: {payload['output_path']} ({payload['byte_count']} bytes)",
            f"sha256: {payload['output_sha256']}",
            f"Drafted by: {payload['harness']}"
            + (
                ""
                if payload["harness_model"] is None
                else f" ({payload['harness_model']})"
            ),
            f"Recorded at: {payload['drafted_at']} (ledger line {payload['seq']})",
        ]
    )
    if payload["evidence_run_ids"]:
        lines.append(f"Evidence read: {', '.join(payload['evidence_run_ids'])}")
    lines.append(
        "This is a proposal for a human to read. Nothing was implemented, "
        "prepared, or committed."
    )
    return lines


def assurance_plan_from_args(args: argparse.Namespace) -> int:
    """Draft one improvement proposal for an issue, and nothing beyond it."""
    # Imported here rather than at module scope for the same reason
    # ``process-pending`` defers its import: drafting launches a harness, and
    # every browsing command in this module must stay able to read a ledger
    # without loading the machinery that can run one.
    from . import assurance_draft as draft

    def refuse(reason: str, error: str) -> int:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_PLAN,
            text_label=TEXT_LABEL_PLAN,
            reason=reason,
            error=error,
            hint=_PLAN_HINTS.get(reason),
        )

    try:
        result = draft.draft_improvement_plan(
            args.issue,
            harness=args.harness,
            output_path=args.output,
            harness_model=args.harness_model,
            effort=args.effort,
            timeout_seconds=(
                draft.DEFAULT_DRAFT_TIMEOUT_SECONDS
                if args.timeout is None
                else args.timeout
            ),
            state_dir=args.state_dir,
        )
    except draft.DraftError as error:
        return refuse(error.reason, str(error))
    except AssuranceError as error:
        return refuse(REASON_INVALID_ISSUE_ID, str(error))
    except ledger.LedgerError as error:
        return refuse(error.reason, str(error))
    except OSError as error:
        return refuse(REASON_LEDGER_READ_FAILED, str(error))

    payload = responses.success_payload(
        COMMAND_ASSURANCE_PLAN,
        requested_issue_id=result.requested_issue_id,
        issue_id=result.issue_id,
        merged=result.merged,
        harness=result.harness,
        harness_model=result.harness_model,
        output_path=str(result.output_path),
        output_sha256=result.output_sha256,
        byte_count=result.byte_count,
        drafted_at=result.drafted_at,
        seq=result.seq,
        evidence_run_ids=list(result.evidence_run_ids),
        read_roots=[str(root) for root in result.read_roots],
        ledger_path=str(ledger.ledger_path(args.state_dir)),
    )
    if args.json:
        responses.print_json(payload)
        return 0
    for line in _plan_text_lines(payload):
        print(line)
    return 0


# ---------------------------------------------------------------------------
# asl assurance reconcile
# ---------------------------------------------------------------------------


_RECONCILE_HINTS: dict[str, str] = {
    reconcile.STALE_CATALOG_DIGEST: (
        "run `asl assurance reconcile --json` again and review the new preview"
    ),
    reconcile.UNRESOLVED_DECISION: (
        "rewrite each 'review' decision as 'merge' or 'keep-distinct'"
    ),
    reconcile.IDENTITY_KEY_CONFLICT: (
        "run `asl assurance show ISSUE` to see which issue already owns the key"
    ),
    REASON_MANIFEST_READ_FAILED: (
        "generate one with `asl assurance reconcile --json | jq .manifest`"
    ),
}


def _read_manifest(args: argparse.Namespace) -> ReconciliationManifest:
    """Load and validate the reviewed manifest named by ``--apply``."""
    path = Path(args.apply).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AssuranceError(f"cannot read manifest {path}: {error}") from error
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise AssuranceError(f"manifest {path} is not valid JSON: {error}") from error
    return parse_reconciliation_manifest(payload)


def _reconcile_preview_text_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Inbox: {payload['issue_count']} canonical issue(s), "
        f"{payload['bound_count']} with an identity key.",
        f"Catalog: {payload['catalog_digest']}",
    ]
    bindings = payload["bindings"]
    if not bindings:
        lines.append("Every canonical issue already has its identity key.")
    else:
        lines.append(f"Safe bindings to add ({len(bindings)}):")
        for item in bindings[:MAX_RENDERED_RECONCILE_ROWS]:
            lines.append(f"  bind {item['identity_key']} -> {item['issue_id']}")
        omitted = len(bindings) - MAX_RENDERED_RECONCILE_ROWS
        if omitted > 0:
            lines.append(f"  ... and {omitted} more")
    for item in payload["blocked"]:
        lines.append(
            f"  blocked {item['issue_id']} ({item['reason']}): {item['detail']}"
        )
    unresolved = payload["unresolved"]
    if not unresolved:
        lines.append("No pairs need review.")
    else:
        lines.append(f"Pairs awaiting a verdict ({len(unresolved)}):")
        for item in unresolved[:MAX_RENDERED_RECONCILE_ROWS]:
            reasons = ", ".join(item["reasons"]) or "none"
            lines.append(
                f"  review {' + '.join(item['issue_ids'])} "
                f"(reasons: {reasons}; similarity {item['similarity']:.2f})"
            )
        omitted = len(unresolved) - MAX_RENDERED_RECONCILE_ROWS
        if omitted > 0:
            lines.append(f"  ... and {omitted} more")
    if payload["truncated_pairs"]:
        lines.append(
            f"{payload['truncated_pairs']} weaker pair(s) were not offered; "
            "reconcile again after applying this manifest to see them."
        )
    if payload["suppressed_pairs"]:
        lines.append(
            f"{payload['suppressed_pairs']} pair(s) were already reviewed as "
            "distinct defects and are not asked about again."
        )
    lines.append("Nothing was written. To act on this:")
    lines.append("  asl assurance reconcile --json | jq .manifest > manifest.json")
    lines.append("  # rewrite each 'review' decision as 'merge' or 'keep-distinct'")
    lines.append("  asl assurance reconcile --apply manifest.json --yes")
    return lines


def _reconcile_apply_text_lines(payload: dict[str, Any]) -> list[str]:
    if not payload["applied"]:
        return [
            "Every decision in this manifest is already recorded; nothing was written."
        ]
    lines = [
        f"Applied {payload['records']} record(s): {payload['bindings']} binding(s), "
        f"{payload['kept_distinct']} ruling(s) and {payload['merges']} merge(s).",
    ]
    if payload["kept_distinct"]:
        lines.append(
            f"{payload['kept_distinct']} pair(s) were reviewed as distinct defects; "
            "they stop being suggested until one of them changes."
        )
    if payload["merges"]:
        lines.append("Merging is irreversible.")
    lines.append(f"Ledger: {payload['ledger_path']}")
    return lines


def _reconcile_apply(args: argparse.Namespace) -> int:
    """Apply one reviewed manifest as a single append, or report why not."""
    try:
        manifest = _read_manifest(args)
    except AssuranceError as error:
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_RECONCILE_APPLY,
            text_label=TEXT_LABEL_RECONCILE,
            reason=REASON_MANIFEST_READ_FAILED,
            error=str(error),
            hint=_RECONCILE_HINTS[REASON_MANIFEST_READ_FAILED],
        )

    merges = manifest.of_kind(DECISION_MERGE)
    if merges and not _confirm(
        args,
        f"Apply {len(manifest.decisions)} decision(s), including {len(merges)} "
        "merge(s)? Merging cannot be undone.",
    ):
        return _print_error(
            args,
            command_name=COMMAND_ASSURANCE_RECONCILE_APPLY,
            text_label=TEXT_LABEL_RECONCILE,
            reason=REASON_CANCELLED,
            error="applying the reconciliation manifest was cancelled",
        )

    try:
        result = reconcile.apply_manifest(manifest, state_dir=args.state_dir)
    except (AssuranceError, OSError) as error:
        return _report_write_failure(
            args,
            error,
            command_name=COMMAND_ASSURANCE_RECONCILE_APPLY,
            text_label=TEXT_LABEL_RECONCILE,
            hints=_RECONCILE_HINTS,
        )

    payload = responses.success_payload(
        COMMAND_ASSURANCE_RECONCILE_APPLY,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        manifest_path=str(Path(args.apply).expanduser()),
        catalog_digest=manifest.catalog_digest,
        **result.to_dict(),
    )
    if args.json:
        responses.print_json(payload)
        return 0
    for line in _reconcile_apply_text_lines(payload):
        print(line)
    return 0


def assurance_reconcile_from_args(args: argparse.Namespace) -> int:
    """Preview the inbox's identity repairs, or apply a reviewed manifest.

    Without ``--apply`` this reads and reports and writes nothing at all, not
    even the manifest it prints: the preview is the input to a human decision,
    and a command that left a file behind would invite applying one nobody read.
    """
    if args.apply is not None:
        return _reconcile_apply(args)

    inbox = _read_inbox(
        args,
        command_name=COMMAND_ASSURANCE_RECONCILE,
        text_label=TEXT_LABEL_RECONCILE,
    )
    if inbox is None:
        return 1

    warnings = _warning_fields(inbox)
    preview = reconcile.build_preview(inbox)
    payload = responses.success_payload(
        COMMAND_ASSURANCE_RECONCILE,
        ledger_path=str(ledger.ledger_path(args.state_dir)),
        **preview.to_dict(),
        **warnings,
    )
    if args.json:
        responses.print_json(payload)
        return 0
    _print_warnings(warnings)
    for line in _reconcile_preview_text_lines(payload):
        print(line)
    return 0
