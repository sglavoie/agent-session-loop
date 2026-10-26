"""Focused implementation extracted from :mod:`.assurance_cli`."""

from __future__ import annotations

import argparse
from typing import Any

from . import assurance_ledger as ledger
from . import responses, state, state_plan_refs
from .assurance_cli_common import (
    COMMAND_ASSURANCE_PLAN,
    COMMAND_ASSURANCE_PROCESS_PENDING,
    REASON_AMBIGUOUS_PLAN,
    REASON_INPUT_ERROR,
    REASON_INVALID_ISSUE_ID,
    REASON_LEDGER_READ_FAILED,
    REASON_PLAN_NOT_FOUND,
    TEXT_LABEL_PLAN,
    TEXT_LABEL_PROCESS_PENDING,
    _print_error,
    _unknown_plan_label,
)
from .assurance_models_primitives import AssuranceError
from .cli_parser_support import POSITIVE_INTEGER, flag_refusal


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
    from .harnesses import HARNESS_NAMES

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
            flag_refusal("--limit", POSITIVE_INTEGER, args.limit),
        )
    if args.audit_timeout is not None and args.audit_timeout < 1:
        return refuse(
            REASON_INPUT_ERROR,
            flag_refusal("--audit-timeout", POSITIVE_INTEGER, args.audit_timeout),
        )
    # Checked here, as `assurance plan` checks --harness: left to the audit
    # role, a typo marked every run unusable after archiving its leftovers.
    if args.audit_harness is not None and args.audit_harness not in HARNESS_NAMES:
        return refuse(
            REASON_INPUT_ERROR,
            f"--audit-harness must be one of {', '.join(HARNESS_NAMES)}; "
            f"got {args.audit_harness!r}",
        )

    plan_dir = None
    if args.plan is not None:
        try:
            plan_dir = state_plan_refs.resolve_plan_reference(
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
        unknown = _unknown_plan_label(args.plan, plan_dir)
        if unknown is not None:
            return refuse(REASON_PLAN_NOT_FOUND, unknown)

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
    except ledger.LedgerError as error:
        # Ahead of its base class, or a ledger failure reads as a bad issue id.
        return refuse(error.reason, str(error))
    except AssuranceError as error:
        return refuse(REASON_INVALID_ISSUE_ID, str(error))
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
