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
    qna,
    runner,
    session_overview,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)


_WORKBENCH_SERVE_CHILD_DESTS = {
    "host": "_workbench_serve_host",
    "port": "_workbench_serve_port",
    "state_dir": "_workbench_serve_state_dir",
    "plan_root": "_workbench_serve_plan_root",
    "open": "_workbench_serve_open",
    "json": "_workbench_serve_json",
}


def _workbench_dest(name: str, *, child_parser: bool) -> str:
    if child_parser:
        return _WORKBENCH_SERVE_CHILD_DESTS[name]
    return name


def _workbench_default(value: object, *, child_parser: bool) -> object:
    if child_parser:
        return argparse.SUPPRESS
    return value


def _normalize_workbench_serve_args(args: argparse.Namespace) -> argparse.Namespace:
    if (
        getattr(args, "command", None) != "workbench"
        or getattr(args, "workbench_command", None) != "serve"
    ):
        return args

    for public_dest, child_dest in _WORKBENCH_SERVE_CHILD_DESTS.items():
        if public_dest == "plan_root":
            continue
        if hasattr(args, child_dest):
            setattr(args, public_dest, getattr(args, child_dest))
            delattr(args, child_dest)

    child_plan_root = _WORKBENCH_SERVE_CHILD_DESTS["plan_root"]
    if hasattr(args, child_plan_root):
        plan_roots = list(getattr(args, "plan_root", None) or ())
        plan_roots.extend(getattr(args, child_plan_root))
        args.plan_root = plan_roots
        delattr(args, child_plan_root)

    return args


class _AgentSessionLoopArgumentParser(argparse.ArgumentParser):
    def parse_args(
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        return _normalize_workbench_serve_args(parsed)


def _add_workbench_serve_args(
    parser: argparse.ArgumentParser, *, child_parser: bool = False
) -> None:
    parser.add_argument(
        "--host",
        dest=_workbench_dest("host", child_parser=child_parser),
        default=_workbench_default(
            workbench_server.DEFAULT_HOST, child_parser=child_parser
        ),
        metavar="HOST",
        help=(
            f"Host interface to bind (default: {workbench_server.DEFAULT_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        dest=_workbench_dest("port", child_parser=child_parser),
        type=int,
        default=_workbench_default(
            workbench_server.DEFAULT_PORT, child_parser=child_parser
        ),
        metavar="PORT",
        help=(
            f"TCP port to bind (default: {workbench_server.DEFAULT_PORT}; "
            "use 0 for an ephemeral port)"
        ),
    )
    parser.add_argument(
        "--state-dir",
        dest=_workbench_dest("state_dir", child_parser=child_parser),
        default=_workbench_default(None, child_parser=child_parser),
        metavar="DIR",
        help="Override operational state directory",
    )
    parser.add_argument(
        "--plan-root",
        dest=_workbench_dest("plan_root", child_parser=child_parser),
        action="append",
        default=_workbench_default(None, child_parser=child_parser),
        metavar="DIR",
        help=(
            "Extra plan root directory to scan for discoverable plans "
            "(may be repeated)"
        ),
    )
    parser.add_argument(
        "--open",
        dest=_workbench_dest("open", child_parser=child_parser),
        action="store_true",
        default=_workbench_default(False, child_parser=child_parser),
        help="Open the Workbench URL in the default browser after binding",
    )
    parser.add_argument(
        "--json",
        dest=_workbench_dest("json", child_parser=child_parser),
        action="store_true",
        default=_workbench_default(False, child_parser=child_parser),
        help="Print machine-readable JSON startup output",
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
    parser = _AgentSessionLoopArgumentParser(
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
    reply_group = run_parser.add_mutually_exclusive_group()
    reply_group.add_argument(
        "--reply",
        help=(
            "Resume the latest paused session with an inline reply; the "
            "text is sent as the operator answer for the first resumed turn"
        ),
    )
    reply_group.add_argument(
        "--reply-stdin",
        dest="reply_stdin",
        action="store_true",
        help=(
            "Read the operator reply from stdin instead of --reply, so the "
            "text never appears in argv; behaves identically to --reply TEXT "
            "otherwise"
        ),
    )
    run_parser.set_defaults(handler=runner.run_from_args, reply_stdin=False)

    questions_parser = subparsers.add_parser(
        "questions",
        help=(
            "Print a resolver prompt for the latest paused run so a second "
            "agent can collect a reply suitable for `asl run --reply`."
        ),
    )
    questions_parser.add_argument(
        "plan_dir", help="Split plan directory whose paused run to inspect"
    )
    questions_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    questions_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    questions_parser.set_defaults(handler=qna.questions_from_args)

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

    workbench_parser = subparsers.add_parser(
        "workbench",
        help="Run the local Workbench HTTP control plane.",
    )
    _add_workbench_serve_args(workbench_parser)
    workbench_parser.set_defaults(
        workbench_command=None,
        handler=workbench_server.workbench_serve_from_args,
    )
    workbench_sub = workbench_parser.add_subparsers(
        dest="workbench_command",
    )

    workbench_serve_parser = workbench_sub.add_parser(
        "serve",
        help="Start the loopback Workbench HTTP server.",
    )
    _add_workbench_serve_args(workbench_serve_parser, child_parser=True)
    workbench_serve_parser.set_defaults(
        handler=workbench_server.workbench_serve_from_args,
    )

    workbench_pin_parser = workbench_sub.add_parser(
        "pin",
        help="Pin a plan directory in the Workbench registry.",
    )
    workbench_pin_parser.add_argument(
        "plan_dir",
        help="Plan directory to pin (must exist on disk)",
    )
    workbench_pin_parser.add_argument(
        "--cwd",
        default=None,
        help="Default working directory the Workbench should launch this plan in",
    )
    workbench_pin_parser.add_argument(
        "--label",
        default=None,
        help="Human-readable label for the pin",
    )
    workbench_pin_parser.set_defaults(
        handler=workbench.workbench_pin_from_args,
    )

    workbench_unpin_parser = workbench_sub.add_parser(
        "unpin",
        help="Remove a pinned plan directory from the Workbench registry.",
    )
    workbench_unpin_parser.add_argument(
        "plan_dir",
        help="Plan directory to unpin",
    )
    workbench_unpin_parser.set_defaults(
        handler=workbench.workbench_unpin_from_args,
    )

    workbench_registry_parser = workbench_sub.add_parser(
        "registry",
        help="Print the current Workbench registry contents.",
    )
    workbench_registry_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the registry payload as machine-readable JSON",
    )
    workbench_registry_parser.set_defaults(
        handler=workbench.workbench_registry_from_args,
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(prog=Path(sys.argv[0]).name)
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    return args.handler(args)
