"""Parser definitions for the bridge, agent-mux and diagnostics commands."""

from __future__ import annotations

import argparse

from .cli_defaults import _BRIDGE_PROTOCOL_VERSION
from .cli_parser_support import _AgentSessionLoopArgumentParser, _group_epilogue


def _add_bridge_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl bridge` group."""
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
    bridge_serve_parser.set_defaults(handler_ref="bridge_server:bridge_serve_from_args")


def _add_agent_mux_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl agent-mux` group."""
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


def _add_diagnostics_commands(
    subparsers: argparse._SubParsersAction[_AgentSessionLoopArgumentParser],
) -> None:
    """The nested `asl diagnostics` group."""
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
