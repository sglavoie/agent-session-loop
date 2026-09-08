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
from . import cli_trace, contracts, harnesses, run_flags, session_overview, tail

# Build-time defaults mirrored from heavier modules so light commands do not
# import `metrics` (pulls in review/compact-state) or `workbench_server` (pulls
# in the whole workbench subsystem) merely to build the parser. The
# `test_cli_build_time_default_constants_match_source_modules` guard keeps these
# in sync with their authoritative definitions.
_WORKBENCH_DEFAULT_HOST = "127.0.0.1"  # workbench_server.DEFAULT_HOST
_WORKBENCH_DEFAULT_PORT = 8765  # workbench_server.DEFAULT_PORT
_METRICS_DEFAULT_SINCE_DAYS = 7  # metrics.DEFAULT_SINCE_DAYS
_RETENTION_DEFAULT_MAX_AGE_DAYS = 30.0  # retention.DEFAULT_MAX_AGE_DAYS
_MAINTENANCE_DEFAULT_HOUR = 3  # maintenance.DEFAULT_HOUR
_MAINTENANCE_DEFAULT_MINUTE = 30  # maintenance.DEFAULT_MINUTE
_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS = (
    900.0  # maintenance.DEFAULT_MAX_RUNTIME_SECONDS
)
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
_COMMIT_DEFAULT_GENERATION_TIMEOUT_SECONDS = (
    900  # commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS
)
_INTERRUPTED_EXIT = 130  # turn_execution.INTERRUPTED_EXIT
_BRIDGE_PROTOCOL_VERSION = 2  # bridge.PROTOCOL_VERSION


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


# Nested groups list their leaves and nothing else, so an agent that reads
# `asl outbox --help` sees no flags and concludes they do not exist. Every group
# says where the flags actually live instead. `parent_form_leaf` names the leaf a
# group's bare form runs when the group installs that leaf's flags on itself, so
# the wording never denies flags the same help output displays.
def _group_epilogue(
    group: str,
    leaf_example: str,
    *,
    guide: bool = False,
    parent_form_leaf: str | None = None,
) -> str:
    if parent_form_leaf is None:
        lines = [
            f"This help lists the `asl {group}` leaf commands only.",
            f"Flags belong to a leaf, so read `{leaf_example}` for its flags.",
        ]
    else:
        lines = [
            f"This help lists the `asl {group}` leaf commands plus the flags "
            f"the bare `asl {group}` form accepts.",
            f"Bare `asl {group}` runs `{parent_form_leaf}` with those flags; "
            f"every other leaf has its own, so read `{leaf_example}` "
            "or that leaf's help.",
        ]
    if guide:
        lines.append("Run `asl supervisor-guide` for the whole supervision workflow.")
    return "\n".join(lines)


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
        "--agent-mux",
        action="store_true",
        help=(
            "Also report the ASL-owned AgentMux compatibility contract: "
            "component schema versions, digests, contract-set id, state root, "
            "and fixture validation"
        ),
    )
    contract_check_parser.add_argument(
        "--state-dir",
        help="Override operational state directory (reported by --agent-mux)",
    )
    contract_check_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    contract_check_parser.set_defaults(
        handler_ref="verification:contract_check_from_args"
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

    maintenance_parser = subparsers.add_parser(
        "maintenance",
        help=(
            "Own the scheduled storage maintenance job that keeps the state "
            "corpus inside its budget without touching protected evidence."
        ),
        epilog=_group_epilogue("maintenance", "asl maintenance run --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    maintenance_sub = maintenance_parser.add_subparsers(
        dest="maintenance_command", required=True
    )

    maintenance_install_parser = maintenance_sub.add_parser(
        "install",
        help="Install and load the daily ASL-owned maintenance job.",
        description=(
            "Write the launchd job definition ASL owns and bootstrap it into "
            "this user's GUI domain. The job runs `asl maintenance run --apply` "
            "once a day under a bounded runtime; it does nothing unless the "
            "corpus is over budget or the volume is low on free space. "
            "Installing never starts a pass."
        ),
    )
    maintenance_install_parser.add_argument(
        "--executable",
        metavar="PATH",
        help=(
            "Absolute path to the asl executable launchd should run "
            "(default: resolved from PATH at install time)"
        ),
    )
    maintenance_install_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_install_parser.add_argument(
        "--hour",
        type=int,
        default=_MAINTENANCE_DEFAULT_HOUR,
        metavar="HOUR",
        help=f"Local hour to run at (default: {_MAINTENANCE_DEFAULT_HOUR})",
    )
    maintenance_install_parser.add_argument(
        "--minute",
        type=int,
        default=_MAINTENANCE_DEFAULT_MINUTE,
        metavar="MINUTE",
        help=f"Local minute to run at (default: {_MAINTENANCE_DEFAULT_MINUTE})",
    )
    maintenance_install_parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS,
        metavar="SECONDS",
        help=(
            "Wall-clock bound on each scheduled pass "
            f"(default: {_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS:g})"
        ),
    )
    maintenance_install_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Write the job definition without loading it into launchd",
    )
    maintenance_install_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_install_parser.set_defaults(handler_ref="maintenance:install_from_args")

    maintenance_run_parser = maintenance_sub.add_parser(
        "run",
        help="Run one maintenance pass over the state corpus (default: dry-run).",
        description=(
            "Measure the corpus against its budget and the volume's free space, "
            "and when either threshold is crossed reclaim what retention policy "
            "permits: expired run directories, then compressed and budgeted log "
            "bodies of terminal newest runs older than the compaction age. Live, "
            "locked, unfinished, pinned, and recent evidence is never touched, "
            "and liveness is re-read under the maintenance lock immediately "
            "before any mutation."
        ),
    )
    maintenance_run_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_run_mode = maintenance_run_parser.add_mutually_exclusive_group()
    maintenance_run_mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform the planned maintenance (default: dry-run, no writes)",
    )
    maintenance_run_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Project the maintenance without mutating anything (the default)",
    )
    maintenance_run_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Plan and apply even when both thresholds are satisfied; policy is "
            "unchanged, so protected evidence is still never removed"
        ),
    )
    maintenance_run_parser.add_argument(
        "--max-age-days",
        type=float,
        default=_RETENTION_DEFAULT_MAX_AGE_DAYS,
        metavar="DAYS",
        help=(
            "Reclaim runs that finished at least this many days ago "
            f"(default: {_RETENTION_DEFAULT_MAX_AGE_DAYS:g})"
        ),
    )
    maintenance_run_parser.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS,
        metavar="SECONDS",
        help=(
            "Stop cleanly after this much wall clock and report the remainder "
            f"(default: {_MAINTENANCE_DEFAULT_MAX_RUNTIME_SECONDS:g})"
        ),
    )
    maintenance_run_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_run_parser.set_defaults(handler_ref="maintenance:run_from_args")

    maintenance_status_parser = maintenance_sub.add_parser(
        "status",
        help="Report job presence, last run, reclaimed bytes, and failures.",
        description=(
            "Verify the installed job: whether its definition is present and "
            "current, whether launchd has it loaded, and what the last recorded "
            "pass reclaimed or failed to reclaim. Exits non-zero only when the "
            "last recorded pass itself failed."
        ),
    )
    maintenance_status_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_status_parser.add_argument(
        "--executable",
        metavar="PATH",
        help="Executable to compare the installed definition against",
    )
    maintenance_status_parser.add_argument(
        "--no-probe",
        action="store_true",
        help="Skip the launchctl query and report the definition only",
    )
    maintenance_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_status_parser.set_defaults(handler_ref="maintenance:status_from_args")

    maintenance_uninstall_parser = maintenance_sub.add_parser(
        "uninstall",
        help="Unload and remove the maintenance job, keeping every artifact.",
        description=(
            "Boot the job out of launchd and delete its definition. Nothing in "
            "the state corpus is removed: the maintenance manifest and job log "
            "outlive the decision to stop maintaining automatically."
        ),
    )
    maintenance_uninstall_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    maintenance_uninstall_parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Remove the job definition without asking launchd to unload it",
    )
    maintenance_uninstall_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    maintenance_uninstall_parser.set_defaults(
        handler_ref="maintenance:uninstall_from_args"
    )

    migrate_state_parser = subparsers.add_parser(
        "migrate-state",
        help=(
            "Relocate an orphaned state directory after a plan directory move, "
            "or upgrade a plan's state schema in place (default: dry-run)."
        ),
    )
    migrate_state_parser.add_argument(
        "plan_dir", help="Current (new) plan directory whose state to restore"
    )
    migrate_state_parser.add_argument(
        "--from",
        dest="from_plan_dir",
        help=(
            "Old plan directory path the state was keyed under; omit to upgrade "
            "this plan's own state to the current schema version instead"
        ),
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
        epilog=_group_epilogue("bridge", "asl bridge serve --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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

    agent_mux_parser = subparsers.add_parser(
        "agent-mux",
        help="Report and prepare this installation for the Agent Mux cockpit.",
        epilog=_group_epilogue("agent-mux", "asl agent-mux pairing --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    agent_mux_sub = agent_mux_parser.add_subparsers(
        dest="agent_mux_command", required=True
    )
    pairing_parser = agent_mux_sub.add_parser(
        "pairing",
        help="Survey this executable, contract set, and whole state root.",
        description=(
            "Report the facts `amx config asl-sync` needs before it may rewrite "
            "Agent Mux's configuration: the absolute executable speaking, the "
            "contract set it implements, the canonical state root, any active "
            "run whose state predates the projection schema, and how much of "
            "the inactive remainder can be migrated. Exits nonzero when an "
            "active incompatible run is present; such a run is named and never "
            "stopped or migrated."
        ),
    )
    pairing_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    pairing_parser.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "Upgrade every migratable inactive plan to the current state schema "
            "(refused outright while any active incompatible run exists)"
        ),
    )
    pairing_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    pairing_parser.set_defaults(handler_ref="agent_mux_pairing:pairing_from_args")

    diagnostics_parser = subparsers.add_parser(
        "diagnostics",
        help="Publish this installation's sanitized diagnostics for other tools.",
        epilog=_group_epilogue("diagnostics", "asl diagnostics export --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    diagnostics_sub = diagnostics_parser.add_subparsers(
        dest="diagnostics_command", required=True
    )
    diagnostics_export_parser = diagnostics_sub.add_parser(
        "export",
        help="Write the bounded, deterministic, sanitized diagnostics export.",
        description=(
            "Write the only supported view of ASL's internal state for another "
            "program to read. The document correlates run IDs, lifecycle source "
            "events, request IDs, bridge operation IDs, projection generations "
            "and the contract set, and carries this build's identity and binary "
            "hash. It is bounded (every cap is published in `limits` and every "
            "cap that bites is counted in `omissions`), deterministic (no wall "
            "clock, so repeated capture over frozen inputs is byte-identical), "
            "and control-plane only: question and reply bodies, control "
            "messages, prompts, model output, bridge stderr and verification "
            "output are never read. Consumers must read this instead of "
            "state.json, events.jsonl or mailbox.jsonl, which are private "
            "formats."
        ),
    )
    diagnostics_export_parser.add_argument(
        "--agent-mux",
        action="store_true",
        help="Export the Agent Mux correlation profile (currently the only one)",
    )
    diagnostics_export_parser.add_argument(
        "--state-dir",
        help="Override operational state directory",
    )
    diagnostics_export_parser.add_argument(
        "--run-id",
        help="Limit the export to one run ID",
    )
    diagnostics_export_parser.add_argument(
        "--plan-dir",
        help="Limit the export to one plan directory",
    )
    diagnostics_export_parser.add_argument(
        "--output",
        metavar="PATH",
        help="Write the export to PATH instead of standard output",
    )
    diagnostics_export_parser.add_argument(
        "--json",
        action="store_true",
        help="Wrap the export in the standard JSON response envelope",
    )
    diagnostics_export_parser.set_defaults(
        handler_ref="diagnostics_export:export_from_args"
    )

    overseer_parser = subparsers.add_parser(
        "overseer",
        help="Watch authorized runs on behalf of an invoked overseer host agent.",
        epilog=_group_epilogue("overseer", "asl overseer scan --help"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    overseer_sub = overseer_parser.add_subparsers(
        dest="overseer_command", required=True
    )

    overseer_scan_parser = overseer_sub.add_parser(
        "scan",
        help="List every run discovery can see and why each is or is not in play.",
        description=(
            "Report the fleet as ASL's own discovery sees it: every plan under "
            "the state root, its run, its lifecycle, whether it is eligible and "
            "the reasons it is not. Discovery only -- no run's content is read, "
            "so AgentMux is not consulted and a plan listed as eligible here may "
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
        type=float,
        help="How far back a pause still counts as recent (default 24)",
    )
    overseer_scan_parser.add_argument(
        "--state-dir",
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
        help="Enrol the runs AgentMux authorizes and hold a watch lease on each.",
        description=(
            "Sweep the fleet, ask AgentMux about each plan in scope, and take a "
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
        type=float,
        help="Keep sweeping for this long instead of running a single sweep",
    )
    overseer_watch_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=15.0,
        help="Wait this long between sweeps under --for-seconds (default 15)",
    )
    overseer_watch_parser.add_argument(
        "--sweep-seconds",
        type=float,
        default=60.0,
        help="Budget for one sweep; plans not reached are reported deferred",
    )
    overseer_watch_parser.add_argument(
        "--lease-seconds",
        type=float,
        default=300.0,
        help="How long each watch lease is held before it expires (default 300)",
    )
    overseer_watch_parser.add_argument(
        "--pause-window-hours",
        type=float,
        help="How far back a pause still counts as recent (default 24)",
    )
    overseer_watch_parser.add_argument(
        "--release",
        action="store_true",
        help="Release every lease this watcher holds and exit",
    )
    overseer_watch_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for each AgentMux authorization (default 10)",
    )
    overseer_watch_parser.add_argument(
        "--state-dir",
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
            "Ask AgentMux for context access, then capture what the host needs "
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
    overseer_inspect_parser.add_argument("plan_dir", help="Plan directory to inspect")
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
        type=int,
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
        type=int,
        default=4000,
        help="Character ceiling for each expansion (default 4000)",
    )
    overseer_inspect_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for the AgentMux authorization (default 10)",
    )
    overseer_inspect_parser.add_argument(
        "--state-dir",
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
            "record with the stable statement IDs a claim must cite; the host "
            "reads what those statements mean and passes claims back through "
            "`--claims-file` or `--claims-stdin`. Everything after that is "
            "mechanical: the register rejects claims that cite nothing, widen "
            "their scope or name an unknown topic, and the ruling follows from "
            "what survives. A refusal is an escalation, printed with the "
            "question to put to a human, the work that stays blocked, and the "
            "other plans this invocation may keep working on meanwhile. There "
            "is no timeout and no default answer: silence is not consent."
        ),
    )
    overseer_decide_parser.add_argument("plan_dir", help="Plan directory in question")
    overseer_decide_parser.add_argument(
        "--operation",
        default="reply",
        choices=("reply", "guidance", "verify", "commit", "resume"),
        help="The operation to resolve (default reply)",
    )
    overseer_decide_parser.add_argument(
        "--incident-id",
        type=int,
        help="Narrow the scope to one open incident",
    )
    overseer_decide_parser.add_argument(
        "--raises",
        action="append",
        metavar="TOPIC",
        # overseer_decisions.HUMAN_TOPICS, restated: this parser imports no
        # overseer module.
        choices=("acceptance", "scope", "design", "safety"),
        help=(
            "A human question this operation depends on; repeat per topic. A "
            "topic no in-scope decision settles refuses `human_decision` "
            "however the operation itself was allowed. Topics some claimed "
            "in-scope decision already names are raised anyway"
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
        type=float,
        default=10.0,
        help="Seconds to wait for the AgentMux authorization (default 10)",
    )
    overseer_decide_parser.add_argument(
        "--state-dir",
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
            "since is refused `evidence_changed` rather than resolved past. "
            "The operational scope is reply, "
            "guidance, verify, commit and resume; anything else, and anything "
            "no adapter implements, is refused and escalated rather than "
            "improvised. `--check` names a check the session's completion "
            "contract already declares and the argv comes from that contract, "
            "so an undeclared name is refused rather than run. "
            "Application-code repairs belong to a worker, not here."
        ),
    )
    overseer_act_parser.add_argument("plan_dir", help="Plan directory to act on")
    overseer_act_parser.add_argument(
        "--watcher-id",
        required=True,
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
        type=int,
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
        # overseer_decisions.HUMAN_TOPICS, restated: this parser imports no
        # overseer module.
        choices=("acceptance", "scope", "design", "safety"),
        help=(
            "A human question this action depends on; repeat per topic. It is "
            "carried into every authority recheck, and a claimed in-scope hold "
            "on any human topic is carried whether or not it is named here"
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
            "these claims reported; the action is refused `evidence_changed` "
            "if human evidence in scope moved since it was read"
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
        type=float,
        default=10.0,
        help="Seconds to wait for each AgentMux authorization (default 10)",
    )
    overseer_act_parser.add_argument(
        "--state-dir",
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
        "plan_dir", help="Plan directory to reconcile"
    )
    overseer_reconcile_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for each AgentMux authorization (default 10)",
    )
    overseer_reconcile_parser.add_argument(
        "--state-dir",
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

    workbench_parser = subparsers.add_parser(
        "workbench",
        help="Run the local Workbench HTTP control plane.",
        epilog=_group_epilogue(
            "workbench",
            "asl workbench serve --help",
            parent_form_leaf="asl workbench serve",
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exit_request:
        cli_trace.record_parser_exit(parser, raw_argv, exit_request.code)
        raise
    invocation = cli_trace.start(
        parser, raw_argv, state_dir=getattr(args, "state_dir", None)
    )
    try:
        exit_code = resolve_handler(args)(args)
    except KeyboardInterrupt:
        invocation.finish(
            exit_code=_INTERRUPTED_EXIT, outcome=cli_trace.OUTCOME_INTERRUPTED
        )
        return _INTERRUPTED_EXIT
    except SystemExit as exit_request:
        raised_code = cli_trace.normalize_exit_code(exit_request.code)
        invocation.finish(
            exit_code=raised_code,
            outcome=cli_trace.outcome_for_exit_code(raised_code),
        )
        raise
    except Exception as error:
        invocation.finish(
            exit_code=None, outcome=cli_trace.OUTCOME_EXCEPTION, exception=error
        )
        raise
    invocation.finish(
        exit_code=exit_code, outcome=cli_trace.outcome_for_exit_code(exit_code)
    )
    return exit_code
