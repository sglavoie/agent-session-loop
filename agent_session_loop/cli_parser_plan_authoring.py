"""Parser definitions for the plan authoring commands."""

from __future__ import annotations

import argparse

from . import harnesses
from .cli_defaults import (
    _PREPARE_DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
    _PREPARE_DEFAULT_VALIDATOR_JOBS,
    _PREPARE_DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
)
from .cli_parser_support import (
    _add_unsafe_bypass_flags,
    _AgentSessionLoopArgumentParser,
)


def _add_plan_authoring_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """Authoring: split, prepare, expand-ingest, register-repositories, preflight."""
    split_parser = subparsers.add_parser(
        "split",
        help=(
            "Split a multi-session plan into a collision-safe per-session "
            "directory using the deterministic authoring API."
        ),
    )
    # The handler requires exactly one source itself so a missing or doubled
    # source keeps its JSON ``reason``; usage still shows the alternatives.
    split_parser.show_as_alternatives(
        split_parser.add_argument(
            "--plan-file",
            dest="plan_file",
            default=None,
            help="Path to the source plan markdown file to split",
        ),
        split_parser.add_argument(
            "--inline-stdin",
            dest="inline_stdin",
            action="store_true",
            help=(
                "Capture the source plan from stdin (the last <proposed_plan> "
                "block, else the raw input) as a durable source file"
            ),
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
    split_parser.set_defaults(handler_ref="authoring_split_cli:split_from_args")

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
        metavar="DIR",
        default=None,
        help="Override operational state directory",
    )
    _add_unsafe_bypass_flags(prepare_parser)
    prepare_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON output",
    )
    prepare_parser.set_defaults(handler_ref="prepare_cli:prepare_from_args")

    expand_ingest_parser = subparsers.add_parser(
        "expand-ingest",
        help=(
            "Ingest one validator-output batch file: merge it into the stored "
            "aggregate, render its expansions, and apply its factual corrections."
        ),
    )
    expand_ingest_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        help="Split plan directory to ingest validator output into",
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
    expand_ingest_parser.set_defaults(
        handler_ref="prepare_expand_ingest:expand_ingest_from_args"
    )

    register_repositories_parser = subparsers.add_parser(
        "register-repositories",
        help=(
            "Derive and apply the `## Repositories` registry a legacy "
            "multi-repository plan needs, or name the fact that refuses it."
        ),
    )
    register_repositories_parser.add_argument(
        "plan_dir",
        metavar="PLAN_DIR",
        nargs="?",
        default=None,
        help="Split plan directory to migrate in place",
    )
    register_repositories_parser.show_as_alternatives(
        register_repositories_parser.add_argument(
            "--plan-file",
            dest="plan_file",
            default=None,
            help=(
                "Source plan markdown file to migrate; the migrated markdown is "
                "emitted and the input file is never written"
            ),
        ),
        register_repositories_parser.add_argument(
            "--inline-stdin",
            dest="inline_stdin",
            action="store_true",
            help=(
                "Read the source plan from stdin (the last <proposed_plan> block, "
                "else the raw input)"
            ),
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
        metavar="DIR",
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
    preflight_parser.add_argument(
        "plan_dir", metavar="PLAN_DIR", help="Split plan directory to inspect"
    )
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
        metavar="DIR",
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
    preflight_parser.set_defaults(handler_ref="preflight_cli:preflight_from_args")
