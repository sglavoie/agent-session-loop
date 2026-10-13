"""Parser definitions for the workbench commands."""

from __future__ import annotations

import argparse

from .cli_parser_support import (
    _add_workbench_serve_args,
    _AgentSessionLoopArgumentParser,
    _group_epilogue,
)


def _add_workbench_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl workbench` group."""
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

    workbench_connect_parser = workbench_sub.add_parser(
        "connect",
        help="Publish observations to a remote Workbench service.",
    )
    workbench_connect_parser.add_argument(
        "--target-id",
        required=True,
        metavar="ID",
        help="Identity of the service being published to",
    )
    workbench_connect_parser.add_argument(
        "--origin",
        required=True,
        metavar="URL",
        help="https origin of the service (frames POST to /v1/ingest)",
    )
    workbench_connect_parser.add_argument(
        "--token-file",
        required=True,
        metavar="FILE",
        help=('JSON file holding {"connector_token": ...}, readable only by its owner'),
    )
    workbench_connect_parser.add_argument(
        "--connector-id",
        default=None,
        metavar="ID",
        help="Identity this Mac publishes under (default: derived from hostname)",
    )
    workbench_connect_parser.add_argument(
        "--state-dir",
        default=None,
        metavar="DIR",
        help="Override operational state directory",
    )
    workbench_connect_parser.add_argument(
        "--plan-root",
        action="append",
        default=None,
        metavar="DIR",
        help="Extra plan root directory to observe (may be repeated)",
    )
    workbench_connect_parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="How often to publish liveness (default: 15)",
    )
    workbench_connect_parser.add_argument(
        "--observation-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="How often to walk the corpus (default: 30)",
    )
    workbench_connect_parser.add_argument(
        "--allow-remote-actions",
        action="store_true",
        help=("Accept mutating commands from the service (default: observation-only)"),
    )
    workbench_connect_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON startup output",
    )
    workbench_connect_parser.set_defaults(
        handler_ref="workbench_connector:workbench_connect_from_args",
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
        handler_ref="workbench_archive:workbench_pin_from_args",
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
        handler_ref="workbench_archive:workbench_unpin_from_args",
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
