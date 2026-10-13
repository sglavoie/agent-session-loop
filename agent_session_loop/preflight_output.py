"""JSON payloads and text rendering for preflight reports."""

from __future__ import annotations

import sys
from dataclasses import asdict
from typing import Any

from . import (
    repository_intent,
    repository_scope,
    workspaces,
)
from .plans import (
    ReadinessMetadata,
)
from .preflight_auto_repair import (
    AutoRepair,
    build_auto_repair_plan,
    build_suggested_pause,
    repair_hint_payload,
)
from .preflight_footprint import _footprint_targets_by_session
from .preflight_models import PreflightFinding, PreflightReport

PREFLIGHT_EXIT_REASON_OK = "ok"
PREFLIGHT_EXIT_REASON_RED = "preflight-red"
PREFLIGHT_EXIT_REASON_YELLOW = "preflight-yellow"
PREFLIGHT_EXIT_REASON_MARKED = "loop-ready-marked"
PREFLIGHT_EXIT_REASON_MARK_REFUSED = "loop-ready-refused"
PREFLIGHT_EXIT_REASON_MARK_FAILED = "loop-ready-write-failed"


def _finding_payload(finding: PreflightFinding) -> dict[str, Any]:
    return asdict(finding)


def _readiness_payload(readiness: ReadinessMetadata | None) -> dict[str, Any] | None:
    if readiness is None:
        return None
    return asdict(readiness)


def _counts_by_severity(report: PreflightReport) -> dict[str, int]:
    return {
        "red": len(report.red_findings),
        "yellow": len(report.yellow_findings),
        "green": len(report.green_findings),
    }


def _primary_error_reason(report: PreflightReport) -> str:
    if report.red_findings:
        return PREFLIGHT_EXIT_REASON_RED
    return PREFLIGHT_EXIT_REASON_YELLOW


def _preflight_payload_fields(
    report: PreflightReport,
    *,
    plan_dir_arg: str,
    cwd: str | None,
    harness: str | None,
    commit_mode: str | None,
    state_dir: str | None,
    max_pending_sessions: int | None,
    mark_loop_ready: bool,
    marked: bool | None = None,
    mark_error: str | None = None,
    auto_repairs: tuple[AutoRepair, ...] = (),
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "plan_dir": plan_dir_arg,
        "plan_dir_resolved": str(report.plan_dir),
        "cwd": cwd,
        "harness": harness,
        "commit_mode": commit_mode,
        "state_dir": state_dir,
        "max_pending_sessions": max_pending_sessions,
        "ok": report.ok,
        "findings": [_finding_payload(finding) for finding in report.findings],
        "findings_by_severity": _counts_by_severity(report),
        "readiness": _readiness_payload(report.readiness),
        "expected_readiness_sha256": report.expected_readiness_sha256,
        "mark_loop_ready_requested": mark_loop_ready,
        "marked_loop_ready": bool(marked) if marked is not None else False,
        "repair_hints": [
            repair_hint_payload(hint, plan_dir=plan_dir_arg, cwd=cwd)
            for hint in report.repair_hints
        ],
        "auto_repairs": [asdict(repair) for repair in auto_repairs],
        "auto_repair_plan": list(
            build_auto_repair_plan(
                report, plan_dir=plan_dir_arg, cwd=cwd, state_dir=state_dir
            )
        ),
        "suggested_pause": build_suggested_pause(
            report, plan_dir=plan_dir_arg, cwd=cwd, state_dir=state_dir
        ),
    }
    if report.repository_registry is not None:
        fields.update(
            {
                "workspace_root": (
                    str(report.verification_context.root)
                    if report.verification_context is not None
                    else cwd
                ),
                "registry_digest": report.repository_registry.digest,
                "repositories": [
                    asdict(repository) for repository in report.repositories
                ],
            }
        )
    footprint_payload = _repository_footprint_payload(report)
    if footprint_payload is not None:
        fields["repository_footprint"] = footprint_payload
    intent_payload = _repository_intent_payload(report)
    if intent_payload is not None:
        fields["repository_intent"] = intent_payload
    if mark_error is not None:
        fields["mark_error"] = mark_error
    return fields


def _repository_footprint_payload(report: PreflightReport) -> dict[str, Any] | None:
    if report.repository_footprint is None:
        return None
    return repository_scope.footprint_payload(
        _footprint_targets_by_session(report.contracts_by_session, report.snapshot),
        report.repository_footprint,
        registry_present=report.repository_registry is not None,
    )


def _repository_intent_payload(report: PreflightReport) -> dict[str, Any] | None:
    """Serialize intent only when it is the reason a plan is refused.

    Every plan has an intent; only a multi-repository one is news, and the
    report is bounded so a wide plan cannot flood the payload.
    """
    if report.repository_intent is None:
        return None
    if not report.repository_intent.registry_required:
        return None
    return repository_intent.intent_report(report.repository_intent)


def _format_finding_text(finding: PreflightFinding) -> str:
    severity_marker = f"[{finding.severity.upper()}]"
    session_label = f" {finding.session_id}" if finding.session_id else ""
    line = f"  {severity_marker} {finding.category}{session_label}: {finding.message}"
    if finding.path:
        location = finding.path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        line += f" ({location})"
    return line


def _print_text_preflight(
    report: PreflightReport,
    *,
    plan_dir_arg: str,
    cwd: str | None,
    harness: str | None,
    commit_mode: str | None,
    max_pending_sessions: int | None,
    marked: bool,
    mark_loop_ready: bool,
    mark_error: str | None,
    auto_repairs: tuple[AutoRepair, ...] = (),
) -> None:
    counts = _counts_by_severity(report)
    session_count = len(report.snapshot.sessions) if report.snapshot is not None else 0
    print(f"Plan: {plan_dir_arg}")
    print(f"Sessions: {session_count}")
    if report.repository_registry is not None:
        if cwd is not None:
            print(f"Workspace root: {cwd}")
        print("Repositories:")
        visible, omitted = workspaces.bounded_repository_rows(report.repositories)
        for repository in visible:
            resolved = (
                f" -> {repository.resolved_root}"
                if repository.resolved_root is not None
                else ""
            )
            print(
                f"  {repository.alias}: {repository.path}{resolved} "
                f"({repository.origin})"
            )
        if omitted:
            print(f"  ... {omitted} more repositories")
    elif cwd is not None:
        print(f"Repo root: {cwd}")
    if harness is not None:
        print(f"Harness: {harness}")
    if commit_mode is not None:
        print(f"Commit mode: {commit_mode}")
    if max_pending_sessions is not None:
        print(f"Max pending sessions: {max_pending_sessions}")
    print(
        f"Findings: {len(report.findings)} "
        f"(red {counts['red']}, yellow {counts['yellow']}, green {counts['green']})"
    )
    for finding in report.findings:
        print(_format_finding_text(finding))
    for repair in auto_repairs:
        print(_format_auto_repair_text(repair))
    if report.ok:
        print("Preflight: ok")
    else:
        print(
            f"Preflight: failed ({counts['red']} red, {counts['yellow']} yellow)",
            file=sys.stderr,
        )
    if mark_loop_ready:
        if marked:
            print("Loop-ready: marked")
        elif mark_error is not None:
            print(f"Loop-ready: not marked ({mark_error})", file=sys.stderr)
        else:
            print(
                "Loop-ready: refused (preflight not ok)",
                file=sys.stderr,
            )


def _format_auto_repair_text(repair: AutoRepair) -> str:
    if repair.category == "progress-journal-missing":
        noun = "entry" if repair.entries_written == 1 else "entries"
        return (
            f"Auto-repair: rebuilt progress journal "
            f"({repair.entries_written} {noun} written)"
        )
    return f"Auto-repair: {repair.action} ({repair.category})"
