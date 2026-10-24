"""Parser definitions for the assurance commands."""

from __future__ import annotations

import argparse

from .cli_parser_support import _AgentSessionLoopArgumentParser, _group_epilogue


def _add_assurance_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl assurance` group."""
    assurance_parser = subparsers.add_parser(
        "assurance",
        help="Browse the assurance inbox of audited runs and open issues.",
        epilog=_group_epilogue("assurance", "asl assurance list --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    assurance_sub = assurance_parser.add_subparsers(
        dest="assurance_command",
        required=True,
    )

    assurance_list_parser = assurance_sub.add_parser(
        "list",
        help="List inbox issues, most recently observed first.",
    )
    assurance_list_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="ROWS",
        help="Maximum rows to print (default: 50)",
    )
    assurance_list_parser.add_argument(
        "--all",
        action="store_true",
        help="Print every issue in every state, with no row cap",
    )
    assurance_list_parser.add_argument(
        "--state",
        metavar="open|resolved|dismissed|accepted-risk",
        help="Restrict rows to one lifecycle state (default: open)",
    )
    assurance_list_parser.add_argument(
        "--severity",
        metavar="low|medium|high|critical",
        help="Restrict rows to one severity (default: any)",
    )
    assurance_list_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_list_parser.set_defaults(
        handler_ref="assurance_cli:assurance_list_from_args",
    )

    assurance_show_parser = assurance_sub.add_parser(
        "show",
        help="Print the full history the ledger records for one issue.",
    )
    assurance_show_parser.add_argument(
        "issue",
        metavar="ISSUE",
        help="Issue to print, as the canonical 'ASI-42' or the shorthand '42'",
    )
    assurance_show_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_show_parser.set_defaults(
        handler_ref="assurance_cli:assurance_show_from_args",
    )

    for disposition, effect in (
        ("resolve", "fixed, so a later recurrence reopens it"),
        ("dismiss", "not worth acting on"),
        ("accept-risk", "understood and accepted as it is"),
    ):
        disposition_parser = assurance_sub.add_parser(
            disposition,
            help=f"Record that an issue is {effect}.",
        )
        disposition_parser.add_argument(
            "issue",
            metavar="ISSUE",
            help="Issue to dispose of, as the canonical 'ASI-42' or the shorthand '42'",
        )
        disposition_parser.add_argument(
            "--note",
            metavar="TEXT",
            help="Reason to store alongside the decision",
        )
        disposition_parser.add_argument(
            "--state-dir",
            metavar="DIR",
            help="Override operational state directory",
        )
        disposition_parser.add_argument(
            "--json",
            action="store_true",
            help="Print machine-readable JSON output",
        )
        disposition_parser.set_defaults(
            disposition_kind=disposition,
            handler_ref="assurance_cli:assurance_disposition_from_args",
        )

    assurance_merge_parser = assurance_sub.add_parser(
        "merge",
        help="Move one issue's whole history into another, irreversibly.",
    )
    assurance_merge_parser.add_argument(
        "issue",
        metavar="ISSUE",
        help="Issue whose history moves, as 'ASI-42' or the shorthand '42'",
    )
    assurance_merge_parser.add_argument(
        "target",
        metavar="TARGET",
        help="Issue that absorbs it, as 'ASI-42' or the shorthand '42'",
    )
    assurance_merge_parser.add_argument(
        "--note",
        metavar="TEXT",
        help="Reason to store alongside the merge",
    )
    assurance_merge_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation for this irreversible merge",
    )
    assurance_merge_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_merge_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_merge_parser.set_defaults(
        handler_ref="assurance_cli:assurance_merge_from_args",
    )

    assurance_closeout_parser = assurance_sub.add_parser(
        "closeout",
        help="Record what one audited run's findings were worth.",
    )
    assurance_closeout_parser.add_argument(
        "run_id",
        metavar="RUN_ID",
        help="Run to close out, as printed by 'asl run' and 'asl status'",
    )
    assurance_closeout_parser.add_argument(
        "--outcome",
        required=True,
        metavar="accepted|reworked|escaped",
        help="What the audit was worth on this run",
    )
    assurance_closeout_parser.add_argument(
        "--rework-minutes",
        type=float,
        default=None,
        metavar="N",
        help="Minutes of rework the findings caused",
    )
    assurance_closeout_parser.add_argument(
        "--issue",
        action="append",
        default=[],
        metavar="ISSUE",
        help="Issue this closeout judges; repeat for several",
    )
    assurance_closeout_parser.add_argument(
        "--note",
        metavar="TEXT",
        help="Reason to store alongside the closeout",
    )
    assurance_closeout_parser.add_argument(
        "--plan",
        metavar="PLAN_DIR_OR_ID",
        help="Plan the run belongs to; required only when the run id is ambiguous",
    )
    assurance_closeout_parser.add_argument(
        "--replace",
        action="store_true",
        help="Supersede the run's existing closeout with this one",
    )
    assurance_closeout_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_closeout_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_closeout_parser.set_defaults(
        handler_ref="assurance_cli:assurance_closeout_from_args",
    )

    assurance_pending_parser = assurance_sub.add_parser(
        "process-pending",
        help="Audit runs whose own process died before it could audit them.",
    )
    assurance_pending_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="RUNS",
        help="Maximum runs to audit in this pass (default: 20)",
    )
    assurance_pending_parser.add_argument(
        "--run-id",
        metavar="RUN_ID",
        help=(
            "Audit only this run, even if its record says to skip it or its "
            "only published audit was unavailable"
        ),
    )
    assurance_pending_parser.add_argument(
        "--plan",
        metavar="PLAN_DIR_OR_ID",
        help="Restrict the pass to one plan's runs",
    )
    assurance_pending_parser.add_argument(
        "--audit-harness",
        metavar="HARNESS",
        help="Audit harness to use instead of the one the dead run recorded",
    )
    assurance_pending_parser.add_argument(
        "--audit-harness-model",
        metavar="MODEL",
        help="Audit harness model to use instead of the recorded one",
    )
    assurance_pending_parser.add_argument(
        "--audit-effort",
        metavar="EFFORT",
        help="Audit reasoning effort to use instead of the recorded one",
    )
    assurance_pending_parser.add_argument(
        "--audit-timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Audit turn timeout to use instead of the recorded one",
    )
    assurance_pending_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_pending_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_pending_parser.set_defaults(
        handler_ref="assurance_cli:assurance_process_pending_from_args",
    )

    assurance_plan_parser = assurance_sub.add_parser(
        "plan",
        help="Draft an improvement proposal for one issue, for a human to review.",
    )
    assurance_plan_parser.add_argument(
        "issue",
        metavar="ISSUE",
        help="Issue to draft a proposal for (ASI-42 or 42)",
    )
    assurance_plan_parser.add_argument(
        "--harness",
        required=True,
        metavar="HARNESS",
        help="Harness that drafts the proposal, run read-only",
    )
    assurance_plan_parser.add_argument(
        "--output",
        required=True,
        metavar="PATH",
        help="Where to write the proposal; an existing path is refused",
    )
    assurance_plan_parser.add_argument(
        "--harness-model",
        metavar="MODEL",
        help="Model the drafting harness should use",
    )
    assurance_plan_parser.add_argument(
        "--effort",
        metavar="EFFORT",
        help="Reasoning effort for the drafting turn",
    )
    assurance_plan_parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Drafting turn timeout (default: 600)",
    )
    assurance_plan_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_plan_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_plan_parser.set_defaults(
        handler_ref="assurance_cli:assurance_plan_from_args",
    )

    assurance_reconcile_parser = assurance_sub.add_parser(
        "reconcile",
        help=("Preview the inbox's identity repairs, or apply a reviewed manifest."),
    )
    assurance_reconcile_parser.add_argument(
        "--apply",
        metavar="MANIFEST",
        help="Apply the reviewed decisions in this manifest instead of previewing",
    )
    assurance_reconcile_parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not ask before applying merges, which cannot be undone",
    )
    assurance_reconcile_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    assurance_reconcile_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    assurance_reconcile_parser.set_defaults(
        handler_ref="assurance_cli:assurance_reconcile_from_args",
    )
