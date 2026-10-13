"""Parser definitions for the run control commands."""

from __future__ import annotations

import argparse

from . import harnesses
from .cli_defaults import (
    _CONTROL_DEFAULT_ABORT_GRACE_SECONDS,
    _REDIRECT_ACCEPTANCE_WAIT_SECONDS,
    _REDIRECT_DEFAULT_ACK_WAIT_SECONDS,
    _REDIRECT_DEFAULT_TTL_SECONDS,
    _REDIRECT_MAX_ACCEPTANCE_WAIT_SECONDS,
    _REDIRECT_MAX_ACK_WAIT_SECONDS,
    _REDIRECT_MAX_TTL_SECONDS,
)
from .cli_parser_support import (
    _add_control_wait_args,
    _add_plan_reference_args,
    _AgentSessionLoopArgumentParser,
    _group_epilogue,
)


def _add_control_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl control` group."""
    control_parser = subparsers.add_parser(
        "control",
        help=(
            "Queue a control command for the runner that owns a plan "
            "(works for --json runs, where the run-control keys are off)."
        ),
        epilog=_group_epilogue("control", "asl control abort --help", guide=True),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Unlike `workbench`, the bare `asl control` form has no sensible default
    # action, so a subcommand is required.
    control_sub = control_parser.add_subparsers(
        dest="control_command",
        required=True,
    )

    control_stop_parser = control_sub.add_parser(
        "stop-after-current",
        help=(
            "Request a graceful stop once the current session finishes "
            "(programmatic equivalent of the interactive 'p' key)."
        ),
    )
    _add_plan_reference_args(control_stop_parser)
    _add_control_wait_args(control_stop_parser)
    control_stop_parser.set_defaults(
        handler_ref="control_cli:control_stop_after_current_from_args",
    )

    control_abort_parser = control_sub.add_parser(
        "abort",
        help=(
            "Abort the in-flight turn after a bounded grace window "
            "(degrades to stop-after-current when no turn is running)."
        ),
    )
    _add_plan_reference_args(control_abort_parser)
    _add_control_wait_args(control_abort_parser)
    control_abort_parser.add_argument(
        "--grace",
        type=int,
        default=_CONTROL_DEFAULT_ABORT_GRACE_SECONDS,
        metavar="SECONDS",
        help=(
            "Seconds the worker gets to wind down before the turn is killed "
            f"(default: {_CONTROL_DEFAULT_ABORT_GRACE_SECONDS})"
        ),
    )
    control_abort_parser.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help="Reason delivered to the worker with the abort",
    )
    control_abort_parser.set_defaults(
        handler_ref="control_cli:control_abort_from_args",
    )

    control_select_harness_parser = control_sub.add_parser(
        "select-harness",
        help="Choose the harness future sessions of a run should use.",
    )
    _add_plan_reference_args(control_select_harness_parser)
    _add_control_wait_args(control_select_harness_parser)
    control_select_harness_parser.add_argument(
        "--harness",
        required=True,
        choices=harnesses.HARNESS_NAMES,
        help="Harness future sessions should run with",
    )
    control_select_harness_parser.set_defaults(
        handler_ref="control_cli:control_select_harness_from_args",
    )

    control_set_model_parser = control_sub.add_parser(
        "set-model",
        help=(
            "Set or clear the model and effort used by the next implementation turn."
        ),
    )
    _add_plan_reference_args(control_set_model_parser)
    _add_control_wait_args(control_set_model_parser)
    control_set_model_parser.add_argument(
        "--harness",
        choices=harnesses.HARNESS_NAMES,
        default=None,
        help=(
            "Implementation harness to update (default: the selected future "
            "harness when the runner drains the command)"
        ),
    )
    model_operation = control_set_model_parser.add_mutually_exclusive_group()
    model_operation.add_argument(
        "--harness-model",
        dest="harness_model",
        help="Model to request for the target implementation harness",
    )
    model_operation.add_argument(
        "--clear-harness-model",
        action="store_true",
        help="Return the target harness to its run flag or native model default",
    )
    effort_operation = control_set_model_parser.add_mutually_exclusive_group()
    effort_operation.add_argument(
        "--effort",
        help="Reasoning effort to request for the target implementation harness",
    )
    effort_operation.add_argument(
        "--clear-effort",
        action="store_true",
        help="Return the target harness to the run's original effort setting",
    )
    control_set_model_parser.set_defaults(
        handler_ref="control_cli:control_set_model_from_args",
    )

    control_guard_parser = control_sub.add_parser(
        "guard",
        help=(
            "Add, remove, or list run-scoped guards: commands denied through "
            "the worker's PATH, and paths that must end the turn unchanged."
        ),
    )
    control_guard_sub = control_guard_parser.add_subparsers(
        dest="control_guard_command",
        required=True,
    )

    control_guard_add_parser = control_guard_sub.add_parser(
        "add",
        help=(
            "Deny a command, or protect a path, for the remainder of the run "
            "holding this plan."
        ),
    )
    _add_plan_reference_args(control_guard_add_parser)
    _add_control_wait_args(control_guard_add_parser)
    control_guard_add_parser.add_argument(
        "--deny-command",
        action="append",
        default=None,
        metavar="COMMAND",
        help=(
            "Command line to deny, matched as an exact argument prefix after "
            "POSIX tokenization (repeatable; e.g. 'git push origin main')"
        ),
    )
    control_guard_add_parser.add_argument(
        "--protect-path",
        action="append",
        default=None,
        metavar="ALIAS:PATH",
        help=(
            "Repository-qualified path that must end each turn exactly as it "
            "started, compared against the turn-start snapshot (repeatable; "
            "`*` and `?` match within one segment, `**` across segments; use "
            "'.' as the alias for a single-repository plan)"
        ),
    )
    control_guard_add_parser.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help="Reason recorded with the guard",
    )
    control_guard_add_parser.set_defaults(
        handler_ref="control_cli:control_guard_add_from_args",
    )

    control_guard_remove_parser = control_guard_sub.add_parser(
        "remove",
        help="Remove one guard by ID, ending its enforcement.",
    )
    _add_plan_reference_args(control_guard_remove_parser)
    _add_control_wait_args(control_guard_remove_parser)
    control_guard_remove_parser.add_argument(
        "--id",
        dest="id",
        required=True,
        metavar="GUARD_ID",
        help="Identifier reported when the guard was applied",
    )
    control_guard_remove_parser.set_defaults(
        handler_ref="control_cli:control_guard_remove_from_args",
    )

    control_guard_list_parser = control_guard_sub.add_parser(
        "list",
        help="Report the guards the active run is enforcing, and their coverage.",
    )
    _add_plan_reference_args(control_guard_list_parser)
    control_guard_list_parser.set_defaults(
        handler_ref="control_cli:control_guard_list_from_args",
    )

    control_redirect_parser = control_sub.add_parser(
        "redirect",
        help=(
            "Correct the turn that is running now: stop it and hand the "
            "correction to its successor."
        ),
    )
    _add_plan_reference_args(control_redirect_parser)
    redirect_body = control_redirect_parser.add_mutually_exclusive_group()
    redirect_body.add_argument(
        "--text",
        help="Correction delivered to the worker",
    )
    redirect_body.add_argument(
        "--text-stdin",
        dest="text_stdin",
        action="store_true",
        help="Read the correction from standard input instead of --text",
    )
    control_redirect_parser.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help=(
            "Session the correction is meant for; refuses with stale-target "
            "when the run has moved on (default: whichever turn is live)"
        ),
    )
    control_redirect_parser.add_argument(
        "--turn",
        type=int,
        default=None,
        metavar="N",
        help="Turn index the correction is meant for (default: the live turn)",
    )
    control_redirect_parser.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        metavar="RUN_ID",
        help="Run the correction is meant for (default: the live run)",
    )
    control_redirect_parser.add_argument(
        "--operation-id",
        dest="operation_id",
        default=None,
        metavar="ID",
        help=(
            "Reuse an identifier from an earlier uncertain submission; the "
            "same id with the same text is the same request, never a second one"
        ),
    )
    control_redirect_parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "How long the correction stays claimable before it expires "
            f"(default: {_REDIRECT_DEFAULT_TTL_SECONDS}; "
            f"maximum: {_REDIRECT_MAX_TTL_SECONDS})"
        ),
    )
    control_redirect_parser.add_argument(
        "--timeout",
        dest="wait_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Seconds to wait for the runner's acceptance before reporting the "
            "submission uncertain "
            f"(default: {_REDIRECT_ACCEPTANCE_WAIT_SECONDS:g}; "
            f"maximum: {_REDIRECT_MAX_ACCEPTANCE_WAIT_SECONDS:g})"
        ),
    )
    control_redirect_parser.set_defaults(
        handler_ref="redirect_cli:control_redirect_from_args",
    )

    resolve_redirect_parser = control_sub.add_parser(
        "resolve-redirect",
        help="Record an explicit decision about an uncertain submission.",
    )
    _add_plan_reference_args(resolve_redirect_parser)
    resolve_redirect_parser.add_argument("--operation-id", required=True)
    resolve_redirect_parser.add_argument(
        "--outcome", choices=("submitted", "not-submitted"), required=True
    )
    resolve_redirect_parser.add_argument("--reason", required=True)
    resolve_redirect_parser.set_defaults(
        handler_ref="redirect_cli:control_resolve_redirect_from_args"
    )

    control_redirect_status_parser = control_sub.add_parser(
        "redirect-status",
        help="Report what became of this plan's corrections, without changing any.",
    )
    _add_plan_reference_args(control_redirect_status_parser)
    control_redirect_status_parser.add_argument(
        "--redirect-id",
        dest="redirect_id",
        action="append",
        default=None,
        metavar="ID",
        help=(
            "Operation to report, repeatable "
            "(default: every operation the journal holds)"
        ),
    )
    control_redirect_status_parser.add_argument(
        "--wait-acknowledged",
        dest="wait_acknowledged",
        action="store_true",
        help=(
            "Watch until the live attempt receipts the named corrections; "
            "observational, so a timeout retracts nothing"
        ),
    )
    control_redirect_status_parser.add_argument(
        "--timeout",
        dest="wait_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Receipt wait timeout in seconds; requires --wait-acknowledged "
            f"(default: {_REDIRECT_DEFAULT_ACK_WAIT_SECONDS:g}; "
            f"maximum: {_REDIRECT_MAX_ACK_WAIT_SECONDS:g})"
        ),
    )
    control_redirect_status_parser.set_defaults(
        handler_ref="redirect_cli:control_redirect_status_from_args",
    )

    control_recover_conversation_parser = control_sub.add_parser(
        "recover-conversation",
        help=(
            "Report an unresumable conversation and print the one-use relaunch "
            "that authorizes a fresh one."
        ),
    )
    _add_plan_reference_args(control_recover_conversation_parser)
    control_recover_conversation_parser.set_defaults(
        handler_ref="redirect_cli:control_recover_conversation_from_args",
    )
