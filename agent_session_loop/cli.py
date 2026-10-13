"""Command-line interface for agent-session-loop.

This module assembles the parser from the ``cli_parser_*`` modules and resolves
subcommand handlers lazily from their ``handler_ref``."""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import cli_trace
from .cli_defaults import _INTERRUPTED_EXIT
from .cli_parser_assurance import _add_assurance_commands
from .cli_parser_control import _add_control_commands
from .cli_parser_integrations import (
    _add_agent_mux_commands,
    _add_bridge_commands,
    _add_diagnostics_commands,
)
from .cli_parser_mailbox import (
    _add_inbox_commands,
    _add_outbox_commands,
    _add_worker_message_commands,
)
from .cli_parser_maintenance import (
    _add_maintenance_commands,
    _add_state_commands,
    _add_storage_commands,
)
from .cli_parser_overseer import _add_overseer_commands
from .cli_parser_plan_authoring import _add_plan_authoring_commands
from .cli_parser_recovery import (
    _add_progress_commands,
    _add_workspace_recovery_commands,
)
from .cli_parser_run import (
    _add_check_commands,
    _add_run_commands,
    _add_run_inspection_commands,
)
from .cli_parser_support import _AgentSessionLoopArgumentParser
from .cli_parser_workbench import _add_workbench_commands


def build_parser(prog: str = "agent-session-loop") -> argparse.ArgumentParser:
    parser = _AgentSessionLoopArgumentParser(
        prog=prog,
        description="Run split agent session plans one session at a time.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_run_commands(subparsers)
    _add_run_inspection_commands(subparsers)
    _add_check_commands(subparsers)
    _add_plan_authoring_commands(subparsers)
    _add_storage_commands(subparsers)
    _add_maintenance_commands(subparsers)
    _add_state_commands(subparsers)
    _add_workspace_recovery_commands(subparsers)
    _add_progress_commands(subparsers)
    _add_bridge_commands(subparsers)
    _add_agent_mux_commands(subparsers)
    _add_diagnostics_commands(subparsers)
    _add_overseer_commands(subparsers)
    _add_workbench_commands(subparsers)
    _add_control_commands(subparsers)
    _add_inbox_commands(subparsers)
    _add_worker_message_commands(subparsers)
    _add_outbox_commands(subparsers)
    _add_assurance_commands(subparsers)

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
