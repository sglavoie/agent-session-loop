"""Shared constants and synthetic Copilot adapter for harness integration tests."""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_session_loop import (
    harness_copilot,
    harnesses,
    turn_disposition_failures,
    turn_disposition_mailbox,
    turn_disposition_models,
    turn_disposition_redirect,
    turn_dispositions,
)

#: Boundaries the session loop hands a terminating disposition to, and so the
#: modules the idle-baseline guard below has to search alongside the loop
#: itself. The mailbox reply wait is the run's one sanctioned opt-out from the
#: structural idle refresh. Search the entire disposition family so extracting
#: a branch cannot hide a second writer of ``runner_work_finished_at`` or
#: ``idle_baseline_pinned``. The loop is searched as the guard's own root.
IDLE_BASELINE_DELEGATES = (
    turn_dispositions,
    turn_disposition_models,
    turn_disposition_redirect,
    turn_disposition_mailbox,
    turn_disposition_failures,
)

# Bounds a wedged child so it cannot hang the suite; it is not a latency
# assertion, so it stays far above what a run needs on a machine already
# saturated by the parallel shards.
RUN_EXIT_TIMEOUT_SECONDS = 120

# The same backstop for a full ``agent_session_loop run`` driven as a
# subprocess. These invocations take roughly five seconds idle and half again
# that under the parallel shards, so the former inline bounds of 10-15s were
# close enough to real latency to fail as a timeout rather than as the
# assertion the test actually makes. Like the bound above it is not a latency
# assertion: it exists so a hung run surfaces as a failure instead of running
# out the suite's own clock.
LOOP_RUN_TIMEOUT_SECONDS = 120

# How long the fake harness parks at ``FAKE_CODEX_STREAM_GATE_DIR`` waiting for
# the test to release it, and how long a test waits for it to arrive there. A
# test that holds the gate is asserting on a turn that is still in flight, so
# the bound is a hang backstop on the observer, not a window the observation has
# to fit inside: the turn cannot end early no matter how loaded the machine is.
STREAM_GATE_TIMEOUT_SECONDS = 120

# The exact message the harness returned in run 20260814T210616107741Z-55923,
# which fell through to a fail-closed ``nonzero-exit`` and paused the run with
# four sessions still pending (ASI-125).
OBSERVED_SOCKET_CLOSURE = (
    "API Error: The socket connection was closed unexpectedly. For more "
    "information, pass `verbose: true` in the second argument to fetch()"
)

# The exact message the harness returned in run 20260825T004049070293Z-54505
# (turn-0001-session-1/last-message.txt). The classifier's literal
# "authentication" alternative did not match the verb form, so it fell through
# to the fail-closed ``nonzero-exit`` and the pause projected ``reply`` (ASI-135).
OBSERVED_AUTH_EXPIRY = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)

# The exact message the harness returned in run 20260903T172322002830Z-1846
# (turn-0001-session-1/last-message.txt). The provider says the failure is
# temporary and asks the caller to try again; the classifier had no rule for a
# 500 -- the broad transport rule deliberately covers only 502/503/504 -- so it
# fell through to the fail-closed ``nonzero-exit``, allowed zero retries, and
# paused for a reply nobody could usefully give (ASI-135).
OBSERVED_PROVIDER_SERVER_ERROR = (
    "API Error: 500 Unable to complete this request right now. Please try "
    "again. This is a server-side issue, usually temporary — try again in a "
    "moment. If it persists, check https://status.claude.com."
)

# The launch-time diagnostic every observed Codex turn opens with: its MCP
# client dies, the turn runs to completion anyway (ASI-1).
OBSERVED_LAUNCH_CLOSURE = (
    "2026-08-16T02:38:52.929927Z ERROR rmcp::transport::worker: worker quit "
    "with fatal: Transport channel closed, when "
    'Client(HttpRequest(HttpRequest("http/request failed: error sending '
    'request for url (http://127.0.0.1:8765/mcp)")))'
)


class _PythonScriptCopilotAdapter(harness_copilot.CopilotAdapter):
    """Run a synthetic Copilot harness through Python instead of direct exec.

    macOS attaches ``com.apple.provenance`` to locally generated executables
    and may open a Gatekeeper verification window when these temporary test
    scripts are executed directly.  Making the trusted Python interpreter the
    executable preserves the adapter arguments and process-group behavior
    without asking Gatekeeper to assess each disposable script.

    The turns below run on the plain exec transport rather than copilot's ACP
    one.  What they exercise is the machinery every transport sits on -- reader
    joins, stream accounting, process-group sweeps, the abort window -- and the
    scripts they drive are a few lines of ``print`` that could not answer a
    handshake.  The conversation itself has its own suite in
    tests/test_asl_copilot_acp.py.
    """

    def __init__(self, script: Path) -> None:
        super().__init__(executable=sys.executable)
        self.script = script

    def build_invocation(self, **kwargs: Any) -> harnesses.HarnessInvocation:
        invocation = super().build_invocation(**kwargs)
        return dataclasses.replace(invocation, transport=harnesses.TRANSPORT_EXEC)

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: harnesses.AccessProfile = harnesses.WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        argv = super().build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            env=env,
        )
        return [argv[0], str(self.script), *argv[1:]]
