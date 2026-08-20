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
_RETENTION_DEFAULT_MAX_AGE_DAYS = 30.0  # retention.DEFAULT_MAX_AGE_DAYS
_PREPARE_DEFAULT_VALIDATOR_JOBS = 2  # prepare.DEFAULT_VALIDATOR_JOBS
_PREPARE_DEFAULT_VALIDATOR_TIMEOUT_SECONDS = (
    900  # prepare.DEFAULT_VALIDATOR_TIMEOUT_SECONDS
)
_PREPARE_DEFAULT_VALIDATOR_HEARTBEAT_SECONDS = (
    30  # prepare.DEFAULT_VALIDATOR_HEARTBEAT_SECONDS
)
_CONTROL_DEFAULT_ABORT_GRACE_SECONDS = 120  # control_queue.DEFAULT_ABORT_GRACE_SECONDS
_CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS = (
    60.0  # control_cli.DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS
)
_INTERRUPTED_EXIT = 130  # runner.INTERRUPTED_EXIT
_BRIDGE_PROTOCOL_VERSION = 1  # bridge.PROTOCOL_VERSION


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
        default=_workbench_default(_WORKBENCH_DEFAULT_HOST, child_parser=child_parser),
        metavar="HOST",
        help=(f"Host interface to bind (default: {_WORKBENCH_DEFAULT_HOST})"),
    )
    parser.add_argument(
        "--port",
        dest=_workbench_dest("port", child_parser=child_parser),
        type=int,
        default=_workbench_default(_WORKBENCH_DEFAULT_PORT, child_parser=child_parser),
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
            "Extra plan root directory to scan for discoverable plans (may be repeated)"
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


def _add_control_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for the runner's receipt instead of firing and forgetting",
    )
    parser.add_argument(
        "--timeout",
        # Bound to the same dest the mailbox wait uses, so the value is never
        # confused with an unrelated `timeout` on a shared namespace.
        dest="wait_timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help=(
            "Receipt wait timeout in seconds; requires --wait "
            f"(default: {_CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS:g}; "
            "maximum: 1800)"
        ),
    )


def _add_unsafe_bypass_flags(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(unsafe_bypass=True)
    parser.add_argument(
        "--unsafe-bypass",
        dest="unsafe_bypass",
        action="store_true",
        help="Use harness-specific bypass flags for sandbox and approval prompts "
        "(default)",
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
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan (default: current directory)"
        ),
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
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan (default: current directory)"
        ),
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
        "--force-new",
        action="store_true",
        help=(
            "Allow an intentional duplicate split by creating the next "
            "numbered sibling directory instead of refusing the existing plan id"
        ),
    )
    split_parser.add_argument(
        "--check",
        dest="check",
        action="store_true",
        help=(
            "Lint the source plan's session headings and exit without writing "
            "anything; reports every malformed heading at once"
        ),
    )
    split_parser.add_argument(
        "--contracts-file",
        dest="contracts_file",
        default=None,
        help=(
            "JSON file of completion contracts keyed by canonical session id "
            '({"1": {"done_when": [...], "verify": {...}}, ...}); '
            "required unless --check is given"
        ),
    )
    split_parser.add_argument(
        "--cwd",
        dest="cwd",
        default=".",
        help=(
            "Root the plan is expected to stay inside; a plan that reaches "
            "into another repository is refused until it declares a "
            "`## Repositories` registry (default: current directory)"
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
        "--rebind-workspace",
        action="store_true",
        help=(
            "Atomically replace a registered plan's workspace binding and all "
            "workspace-dependent preparation evidence (requires --resume)"
        ),
    )
    prepare_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Repository root for a legacy plan or workspace root for a "
            "registered plan "
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
        "--validator-model",
        dest="validator_model",
        help=(
            "Validator harness model to request (omit to preserve that "
            "harness's native default)"
        ),
    )
    prepare_parser.add_argument(
        "--validator-effort",
        dest="validator_effort",
        help=(
            "Validator harness reasoning effort to request (omit to preserve "
            "that harness's native default)"
        ),
    )
    prepare_parser.add_argument(
        "--validator-jobs",
        dest="validator_jobs",
        type=int,
        default=_PREPARE_DEFAULT_VALIDATOR_JOBS,
        help=(
            "Maximum concurrent read-only validator workers "
            f"(default: {_PREPARE_DEFAULT_VALIDATOR_JOBS})"
        ),
    )
    prepare_parser.add_argument(
        "--validator-timeout",
        dest="validator_timeout",
        type=int,
        default=_PREPARE_DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
        help=(
            "Per-validator worker timeout in seconds "
            f"(default: {_PREPARE_DEFAULT_VALIDATOR_TIMEOUT_SECONDS})"
        ),
    )
    prepare_parser.add_argument(
        "--validator-heartbeat",
        dest="validator_heartbeat",
        type=int,
        default=_PREPARE_DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
        help=(
            "Seconds between validator progress heartbeats on stderr "
            f"(default: {_PREPARE_DEFAULT_VALIDATOR_HEARTBEAT_SECONDS})"
        ),
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
        help=(
            "Implementation baseline/fallback model for the resulting `asl run` command"
        ),
    )
    prepare_spark = prepare_parser.add_mutually_exclusive_group()
    prepare_spark.add_argument(
        "--spark",
        dest="spark",
        action="store_true",
        default=False,
        help="Opt the resulting run command into eligible-session Spark routing",
    )
    prepare_spark.add_argument(
        "--no-spark",
        dest="spark",
        action="store_false",
        help="Clear Spark routing when preparation is resumed",
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
        "--force-new",
        action="store_true",
        help=(
            "When preparing from a source plan, allow an intentional duplicate "
            "split by creating the next numbered sibling directory"
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

    register_repositories_parser = subparsers.add_parser(
        "register-repositories",
        help=(
            "Derive and apply the `## Repositories` registry a legacy "
            "multi-repository plan needs, or name the fact that refuses it."
        ),
    )
    register_repositories_parser.add_argument(
        "plan_dir",
        nargs="?",
        default=None,
        help="Split plan directory to migrate in place",
    )
    register_repositories_source = (
        register_repositories_parser.add_mutually_exclusive_group()
    )
    register_repositories_source.add_argument(
        "--plan-file",
        dest="plan_file",
        default=None,
        help=(
            "Source plan markdown file to migrate; the migrated markdown is "
            "emitted and the input file is never written"
        ),
    )
    register_repositories_source.add_argument(
        "--inline-stdin",
        dest="inline_stdin",
        action="store_true",
        help=(
            "Read the source plan from stdin (the last <proposed_plan> block, "
            "else the raw input)"
        ),
    )
    register_repositories_parser.add_argument(
        "--output",
        default=None,
        help=(
            "Write the migrated source plan here instead of stdout "
            "(source-plan mode only)"
        ),
    )
    register_repositories_parser.add_argument(
        "--cwd",
        default=".",
        help=(
            "Root the plan is currently bound to; the workspace root is "
            "derived from the repositories the plan reaches "
            "(default: current directory)"
        ),
    )
    register_repositories_parser.add_argument(
        "--state-dir",
        default=None,
        help="Override operational state directory",
    )
    register_repositories_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    register_repositories_parser.set_defaults(
        handler_ref="repository_registration:register_repositories_from_args"
    )

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Check that a split plan directory is loop-ready.",
    )
    preflight_parser.add_argument("plan_dir", help="Split plan directory to inspect")
    preflight_parser.add_argument(
        "--cwd",
        default=None,
        help=(
            "Repository root for legacy environment checks or workspace root "
            "for registered checks (default: skip cwd checks)"
        ),
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

    gc_parser = subparsers.add_parser(
        "gc",
        help=(
            "Reclaim persisted run directories older than an age cutoff, for "
            "one plan or the whole state root (default: dry-run)."
        ),
    )
    gc_parser.add_argument(
        "plan_dir",
        nargs="?",
        help="Split plan directory to sweep (default: every plan in the state root)",
    )
    gc_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    gc_parser.add_argument(
        "--max-age-days",
        type=float,
        default=_RETENTION_DEFAULT_MAX_AGE_DAYS,
        metavar="DAYS",
        help=(
            "Reclaim runs that finished at least this many days ago "
            f"(default: {_RETENTION_DEFAULT_MAX_AGE_DAYS:g})"
        ),
    )
    gc_mode = gc_parser.add_mutually_exclusive_group()
    gc_mode.add_argument(
        "--apply",
        action="store_true",
        help="Delete the reclaimable run directories (default: dry-run, no writes)",
    )
    gc_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Project the reclaim without deleting anything (the default)",
    )
    gc_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    gc_parser.set_defaults(handler_ref="retention:gc_from_args")

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
    backfill_parser.set_defaults(handler_ref="progress_cli:backfill_progress_from_args")

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

    plans_parser = subparsers.add_parser(
        "plans",
        help=(
            "List discovered plan directories, with filters for stale, "
            "never-run, or specific readiness states."
        ),
    )
    plans_parser.add_argument(
        "--state-dir",
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
    plans_parser.set_defaults(handler_ref="workbench:plans_from_args")

    bridge_parser = subparsers.add_parser(
        "bridge",
        help="Serve the read-only NDJSON run projection to a supervision client.",
    )
    bridge_sub = bridge_parser.add_subparsers(dest="bridge_command", required=True)
    bridge_serve_parser = bridge_sub.add_parser(
        "serve",
        help="Stream hello, run snapshots, and change events over stdio.",
    )
    bridge_serve_parser.add_argument(
        "--stdio",
        action="store_true",
        default=True,
        help="Speak the protocol on stdin/stdout (the only transport; default)",
    )
    bridge_serve_parser.add_argument(
        "--protocol",
        type=int,
        default=_BRIDGE_PROTOCOL_VERSION,
        metavar="VERSION",
        help=(
            "Protocol version to speak "
            f"(default and only supported: {_BRIDGE_PROTOCOL_VERSION})"
        ),
    )
    bridge_serve_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    bridge_serve_parser.add_argument(
        "--plan-root",
        action="append",
        default=None,
        metavar="DIR",
        help="Extra plan root directory to scan for plans (may be repeated)",
    )
    bridge_serve_parser.set_defaults(handler_ref="bridge:bridge_serve_from_args")

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
        help=(
            "Default legacy repository root; registered plans always launch "
            "from their prepared workspace binding"
        ),
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
        handler_ref="mailbox_cli:inbox_reply_from_args",
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
        handler_ref="mailbox_cli:inbox_resolve_from_args",
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
    ask_parser.set_defaults(handler_ref="mailbox_cli:ask_from_args")

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
    notify_parser.set_defaults(handler_ref="mailbox_cli:notify_from_args")

    outbox_parser = subparsers.add_parser(
        "outbox",
        help="Read worker questions or notices from the supervisor side.",
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
        handler_ref="mailbox_cli:outbox_wait_from_args",
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
        handler_ref="mailbox_cli:outbox_supervise_from_args",
    )

    assurance_parser = subparsers.add_parser(
        "assurance",
        help="Browse the assurance inbox of audited runs and open issues.",
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
        help="Audit only this run, even if its record says to skip it",
    )
    assurance_pending_parser.add_argument(
        "--plan",
        metavar="PLAN_DIR_OR_ID",
        help="Restrict the pass to one plan's runs",
    )
    assurance_pending_parser.add_argument(
        "--audit-harness",
        metavar="NAME",
        help="Audit harness to use instead of the one the dead run recorded",
    )
    assurance_pending_parser.add_argument(
        "--audit-harness-model",
        metavar="MODEL",
        help="Audit harness model to use instead of the recorded one",
    )
    assurance_pending_parser.add_argument(
        "--audit-effort",
        metavar="LEVEL",
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
        metavar="NAME",
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
        metavar="LEVEL",
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
    try:
        return resolve_handler(args)(args)
    except KeyboardInterrupt:
        return _INTERRUPTED_EXIT
