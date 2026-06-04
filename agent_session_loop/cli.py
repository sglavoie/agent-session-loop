"""Command-line interface for agent-session-loop."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import harnesses, runner, state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-session-loop",
        description="Run split agent session plans one session at a time.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run pending sessions from a split plan directory.",
    )
    run_parser.add_argument("plan_dir", help="Split plan directory to execute")
    run_parser.add_argument(
        "--cwd",
        required=True,
        help="Repository root where the harness should run",
    )
    run_parser.add_argument(
        "--harness",
        choices=harnesses.HARNESS_NAMES,
        default="codex",
        help="Agent harness to invoke",
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
    run_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    run_parser.add_argument(
        "--unsafe-bypass",
        action="store_true",
        help="Use harness-specific bypass flags for sandbox and approval prompts",
    )
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
    run_parser.set_defaults(handler=runner.run_from_args)

    status_parser = subparsers.add_parser(
        "status",
        help="Show session and operational runner status.",
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
        "--cwd",
        required=True,
        help="Repository root where the harness should run",
    )
    doctor_parser.add_argument(
        "--plan-dir",
        required=True,
        help="Split plan directory to inspect",
    )
    doctor_parser.set_defaults(handler=runner.doctor_from_args)

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
    unlock_parser.set_defaults(handler=state.unlock_from_args)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    return args.handler(args)
