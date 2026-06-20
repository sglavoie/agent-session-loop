"""Command-line interface for agent-session-loop."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import (
    harnesses,
    preflight,
    progress_cli,
    runner,
    session_overview,
    state,
    tail,
    verification,
)


def _add_unsafe_bypass_flags(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(unsafe_bypass=True)
    parser.add_argument(
        "--unsafe-bypass",
        dest="unsafe_bypass",
        action="store_true",
        help="Use harness-specific bypass flags for sandbox and approval prompts (default)",
    )
    parser.add_argument(
        "--no-unsafe-bypass",
        dest="unsafe_bypass",
        action="store_false",
        help="Use the harness default sandbox and approval behavior",
    )


def build_parser(prog: str = "agent-session-loop") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run split agent session plans one session at a time.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run pending session IDs from a split plan directory.",
    )
    run_parser.add_argument("plan_dir", help="Split plan directory to execute")
    run_parser.add_argument(
        "--cwd",
        default=".",
        help="Repository root where the harness should run (default: current directory)",
    )
    run_parser.add_argument(
        "--harness",
        choices=harnesses.HARNESS_NAMES,
        default="codex",
        help="Agent harness to invoke",
    )
    run_parser.add_argument(
        "--review-harness",
        choices=harnesses.HARNESS_NAMES,
        default=None,
        help=(
            "Append a generated final-review session to the plan and run it "
            "with this harness after all implementation sessions complete; "
            "must differ from --harness"
        ),
    )
    run_parser.add_argument(
        "--model",
        help="Harness model to request",
    )
    run_parser.add_argument(
        "--effort",
        help="Harness reasoning effort to request",
    )
    run_parser.add_argument(
        "--timeout",
        type=int,
        help="Per-session harness timeout in seconds",
    )
    run_parser.add_argument(
        "--max-sessions",
        type=int,
        help="Maximum sessions to complete before stopping",
    )
    commit_group = run_parser.add_mutually_exclusive_group()
    commit_group.add_argument(
        "--commit-per-session",
        dest="commit_mode",
        action="store_const",
        const="per-session",
        default=None,
        help="Run 'spec commit --yes --no-verify' after each completed session",
    )
    commit_group.add_argument(
        "--commit-at-end",
        dest="commit_mode",
        action="store_const",
        const="end",
        help="Run 'spec commit --yes --no-verify' once after all sessions complete",
    )
    run_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    run_parser.add_argument(
        "--note",
        help="Reference note to show in run status and final output",
    )
    _add_unsafe_bypass_flags(run_parser)
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and show planned work without invoking a harness",
    )
    run_parser.add_argument(
        "--allow-not-loop-ready",
        action="store_true",
        help=(
            "Bypass the readiness gate and run even when the plan is not "
            "marked loop-ready (preflight findings are still reported)"
        ),
    )
    run_parser.set_defaults(handler=runner.run_from_args)

    status_parser = subparsers.add_parser(
        "status",
        help="Show session ID and operational runner status.",
    )
    status_parser.add_argument("plan_dir", help="Split plan directory to inspect")
    status_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    status_parser.set_defaults(handler=state.status_from_args)

    tail_parser = subparsers.add_parser(
        "tail",
        help="Show and follow live run events and harness logs by session ID.",
    )
    tail_parser.add_argument("plan_dir", help="Split plan directory to tail")
    tail_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    tail_parser.add_argument(
        "--lines",
        type=int,
        default=tail.DEFAULT_LINES,
        help="Recent activity lines to show before following",
    )
    tail_parser.add_argument(
        "--no-follow",
        action="store_true",
        help="Print recent activity and exit without waiting",
    )
    tail_parser.set_defaults(handler=tail.tail_from_args)

    sessions_help = (
        "Show running and recent ASL-managed sessions across plan directories."
    )
    sessions_parser = subparsers.add_parser("sessions", help=sessions_help)
    sessions_parser.add_argument(
        "--running",
        action="store_true",
        help="Show only currently running sessions",
    )
    sessions_parser.add_argument(
        "--recent",
        action="store_true",
        help="Show only recently completed sessions",
    )
    sessions_parser.add_argument(
        "--limit",
        type=int,
        default=session_overview.DEFAULT_RECENT_LIMIT,
        help=(
            "Maximum recent rows to show "
            f"(default: {session_overview.DEFAULT_RECENT_LIMIT})"
        ),
    )
    sessions_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    sessions_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Use full plan directory paths in text output",
    )
    sessions_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    sessions_parser.set_defaults(handler=session_overview.sessions_from_args)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check harness and path prerequisites.",
    )
    doctor_parser.add_argument(
        "--harness",
        required=True,
        choices=harnesses.HARNESS_NAMES,
        help="Agent harness to check",
    )
    doctor_parser.add_argument(
        "--model",
        help="Harness model to request",
    )
    doctor_parser.add_argument(
        "--effort",
        help="Harness reasoning effort to request",
    )
    doctor_parser.add_argument(
        "--cwd",
        default=".",
        help="Repository root where the harness should run (default: current directory)",
    )
    doctor_parser.add_argument(
        "--plan-dir",
        required=True,
        help="Split plan directory to inspect",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    _add_unsafe_bypass_flags(doctor_parser)
    doctor_parser.set_defaults(handler=runner.doctor_from_args)

    verify_parser = subparsers.add_parser(
        "verify",
        help="Execute a session's completion contract Verify recipe.",
    )
    verify_parser.add_argument("plan_dir", help="Split plan directory to inspect")
    verify_parser.add_argument(
        "--session",
        required=True,
        help="Session ID whose completion contract should be verified",
    )
    verify_parser.add_argument(
        "--cwd",
        default=".",
        help="Repository root for verification checks (default: current directory)",
    )
    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    verify_parser.set_defaults(handler=verification.verify_from_args)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Check that a split plan directory is loop-ready.",
    )
    preflight_parser.add_argument("plan_dir", help="Split plan directory to inspect")
    preflight_parser.add_argument(
        "--cwd",
        default=None,
        help="Repository root for environment checks (default: skip cwd checks)",
    )
    preflight_parser.add_argument(
        "--harness",
        choices=harnesses.HARNESS_NAMES,
        default=None,
        help="Agent harness to check (default: skip harness checks)",
    )
    preflight_commit = preflight_parser.add_mutually_exclusive_group()
    preflight_commit.add_argument(
        "--commit-per-session",
        dest="commit_mode",
        action="store_const",
        const="per-session",
        default=None,
        help="Validate environment for per-session commit mode",
    )
    preflight_commit.add_argument(
        "--commit-at-end",
        dest="commit_mode",
        action="store_const",
        const="end",
        help="Validate environment for commit-at-end mode",
    )
    preflight_parser.add_argument(
        "--state-dir",
        default=None,
        help="Override operational state directory",
    )
    preflight_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    preflight_parser.add_argument(
        "-m",
        "--mark-loop-ready",
        action="store_true",
        help="When checks pass, mark the plan readiness state as loop-ready",
    )
    preflight_parser.set_defaults(handler=preflight.preflight_from_args)

    unlock_parser = subparsers.add_parser(
        "unlock",
        help="Remove an operational lock for a plan directory.",
    )
    unlock_parser.add_argument("plan_dir", help="Split plan directory to unlock")
    unlock_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    unlock_parser.add_argument(
        "--force",
        action="store_true",
        help="Remove the lock even when it appears active",
    )
    unlock_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    unlock_parser.set_defaults(handler=state.unlock_from_args)

    backfill_parser = subparsers.add_parser(
        "backfill-progress",
        help=(
            "Reconstruct progress.md from completed session handoffs "
            "(timestamps approximated from session file mtimes)"
        ),
    )
    backfill_parser.add_argument(
        "plan_dir", help="Split plan directory whose progress journal to rebuild"
    )
    backfill_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing progress.md instead of refusing",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the would-be journal contents to stdout without writing",
    )
    backfill_parser.set_defaults(
        handler=progress_cli.backfill_progress_from_args
    )

    progress_parser = subparsers.add_parser(
        "progress",
        help="Print, JSON-dump, or follow the runner-managed progress journal.",
    )
    progress_parser.add_argument(
        "plan_dir", help="Split plan directory whose progress journal to inspect"
    )
    progress_parser.add_argument(
        "--tail",
        action="store_true",
        help="Follow the journal and print new entries as they appear",
    )
    progress_parser.add_argument(
        "--json",
        action="store_true",
        help="Print parsed entries as machine-readable JSON",
    )
    progress_parser.set_defaults(handler=progress_cli.progress_from_args)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(prog=Path(sys.argv[0]).name)
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    return args.handler(args)
