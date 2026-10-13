"""Command-line argument parser for the autopilot helper."""

from __future__ import annotations

import argparse

from asl_autopilot_constants import HELPER_DESCRIPTION, HELPER_NAME

from agent_session_loop import mailbox_validation as _mailbox_validation


def _supervision_id_argument(value: str) -> str:
    if not _mailbox_validation.is_supervision_id(value):
        raise argparse.ArgumentTypeError("--supervision-id must be a UUID")
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    """Return the argument parser for the autopilot helper CLI.

    The surface is intentionally small and stable so the wrapper, the SKILL
    workflow, and later sessions can rely on it:

    - ``--command`` carries the exact ``asl run ...`` command to supervise.
    - ``--json`` requests machine-readable output on stdout.
    - ``--reply-stdin`` resumes a paused run using reply text streamed on stdin.
    - ``--wait-quota-reset`` opts in to sleeping through a quota reset instead
      of stopping ``user_required``.
    - ``--supervision-id`` carries the launching agent's correlation identity
      onto the terminal ``supervision-end`` record.
    - ``--detach`` launches the supervision into its own session and returns,
      so the run does not die with the shell that started it.
    - ``--supervision-log-dir`` overrides where a detached launch writes its
      result and stderr.
    """
    parser = argparse.ArgumentParser(
        prog=HELPER_NAME,
        description=HELPER_DESCRIPTION,
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Exact `asl run ...` command to supervise (env assignments and quoted "
        "args allowed).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON on stdout (stderr stays reserved for "
        "progress).",
    )
    parser.add_argument(
        "--reply-stdin",
        action="store_true",
        help="Resume a paused run using inferred reply text streamed on stdin.",
    )
    parser.add_argument(
        "--supervision-id",
        type=_supervision_id_argument,
        metavar="UUID",
        help=(
            "Correlation identity for this launch, stamped on the terminal "
            "supervision-end record so the launching agent can match it "
            "(default: a fresh uuid this process mints)."
        ),
    )
    parser.add_argument(
        "--wait-quota-reset",
        action="store_true",
        help=(
            "On a quota-exhausted pause carrying a parsed reset time, sleep "
            "until that reset (plus a buffer) and relaunch instead of stopping "
            "for a human."
        ),
    )
    parser.add_argument(
        "--detach",
        action="store_true",
        help=(
            "Run the supervision in its own session and return immediately, "
            "printing a JSON launch handshake. The supervision outcome lands in "
            "`result.json` under the log directory instead of on this stdout."
        ),
    )
    parser.add_argument(
        "--supervision-log-dir",
        metavar="DIR",
        help=(
            "Where a `--detach` launch writes `result.json` and `stderr.log` "
            "(default: <plan state dir>/supervision/<supervision-id>/). Ignored "
            "without --detach."
        ),
    )
    return parser
