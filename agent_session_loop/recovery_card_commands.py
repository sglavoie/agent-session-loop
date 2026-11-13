"""Copyable repair commands for recovery cards."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .recovery_card_models import (
    RecoveryCopyCommand,
)


def _quote_command_path(path: Path | str) -> str:
    return shlex.quote(str(path))


def _preflight_repair_commands(plan_dir: Path) -> tuple[RecoveryCopyCommand, ...]:
    quoted_plan = _quote_command_path(plan_dir)
    quoted_skill_plan = _quote_command_path(str(plan_dir) + os.sep)
    return (
        RecoveryCopyCommand(
            label="Repair readiness",
            command=f"asl preflight {quoted_plan} --mark-loop-ready",
            description=(
                "Copy this into a shell after repairing plan-readiness findings."
            ),
        ),
        RecoveryCopyCommand(
            label="Iterate preflight",
            command=f"$plan-preflight-iterate {quoted_skill_plan}",
            description=(
                "Copy this into the harness when preflight findings need an "
                "interactive repair pass."
            ),
        ),
    )


def _verification_repair_commands(
    plan_dir: Path,
    *,
    session_id: str | None,
) -> tuple[RecoveryCopyCommand, ...]:
    quoted_plan = _quote_command_path(plan_dir)
    commands = [
        RecoveryCopyCommand(
            label="Run preflight",
            command=f"asl preflight {quoted_plan} --json",
            description=(
                "Copy this into a shell to inspect current contract and "
                "readiness findings."
            ),
        )
    ]
    if session_id:
        commands.insert(
            0,
            RecoveryCopyCommand(
                label="Re-run verification",
                command=(
                    "asl verify "
                    f"{quoted_plan} --session {shlex.quote(session_id)} --json"
                ),
                description=(
                    "Copy this into a shell after repairing the session or "
                    "its Completion Contract."
                ),
            ),
        )
    return tuple(commands)
