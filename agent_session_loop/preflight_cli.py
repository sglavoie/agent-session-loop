"""The ``asl preflight`` command: exit reasons, loop-ready marking, argv."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from . import (
    progress_repair,
    responses,
)
from .cli_defaults import program_name
from .literals import as_optional_literal
from .plans_errors import PlanError
from .plans_models import ReadinessMetadata
from .plans_readiness import write_readiness_metadata
from .preflight import run_preflight
from .preflight_auto_repair import AutoRepair
from .preflight_models import (
    COMMIT_MODES,
    NON_BYPASSABLE_PREFLIGHT_CATEGORIES,
    PreflightFinding,
    PreflightReport,
)
from .preflight_output import (
    PREFLIGHT_EXIT_REASON_MARK_FAILED,
    PREFLIGHT_EXIT_REASON_MARK_REFUSED,
    PREFLIGHT_EXIT_REASON_MARKED,
    PREFLIGHT_EXIT_REASON_OK,
    _preflight_payload_fields,
    _primary_error_reason,
    _print_text_preflight,
)


def _mark_loop_ready(
    report: PreflightReport,
) -> tuple[bool, str | None]:
    non_bypassable = sorted(
        {
            finding.category
            for finding in report.findings
            if finding.severity == "red"
            and finding.category in NON_BYPASSABLE_PREFLIGHT_CATEGORIES
        }
    )
    if non_bypassable:
        return False, (
            "preflight raised a non-bypassable finding "
            f"({', '.join(non_bypassable)}); readiness cannot be marked until "
            "it is resolved"
        )
    if report.expected_readiness_sha256 is None:
        return False, "no readiness sha-256 was computed; cannot mark loop-ready"
    metadata = ReadinessMetadata(
        state="loop-ready",
        readiness_sha256=report.expected_readiness_sha256,
        checked_at=datetime.now(UTC).isoformat(),
    )
    try:
        write_readiness_metadata(report.plan_dir, metadata)
    except PlanError as error:
        return False, str(error)
    return True, None


def _mark_loop_ready_blockers(
    report: PreflightReport,
) -> tuple[PreflightFinding, ...]:
    """Return findings that must be fixed before readiness can be marked.

    Manifest-readiness findings are precisely what ``--mark-loop-ready`` repairs:
    missing metadata, stale SHA values, and draft/reviewed states. All other red
    or yellow findings still block marking.
    """
    return tuple(
        finding
        for finding in report.findings
        if finding.severity in {"red", "yellow"}
        and finding.category != "manifest-readiness"
    )


def _mark_loop_ready_refusal_message(
    blockers: tuple[PreflightFinding, ...],
) -> str:
    red = sum(1 for finding in blockers if finding.severity == "red")
    yellow = sum(1 for finding in blockers if finding.severity == "yellow")
    categories = ", ".join(sorted({finding.category for finding in blockers}))
    return (
        f"preflight still has {red} red and {yellow} yellow "
        f"non-readiness finding(s): {categories}"
    )


def preflight_from_args(args: argparse.Namespace) -> int:
    """Run the ``preflight`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    cwd_arg: str | None = args.cwd
    harness_arg: str | None = args.harness
    # The two flags feeding `commit_mode` are `store_const`, so the CLI can
    # only ever supply a permitted value; narrowing here is what makes that
    # true for a fabricated namespace too, and keeps an unrecognized mode from
    # quietly skipping the git checks that `commit_mode in COMMIT_MODES` gates.
    commit_mode_arg = as_optional_literal(
        args.commit_mode, COMMIT_MODES, "--commit-per-session/--commit-at-end"
    )
    state_dir_arg: str | None = args.state_dir
    max_pending_sessions_arg: int | None = getattr(
        args,
        "max_pending_sessions",
        None,
    )
    want_json: bool = args.json
    mark_loop_ready: bool = args.mark_loop_ready

    if max_pending_sessions_arg is not None and max_pending_sessions_arg <= 0:
        message = "--max-pending-sessions must be a positive integer"
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "preflight",
                    reason="input-error",
                    error=message,
                    paused=False,
                    plan_dir=plan_dir_arg,
                    cwd=cwd_arg,
                    harness=harness_arg,
                    commit_mode=commit_mode_arg,
                    state_dir=state_dir_arg,
                    max_pending_sessions=max_pending_sessions_arg,
                )
            )
        else:
            print(f"{program_name()} preflight: {message}", file=sys.stderr)
        return 1

    try:
        report = run_preflight(
            plan_dir_arg,
            cwd=cwd_arg,
            harness=harness_arg,
            commit_mode=commit_mode_arg,
            state_dir=state_dir_arg,
            max_pending_sessions=max_pending_sessions_arg,
        )
    except PlanError as error:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "preflight",
                    reason="plan-structure",
                    error=str(error),
                    paused=False,
                    plan_dir=plan_dir_arg,
                    cwd=cwd_arg,
                    harness=harness_arg,
                    commit_mode=commit_mode_arg,
                    state_dir=state_dir_arg,
                )
            )
        else:
            print(
                f"{program_name()} preflight: {error}",
                file=sys.stderr,
            )
        return 1

    marked = False
    mark_error: str | None = None
    mark_write_failed = False
    auto_repairs: list[AutoRepair] = []
    if mark_loop_ready:
        blockers = _mark_loop_ready_blockers(report)
        if blockers and all(
            blocker.category == "progress-journal-missing" for blocker in blockers
        ):
            try:
                repair_result = progress_repair.repair_progress_journal(report.plan_dir)
            except PlanError, OSError:
                # Leave blockers in place; the refusal path below will surface
                # them to the caller. Auto-repair is best-effort.
                pass
            else:
                auto_repairs.append(
                    AutoRepair(
                        action=repair_result.action,
                        category="progress-journal-missing",
                        path=str(repair_result.path),
                        entries_written=repair_result.entries_written,
                        completed_session_ids=repair_result.completed_session_ids,
                        journal_replaced=repair_result.journal_replaced,
                    )
                )
                try:
                    report = run_preflight(
                        plan_dir_arg,
                        cwd=cwd_arg,
                        harness=harness_arg,
                        commit_mode=commit_mode_arg,
                        state_dir=state_dir_arg,
                        max_pending_sessions=max_pending_sessions_arg,
                    )
                except PlanError as error:
                    mark_error = str(error)
                    mark_write_failed = True
                else:
                    blockers = _mark_loop_ready_blockers(report)
        if not mark_write_failed:
            if blockers:
                mark_error = _mark_loop_ready_refusal_message(blockers)
            else:
                marked, mark_error = _mark_loop_ready(report)
                mark_write_failed = mark_error is not None
                if marked:
                    try:
                        report = run_preflight(
                            plan_dir_arg,
                            cwd=cwd_arg,
                            harness=harness_arg,
                            commit_mode=commit_mode_arg,
                            state_dir=state_dir_arg,
                            max_pending_sessions=max_pending_sessions_arg,
                        )
                    except PlanError as error:
                        mark_error = str(error)
                        mark_write_failed = True

    payload_fields = _preflight_payload_fields(
        report,
        plan_dir_arg=str(plan_dir_arg),
        cwd=cwd_arg,
        harness=harness_arg,
        commit_mode=commit_mode_arg,
        state_dir=state_dir_arg,
        max_pending_sessions=max_pending_sessions_arg,
        mark_loop_ready=mark_loop_ready,
        marked=marked,
        mark_error=mark_error,
        auto_repairs=tuple(auto_repairs),
    )

    if want_json:
        if report.ok:
            if mark_loop_ready and not marked:
                reason = (
                    PREFLIGHT_EXIT_REASON_MARK_FAILED
                    if mark_error is not None
                    else PREFLIGHT_EXIT_REASON_MARK_REFUSED
                )
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=reason,
                        error=mark_error or "loop-ready marking did not complete",
                        paused=False,
                        **payload_fields,
                    )
                )
            else:
                reason = (
                    PREFLIGHT_EXIT_REASON_MARKED if marked else PREFLIGHT_EXIT_REASON_OK
                )
                responses.print_json(
                    responses.success_payload(
                        "preflight",
                        reason=reason,
                        **payload_fields,
                    )
                )
        else:
            if mark_loop_ready and mark_write_failed:
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=PREFLIGHT_EXIT_REASON_MARK_FAILED,
                        error=mark_error or "loop-ready marking did not complete",
                        paused=False,
                        **payload_fields,
                    )
                )
            else:
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=_primary_error_reason(report),
                        error=(
                            f"{len(report.red_findings)} red, "
                            f"{len(report.yellow_findings)} yellow finding(s)"
                        ),
                        paused=False,
                        **payload_fields,
                    )
                )
    else:
        _print_text_preflight(
            report,
            plan_dir_arg=str(plan_dir_arg),
            cwd=cwd_arg,
            harness=harness_arg,
            commit_mode=commit_mode_arg,
            max_pending_sessions=max_pending_sessions_arg,
            marked=marked,
            mark_loop_ready=mark_loop_ready,
            mark_error=mark_error,
            auto_repairs=tuple(auto_repairs),
        )

    if not report.ok:
        return 1
    if mark_loop_ready and not marked:
        return 1
    return 0
