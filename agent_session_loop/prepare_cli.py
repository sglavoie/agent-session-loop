"""Command-line entry point and output rendering for ``asl prepare``."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import responses
from .cli_defaults import program_name
from .plans_errors import PlanError
from .prepare import prepare_plan
from .prepare_models import (
    DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
    DEFAULT_VALIDATOR_JOBS,
    DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
)
from .prepare_rebind import _workspace_rebind_payload
from .prepare_results import (
    PREPARE_EXIT_REASON_CWD_ERROR,
    PREPARE_EXIT_REASON_INPUT_ERROR,
    PREPARE_EXIT_REASON_OK,
    PREPARE_EXIT_REASON_PLAN_STRUCTURE,
    PREPARE_EXIT_REASON_WORKSPACE_REBIND_FAILED,
    PREPARE_STEP_FAILED,
    PREPARE_STEP_OK,
    PREPARE_STEP_SKIPPED,
    PrepareResult,
    _WorkspaceRebindConcurrencyError,
)
from .prepare_validation_support import _positive_validator_control


def _result_payload_fields(result: PrepareResult) -> dict[str, Any]:
    fields = {
        "plan_dir": str(result.plan_dir) if result.plan_dir is not None else None,
        "source_path": result.source_path,
        "session_ids": list(result.session_ids),
        "steps": [step.to_dict() for step in result.steps],
        "blockers": [blocker.to_dict() for blocker in result.blockers],
        "corrections": [outcome.to_dict() for outcome in result.corrections],
        "baseline": result.baseline,
        "marked_loop_ready": result.marked_loop_ready,
        "run_command": result.run_command,
        "resume_command": result.resume_command,
    }
    if result.workspace_rebind is not None:
        fields["workspace_rebind"] = result.workspace_rebind
    if result.workspace is not None:
        fields.update(result.workspace)
        fields["workspace"] = result.workspace
    return fields


_PREPARE_STEP_MARKERS = {
    PREPARE_STEP_OK: "OK",
    PREPARE_STEP_FAILED: "FAIL",
    PREPARE_STEP_SKIPPED: "SKIP",
}


def _print_text_prepare(result: PrepareResult, *, plan_arg: str) -> None:
    print(f"Plan: {plan_arg}")
    if result.plan_dir is not None:
        print(f"Plan directory: {result.plan_dir}")
    if result.workspace is not None:
        print(f"Workspace root: {result.workspace.get('workspace_root')}")
        repositories = result.workspace.get("repositories")
        if isinstance(repositories, list):
            aliases = [
                str(row.get("alias")) for row in repositories if isinstance(row, dict)
            ]
            if aliases:
                print("Repositories: " + ", ".join(aliases))
    for step in result.steps:
        marker = _PREPARE_STEP_MARKERS.get(step.status, step.status.upper())
        print(f"  [{marker}] {step.name}: {step.detail}")
    for blocker in result.blockers:
        location = f" (session {blocker.session_id})" if blocker.session_id else ""
        print(f"  blocker [{blocker.stage}]{location}: {blocker.message}")
    if result.ok and result.run_command is not None:
        print(f"Run command: {result.run_command}")
    elif result.resume_command is not None:
        print(f"Resume with: {result.resume_command}")


def prepare_from_args(args: Any) -> int:
    """Run the ``prepare`` subcommand from CLI arguments."""
    want_json = bool(args.json)
    plan_arg = str(args.plan)
    resume = bool(args.resume)
    rebind_workspace = bool(getattr(args, "rebind_workspace", False))
    harness_name = args.harness
    harness_model = args.harness_model
    spark = bool(getattr(args, "spark", False))
    validator_harness = args.validator_harness
    validator_model = getattr(args, "validator_model", None)
    validator_effort = getattr(args, "validator_effort", None)
    validator_jobs = getattr(args, "validator_jobs", DEFAULT_VALIDATOR_JOBS)
    validator_timeout = getattr(
        args, "validator_timeout", DEFAULT_VALIDATOR_TIMEOUT_SECONDS
    )
    validator_heartbeat = getattr(
        args, "validator_heartbeat", DEFAULT_VALIDATOR_HEARTBEAT_SECONDS
    )
    review_harness = args.review_harness
    review_harness_model = args.review_harness_model
    common_fields = {
        "plan": plan_arg,
        "cwd": str(args.cwd),
        "harness": harness_name,
        "harness_model": harness_model,
        "spark": spark,
        "validator_harness": validator_harness,
        "validator_model": validator_model,
        "validator_effort": validator_effort,
        "validator_jobs": validator_jobs,
        "validator_timeout": validator_timeout,
        "validator_heartbeat": validator_heartbeat,
        "validator": {
            "harness": validator_harness,
            "model": validator_model,
            "effort": validator_effort,
            "jobs": validator_jobs,
            "timeout_seconds": validator_timeout,
            "heartbeat_seconds": validator_heartbeat,
        },
        "review_harness": review_harness,
        "review_harness_model": review_harness_model,
        "resume": resume,
        "base_dir": str(args.base_dir) if args.base_dir else None,
    }
    if getattr(args, "force_new", False):
        common_fields["force_new"] = True
    if rebind_workspace:
        common_fields["rebind_workspace"] = True

    def _emit_input_error(
        reason: str,
        error: str,
        *,
        workspace_rebind: dict[str, Any] | None = None,
    ) -> int:
        if want_json:
            extra_fields: dict[str, Any] = {}
            if workspace_rebind is not None:
                extra_fields["workspace_rebind"] = workspace_rebind
            responses.print_json(
                responses.error_payload(
                    "prepare",
                    reason=reason,
                    error=error,
                    paused=False,
                    **common_fields,
                    **extra_fields,
                )
            )
        else:
            print(f"{program_name()} prepare: {error}", file=sys.stderr)
        return 1

    try:
        validator_jobs = _positive_validator_control(
            validator_jobs, name="--validator-jobs"
        )
        validator_timeout = _positive_validator_control(
            validator_timeout, name="--validator-timeout"
        )
        validator_heartbeat = _positive_validator_control(
            validator_heartbeat, name="--validator-heartbeat"
        )
    except PlanError as error:
        return _emit_input_error(PREPARE_EXIT_REASON_INPUT_ERROR, str(error))

    if review_harness is not None and review_harness == harness_name:
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            "--review-harness must differ from --harness",
        )
    if spark and harness_name != "codex":
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            "--spark requires the implementation --harness to be 'codex'",
        )
    if review_harness_model is not None and review_harness is None:
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            "--review-harness-model requires --review-harness",
        )
    if rebind_workspace and not resume:
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            "--rebind-workspace requires --resume",
        )
    cwd_path = Path(os.path.expanduser(str(args.cwd)))
    if not cwd_path.is_dir():
        return _emit_input_error(
            PREPARE_EXIT_REASON_CWD_ERROR,
            f"cwd is not a directory: {args.cwd}",
        )
    plan_path = Path(os.path.expanduser(plan_arg))
    if resume:
        if (
            not plan_path.is_dir()
            or not (plan_path / "manifest.md").is_file()
            or not (plan_path / "context.md").is_file()
        ):
            return _emit_input_error(
                PREPARE_EXIT_REASON_INPUT_ERROR,
                (
                    "--resume requires an existing plan directory containing "
                    f"manifest.md and context.md: {plan_arg}"
                ),
            )
    elif plan_path.is_dir():
        is_split_plan = (plan_path / "manifest.md").is_file() and (
            plan_path / "context.md"
        ).is_file()
        hint = (
            "; pass --resume to resume a prepared plan directory"
            if is_split_plan
            else "; pass the source plan markdown file"
        )
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            f"source plan path is a directory, not a file: {plan_arg}{hint}",
        )
    elif not plan_path.is_file():
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            f"source plan file does not exist: {plan_arg}",
        )

    try:
        result = prepare_plan(
            None if resume else plan_path,
            cwd=args.cwd,
            harness=harness_name,
            harness_model=harness_model,
            spark=spark,
            validator_harness=validator_harness,
            validator_model=validator_model,
            validator_effort=validator_effort,
            validator_jobs=validator_jobs,
            validator_timeout=validator_timeout,
            validator_heartbeat=validator_heartbeat,
            review_harness=review_harness,
            review_harness_model=review_harness_model,
            base_dir=args.base_dir,
            force_new=bool(getattr(args, "force_new", False)),
            resume_plan_dir=plan_path if resume else None,
            unsafe_bypass=getattr(args, "unsafe_bypass", True),
            state_dir=getattr(args, "state_dir", None),
            rebind_workspace=rebind_workspace,
        )
    except _WorkspaceRebindConcurrencyError as error:
        return _emit_input_error(
            PREPARE_EXIT_REASON_WORKSPACE_REBIND_FAILED,
            str(error),
            workspace_rebind=_workspace_rebind_payload(
                error.old_workspace,
                error.new_workspace,
                failure_stage=error.stage,
                error=str(error),
            ),
        )
    except PlanError as error:
        return _emit_input_error(
            (
                PREPARE_EXIT_REASON_INPUT_ERROR
                if rebind_workspace
                else PREPARE_EXIT_REASON_PLAN_STRUCTURE
            ),
            str(error),
        )

    fields = {**common_fields, **_result_payload_fields(result)}
    if result.ok:
        if want_json:
            responses.print_json(
                responses.success_payload(
                    "prepare", reason=PREPARE_EXIT_REASON_OK, **fields
                )
            )
        else:
            _print_text_prepare(result, plan_arg=plan_arg)
        return 0

    error_message = (
        result.blockers[0].message
        if result.blockers
        else f"preparation failed: {result.reason}"
    )
    if want_json:
        responses.print_json(
            responses.error_payload(
                "prepare",
                reason=result.reason,
                error=error_message,
                paused=False,
                **fields,
            )
        )
    else:
        _print_text_prepare(result, plan_arg=plan_arg)
        print(f"{program_name()} prepare: {error_message}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
