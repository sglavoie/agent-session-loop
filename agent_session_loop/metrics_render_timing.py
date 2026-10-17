"""Format metric values and render execution-timing report sections."""

from __future__ import annotations

from typing import Any

from . import (
    metrics_models,
    metrics_render,
)


def _format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(count)} B"


def _format_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _format_hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f} h"


def _print_execution_timing_section(section: dict[str, Any]) -> None:
    """Print where run time went, with the coverage that qualifies it.

    Coverage is printed before any share so a share is never read as a census,
    the cumulative check figure is labelled as work rather than elapsed time,
    and the ranked lists say how much of the whole they show.
    """
    print("\nExecution timing:")
    if not section:
        print("  not collected")
        return
    coverage = section["coverage"]
    print(
        f"  runs {coverage['runs_included']} in window "
        f"of {coverage['runs_with_evidence']} with evidence "
        f"across {coverage['plans_with_evidence']} plan(s)"
    )
    if coverage["runs_included"] == 0:
        print("  no run finished inside the window; nothing is measured")
        return
    included = coverage["included_runs_by_classification"]
    print(
        "  included by trust: "
        + ", ".join(f"{name} {included.get(name, 0)}" for name in included)
    )
    if coverage["exclusions"]:
        print(f"  excluded: {metrics_render._format_counts(coverage['exclusions'])}")
    if coverage["unattributable_records"] or coverage["unattributable_turn_results"]:
        print(
            f"  unattributable evidence: "
            f"{coverage['unattributable_records']} event record(s), "
            f"{coverage['unattributable_turn_results']} turn result(s)"
        )
    phases = section["phases"]
    print(
        f"  wall clock {_format_hours(phases['wall_seconds'])} known for "
        f"{coverage['runs_with_known_wall']} of "
        f"{coverage['runs_included']} included run(s)"
    )
    print("  measured seconds by phase (every included run, partial ones too):")
    for phase in metrics_models.TIMING_PHASES:
        print(
            f"    {phase} {_format_hours(phases['seconds'][phase])} "
            f"({phases['runs_with_phase'][phase]} run(s))"
        )
    shares = phases["shares"]
    if shares is None:
        print(
            f"  shares: n/a - no complete run with a known duration "
            f"({coverage['runs_outside_share_basis']} included run(s) "
            f"carry a gap or an anomaly)"
        )
    else:
        print(
            f"  shares over {phases['share_basis_runs']} complete run(s) "
            f"({_format_hours(phases['share_basis_seconds'])}), "
            f"{coverage['runs_outside_share_basis']} included run(s) excluded "
            f"from that basis:"
        )
        print(
            "    "
            + ", ".join(
                f"{phase} {shares[phase] * 100:.1f}%"
                for phase in metrics_models.TIMING_PHASES
            )
        )
    checks = section["verification_checks"]
    print(
        f"  verification checks {checks['checks']} across "
        f"{checks['runs_with_checks']} run(s): cumulative check work "
        f"{_format_hours(checks['cumulative_seconds'])} "
        f"(concurrent checks overlap, so this is work done and not elapsed "
        f"run time) beside verification phase "
        f"{_format_hours(checks['phase_seconds'])}"
    )
    repeated = section["repeated_attempts"]
    print(
        f"  repeated attempts {repeated['additional_attempts']} after the first "
        f"in {repeated['sessions_with_additional_attempts']} session(s), "
        f"{_format_hours(repeated['additional_attempt_seconds'])} "
        f"(time spent re-attempting; some of it produced the work that landed)"
    )
    if repeated["additional_attempts_without_duration"]:
        print(
            f"    {repeated['additional_attempts_without_duration']} of those "
            f"recorded no duration"
        )
    if repeated["additional_attempts_by_phase"]:
        print(
            "    by phase: "
            + metrics_render._format_counts(repeated["additional_attempts_by_phase"])
        )
    review_scope = section["review_scope"]
    if review_scope["selections"]:
        print(
            f"  review scope selections {review_scope['selections']} "
            f"(refreshed {review_scope['refreshed_selections']})"
            + (
                f": {metrics_render._format_counts(review_scope['by_scope'])}"
                if review_scope["by_scope"]
                else ""
            )
        )
        if review_scope["fallback_reasons"]:
            print(
                "    fallback reasons: "
                + metrics_render._format_counts(review_scope["fallback_reasons"])
            )
        else:
            print("    fallback reasons: none recorded")
    plan_rows = section["plans"]
    if plan_rows:
        shown = plan_rows[: metrics_models.TIMING_PLAN_LIMIT]
        print(f"  top plans by duration ({len(shown)} of {len(plan_rows)}):")
        for row in shown:
            print(
                f"    {_format_hours(row['wall_seconds'])} "
                f"{row['plan_dir'] or row['plan_key']} "
                f"(runs {row['runs']}, known wall {row['runs_with_known_wall']}, "
                f"repeats {row['additional_attempts']})"
            )
    scopes = checks["scopes"]
    if scopes:
        shown_checks = scopes[: metrics_models.TIMING_CHECK_LIMIT]
        print(
            f"  top checks by cumulative work ({len(shown_checks)} of {len(scopes)}):"
        )
        for row in shown_checks:
            where = (
                row["repository"]
                or row["working_directory"]
                or row["plan_key"]
                or metrics_models.UNKNOWN_LABEL
            )
            print(
                f"    {_format_hours(row['seconds'])} "
                f"{row['checks']} execution(s) of {row['command']} [{where}]"
            )
        if checks["checks_without_command"]:
            print(
                f"    {checks['checks_without_command']} of "
                f"{checks['scoped_checks']} scoped check(s) recorded no command"
            )
