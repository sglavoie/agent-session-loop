"""Parser definitions for the storage, maintenance and state commands."""

from __future__ import annotations

import argparse

from .cli_defaults import (
    _MAINTENANCE_DEFAULT_HOUR,
    _MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS,
    _MAINTENANCE_DEFAULT_MINUTE,
    _METRICS_DEFAULT_SINCE_DAYS,
    _RETENTION_DEFAULT_MAX_AGE_DAYS,
)
from .cli_parser_support import _AgentSessionLoopArgumentParser, _group_epilogue


def _add_storage_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """On-demand storage reclamation: compact-state and gc."""
    compact_state_parser = subparsers.add_parser(
        "compact-state",
        help=(
            "Reclaim duplicated operational JSON by compacting v1 state, "
            "events, and result records to schema v2 (default: dry-run)."
        ),
    )
    compact_state_parser.add_argument(
        "plan_dir", help="Split plan directory whose state to compact"
    )
    compact_state_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    compact_state_parser.add_argument(
        "--apply",
        action="store_true",
        help="Rewrite records on disk (default: dry-run, no writes)",
    )
    compact_state_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    compact_state_parser.set_defaults(
        handler_ref="compact_state:compact_state_from_args"
    )

    gc_parser = subparsers.add_parser(
        "gc",
        help=(
            "Reclaim persisted run directories older than an age cutoff, for "
            "one plan or the whole state root (default: dry-run)."
        ),
    )
    gc_parser.add_argument(
        "plan_dir",
        nargs="?",
        help="Split plan directory to sweep (default: every plan in the state root)",
    )
    gc_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    gc_parser.add_argument(
        "--max-age-days",
        type=float,
        default=_RETENTION_DEFAULT_MAX_AGE_DAYS,
        metavar="DAYS",
        help=(
            "Reclaim runs that finished at least this many days ago "
            f"(default: {_RETENTION_DEFAULT_MAX_AGE_DAYS:g})"
        ),
    )
    gc_mode = gc_parser.add_mutually_exclusive_group()
    gc_mode.add_argument(
        "--apply",
        action="store_true",
        help="Delete the reclaimable run directories (default: dry-run, no writes)",
    )
    gc_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Project the reclaim without deleting anything (the default)",
    )
    gc_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    gc_parser.set_defaults(handler_ref="retention:gc_from_args")


def _add_maintenance_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl maintenance` group."""
    maintenance_parser = subparsers.add_parser(
        "maintenance",
        help=(
            "Own the scheduled storage maintenance job that keeps the state "
            "corpus inside its budget without touching protected evidence."
        ),
        epilog=_group_epilogue("maintenance", "asl maintenance run --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    maintenance_sub = maintenance_parser.add_subparsers(
        dest="maintenance_command", required=True
    )

    maintenance_install_parser = maintenance_sub.add_parser(
        "install",
        help="Install and load the daily ASL-owned maintenance job.",
        description=(
            "Write the launchd job definition ASL owns and bootstrap it into "
            "this user's GUI domain. The job runs `asl maintenance run --apply` "
            "once a day under a bounded runtime; it does nothing unless the "
            "corpus is over budget or the volume is low on free space. "
            "Installing never starts a pass."
        ),
    )
    maintenance_install_parser.add_argument(
        "--executable",
        metavar="PATH",
        help=(
            "Absolute path to the asl executable launchd should run "
            "(default: resolved from PATH at install time)"
        ),
    )
    maintenance_install_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_install_parser.add_argument(
        "--hour",
        type=int,
        default=_MAINTENANCE_DEFAULT_HOUR,
        metavar="HOUR",
        help=f"Local hour to run at (default: {_MAINTENANCE_DEFAULT_HOUR})",
    )
    maintenance_install_parser.add_argument(
        "--minute",
        type=int,
        default=_MAINTENANCE_DEFAULT_MINUTE,
        metavar="MINUTE",
        help=f"Local minute to run at (default: {_MAINTENANCE_DEFAULT_MINUTE})",
    )
    maintenance_install_parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS,
        metavar="SECONDS",
        help=(
            "Wall-clock bound on each scheduled pass "
            f"(default: {_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS:g})"
        ),
    )
    maintenance_install_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Write the job definition without loading it into launchd",
    )
    maintenance_install_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_install_parser.set_defaults(handler_ref="maintenance:install_from_args")

    maintenance_run_parser = maintenance_sub.add_parser(
        "run",
        help="Run one maintenance pass over the state corpus (default: dry-run).",
        description=(
            "Measure the corpus against its budget and the volume's free space, "
            "and when either threshold is crossed reclaim what retention policy "
            "permits: expired run directories, then compressed and budgeted log "
            "bodies of terminal newest runs older than the compaction age. Live, "
            "locked, unfinished, pinned, and recent evidence is never touched, "
            "and liveness is re-read under the maintenance lock immediately "
            "before any mutation."
        ),
    )
    maintenance_run_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_run_mode = maintenance_run_parser.add_mutually_exclusive_group()
    maintenance_run_mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform the planned maintenance (default: dry-run, no writes)",
    )
    maintenance_run_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Project the maintenance without mutating anything (the default)",
    )
    maintenance_run_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Plan and apply even when both thresholds are satisfied; policy is "
            "unchanged, so protected evidence is still never removed"
        ),
    )
    maintenance_run_parser.add_argument(
        "--max-age-days",
        type=float,
        default=_RETENTION_DEFAULT_MAX_AGE_DAYS,
        metavar="DAYS",
        help=(
            "Reclaim runs that finished at least this many days ago "
            f"(default: {_RETENTION_DEFAULT_MAX_AGE_DAYS:g})"
        ),
    )
    maintenance_run_parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS,
        metavar="SECONDS",
        help=(
            "Stop cleanly after this much wall clock and report the remainder "
            f"(default: {_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS:g})"
        ),
    )
    maintenance_run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_run_parser.set_defaults(handler_ref="maintenance:run_from_args")

    maintenance_status_parser = maintenance_sub.add_parser(
        "status",
        help="Report job presence, last run, reclaimed bytes, and failures.",
        description=(
            "Verify the installed job: whether its definition is present and "
            "current, whether launchd has it loaded, and what the last recorded "
            "pass reclaimed or failed to reclaim. Exits non-zero only when the "
            "last recorded pass itself failed."
        ),
    )
    maintenance_status_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_status_parser.add_argument(
        "--executable",
        metavar="PATH",
        help="Executable to compare the installed definition against",
    )
    maintenance_status_parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the launchctl query and report the definition only",
    )
    maintenance_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_status_parser.set_defaults(handler_ref="maintenance:status_from_args")

    maintenance_uninstall_parser = maintenance_sub.add_parser(
        "uninstall",
        help="Unload and remove the maintenance job, keeping every artifact.",
        description=(
            "Boot the job out of launchd and delete its definition. Nothing in "
            "the state corpus is removed: the maintenance manifest and job log "
            "outlive the decision to stop maintaining automatically."
        ),
    )
    maintenance_uninstall_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_uninstall_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Remove the job definition without asking launchd to unload it",
    )
    maintenance_uninstall_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_uninstall_parser.set_defaults(
        handler_ref="maintenance:uninstall_from_args"
    )


def _add_state_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """State-corpus commands: migrate-state and metrics."""
    migrate_state_parser = subparsers.add_parser(
        "migrate-state",
        help=(
            "Relocate an orphaned state directory after a plan directory move, "
            "or upgrade a plan's state schema in place (default: dry-run)."
        ),
    )
    migrate_state_parser.add_argument(
        "plan_dir", help="Current (new) plan directory whose state to restore"
    )
    migrate_state_parser.add_argument(
        "--from",
        dest="from_plan_dir",
        help=(
            "Old plan directory path the state was keyed under; omit to upgrade "
            "this plan's own state to the current schema version instead"
        ),
    )
    migrate_state_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    migrate_state_parser.add_argument(
        "--apply",
        action="store_true",
        help="Rename and rewrite state on disk (default: dry-run, no writes)",
    )
    migrate_state_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    migrate_state_parser.set_defaults(
        handler_ref="state_cli_handlers:migrate_state_from_args"
    )

    metrics_parser = subparsers.add_parser(
        "metrics",
        help=(
            "Report workflow reliability, execution timing, and storage "
            "metrics from the persisted state corpus "
            "(default window: last 7 days)."
        ),
        description=(
            "Report workflow reliability, execution timing, and storage "
            "metrics from the persisted state corpus (default window: last 7 "
            "days). The execution-timing section reports where the runs that "
            "finished inside the window spent their wall clock — "
            "implementation, generated review repairs, unclassified worker "
            "turns, review, verification, commits, assurance, and the "
            "unattributed residual — alongside the coverage that qualifies "
            "those numbers. Measured seconds cover every included run; shares "
            "cover only runs whose evidence is complete, and are reported as "
            "unavailable rather than as zeroes when no run qualifies. "
            "Cumulative verification check work is reported beside, never "
            "inside, the verification phase, because concurrent checks "
            "overlap. Repeated attempts count turns that followed an earlier "
            "attempt of the same session; that time is not all discarded "
            "work. Text output ranks the 10 longest plans and check scopes; "
            "--json carries every plan and scope."
        ),
    )
    metrics_parser.add_argument(
        "--since-days",
        type=int,
        default=_METRICS_DEFAULT_SINCE_DAYS,
        help=(
            "Number of days of history to aggregate "
            f"(default: {_METRICS_DEFAULT_SINCE_DAYS})"
        ),
    )
    metrics_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    metrics_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    metrics_parser.set_defaults(handler_ref="metrics:metrics_from_args")
