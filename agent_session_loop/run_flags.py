"""Declarative single source of truth for the ``asl run`` flag surface.

Historically the ``asl run`` option surface was defined twice: once in
``cli.py``'s run subparser (real user-facing defaults) and again, hand-mirrored,
in ``skills/asl/scripts/asl_autopilot.py``'s ``_build_run_parser`` (the
supervisor helper, which needs ``None`` defaults so it can tell which flags the
operator actually passed). The two drifted whenever a flag changed.

This module replaces both hand-written parsers with one declarative spec
(:data:`RUN_FLAG_SPEC`). :func:`add_run_arguments` builds an ``argparse`` parser
from the spec in one of three modes, and :func:`validate_run_namespace` holds the
numeric/harness cross-field checks that both ``runner._validate_run_inputs`` and
the supervisor helper enforce, so they can never disagree.

The spec emits flags in the same fixed order the helper's argv reconstruction
uses (``asl_autopilot.parse_command``), so a reconstructed child ``argv`` stays
canonical without any extra bookkeeping.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from . import harnesses


@dataclass(frozen=True)
class RunFlag:
    """Descriptor for one ``asl run`` option (or the ``plan_dir`` positional).

    One :class:`RunFlag` maps to exactly one ``argparse`` action, so options that
    share a destination (``--unsafe-bypass``/``--no-unsafe-bypass``,
    ``--commit-per-session``/``--commit-at-end``) get one descriptor each.
    """

    dest: str
    option_strings: tuple[str, ...] = ()
    positional: bool = False
    action: str | None = None
    type: Any = None
    choices: Any = None
    const: Any = None
    cli_default: Any = None
    help: str = ""
    mutex_group: str | None = None


@dataclass(frozen=True)
class FlagError:
    """A single validation failure carrying its stable operator-facing message."""

    message: str


# The declarative flag surface, in the fixed order the CLI subparser and the
# helper's argv reconstruction both use. ``cli_default`` is the real user-facing
# default; the supervisor mode strips any meaningful default to ``None`` so the
# helper can detect operator-supplied flags, and the resume mode suppresses
# unset flags entirely (Session 4 overlays them onto a persisted config).
RUN_FLAG_SPEC: tuple[RunFlag, ...] = (
    RunFlag(
        dest="plan_dir",
        positional=True,
        help="Split plan directory to execute",
    ),
    RunFlag(
        dest="cwd",
        option_strings=("--cwd",),
        cli_default=".",
        help=(
            "Repository root where the harness should run "
            "(default: current directory)"
        ),
    ),
    RunFlag(
        dest="harness",
        option_strings=("--harness",),
        choices=harnesses.HARNESS_NAMES,
        cli_default="codex",
        help="Agent harness to invoke",
    ),
    RunFlag(
        dest="review_harness",
        option_strings=("--review-harness",),
        choices=harnesses.HARNESS_NAMES,
        help=(
            "Append a generated final-review session to the plan and run it "
            "with this harness after all implementation sessions complete; "
            "must differ from --harness"
        ),
    ),
    RunFlag(
        dest="harness_model",
        option_strings=("--harness-model",),
        help="Implementation harness model to request",
    ),
    RunFlag(
        dest="review_harness_model",
        option_strings=("--review-harness-model",),
        help="Generated review harness model to request",
    ),
    RunFlag(
        dest="effort",
        option_strings=("--effort",),
        help="Harness reasoning effort to request",
    ),
    RunFlag(
        dest="timeout",
        option_strings=("--timeout",),
        type=int,
        help="Per-session harness timeout in seconds",
    ),
    RunFlag(
        dest="max_sessions",
        option_strings=("--max-sessions",),
        type=int,
        help="Maximum sessions to complete before stopping",
    ),
    RunFlag(
        dest="max_pending_sessions",
        option_strings=("--max-pending-sessions",),
        type=int,
        help=(
            "Override the preflight pending-session budget gate "
            "(default: 24, which turns yellow at 25 pending sessions)"
        ),
    ),
    RunFlag(
        dest="max_review_fix_cycles",
        option_strings=("--max-review-fix-cycles",),
        type=int,
        help=(
            "Maximum automatic repair/rereview cycles after a findings "
            "review (default: 2). 0 still runs the review but creates no "
            "repair sessions"
        ),
    ),
    RunFlag(
        dest="max_review_fix_plans",
        option_strings=("--max-review-fix-plans",),
        type=int,
        help="Deprecated compatibility alias for --max-review-fix-cycles",
    ),
    RunFlag(
        dest="review_guard_exclude",
        option_strings=("--review-guard-exclude",),
        action="append",
        help=(
            "Repo-relative path the review mutation guard ignores; repeat for "
            "several paths. Merged with the os.pathsep-separated "
            "ASL_REVIEW_GUARD_EXCLUDE environment variable"
        ),
    ),
    RunFlag(
        dest="review_guard_include_ignored",
        option_strings=("--review-guard-include-ignored",),
        action="store_true",
        help=(
            "Include gitignored untracked files in the review mutation guard's "
            "evidence; off by default so build caches and harness-rewritten "
            "settings do not revert a review turn"
        ),
    ),
    RunFlag(
        dest="commit_mode",
        option_strings=("--commit-per-session",),
        action="store_const",
        const="per-session",
        mutex_group="commit",
        help="Run 'spec commit --yes --no-verify' after each completed session",
    ),
    RunFlag(
        dest="commit_mode",
        option_strings=("--commit-at-end",),
        action="store_const",
        const="end",
        mutex_group="commit",
        help="Run 'spec commit --yes --no-verify' once after all sessions complete",
    ),
    RunFlag(
        dest="state_dir",
        option_strings=("--state-dir",),
        help="Override operational state directory",
    ),
    RunFlag(
        dest="note",
        option_strings=("--note",),
        help="Reference note to show in run status and final output",
    ),
    RunFlag(
        dest="unsafe_bypass",
        option_strings=("--unsafe-bypass",),
        action="store_true",
        cli_default=True,
        help=(
            "Use harness-specific bypass flags for sandbox and approval "
            "prompts (default)"
        ),
    ),
    RunFlag(
        dest="unsafe_bypass",
        option_strings=("--no-unsafe-bypass",),
        action="store_false",
        cli_default=True,
        help="Use the harness default sandbox and approval behavior",
    ),
    RunFlag(
        dest="no_mailbox",
        option_strings=("--no-mailbox",),
        action="store_true",
        cli_default=False,
        help=(
            "Disable live mailbox guidance and question/reply handling even "
            "when a mailbox or fresh supervisor lease exists"
        ),
    ),
    RunFlag(
        dest="json",
        option_strings=("--json",),
        action="store_true",
        cli_default=False,
        help="Print machine-readable JSON output",
    ),
    RunFlag(
        dest="dry_run",
        option_strings=("--dry-run",),
        action="store_true",
        cli_default=False,
        help="Validate inputs and show planned work without invoking a harness",
    ),
    RunFlag(
        dest="allow_not_loop_ready",
        option_strings=("--allow-not-loop-ready",),
        action="store_true",
        cli_default=False,
        help=(
            "Bypass the readiness gate and run even when the plan is not "
            "marked loop-ready (preflight findings are still reported)"
        ),
    ),
    RunFlag(
        dest="resume",
        option_strings=("--resume",),
        action="store_true",
        cli_default=False,
        help=(
            "Rehydrate the harness, model, effort, and other flags from the "
            "run_config persisted in this plan's state.json; flags passed "
            "explicitly still win. Pass --state-dir too when the original run "
            "used one"
        ),
    ),
    RunFlag(
        dest="reply",
        option_strings=("--reply",),
        mutex_group="reply",
        help=(
            "Resume the latest paused session with an inline reply; the "
            "text is sent as the operator answer for the first resumed turn"
        ),
    ),
    RunFlag(
        dest="reply_stdin",
        option_strings=("--reply-stdin",),
        action="store_true",
        cli_default=False,
        mutex_group="reply",
        help=(
            "Read the operator reply from stdin instead of --reply, so the "
            "text never appears in argv; behaves identically to --reply TEXT "
            "otherwise"
        ),
    ),
)

_MODES = ("cli", "supervisor", "resume")


def _supervisor_default(cli_default: Any) -> Any:
    """Strip a meaningful CLI default so the helper can detect user intent.

    Value/tri-state flags collapse to ``None`` (``--cwd``, ``--harness``,
    ``--unsafe-bypass``); boolean ``store_true`` flags whose CLI default is
    already ``False`` keep it, matching the helper's historical parser.
    """
    if cli_default is None or cli_default is False:
        return cli_default
    return None


def _default_for(flag: RunFlag, mode: str) -> Any:
    if mode == "resume":
        return argparse.SUPPRESS
    if mode == "supervisor":
        return _supervisor_default(flag.cli_default)
    return flag.cli_default


def _add_flag(container: Any, flag: RunFlag, mode: str) -> None:
    if flag.positional:
        # Positionals are always required; a default is meaningless and the
        # help is only rendered for the CLI parser (the supervisor parser is
        # built with add_help=False).
        container.add_argument(flag.dest, help=flag.help or None)
        return

    kwargs: dict[str, Any] = {
        "dest": flag.dest,
        "default": _default_for(flag, mode),
    }
    if flag.action is not None:
        kwargs["action"] = flag.action
    if flag.action == "store_const":
        kwargs["const"] = flag.const
    if flag.type is not None:
        kwargs["type"] = flag.type
    if flag.choices is not None:
        kwargs["choices"] = flag.choices
    if flag.help:
        kwargs["help"] = flag.help
    container.add_argument(*flag.option_strings, **kwargs)


def add_run_arguments(
    parser: argparse.ArgumentParser, mode: str = "cli"
) -> argparse.ArgumentParser:
    """Populate ``parser`` with the ``asl run`` flag surface from the spec.

    ``mode`` selects the default policy: ``"cli"`` uses the real user-facing
    defaults, ``"supervisor"`` uses ``None`` defaults so the helper can detect
    operator-supplied flags, and ``"resume"`` suppresses unset flags entirely so
    they can be overlaid onto a persisted run config.
    """
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {_MODES}")

    groups: dict[str, Any] = {}
    for flag in RUN_FLAG_SPEC:
        if flag.mutex_group is None:
            container: Any = parser
        else:
            container = groups.get(flag.mutex_group)
            if container is None:
                container = parser.add_mutually_exclusive_group()
                groups[flag.mutex_group] = container
        _add_flag(container, flag, mode)
    return parser


def validate_run_namespace(
    ns: argparse.Namespace, *, defer_resume_dependent: bool = False
) -> list[FlagError]:
    """Return the shared numeric/harness cross-field errors for ``ns``.

    This is the single source of truth for the checks that both
    ``runner._validate_run_inputs`` and the supervisor helper enforce, so they
    can never disagree. Attributes are read defensively with ``getattr`` so a
    partial namespace (e.g. a resume overlay) validates cleanly. Callers decide
    how to surface the errors (the runner raises ``RunError`` from the first;
    the helper returns an ``invalid-argument`` parse failure).

    ``defer_resume_dependent`` skips the cross-field checks whose verdict depends
    on values a ``--resume`` overlay may still restore from the persisted
    ``run_config`` — specifically ``--review-harness-model`` requiring
    ``--review-harness``, where the harness can be rehydrated from state. The
    runner validates *after* applying the resume overlay (``_apply_resume_config``
    runs before ``_validate_run_inputs``), so it always sees the effective config
    and never sets this flag. The supervisor helper validates the raw command
    *before* any overlay, so it sets this flag on a resume command and safely
    defers the check to the child, which the runner re-runs against the overlaid
    namespace. This keeps supervisor and runner parity for a model-only resume
    override while still rejecting the same misuse on a non-resume command.
    """
    errors: list[FlagError] = []

    timeout = getattr(ns, "timeout", None)
    if timeout is not None and timeout <= 0:
        errors.append(FlagError("--timeout must be a positive integer"))

    max_sessions = getattr(ns, "max_sessions", None)
    if max_sessions is not None and max_sessions <= 0:
        errors.append(FlagError("--max-sessions must be a positive integer"))

    max_pending = getattr(ns, "max_pending_sessions", None)
    if max_pending is not None and max_pending <= 0:
        errors.append(
            FlagError("--max-pending-sessions must be a positive integer")
        )

    cycles = getattr(ns, "max_review_fix_cycles", None)
    if cycles is not None and cycles < 0:
        errors.append(
            FlagError("--max-review-fix-cycles must be zero or a positive integer")
        )

    legacy_cycles = getattr(ns, "max_review_fix_plans", None)
    if legacy_cycles is not None and legacy_cycles < 0:
        errors.append(
            FlagError("--max-review-fix-plans must be zero or a positive integer")
        )

    if cycles is not None and legacy_cycles is not None:
        errors.append(
            FlagError(
                "--max-review-fix-plans is a deprecated alias of "
                "--max-review-fix-cycles; pass only one"
            )
        )

    harness = getattr(ns, "harness", None)
    review_harness = getattr(ns, "review_harness", None)
    if review_harness is not None and review_harness == harness:
        errors.append(
            FlagError(
                "--review-harness must differ from --harness "
                f"(both set to {harness!r})"
            )
        )

    review_harness_model = getattr(ns, "review_harness_model", None)
    if (
        review_harness_model is not None
        and review_harness is None
        and not defer_resume_dependent
    ):
        errors.append(FlagError("--review-harness-model requires --review-harness"))

    return errors
