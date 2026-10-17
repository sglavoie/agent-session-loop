"""Render operational, assurance, and CLI invocation metrics as text."""

from __future__ import annotations

from typing import Any

from . import (
    metrics_models,
)
from . import (
    metrics_render_timing as timing_render,
)


def _print_assurance_section(assurance: dict[str, Any]) -> None:
    """Print the supervisor-audit section, separating coverage from correctness."""
    print("\nSupervisor audit:")
    if not assurance:
        print("  not collected")
        return
    if not assurance.get("available"):
        print(f"  ledger unreadable: {assurance.get('error')}")
        return
    coverage = assurance["coverage"]
    print(
        f"  runs recorded {assurance['runs_recorded']} "
        f"(eligible {coverage['eligible_runs']}, "
        f"ineligible {coverage['ineligible_runs']}), "
        f"audited {coverage['audited_runs']} "
        f"({timing_render._format_rate(coverage['coverage_rate'])})"
    )
    states = assurance["audit_states"]
    print("  states " + ", ".join(f"{name} {states.get(name, 0)}" for name in states))
    issues = assurance["issues"]
    print(
        f"  issues: {issues['observations']} sighting(s), "
        f"{issues['new']} new, {issues['recurring']} recurring, "
        f"{issues['open_total']} open now"
    )
    if issues["by_severity"]:
        print(
            "  by severity: "
            + ", ".join(
                f"{name} {count}" for name, count in issues["by_severity"].items()
            )
        )
    if issues["by_category"]:
        print(
            "  by category: "
            + ", ".join(
                f"{name} {count}" for name, count in issues["by_category"].items()
            )
        )
    print(
        f"  recurrence after resolution: "
        f"{issues['recurrence_after_resolution']} sighting(s) across "
        f"{issues['issues_recurring_after_resolution']} issue(s)"
    )
    recurring_categories = issues["recurrence_after_resolution_by_category"]
    if recurring_categories:
        print(
            "  recurrence by category: "
            + ", ".join(
                f"{name} {count}" for name, count in recurring_categories.items()
            )
        )
    dispositions = assurance["dispositions"]
    latency = dispositions["time_to_disposition"]
    median = latency["median_hours"]
    dismissal_rate = timing_render._format_rate(dispositions["dismissal_rate"])
    accepted_risk_rate = timing_render._format_rate(dispositions["accepted_risk_rate"])
    print(
        f"  dispositions {dispositions['total']} "
        f"(dismissed {dismissal_rate}, "
        f"accepted risk {accepted_risk_rate}), "
        f"median time to disposition "
        f"{'n/a' if median is None else f'{median:.1f} h'}"
    )
    closeouts = assurance["closeouts"]
    outcomes = ", ".join(
        f"{name} {count}" for name, count in closeouts["by_outcome"].items()
    )
    print(
        f"  closeouts {closeouts['total']}"
        f"{f' ({outcomes})' if outcomes else ''}, "
        f"escaped issues {closeouts['escaped_issues']}, "
        f"rework {closeouts['rework_minutes']:.0f} min"
    )
    integrity = assurance["integrity"]
    if integrity["anomalies"] or integrity["rejected_records"]:
        print(
            f"  ledger integrity: {integrity['anomalies']} anomaly(ies), "
            f"{integrity['rejected_records']} rejected record(s)"
        )


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def _format_duration_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def _print_cli_invocations_section(section: dict[str, Any]) -> None:
    """Print which command surfaces ran and which of them failed."""
    print("\nCLI invocations:")
    if not section:
        print("  not collected")
        return
    print(
        f"  trace root {section['trace_root']} "
        f"(retention {section['retention_days']} day(s), "
        f"{section['records_read']} record(s) read, "
        f"{section['records_ignored']} ignored)"
    )
    print(
        f"  starts {section['starts']}, completions {section['completions']}, "
        f"unmatched starts {section['unmatched_starts']}"
    )
    print(
        f"  successes {section['successes']}, "
        f"nonzero exits {section['nonzero_exits']}, "
        f"usage errors {section['usage_errors']}, "
        f"help {section['help_calls']}, "
        f"interrupted {section['interruptions']}, "
        f"exceptions {section['exceptions']}"
    )
    durations = section["durations_ms"]
    print(
        f"  duration: median {_format_duration_ms(durations['median'])}, "
        f"p95 {_format_duration_ms(durations['p95'])}, "
        f"max {_format_duration_ms(durations['max'])}"
    )
    for label, counts in (
        ("by command", section["by_command"]),
        ("by help depth", section["by_help_depth"]),
        ("by exit code", section["by_exit_code"]),
        ("by harness", section["by_harness"]),
        ("by origin", section["by_origin"]),
    ):
        if counts:
            print(f"  {label}: {_format_counts(counts)}")
    failures = section["recent_failures"]
    if not failures:
        return
    print(f"  recent failures ({len(failures)}):")
    for entry in failures:
        exception = entry["exception_type"]
        print(
            f"    {entry['timestamp']} {entry['command']} "
            f"{entry['outcome']} exit {entry['exit_code']} "
            f"[{entry['origin']}]"
            f"{f' {exception}' if exception else ''}"
        )


def _print_report(report: metrics_models.MetricsReport) -> None:
    print(f"Metrics for {report.state_root}")
    print(f"Window: last {report.since_days} day(s), since {report.window_start}")
    print(
        f"State dirs: {report.state_dirs_scanned} scanned, "
        f"{report.state_dirs_active} active in window"
    )
    turns = report.turns
    print("\nTurns:")
    print(
        f"  total {turns['total']}, successful {turns['successful']}, "
        f"unsuccessful {turns['unsuccessful']}, interrupted {turns['interrupted']}"
    )
    print(
        f"  unsuccessful runtime "
        f"{turns['unsuccessful_runtime_seconds'] / 3600:.2f} h "
        f"(successful {turns['successful_runtime_seconds'] / 3600:.2f} h)"
    )
    timing_render._print_execution_timing_section(report.execution_timing)
    first_pass = report.first_pass
    print("\nFirst-pass completion (implementation sessions):")
    print(
        f"  attempted {first_pass['sessions_attempted']}, "
        f"completed {first_pass['sessions_completed']}, "
        f"first-pass {first_pass['sessions_completed_first_pass']} "
        f"({timing_render._format_rate(first_pass['first_pass_rate'])})"
    )
    pauses = report.pauses
    print("\nPauses:")
    print(
        f"  total {pauses['total']} "
        f"(never-retry {pauses['classified_never_retry']}, "
        f"retryable {pauses['classified_retryable']})"
    )
    for reason, count in pauses["by_reason"].items():
        print(f"  {reason}: {count}")
    repair = report.verification_repair
    print("\nVerification repair:")
    print(
        f"  verify-failed pauses {repair['verify_failed_pauses']} "
        f"(exhausted {repair['repair_exhausted_pauses']}), "
        f"sessions {repair['sessions_with_verify_failures']}, "
        f"repaired {repair['sessions_repaired']} "
        f"({timing_render._format_rate(repair['repair_success_rate'])})"
    )
    paused_work = repair["paused_work_commits_by_result"]
    print(
        f"  paused-work settlements {repair['paused_work_commits']}"
        + (
            " ("
            + ", ".join(f"{result} {count}" for result, count in paused_work.items())
            + ")"
            if paused_work
            else ""
        )
    )
    cycles = report.review_cycles
    print("\nReview cycles:")
    print(
        f"  recorded {cycles['cycles_recorded']}, "
        f"unique materializations {cycles['unique_materializations']}"
    )
    print(
        f"  reopen attempts {cycles['reopen_attempts']}, "
        f"successful materializations "
        f"{cycles['successful_reopen_materializations']}, "
        f"failures {cycles['reopen_failures']}"
    )
    print(
        f"  findings introduced {cycles['findings_introduced']}, "
        f"closed {cycles['findings_closed']}, "
        f"repeated {cycles['findings_repeated']}"
    )
    for disposition, count in cycles["by_disposition"].items():
        print(f"  {disposition}: {count}")
    for disposition, count in cycles["reopen_attempts_by_source_disposition"].items():
        print(f"  reopen source {disposition}: {count}")
    for result, count in cycles["reopen_failures_by_result"].items():
        print(f"  reopen failure {result}: {count}")
    delivery = report.agent_mux_delivery
    print("\nAgentMux delivery:")
    print(
        f"  total {delivery['total']}, acknowledged {delivery['acknowledged']}, "
        f"failed {delivery['failed']}, "
        f"legacy unacknowledged {delivery['legacy_unacknowledged']}"
    )
    for status, count in delivery["by_status"].items():
        print(f"  {status}: {count}")
    artifacts_section = report.artifacts
    grown_bytes = timing_render._format_bytes(artifacts_section["grown_bytes"])
    print("\nArtifacts:")
    print(
        f"  corpus {timing_render._format_bytes(artifacts_section['total_bytes'])}, "
        f"grown in window {grown_bytes} "
        f"({artifacts_section['grown_files']} files)"
    )
    _print_assurance_section(report.assurance)
    compaction_section = report.compaction
    projected_bytes = timing_render._format_bytes(
        compaction_section["projected_bytes_reclaimed"]
    )
    print("\nCompaction (dry-run over active dirs):")
    print(
        f"  projected reclaim "
        f"{projected_bytes} "
        f"across {compaction_section['dirs_projected']} dir(s)"
    )
    if compaction_section["dirs_skipped"]:
        print(f"  skipped: {len(compaction_section['dirs_skipped'])} dir(s)")
    _print_cli_invocations_section(report.cli_invocations)
