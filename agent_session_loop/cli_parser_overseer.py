"""Parser definitions for the overseer commands."""

from __future__ import annotations

import argparse

from .cli_parser_support import (
    _AgentSessionLoopArgumentParser,
    _group_epilogue,
    non_blank_text,
    non_negative_float,
    positive_float,
    positive_int,
)

# overseer_decisions.HUMAN_TOPICS, restated: this parser imports no overseer
# module. `--raises` shows TOPIC as its metavar, which hides argparse's own
# choice listing, so the help text names them instead.
_HUMAN_TOPICS = ("acceptance", "scope", "design", "safety")
_HUMAN_TOPICS_HELP = ", ".join(_HUMAN_TOPICS)

# Every overseer leaf exits 0, 1 or 2 by its own contract (overseer_cli_common),
# which differs from the rest of `asl` in the one case a script is most likely
# to branch on: a plan nobody can see.
_OVERSEER_EXIT_STATUS = (
    "\n\nExit status: 0 when the leaf answered and found nothing wrong;\n"
    "1 for a substantive refusal (a withheld packet, a partial sweep,\n"
    "an unsettled action); 2 for a malformed invocation or unreadable input.\n"
    "A plan directory discovery has no record of, including one that does not\n"
    "exist, exits 2 with the snake_case reason `plan_unobserved`, unlike the\n"
    "`plan-not-found` exit 1 that other `asl` commands report."
)


def _add_overseer_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl overseer` group."""
    overseer_parser = subparsers.add_parser(
        "overseer",
        help="Watch authorized runs on behalf of an invoked overseer host agent.",
        epilog=_group_epilogue("overseer", "asl overseer scan --help")
        + _OVERSEER_EXIT_STATUS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    overseer_sub = overseer_parser.add_subparsers(
        dest="overseer_command", required=True, metavar="COMMAND"
    )

    overseer_scan_parser = overseer_sub.add_parser(
        "scan",
        help="List every run discovery can see and why each is or is not in play.",
        description=(
            "Report the fleet as ASL's own discovery sees it: every plan under "
            "the state root, its run, its lifecycle, whether it is eligible and "
            "the reasons it is not. Discovery only -- no run's content is read, "
            "so Agent Mux is not consulted and a plan listed as eligible here may "
            "still be refused the moment `asl overseer watch` asks about it. "
            "Exits nonzero when the view is partial, so a caller cannot read "
            "'nothing to do' out of 'could not look'."
        ),
    )
    overseer_scan_parser.add_argument(
        "--plan",
        dest="plans",
        action="append",
        metavar="PLAN_DIR",
        help="Limit the listing to this plan (repeatable)",
    )
    overseer_scan_parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        metavar="DIR",
        help="Limit the listing to runs inside this project directory (repeatable)",
    )
    overseer_scan_parser.add_argument(
        "--pause-window-hours",
        type=non_negative_float,
        metavar="HOURS",
        help="How far back a pause still counts as recent (default 24)",
    )
    overseer_scan_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    overseer_scan_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    overseer_scan_parser.set_defaults(handler_ref="overseer_cli:scan_from_args")

    overseer_watch_parser = overseer_sub.add_parser(
        "watch",
        help="Enroll the runs Agent Mux authorizes and hold a watch lease on each.",
        description=(
            "Sweep the fleet, ask Agent Mux about each plan in scope, and take a "
            "lease on the ones it authorizes. One sweep by default; "
            "`--for-seconds` keeps sweeping until the deadline, which is how "
            "runs that start mid-invocation and pauses that become eligible "
            "mid-invocation get picked up without re-invoking anything. "
            "`--release` hands every lease this watcher holds back, which is "
            "what ending an invocation means: an abandoned lease does expire, "
            "but not before it has kept another watcher out for its whole term. "
            "Exits nonzero when a sweep did not reach every plan it wanted to."
        ),
    )
    overseer_watch_parser.add_argument(
        "--watcher-id",
        required=True,
        type=non_blank_text,
        help="Stable identity for this invocation; leases are held under it",
    )
    overseer_watch_parser.add_argument(
        "--plan",
        dest="plans",
        action="append",
        metavar="PLAN_DIR",
        help="Watch only this plan (repeatable); naming a plan relaxes the age window",
    )
    overseer_watch_parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        metavar="DIR",
        help="Watch only runs inside this project directory (repeatable)",
    )
    overseer_watch_parser.add_argument(
        "--for-seconds",
        type=non_negative_float,
        metavar="SECONDS",
        help="Keep sweeping for this long instead of running a single sweep",
    )
    overseer_watch_parser.add_argument(
        "--interval-seconds",
        type=non_negative_float,
        metavar="SECONDS",
        default=15.0,
        help="Wait this long between sweeps under --for-seconds (default 15)",
    )
    overseer_watch_parser.add_argument(
        "--sweep-seconds",
        type=positive_float,
        metavar="SECONDS",
        default=60.0,
        help="Budget for one sweep; plans not reached are reported deferred",
    )
    overseer_watch_parser.add_argument(
        "--lease-seconds",
        type=positive_float,
        metavar="SECONDS",
        default=300.0,
        help="How long each watch lease is held before it expires (default 300)",
    )
    overseer_watch_parser.add_argument(
        "--pause-window-hours",
        type=non_negative_float,
        metavar="HOURS",
        help="How far back a pause still counts as recent (default 24)",
    )
    overseer_watch_parser.add_argument(
        "--release",
        action="store_true",
        help="Release every lease this watcher holds and exit",
    )
    overseer_watch_parser.add_argument(
        "--timeout",
        "--wait-timeout",
        dest="timeout",
        type=positive_float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to wait for each Agent Mux authorization (default 10)",
    )
    overseer_watch_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    overseer_watch_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    overseer_watch_parser.set_defaults(handler_ref="overseer_cli:watch_from_args")

    overseer_inspect_parser = overseer_sub.add_parser(
        "inspect",
        help="Open an incident and capture a bounded packet of a run's context.",
        description=(
            "Ask Agent Mux for context access, then capture what the host needs "
            "to reason about one run: the pending questions, the run's own "
            "summary of itself, and what could not be read. The packet is a "
            "summary, not a transcript. `--expand` widens exactly one named "
            "source at a time under a published character and record ceiling, "
            "so context grows because the host asked for something and not "
            "because a run happened to be verbose. The printed `scope` and "
            "`token` are what `asl overseer act` must be given later; they are "
            "captured here so a run that moves in between produces a stale "
            "action refusal instead of an action aimed at a state that is gone."
        ),
    )
    overseer_inspect_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Plan directory to inspect"
    )
    overseer_inspect_parser.add_argument(
        "--kind",
        default="question_pending",
        help="Why this incident is being opened (default question_pending)",
    )
    overseer_inspect_parser.add_argument(
        "--summary",
        default="",
        help="One line recording what the host is looking into",
    )
    overseer_inspect_parser.add_argument(
        "--incident-id",
        type=positive_int,
        metavar="ID",
        help="Reload this existing incident instead of opening a new one",
    )
    overseer_inspect_parser.add_argument(
        "--expand",
        action="append",
        # The closed vocabulary is overseer_context.SOURCE_KINDS; it is repeated
        # here rather than imported so `asl --help` stays off the heavy modules.
        choices=(
            "status",
            "questions",
            "run_config",
            "verification",
            "worker_changes",
            "supervision",
        ),
        help="Widen one recorded source of the packet (repeatable)",
    )
    overseer_inspect_parser.add_argument(
        "--limit",
        type=positive_int,
        metavar="CHARS",
        default=4000,
        help="Character ceiling for each expansion (default 4000)",
    )
    overseer_inspect_parser.add_argument(
        "--timeout",
        "--wait-timeout",
        dest="timeout",
        type=positive_float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to wait for the Agent Mux authorization (default 10)",
    )
    overseer_inspect_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    overseer_inspect_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    overseer_inspect_parser.set_defaults(handler_ref="overseer_cli:inspect_from_args")

    overseer_decide_parser = overseer_sub.add_parser(
        "decide",
        help="Resolve one operation against the decisions humans already made.",
        description=(
            "Reuse what a person already settled on this plan instead of "
            "settling it again. `--list-statements` prints the supervision "
            "record with the stable statement IDs a claim must cite along with "
            "the `evidence_digest` of that reading; the host reads what those "
            "statements mean and passes claims back through `--claims-file` or "
            "`--claims-stdin` together with that digest as "
            "`--reviewed-evidence`. Claims without it are refused "
            "`evidence_unbound`, because claims about a record nobody named are "
            "not a reading of anything. Everything after that is "
            "mechanical: the register rejects claims that cite nothing, widen "
            "their scope or name an unknown topic, and the ruling follows from "
            "what survives. A refusal is an escalation, printed with the "
            "question to put to a human, the work that stays blocked, and the "
            "other plans this invocation may keep working on meanwhile. There "
            "is no timeout and no default answer: silence is not consent."
        ),
    )
    overseer_decide_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Plan directory in question"
    )
    overseer_decide_parser.add_argument(
        "--operation",
        default="reply",
        choices=("reply", "guidance", "verify", "commit", "resume"),
        help="The operation to resolve (default reply)",
    )
    overseer_decide_parser.add_argument(
        "--incident-id",
        type=positive_int,
        metavar="ID",
        help="Narrow the scope to one open incident",
    )
    overseer_decide_parser.add_argument(
        "--raises",
        action="append",
        metavar="TOPIC",
        choices=_HUMAN_TOPICS,
        help=(
            "A human question this operation depends on; repeat per topic. A "
            "topic no in-scope decision settles refuses `human_decision` "
            "however the operation itself was allowed. Topics some claimed "
            "in-scope decision already names are raised anyway. "
            f"TOPIC is one of: {_HUMAN_TOPICS_HELP}"
        ),
    )
    overseer_decide_parser.add_argument(
        "--list-statements",
        action="store_true",
        help="Print the statements in scope with their IDs, then stop",
    )
    overseer_decide_parser.add_argument(
        "--claims-file",
        metavar="PATH",
        help="Read the host's claims about those statements from PATH",
    )
    overseer_decide_parser.add_argument(
        "--claims-stdin",
        action="store_true",
        help="Read the host's claims from standard input",
    )
    overseer_decide_parser.add_argument(
        "--reviewed-evidence",
        metavar="DIGEST",
        help=(
            "The `evidence_digest` the `--list-statements` reading these "
            "claims were written from reported. Required with claims: without "
            "it the evaluation is refused `evidence_unbound`, and if human "
            "evidence in scope moved since that reading it is refused "
            "`evidence_changed` instead of ruling on a stale reading"
        ),
    )
    overseer_decide_parser.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        default=True,
        help=(
            "Rule on the mailbox alone. Nothing can authorize under this: a "
            "mailbox record shows an instruction arrived, never who wrote it"
        ),
    )
    overseer_decide_parser.add_argument(
        "--generated-class",
        dest="generated_classes",
        action="append",
        metavar="CLASS",
        help=(
            "A further conversation class whose `user` turns are written by "
            "another agent; repeat per class. Only `main` sessions authorize "
            "anyway, so this restricts and never widens"
        ),
    )
    overseer_decide_parser.add_argument(
        "--timeout",
        "--wait-timeout",
        dest="timeout",
        type=positive_float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to wait for the Agent Mux authorization (default 10)",
    )
    overseer_decide_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    overseer_decide_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    overseer_decide_parser.set_defaults(handler_ref="overseer_cli:decide_from_args")

    overseer_act_parser = overseer_sub.add_parser(
        "act",
        help="Hand one authorized action to the guarded dispatcher.",
        description=(
            "The only leaf that changes anything, and it changes it only "
            "through the guarded dispatcher: the operation is refused unless "
            "the plan is still observed, the run is the one that was read, the "
            "scope has not widened, this watcher still holds the lease, the "
            "action token still matches and a ruling still authorizes it. "
            "`--token` and `--scope` come from the earlier `asl overseer "
            "inspect` and are never recomputed here, because a token recomputed "
            "now would agree with itself. `--reviewed-evidence` binds the "
            "claims to the record they were read from, so a hold that arrived "
            "since is refused `evidence_changed` rather than resolved past. It "
            "is required whenever claims are passed: unbound claims are refused "
            "`evidence_unbound` rather than pinned to whatever this invocation "
            "happens to read. The operational scope is reply, "
            "guidance, verify, commit and resume; anything else, and anything "
            "no adapter implements, is refused and escalated rather than "
            "improvised. `--check` names a check the session's completion "
            "contract already declares and the argv comes from that contract, "
            "so an undeclared name is refused rather than run. "
            "Application-code repairs belong to a worker, not here."
        ),
    )
    overseer_act_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Plan directory to act on"
    )
    overseer_act_parser.add_argument(
        "--watcher-id",
        required=True,
        type=non_blank_text,
        help="The watcher holding the lease on this run",
    )
    overseer_act_parser.add_argument(
        "--operation",
        required=True,
        choices=("reply", "guidance", "verify", "commit", "resume"),
        help="The operation to dispatch",
    )
    overseer_act_parser.add_argument(
        "--run-id",
        required=True,
        help="The run ID `asl overseer inspect` reported",
    )
    overseer_act_parser.add_argument(
        "--incident-id",
        type=positive_int,
        metavar="ID",
        required=True,
        help="The incident this action answers",
    )
    overseer_act_parser.add_argument(
        "--token",
        required=True,
        help="The action token `asl overseer inspect` reported",
    )
    overseer_act_parser.add_argument(
        "--scope",
        required=True,
        help="The scope digest `asl overseer inspect` reported",
    )
    overseer_act_parser.add_argument(
        "--raises",
        action="append",
        metavar="TOPIC",
        choices=_HUMAN_TOPICS,
        help=(
            "A human question this action depends on; repeat per topic. It is "
            "carried into every authority recheck, and a claimed in-scope hold "
            "on any human topic is carried whether or not it is named here. "
            f"TOPIC is one of: {_HUMAN_TOPICS_HELP}"
        ),
    )
    overseer_act_parser.add_argument(
        "--question-id",
        help="For reply: the question being answered",
    )
    overseer_act_parser.add_argument(
        "--text",
        help="For reply and guidance: the message body",
    )
    overseer_act_parser.add_argument(
        "--check",
        help="For verify: the named check to run",
    )
    overseer_act_parser.add_argument(
        "--message",
        help="For commit: the checkpoint message",
    )
    overseer_act_parser.add_argument(
        "--session-id",
        # `--session` is how every other command names one session.
        "--session",
        dest="session_id",
        metavar="SESSION_ID",
        help="For resume: the session to resume (default the run's own next)",
    )
    overseer_act_parser.add_argument(
        "--claims-file",
        metavar="PATH",
        help="Read the host's claims from PATH; authority is rechecked from them",
    )
    overseer_act_parser.add_argument(
        "--claims-stdin",
        action="store_true",
        help="Read the host's claims from standard input",
    )
    overseer_act_parser.add_argument(
        "--reviewed-evidence",
        metavar="DIGEST",
        help=(
            "The `evidence_digest` the `asl overseer decide` that produced "
            "these claims reported. Required with claims: without it the "
            "action is refused `evidence_unbound`, and it is refused "
            "`evidence_changed` if human evidence in scope moved since that "
            "reading"
        ),
    )
    overseer_act_parser.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        default=True,
        help=(
            "Recheck authority against the mailbox alone, which authorizes "
            "nothing; use it to prove an action does not need the transcript"
        ),
    )
    overseer_act_parser.add_argument(
        "--generated-class",
        dest="generated_classes",
        action="append",
        metavar="CLASS",
        help=(
            "A further conversation class whose `user` turns are written by "
            "another agent; repeat per class. Only `main` sessions authorize "
            "anyway, so this restricts and never widens"
        ),
    )
    overseer_act_parser.add_argument(
        "--timeout",
        "--wait-timeout",
        dest="timeout",
        type=positive_float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to wait for each Agent Mux authorization (default 10)",
    )
    overseer_act_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    overseer_act_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    overseer_act_parser.set_defaults(handler_ref="overseer_cli:act_from_args")

    overseer_reconcile_parser = overseer_sub.add_parser(
        "reconcile",
        help="Settle actions a previous dispatch left in doubt.",
        description=(
            "A dispatch that dies between performing an action and recording "
            "its receipt leaves a row saying 'maybe'. Only the adapter that "
            "would have performed it can answer, and it answers from the ledger "
            "row alone because the process that held the action is gone. Run "
            "this before acting again: it is what keeps a retry from becoming a "
            "duplicate. Exits nonzero while any row is still in doubt."
        ),
    )
    overseer_reconcile_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Plan directory to reconcile"
    )
    overseer_reconcile_parser.add_argument(
        "--timeout",
        "--wait-timeout",
        dest="timeout",
        type=positive_float,
        default=10.0,
        metavar="SECONDS",
        help="Seconds to wait for each Agent Mux authorization (default 10)",
    )
    overseer_reconcile_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    overseer_reconcile_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    overseer_reconcile_parser.set_defaults(
        handler_ref="overseer_cli:reconcile_from_args"
    )
