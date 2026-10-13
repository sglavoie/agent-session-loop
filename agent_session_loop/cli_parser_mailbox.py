"""Parser definitions for the inbox, worker message and outbox commands."""

from __future__ import annotations

import argparse

from .cli_parser_support import (
    _add_plan_reference_args,
    _AgentSessionLoopArgumentParser,
    _group_epilogue,
)


def _add_inbox_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl inbox` group."""
    inbox_parser = subparsers.add_parser(
        "inbox",
        help="Write supervisor messages or read pending worker guidance.",
        epilog=_group_epilogue("inbox", "asl inbox reply --help", guide=True),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inbox_sub = inbox_parser.add_subparsers(
        dest="inbox_command",
        required=True,
    )

    inbox_guidance_parser = inbox_sub.add_parser(
        "guidance",
        help="Append implementation guidance for the active worker.",
    )
    _add_plan_reference_args(inbox_guidance_parser)
    inbox_guidance_parser.add_argument("--text", help="Guidance body")
    inbox_guidance_parser.add_argument(
        "--text-stdin",
        action="store_true",
        help="Read the guidance body from standard input",
    )
    inbox_guidance_parser.add_argument(
        "--wait-delivered",
        action="store_true",
        help="Wait for the run to record a delivery result for this guidance",
    )
    inbox_guidance_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Delivery wait timeout in seconds (default: 30; maximum: 1800)",
    )
    inbox_guidance_parser.set_defaults(
        handler_ref="mailbox_cli_guidance:inbox_guidance_from_args",
    )

    inbox_reply_parser = inbox_sub.add_parser(
        "reply",
        help="Append the first reply to an existing worker question.",
    )
    _add_plan_reference_args(inbox_reply_parser)
    inbox_reply_parser.add_argument(
        "--to",
        required=True,
        metavar="QUESTION_ID",
        help="Question UUID to answer",
    )
    inbox_reply_parser.add_argument("--text", help="Reply body")
    inbox_reply_parser.add_argument(
        "--text-stdin",
        action="store_true",
        help="Read the reply body from standard input",
    )
    inbox_reply_parser.add_argument(
        "--wait-consumed",
        action="store_true",
        help="Wait for the worker or runner to record closure consumption",
    )
    inbox_reply_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Consumption wait timeout in seconds (default: 90; maximum: 1800)",
    )
    inbox_reply_parser.set_defaults(
        handler_ref="mailbox_cli_replies:inbox_reply_from_args",
    )

    inbox_resolve_parser = inbox_sub.add_parser(
        "resolve",
        help="Resolve a live worker question without supplying reply text.",
    )
    _add_plan_reference_args(inbox_resolve_parser)
    inbox_resolve_parser.add_argument(
        "--to",
        required=True,
        metavar="QUESTION_ID",
        help="Question UUID to resolve",
    )
    inbox_resolve_parser.add_argument(
        "--action",
        required=True,
        metavar="continue|pause",
        help="Resolve the question by continuing or pausing the worker",
    )
    resolution_note = inbox_resolve_parser.add_mutually_exclusive_group()
    resolution_note.add_argument(
        "--note",
        help="Optional resolution note (not an operator reply)",
    )
    inbox_resolve_parser.add_argument(
        "--wait-consumed",
        action="store_true",
        help="Wait for the worker or runner to record closure consumption",
    )
    inbox_resolve_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Consumption wait timeout in seconds (default: 90; maximum: 1800)",
    )
    resolution_note.add_argument(
        "--note-stdin",
        action="store_true",
        help="Read the optional resolution note from standard input",
    )
    inbox_resolve_parser.set_defaults(
        handler_ref="mailbox_cli_replies:inbox_resolve_from_args",
    )

    inbox_read_parser = inbox_sub.add_parser(
        "read",
        help="Read pending guidance and acknowledge it unless peeking.",
    )
    _add_plan_reference_args(inbox_read_parser)
    inbox_read_parser.add_argument(
        "--peek",
        action="store_true",
        help="Read pending guidance without acknowledging it",
    )
    inbox_read_parser.set_defaults(
        handler_ref="mailbox_cli_inbox:inbox_read_from_args",
    )

    inbox_acknowledge_parser = inbox_sub.add_parser(
        "acknowledge",
        help="Receipt urgent corrections carried into the attempt you are running.",
    )
    _add_plan_reference_args(inbox_acknowledge_parser)
    inbox_acknowledge_parser.add_argument(
        "--redirect-id",
        action="append",
        default=None,
        metavar="OPERATION_ID",
        help="Correction to receipt; repeat to receipt several at once",
    )
    inbox_acknowledge_parser.add_argument(
        "--attempt",
        required=True,
        metavar="TOKEN",
        help="Attempt token from your prompt; it names this attempt only",
    )
    inbox_acknowledge_parser.set_defaults(
        handler_ref="mailbox_cli_inbox:inbox_acknowledge_from_args",
    )


def _add_worker_message_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """Worker-side mailbox writes: ask and notify."""
    ask_parser = subparsers.add_parser(
        "ask",
        help=(
            "Append or resume a worker question and optionally wait for its "
            "first reply or resolution."
        ),
    )
    _add_plan_reference_args(ask_parser)
    ask_parser.add_argument(
        "text",
        nargs="?",
        help="New question body",
    )
    ask_parser.add_argument(
        "--question-id",
        metavar="QUESTION_ID",
        help="Resume an existing question without appending it again",
    )
    ask_parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the first reply or resolution",
    )
    ask_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Closure wait timeout in seconds; requires --wait "
            "(default: 90; maximum: 1800)"
        ),
    )
    ask_parser.set_defaults(handler_ref="mailbox_cli_ask:ask_from_args")

    notify_parser = subparsers.add_parser(
        "notify",
        help="Publish a nonblocking notice from the active worker turn.",
    )
    _add_plan_reference_args(notify_parser)
    notify_parser.add_argument(
        "text",
        nargs="?",
        help="Notice body",
    )
    notify_parser.add_argument(
        "--text-stdin",
        action="store_true",
        help="Read the notice body from standard input",
    )
    notify_parser.set_defaults(handler_ref="mailbox_cli_notify:notify_from_args")


def _add_outbox_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl outbox` group."""
    outbox_parser = subparsers.add_parser(
        "outbox",
        help="Read worker questions or notices from the supervisor side.",
        epilog=_group_epilogue("outbox", "asl outbox supervise --help", guide=True),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    outbox_sub = outbox_parser.add_subparsers(
        dest="outbox_command",
        required=True,
    )
    outbox_wait_parser = outbox_sub.add_parser(
        "wait",
        help="Wait for selected records after a physical mailbox sequence.",
    )
    _add_plan_reference_args(outbox_wait_parser)
    outbox_wait_parser.add_argument(
        "--include",
        choices=("questions", "notices", "all"),
        default="questions",
        help=(
            "Record kinds that wake and populate the wait; 'all' also wakes on "
            "the terminal supervision-end record and reports it through "
            "supervision_ended (default: questions)"
        ),
    )
    outbox_wait_parser.add_argument(
        "--supervision-id",
        metavar="UUID",
        help=(
            "Report supervision_ended only for the terminal record carrying "
            "this launch identity; records from other supervising launches "
            "still advance the cursor (default: any terminal record ends)"
        ),
    )
    outbox_wait_parser.add_argument(
        "--since",
        type=int,
        default=0,
        metavar="SEQ",
        help="Return selected records after this physical sequence (default: 0)",
    )
    outbox_wait_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Long-poll timeout in seconds (default: 300; maximum: 1800)",
    )
    outbox_wait_parser.set_defaults(
        handler_ref="mailbox_cli_outbox:outbox_wait_from_args",
    )

    outbox_supervise_parser = outbox_sub.add_parser(
        "supervise",
        help=("Wait on a resumable per-launch cursor and print one supervision token."),
    )
    _add_plan_reference_args(outbox_supervise_parser)
    outbox_supervise_parser.add_argument(
        "--supervision-id",
        metavar="UUID",
        required=True,
        help=(
            "Launch identity: scopes the terminal record to this supervising "
            "launch and keys the persisted cursor"
        ),
    )
    outbox_supervise_parser.add_argument(
        "--timeout",
        type=float,
        default=550.0,
        metavar="SECONDS",
        help="Long-poll timeout in seconds (default: 550; maximum: 1800)",
    )
    outbox_supervise_parser.set_defaults(
        handler_ref="mailbox_cli_outbox:outbox_supervise_from_args",
    )
