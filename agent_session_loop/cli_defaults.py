"""Build-time defaults mirrored from heavier modules, plus the program name.

Light commands use these instead of importing the heavier modules to build the
parser. A test keeps them in sync with their authoritative definitions.

``program_name`` lives here too because this module imports nothing from the
package: every handler can prefix its error lines with the invoked program name
without pulling in, or cycling back through, the heavier CLI helpers."""

from __future__ import annotations

import sys
from pathlib import Path

_FALLBACK_PROGRAM_NAME = "asl"


def program_name(argv0: str | None = None) -> str:
    """Return the name the CLI was invoked as, for usage lines and errors.

    ``cli.main`` names its parser with this, and every handler prefixes its
    error lines with it, so ``asl status`` reports both as ``asl status`` and
    the ``agent-session-loop`` wrapper as ``agent-session-loop status``.
    Interpreter-level launches (``python -m agent_session_loop`` reports
    ``__main__.py``, test runners report their own script or a
    ``python -m unittest`` string) fall back to ``asl``.
    """
    raw = argv0 if argv0 is not None else (sys.argv[0] if sys.argv else "")
    name = Path(raw).name if raw else ""
    if (
        not name
        or name == "-c"
        or name.endswith(".py")
        or any(character.isspace() for character in name)
    ):
        return _FALLBACK_PROGRAM_NAME
    return name


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


_PREPARE_DEFAULT_VALIDATOR_JOBS = 2  # prepare_models.DEFAULT_VALIDATOR_JOBS


_PREPARE_DEFAULT_VALIDATOR_TIMEOUT_SECONDS = (
    900  # prepare_models.DEFAULT_VALIDATOR_TIMEOUT_SECONDS
)


_PREPARE_DEFAULT_VALIDATOR_HEARTBEAT_SECONDS = (
    30  # prepare_models.DEFAULT_VALIDATOR_HEARTBEAT_SECONDS
)


_CONTROL_DEFAULT_ABORT_GRACE_SECONDS = 120  # control_queue.DEFAULT_ABORT_GRACE_SECONDS


_CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS = (
    60.0  # control_cli.DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS
)


_COMMIT_DEFAULT_GENERATION_TIMEOUT_SECONDS = (
    900  # commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS
)


_REDIRECT_DEFAULT_TTL_SECONDS = 120  # redirect_store_vocab.REDIRECT_DEFAULT_TTL_SECONDS


_REDIRECT_MAX_TTL_SECONDS = 3600  # redirect_store_vocab.REDIRECT_MAX_TTL_SECONDS


_REDIRECT_ACCEPTANCE_WAIT_SECONDS = 2.0  # redirect_admission.ACCEPTANCE_WAIT_SECONDS


_REDIRECT_MAX_ACCEPTANCE_WAIT_SECONDS = (
    30.0  # redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS
)


_REDIRECT_DEFAULT_ACK_WAIT_SECONDS = 30.0  # redirect_ack.DEFAULT_ACK_WAIT_SECONDS


_REDIRECT_MAX_ACK_WAIT_SECONDS = 900.0  # redirect_ack.MAX_ACK_WAIT_SECONDS


_INTERRUPTED_EXIT = 130  # turn_execution_constants.INTERRUPTED_EXIT


_BRIDGE_PROTOCOL_VERSION = 2  # bridge_protocol.PROTOCOL_VERSION
