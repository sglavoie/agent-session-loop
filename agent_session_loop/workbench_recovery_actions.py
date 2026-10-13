"""Operator action cards and copyable repair commands for recovery cards."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .workbench_recovery_models import (
    RecoveryActionCard,
    RecoveryActionTarget,
    RecoveryCopyCommand,
)


def _endpoint_action(
    *,
    action_id: str,
    label: str,
    description: str,
    endpoint: str,
    payload: Mapping[str, Any],
    target: RecoveryActionTarget | None = None,
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id=action_id,
        label=label,
        description=description,
        kind="endpoint",
        target=target,
        endpoint=endpoint,
        method="POST",
        payload=payload,
    )


def _inspect_action(
    description: str,
    *,
    target: RecoveryActionTarget = "artifacts",
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id="inspect-artifacts",
        label="Inspect artifacts",
        description=description,
        kind="inspect",
        target=target,
    )


def _navigate_action(
    *,
    action_id: str,
    label: str,
    description: str,
    target: RecoveryActionTarget,
) -> RecoveryActionCard:
    return RecoveryActionCard(
        id=action_id,
        label=label,
        description=description,
        kind="navigate",
        target=target,
    )


def _retry_run_action(plan_dir_payload: Mapping[str, Any]) -> RecoveryActionCard:
    return _endpoint_action(
        action_id="retry-run",
        label="Retry run",
        description=(
            "Launch the existing runner flow after applying the recovery guidance."
        ),
        endpoint="/api/launch",
        payload=plan_dir_payload,
        target="launch",
    )


def _profile_action(description: str) -> RecoveryActionCard:
    return _navigate_action(
        action_id="review-profile",
        label="Review profile",
        description=description,
        target="profile",
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
