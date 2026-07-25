"""Command-line interface for agent-session-loop."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# Only modules whose constants/helpers are needed to *construct* the parser are
# imported eagerly, and all of these are light (they do not pull in the runner,
# workbench, or review subsystems). Every subcommand handler is resolved lazily
# from its `handler_ref` in `resolve_handler()` so that a light command such as
# `asl status` never imports the ~21k-line handler graph just to dispatch.
from . import contracts, harnesses, run_flags, session_overview, tail

# Build-time defaults mirrored from heavier modules so light commands do not
# import `metrics` (pulls in review/compact-state) or `workbench_server` (pulls
# in the whole workbench subsystem) merely to build the parser. The
# `test_cli_build_time_default_constants_match_source_modules` guard keeps these
# in sync with their authoritative definitions.
_WORKBENCH_DEFAULT_HOST = "127.0.0.1"  # workbench_server.DEFAULT_HOST
_WORKBENCH_DEFAULT_PORT = 8765  # workbench_server.DEFAULT_PORT
_METRICS_DEFAULT_SINCE_DAYS = 7  # metrics.DEFAULT_SINCE_DAYS


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
        parsed = _normalize_workbench_serve_args(parsed)
        # Record the exact tokens that produced this namespace so lazy handler
        # dispatch (e.g. `runner.run_from_args`) can detect which flags were
        # passed explicitly without falling back to the process-global
        # `sys.argv`. This keeps `--resume` explicit-flag provenance correct
        # through `cli.main(argv)` and direct resolved-handler dispatch.
        if not hasattr(parsed, "_run_argv"):
            parsed._run_argv = list(sys.argv[1:] if args is None else args)
        return parsed


def _add_workbench_serve_args(
    parser: argparse.ArgumentParser, *, child_parser: bool = False
) -> None:
    parser.add_argument(
        "--host",
        dest=_workbench_dest("host", child_parser=child_parser),
        default=_workbench_default(
            _WORKBENCH_DEFAULT_HOST, child_parser=child_parser
        ),
        metavar="HOST",
        help=(
            f"Host interface to bind (default: {_WORKBENCH_DEFAULT_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        dest=_workbench_dest("port", child_parser=child_parser),
        type=int,
        default=_workbench_default(
            _WORKBENCH_DEFAULT_PORT, child_parser=child_parser
        ),
        metavar="PORT",
        help=(
            f"TCP port to bind (default: {_WORKBENCH_DEFAULT_PORT}; "
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


def _add_plan_reference_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR_OR_ID",
        help=(
            "Split plan directory to control, or the compact plan label "
            "printed after 'plan' by 'asl sessions'"
        ),
    )
    parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
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
    run_flags.add_run_arguments(run_parser, mode="cli")
    run_parser.set_defaults(handler_ref="runner:run_from_args")

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
        "plan_dir", help="Split plan directory whose review verdict to inspect"
    )
    review_verdict_parser.add_argument(
        "--state-dir",
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
        help="Show session ID and operational runner status.",
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
        help="Override operational state directory",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    status_parser.set_defaults(handler_ref="state:status_from_args")

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
    doctor_parser.set_defaults(handler_ref="runner:doctor_from_args")

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
    verify_parser.set_defaults(handler_ref="verification:verify_from_args")

    contract_check_parser = subparsers.add_parser(
        "contract-check",
        help=(
            "Statically diagnose each session's completion-contract Verify "
            "recipe without running any command."
        ),
    )
    contract_check_parser.add_argument(
        "plan_dir", help="Split plan directory to inspect"
    )
    contract_check_parser.add_argument(
        "--session",
        default=None,
        help=(
            "Restrict the check to a single session id (single-file mode; "
            "default: every session in the plan)"
        ),
    )
    contract_check_parser.add_argument(
        "--cwd",
        default=".",
        help="Repository root for file/regex/command checks (default: current directory)",
    )
    contract_check_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    contract_check_parser.set_defaults(
        handler_ref="verification:contract_check_from_args"
    )

    baseline_parser = subparsers.add_parser(
        "baseline",
        help=(
            "Run each session's baseline-expectation checks and record "
            "pass/fail/timeout evidence for the preflight readiness gate."
        ),
    )
    baseline_parser.add_argument(
        "plan_dir", help="Split plan directory whose baseline checks to run"
    )
    baseline_parser.add_argument(
        "--cwd",
        default=".",
        help="Repository root to run baseline checks in (default: current directory)",
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

    split_parser = subparsers.add_parser(
        "split",
        help=(
            "Split a multi-session plan into a collision-safe per-session "
            "directory using the deterministic authoring API."
        ),
    )
    split_source = split_parser.add_mutually_exclusive_group(required=True)
    split_source.add_argument(
        "--plan-file",
        dest="plan_file",
        help="Path to the source plan markdown file to split",
    )
    split_source.add_argument(
        "--inline-stdin",
        dest="inline_stdin",
        action="store_true",
        help=(
            "Capture the source plan from stdin (the last <proposed_plan> "
            "block, else the raw input) as a durable source file"
        ),
    )
    split_parser.add_argument(
        "--base-dir",
        dest="base_dir",
        default=None,
        help=(
            "Base directory for the captured source and split output "
            "(default: the persistent agent-plans directory)"
        ),
    )
    split_parser.add_argument(
        "--contracts-file",
        dest="contracts_file",
        required=True,
        help=(
            "JSON file of completion contracts keyed by canonical session id "
            "({\"1\": {\"done_when\": [...], \"verify\": {...}}, ...})"
        ),
    )
    split_parser.add_argument(
        "--skill-prefix",
        dest="skill_prefix",
        choices=("auto", "codex", "claude", "copilot"),
        default="auto",
        help=(
            "Skill-invocation prefix for generated files (auto detects the "
            "invoking harness; errors if undetectable)"
        ),
    )
    split_parser.add_argument(
        "--analysis-file",
        dest="analysis_file",
        default=None,
        help="Optional markdown appended verbatim to the manifest execution note",
    )
    split_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    split_parser.set_defaults(handler_ref="authoring:split_from_args")

    prepare_parser = subparsers.add_parser(
        "prepare",
        help=(
            "Split, validate, baseline, and mark a source plan loop-ready "
            "in one deterministic pipeline."
        ),
    )
    prepare_parser.add_argument(
        "plan",
        help=(
            "Source plan markdown file to prepare (or an existing split "
            "plan directory with --resume)"
        ),
    )
    prepare_parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Treat PLAN as an existing plan directory and rerun validation "
            "and baselines without creating another split"
        ),
    )
    prepare_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Repository root the prepared plan will run against "
            "(default: current directory)"
        ),
    )
    prepare_parser.add_argument(
        "--harness",
        choices=harnesses.HARNESS_NAMES,
        default="codex",
        help="Implementation harness for the resulting `asl run` command",
    )
    prepare_parser.add_argument(
        "--validator-harness",
        dest="validator_harness",
        choices=harnesses.HARNESS_NAMES,
        default="codex",
        help="Harness used to run preparation validation workers",
    )
    prepare_parser.add_argument(
        "--review-harness",
        choices=harnesses.HARNESS_NAMES,
        default=None,
        help=(
            "Review harness for the resulting `asl run` command; "
            "must differ from --harness"
        ),
    )
    prepare_parser.add_argument(
        "--harness-model",
        dest="harness_model",
        help="Implementation harness model for the resulting `asl run` command",
    )
    prepare_parser.add_argument(
        "--review-harness-model",
        dest="review_harness_model",
        help="Review harness model for the resulting `asl run` command",
    )
    prepare_parser.add_argument(
        "--base-dir",
        dest="base_dir",
        default=None,
        help=(
            "Base directory for the split plan output "
            "(default: the persistent agent-plans directory)"
        ),
    )
    prepare_parser.add_argument(
        "--state-dir",
        default=None,
        help="Override operational state directory",
    )
    _add_unsafe_bypass_flags(prepare_parser)
    prepare_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    prepare_parser.set_defaults(handler_ref="prepare:prepare_from_args")

    expand_ingest_parser = subparsers.add_parser(
        "expand-ingest",
        help=(
            "Ingest one validator-output batch file: merge it into the stored "
            "aggregate, render its expansions, and apply its factual corrections."
        ),
    )
    expand_ingest_parser.add_argument(
        "plan_dir", help="Split plan directory to ingest validator output into"
    )
    expand_ingest_parser.add_argument(
        "--batch-file",
        dest="batch_file",
        required=True,
        help="JSON file of validator output for one batch of sessions",
    )
    expand_ingest_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    expand_ingest_parser.set_defaults(handler_ref="prepare:expand_ingest_from_args")

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
        "--max-pending-sessions",
        type=int,
        help=(
            "Override the pending-session count allowed before "
            "env-turn-budget turns yellow (default: 24)"
        ),
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
    preflight_parser.set_defaults(handler_ref="preflight:preflight_from_args")

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

    migrate_state_parser = subparsers.add_parser(
        "migrate-state",
        help=(
            "Relocate an orphaned state directory after a plan directory move "
            "(default: dry-run)."
        ),
    )
    migrate_state_parser.add_argument(
        "plan_dir", help="Current (new) plan directory whose state to restore"
    )
    migrate_state_parser.add_argument(
        "--from",
        dest="from_plan_dir",
        required=True,
        help="Old plan directory path the state was keyed under",
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
    migrate_state_parser.set_defaults(handler_ref="state:migrate_state_from_args")

    metrics_parser = subparsers.add_parser(
        "metrics",
        help=(
            "Report workflow reliability and storage metrics from the "
            "persisted state corpus (default window: last 7 days)."
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
    unlock_parser.set_defaults(handler_ref="state:unlock_from_args")

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
        handler_ref="progress_cli:backfill_progress_from_args"
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
    progress_parser.set_defaults(handler_ref="progress_cli:progress_from_args")

    workbench_parser = subparsers.add_parser(
        "workbench",
        help="Run the local Workbench HTTP control plane.",
    )
    _add_workbench_serve_args(workbench_parser)
    workbench_parser.set_defaults(
        workbench_command=None,
        handler_ref="workbench_server:workbench_serve_from_args",
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
        handler_ref="workbench_server:workbench_serve_from_args",
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
        handler_ref="workbench:workbench_pin_from_args",
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
        handler_ref="workbench:workbench_unpin_from_args",
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
        handler_ref="workbench:workbench_registry_from_args",
    )

    control_parser = subparsers.add_parser(
        "control",
        help=(
            "Queue a control command for the runner that owns a plan "
            "(works for --json runs, where the run-control keys are off)."
        ),
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
    control_stop_parser.set_defaults(
        handler_ref="control_cli:control_stop_after_current_from_args",
    )

    control_select_harness_parser = control_sub.add_parser(
        "select-harness",
        help="Choose the harness future sessions of a run should use.",
    )
    _add_plan_reference_args(control_select_harness_parser)
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
            "Set or clear the model and effort used by the next "
            "implementation turn."
        ),
    )
    _add_plan_reference_args(control_set_model_parser)
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

    inbox_parser = subparsers.add_parser(
        "inbox",
        help="Write supervisor messages or read pending worker guidance.",
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
    inbox_guidance_parser.set_defaults(
        handler_ref="mailbox_cli:inbox_guidance_from_args",
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
    inbox_reply_parser.set_defaults(
        handler_ref="mailbox_cli:inbox_reply_from_args",
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
        handler_ref="mailbox_cli:inbox_read_from_args",
    )

    ask_parser = subparsers.add_parser(
        "ask",
        help="Append or resume a worker question and optionally wait for a reply.",
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
        help="Wait for the first reply",
    )
    ask_parser.add_argument(
        "--wait-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Reply wait timeout in seconds; requires --wait "
            "(default: 90; maximum: 900)"
        ),
    )
    ask_parser.set_defaults(handler_ref="mailbox_cli:ask_from_args")

    outbox_parser = subparsers.add_parser(
        "outbox",
        help="Read worker questions from the supervisor side.",
    )
    outbox_sub = outbox_parser.add_subparsers(
        dest="outbox_command",
        required=True,
    )
    outbox_wait_parser = outbox_sub.add_parser(
        "wait",
        help="Wait for questions after a physical mailbox sequence.",
    )
    _add_plan_reference_args(outbox_wait_parser)
    outbox_wait_parser.add_argument(
        "--since",
        type=int,
        default=0,
        metavar="SEQ",
        help="Return questions after this physical sequence (default: 0)",
    )
    outbox_wait_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="Long-poll timeout in seconds (default: 300; maximum: 900)",
    )
    outbox_wait_parser.set_defaults(
        handler_ref="mailbox_cli:outbox_wait_from_args",
    )

    return parser


def resolve_handler(
    args: argparse.Namespace,
) -> Callable[[argparse.Namespace], int]:
    """Import and return the handler named by ``args.handler_ref``.

    Handlers are stored on the parser as ``"module:function"`` strings and only
    resolved when a command actually dispatches, so building or parsing the CLI
    never imports the heavy handler modules (runner, workbench, review, ...).
    """
    module_name, _, attr = args.handler_ref.partition(":")
    module = importlib.import_module(f".{module_name}", __package__)
    return getattr(module, attr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(prog=Path(sys.argv[0]).name)
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    return resolve_handler(args)(args)
