"""Advisory Agent Mux cockpit health check for install verification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import agent_mux_projection
from .install_verify_common import advisory, readable_path, same_file

#: Bound on the paired-cockpit probe. `amx doctor` talks to a local daemon over
#: a unix socket, so it is fast or it is broken; an advisory check must never be
#: what makes install verification hang.
AGENT_MUX_DOCTOR_TIMEOUT_SECONDS = 20


def _amx_doctor(path_env: str | None, home: Path) -> tuple[str | None, Any]:
    """Return the resolved ``amx`` and its parsed ``doctor --json`` report.

    The report is returned even when the command exits nonzero: `amx doctor`
    reserves a nonzero status for an unhealthy daemon, and an unhealthy daemon
    is precisely what this check wants to describe.
    """
    active_amx = shutil.which("amx", path=path_env)
    if active_amx is None:
        return None, None
    env = os.environ.copy()
    env["HOME"] = readable_path(home)
    if path_env is not None:
        env["PATH"] = path_env
    try:
        completed = subprocess.run(
            [active_amx, "doctor", "--json"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=AGENT_MUX_DOCTOR_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return active_amx, error
    try:
        return active_amx, json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return active_amx, error


def _check_agent_mux(
    *, path_env: str | None, home: Path, active_asl: str | None
) -> list[dict[str, Any]]:
    """Report the health of a paired Agent Mux cockpit, advisory by default.

    Every finding here is advisory because Agent Mux is an optional consumer of
    ASL's projection, never a dependency of it. The one thing this check must
    not do is stay silent about a pairing that has quietly broken — a daemon
    resolving a *different* `asl` than the one just verified is the failure mode
    an operator cannot see from either side alone.
    """
    active_amx, report = _amx_doctor(path_env, home)
    if active_amx is None:
        return [
            advisory(
                "agent-mux",
                "amx was not found on PATH; the cockpit integration is not "
                "installed, which ASL does not require",
                ok=True,
            )
        ]
    if isinstance(report, Exception):
        return [
            advisory(
                "agent-mux",
                f"{active_amx} doctor --json could not be read: {report}",
            )
        ]
    if not isinstance(report, dict):
        return [
            advisory(
                "agent-mux",
                f"{active_amx} doctor --json did not emit a report object",
            )
        ]

    checks = [
        advisory(
            "agent-mux",
            f"{active_amx} reports doctor schema "
            f"{report.get('schema_version')} and healthy="
            f"{report.get('healthy')}",
            ok=bool(report.get("healthy")),
        )
    ]

    daemon_block = report.get("daemon")
    asl_block = daemon_block.get("asl") if isinstance(daemon_block, dict) else None
    if not isinstance(asl_block, dict):
        checks.append(
            advisory(
                "agent-mux-bridge",
                "the daemon reports no ASL integration block; run "
                "`amx config asl-sync` to pair this installation",
            )
        )
        return checks

    mode = asl_block.get("mode")
    if mode == "disabled":
        checks.append(
            advisory(
                "agent-mux-bridge",
                "the cockpit integration is disabled in Agent Mux's config",
                ok=True,
            )
        )
        return checks

    if not asl_block.get("available"):
        reason = asl_block.get("reason") or "no reason reported"
        checks.append(
            advisory(
                "agent-mux-bridge",
                f"the daemon cannot reach ASL in {mode} mode: {reason}",
            )
        )
    else:
        checks.append(
            advisory(
                "agent-mux-bridge",
                f"the daemon has a live bridge in {mode} mode "
                f"(mutations_enabled={asl_block.get('mutations_enabled')})",
                ok=True,
            )
        )

    contract_set_id = asl_block.get("contract_set_id")
    if contract_set_id and contract_set_id != agent_mux_projection.CONTRACT_SET_ID:
        checks.append(
            advisory(
                "agent-mux-contract",
                f"the daemon paired with contract set {contract_set_id}; this "
                f"build implements {agent_mux_projection.CONTRACT_SET_ID}",
            )
        )

    # The check this whole block exists for: a daemon that resolved some other
    # `asl` than the one just verified will keep working and keep being wrong,
    # and neither `amx doctor` nor the rest of this command can see it alone.
    executable = asl_block.get("executable")
    if executable and active_asl and not same_file(executable, active_asl):
        checks.append(
            advisory(
                "agent-mux-executable",
                f"the daemon resolved {executable}, but the active asl on PATH "
                f"is {active_asl}; re-run `amx config asl-sync` to repair the "
                f"pairing",
            )
        )
    return checks
