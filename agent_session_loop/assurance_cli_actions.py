"""Focused implementation extracted from :mod:`.assurance_cli`."""

from __future__ import annotations

import argparse
import math
import sys
from typing import Any

from . import assurance_ledger as ledger
from . import assurance_projection as projection
from . import responses, state, state_plan_refs
from .assurance_cli_common import (
    COMMAND_ASSURANCE_CLOSEOUT,
    COMMAND_ASSURANCE_MERGE,
    DISPOSITION_CONSEQUENCE,
    REASON_AMBIGUOUS_PLAN,
    REASON_AMBIGUOUS_RUN,
    REASON_CANCELLED,
    REASON_INPUT_ERROR,
    REASON_INVALID_ISSUE_ID,
    REASON_LEDGER_WRITE_FAILED,
    REASON_UNKNOWN_ISSUE,
    REASON_UNKNOWN_RUN,
    TEXT_LABEL_CLOSEOUT,
    TEXT_LABEL_MERGE,
    _print_error,
)
from .assurance_models_primitives import AssuranceError, parse_issue_id
from .assurance_models_vocab import CLOSEOUT_OUTCOMES


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


def _resolve_run_location(
    args: argparse.Namespace, run_id: str
) -> tuple[state_plan_refs.RunLocation | None, int | None]:
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
            plan_dir = state_plan_refs.resolve_plan_reference(
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
        located = state_plan_refs.find_run_locations(
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
