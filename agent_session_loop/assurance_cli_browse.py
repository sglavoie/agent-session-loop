"""Focused implementation extracted from :mod:`.assurance_cli`."""

from __future__ import annotations

import argparse
from typing import Any

from . import assurance_ledger as ledger
from . import responses
from .assurance_cli_common import (
    COMMAND_ASSURANCE_LIST,
    COMMAND_ASSURANCE_SHOW,
    DEFAULT_LIST_LIMIT,
    DEFAULT_LIST_STATE,
    REASON_INPUT_ERROR,
    REASON_INVALID_ISSUE_ID,
    REASON_UNKNOWN_ISSUE,
    TEXT_LABEL_LIST,
    TEXT_LABEL_SHOW,
    _issue_detail,
    _issue_row,
    _list_text_lines,
    _matching_issues,
    _print_error,
    _print_warnings,
    _read_inbox,
    _warning_fields,
)
from .assurance_models_primitives import AssuranceError, parse_issue_id
from .assurance_models_vocab import ISSUE_STATES, SEVERITIES


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
