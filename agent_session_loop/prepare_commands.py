"""Rendering of the ``asl run`` and resume commands."""

from __future__ import annotations

import shlex
from pathlib import Path


def _format_run_command(
    plan_dir: Path,
    *,
    cwd: Path,
    harness_name: str,
    harness_model: str | None,
    spark: bool,
    review_harness: str | None,
    review_harness_model: str | None,
) -> str:
    argv = [
        "asl",
        "run",
        str(plan_dir),
        "--cwd",
        str(cwd),
        "--harness",
        harness_name,
    ]
    if harness_model:
        argv.extend(["--harness-model", harness_model])
    if spark:
        argv.append("--spark")
    if review_harness:
        argv.extend(["--review-harness", review_harness])
    if review_harness_model:
        argv.extend(["--review-harness-model", review_harness_model])
    return shlex.join(argv)


def _format_resume_command(
    plan_dir: Path,
    *,
    cwd: Path,
    harness_name: str,
    harness_model: str | None,
    spark: bool,
    validator_harness: str,
    validator_model: str | None,
    validator_effort: str | None,
    validator_jobs: int,
    validator_timeout: int,
    validator_heartbeat: int,
    review_harness: str | None,
    review_harness_model: str | None,
    rebind_workspace: bool = False,
) -> str:
    argv = [
        "asl",
        "prepare",
        "--resume",
        str(plan_dir),
        "--cwd",
        str(cwd),
        "--harness",
        harness_name,
        "--validator-harness",
        validator_harness,
        "--validator-jobs",
        str(validator_jobs),
        "--validator-timeout",
        str(validator_timeout),
        "--validator-heartbeat",
        str(validator_heartbeat),
    ]
    if rebind_workspace:
        argv.append("--rebind-workspace")
    if harness_model:
        argv.extend(["--harness-model", harness_model])
    if spark:
        argv.append("--spark")
    if validator_model is not None:
        argv.extend(["--validator-model", validator_model])
    if validator_effort is not None:
        argv.extend(["--validator-effort", validator_effort])
    if review_harness:
        argv.extend(["--review-harness", review_harness])
    if review_harness_model:
        argv.extend(["--review-harness-model", review_harness_model])
    return shlex.join(argv)
