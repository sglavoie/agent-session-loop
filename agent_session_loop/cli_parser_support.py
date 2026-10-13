"""Parser class and argument helpers shared by the subcommand parser builders."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .cli_defaults import (
    _CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS,
    _WORKBENCH_DEFAULT_HOST,
    _WORKBENCH_DEFAULT_PORT,
)

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
