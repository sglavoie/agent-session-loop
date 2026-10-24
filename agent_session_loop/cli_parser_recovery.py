"""Parser definitions for the workspace recovery and progress commands."""

from __future__ import annotations

import argparse

from . import harnesses
from .cli_defaults import _COMMIT_DEFAULT_GENERATION_TIMEOUT_SECONDS
from .cli_parser_support import _AgentSessionLoopArgumentParser


def _add_workspace_recovery_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """Workspace recovery: commit, external changes, ref updates, unlock."""
    commit_parser = subparsers.add_parser(
        "commit",
        help=(
            "Generate a Conventional Commits message from the diff and commit "
            "it, standalone or inside a loop."
        ),
    )
    commit_parser.add_argument(
        "path",
        nargs="?",
        default=None,
        metavar="PATH",
        help="Path inside the git worktree to commit (default: current directory)",
    )
    commit_parser.add_argument(
        "--yes",
        action="store_true",
        help="Commit without asking for confirmation",
    )
    commit_parser.set_defaults(verify=False)
    commit_parser.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        help="Run git hooks on commit",
    )
    commit_parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="Bypass git hooks on commit (default)",
    )
    commit_parser.add_argument(
        "--staged",
        action="store_true",
        help="Commit only what is already staged, leaving the index untouched",
    )
    commit_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print the message and stop without committing",
    )
    commit_parser.add_argument(
        "--guidance",
        metavar="TEXT",
        help="Extra instructions passed to the message generator",
    )
    commit_parser.add_argument(
        "--from-id",
        dest="from_id",
        metavar="SHA",
        help="Print a message describing an existing commit; never commits",
    )
    commit_parser.add_argument(
        "--pathspec",
        action="append",
        metavar="PATH",
        help=("Stage only this repo-relative path instead of every change; repeatable"),
    )
    commit_parser.add_argument(
        "--expect-snapshot",
        dest="expect_snapshot",
        metavar="TOKEN",
        help=(
            "Refuse the commit unless the --pathspec paths still hold the "
            "snapshot a previous --prepare-only call reported"
        ),
    )
    commit_parser.add_argument(
        "--harness",
        choices=harnesses.HARNESS_NAMES,
        help="Agent harness that generates the message",
    )
    commit_parser.add_argument(
        "--model",
        help="Harness model to request",
    )
    commit_parser.add_argument(
        "--effort",
        help="Harness reasoning effort to request",
    )
    commit_parser.add_argument(
        "--timeout",
        type=int,
        metavar="SECONDS",
        default=_COMMIT_DEFAULT_GENERATION_TIMEOUT_SECONDS,
        help=(
            "Bound the message-generation harness turn, in seconds "
            f"(default: {_COMMIT_DEFAULT_GENERATION_TIMEOUT_SECONDS})"
        ),
    )
    commit_parser.add_argument(
        "--prepare-only",
        dest="prepare_only",
        action="store_true",
        help=(
            "Emit a single-line JSON header, a blank line, and the budgeted "
            "diff, then stop"
        ),
    )
    commit_parser.add_argument(
        "--message-file",
        dest="message_file",
        metavar="PATH",
        help="Commit this message instead of generating one ('-' reads stdin)",
    )
    commit_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    commit_parser.set_defaults(handler_ref="commit_message:commit_from_args")

    external_parser = subparsers.add_parser(
        "acknowledge-external-changes",
        help="Preview or acknowledge independent changes and checkpoint retained work.",
    )
    external_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory"
    )
    external_parser.add_argument("--session", required=True, help="Pending session ID")
    external_parser.add_argument(
        "--repository",
        action="append",
        required=True,
        metavar="ALIAS",
        help="Repository alias whose independent changes to review, repeatable",
    )
    external_parser.add_argument(
        "--state-dir", metavar="DIR", help="Override operational state directory"
    )
    external_parser.add_argument(
        "--json", action="store_true", help="Print JSON (also the default)"
    )
    external_parser.add_argument(
        "--accept",
        metavar="DIGEST",
        help="Accept the exact evidence digest returned by a reviewed preview",
    )
    external_parser.add_argument(
        "--reason", help="Explicit operator reason for acknowledging these changes"
    )
    external_parser.add_argument(
        "--checkpoint-retained",
        action="store_true",
        help=(
            "Commit the retained work the preview names as checkpoints "
            "(required when it lists any)"
        ),
    )
    external_parser.set_defaults(
        handler_ref="scope_recovery:acknowledge_external_changes_from_args"
    )

    accept_refs_parser = subparsers.add_parser(
        "accept-ref-updates",
        help="Preview or explicitly accept a paused session's remote-ref-only changes.",
    )
    accept_refs_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory"
    )
    accept_refs_parser.add_argument(
        "--session", required=True, help="Pending session ID"
    )
    accept_refs_parser.add_argument(
        "--state-dir", metavar="DIR", help="Override operational state directory"
    )
    accept_refs_parser.add_argument(
        "--json", action="store_true", help="Print JSON (also the default)"
    )
    accept_refs_parser.add_argument(
        "--accept",
        metavar="DIGEST",
        help="Accept the exact evidence digest returned by a reviewed preview",
    )
    accept_refs_parser.add_argument(
        "--reason", help="Explicit operator reason for retaining these updates"
    )
    accept_refs_parser.set_defaults(
        handler_ref="scope_recovery:accept_ref_updates_from_args"
    )

    unlock_parser = subparsers.add_parser(
        "unlock",
        help="Remove an operational lock for a plan directory.",
    )
    unlock_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory to unlock"
    )
    unlock_parser.add_argument(
        "--state-dir",
        metavar="DIR",
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
    unlock_parser.set_defaults(handler_ref="state_cli_handlers:unlock_from_args")


def _add_progress_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """Progress journal and plan listing: backfill-progress, progress, plans."""
    backfill_parser = subparsers.add_parser(
        "backfill-progress",
        help=(
            "Reconstruct progress.md from completed session handoffs "
            "(timestamps approximated from session file mtimes)."
        ),
    )
    backfill_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        help="Split plan directory whose progress journal to rebuild",
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
    backfill_parser.set_defaults(handler_ref="progress_cli:backfill_progress_from_args")

    progress_parser = subparsers.add_parser(
        "progress",
        help="Print, JSON-dump, or follow the runner-managed progress journal.",
    )
    progress_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        help="Split plan directory whose progress journal to inspect",
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
    progress_parser.set_defaults(handler_ref="progress_cli:progress_from_args")

    plans_parser = subparsers.add_parser(
        "plans",
        help=(
            "List discovered plan directories, with filters for stale, "
            "never-run, or specific readiness states."
        ),
    )
    plans_parser.add_argument(
        "--state-dir",
        metavar="DIR",
        help="Override operational state directory",
    )
    plans_parser.add_argument(
        "--stale-days",
        type=float,
        metavar="DAYS",
        help=(
            "Only show plans whose latest activity, or readiness check when "
            "never launched, is at least this many days old"
        ),
    )
    plans_parser.add_argument(
        "--never-run",
        action="store_true",
        help=(
            "Only show plans whose state_path file is absent (no run state on disk yet)"
        ),
    )
    plans_parser.add_argument(
        "--readiness",
        help=(
            "Only show plans whose manifest readiness state matches this value "
            "(for example: draft, reviewed, loop-ready)"
        ),
    )
    plans_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    plans_parser.set_defaults(handler_ref="workbench_cli:plans_from_args")
