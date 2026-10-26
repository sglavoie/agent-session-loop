"""Parser definitions for the run, run-inspection and check commands."""

from __future__ import annotations

import argparse

from . import contracts, harnesses, run_flags, session_overview, tail
from .cli_parser_support import (
    _add_plan_dir_with_option_alias,
    _add_unsafe_bypass_flags,
    _AgentSessionLoopArgumentParser,
)


def _add_run_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """`asl run`, the loop itself."""
    run_parser = subparsers.add_parser(
        "run",
        help="Run a split plan's pending sessions, one harness turn at a time.",
    )
    run_flags.add_run_arguments(run_parser, mode="cli")
    run_parser.set_defaults(handler_ref="runner:run_from_args")


def _add_run_inspection_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """Read-only views of a run: guide, questions, verdict, status, tail, sessions."""
    supervisor_guide_parser = subparsers.add_parser(
        "supervisor-guide",
        help=(
            "Print the canonical supervisor workflow: skill invocation, "
            "detached helper launch, mailbox operations, and leaf help."
        ),
    )
    supervisor_guide_parser.add_argument(
        "--harness",
        choices=("auto", *harnesses.HARNESS_NAMES),
        default="auto",
        help=(
            "Harness whose skill invocation to show; 'auto' detects the "
            "invoking harness and falls back to every candidate when detection "
            "is absent or ambiguous (default: auto)"
        ),
    )
    supervisor_guide_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    supervisor_guide_parser.set_defaults(
        handler_ref="supervisor_guide:supervisor_guide_from_args",
    )

    questions_parser = subparsers.add_parser(
        "questions",
        help=(
            "Print a resolver prompt for the latest paused run so a second "
            "agent can collect a reply suitable for `asl run --reply`."
        ),
    )
    questions_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        help="Split plan directory whose paused run to inspect",
    )
    questions_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    questions_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    questions_parser.set_defaults(handler_ref="qna:questions_from_args")

    review_verdict_parser = subparsers.add_parser(
        "review-verdict",
        help=(
            "Print the authoritative final-review verdict: full untruncated "
            "findings and summary, continuation evaluation, and remediation "
            "commands."
        ),
    )
    review_verdict_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        help="Split plan directory whose review verdict to inspect",
    )
    review_verdict_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    review_verdict_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    review_verdict_parser.set_defaults(handler_ref="review:review_verdict_from_args")

    status_parser = subparsers.add_parser(
        "status",
        help="Show a plan's sessions, their handoff status, and the runner's state.",
    )
    status_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR_OR_ID",
        help=(
            "Split plan directory to inspect, or the compact plan label "
            "printed after 'plan' by 'asl sessions'"
        ),
    )
    status_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    status_parser.set_defaults(handler_ref="state_status:status_from_args")

    tail_parser = subparsers.add_parser(
        "tail",
        help="Show and follow a plan's run events and harness logs.",
    )
    tail_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory to tail"
    )
    tail_parser.add_argument(
        "--state-dir",
        metavar="DIR",
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
    tail_parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the run's event records as JSON lines, one per line, "
            "instead of formatted activity and harness logs"
        ),
    )
    tail_parser.set_defaults(handler_ref="tail:tail_from_args")

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
        metavar="DIR",
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
    sessions_parser.set_defaults(handler_ref="session_overview:sessions_from_args")


def _add_check_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """Checks: doctor, verify, contract-check, harness-compat, baseline."""
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
        "--harness-model",
        dest="harness_model",
        help="Harness model to request",
    )
    doctor_parser.add_argument(
        "--effort",
        help="Harness reasoning effort to request",
    )
    doctor_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan (default: current directory)"
        ),
    )
    _add_plan_dir_with_option_alias(
        doctor_parser, help_text="Split plan directory to inspect"
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    _add_unsafe_bypass_flags(doctor_parser)
    doctor_parser.set_defaults(handler_ref="runner:doctor_from_args")

    verify_parser = subparsers.add_parser(
        "verify",
        help="Execute a session's completion contract Verify recipe.",
    )
    verify_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory to inspect"
    )
    verify_parser.add_argument(
        "--session",
        required=True,
        metavar="SESSION_ID",
        help="Session ID whose completion contract should be verified",
    )
    verify_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan (default: current directory)"
        ),
    )
    verify_parser.add_argument(
        "--artifact-dir",
        default=None,
        help=(
            "Directory to retain failed command output as gzip artifacts "
            "(default: previews only, no full-output artifacts)"
        ),
    )
    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    verify_parser.set_defaults(handler_ref="verification_cli:verify_from_args")

    contract_check_parser = subparsers.add_parser(
        "contract-check",
        help=(
            "Statically diagnose each session's completion-contract Verify "
            "recipe without running any command."
        ),
    )
    contract_check_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory to inspect"
    )
    contract_check_parser.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help=(
            "Restrict the check to a single session id (single-file mode; "
            "default: every session in the plan)"
        ),
    )
    contract_check_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan (default: current directory)"
        ),
    )
    contract_check_parser.add_argument(
        "--agent-mux",
        action="store_true",
        help=(
            "Also report the ASL-owned Agent Mux compatibility contract: "
            "component schema versions, digests, contract-set id, state root, "
            "and fixture validation"
        ),
    )
    contract_check_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory (reported by --agent-mux)",
    )
    contract_check_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    contract_check_parser.set_defaults(
        handler_ref="verification_contract_check:contract_check_from_args"
    )

    harness_compat_parser = subparsers.add_parser(
        "harness-compat",
        help=(
            "Classify the installed Codex, Claude, and Copilot CLIs against "
            "the pinned support window without using any model."
        ),
    )
    harness_compat_parser.add_argument(
        "--harness",
        action="append",
        choices=list(harnesses.HARNESS_NAMES),
        help=(
            "Restrict the check to one harness (repeatable; "
            "default: every supported harness)"
        ),
    )
    harness_compat_parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit 3 when any harness is unknown rather than only exiting 1 on "
            "a known-incompatible version"
        ),
    )
    harness_compat_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    harness_compat_parser.set_defaults(
        handler_ref="harness_compatibility:harness_compat_from_args"
    )

    baseline_parser = subparsers.add_parser(
        "baseline",
        help=(
            "Run each session's baseline-expectation checks and record "
            "pass/fail/timeout evidence for the preflight readiness gate."
        ),
    )
    baseline_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        help="Split plan directory whose baseline checks to run",
    )
    baseline_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan (default: current directory)"
        ),
    )
    baseline_parser.add_argument(
        "--per-check-timeout",
        dest="per_check_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Clamp each baseline check to this many seconds "
            "(default: the contract budget, capped at "
            f"{contracts.DEFAULT_BASELINE_CHECK_BUDGET_SECONDS:g}s)"
        ),
    )
    baseline_parser.add_argument(
        "--total-timeout",
        dest="total_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Stop running baseline checks once cumulative wall-clock exceeds "
            "this many seconds (default: the summed contract budget, capped at "
            f"{contracts.DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS:g}s)"
        ),
    )
    baseline_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    baseline_parser.set_defaults(handler_ref="baseline:baseline_from_args")
