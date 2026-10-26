"""The rehearsal: everything a turn would resolve, and nothing it would run.

Every other boundary in this package acts. This one only resolves. ``asl
doctor`` is the command an operator runs *before* committing a plan to a run:
it canonicalizes the working directory, inspects the plan, binds the workspace,
picks the next pending session, renders the prompt that session would receive,
and builds the harness invocation that would carry it — then prints all of it
and returns. The harness is located but never launched, the last-message path
is computed but never written, and the plan's lock is never touched, because
the whole value of the report is that reading it costs nothing.

Two halves live here.

* **The report.** :func:`doctor_from_args` is a staged resolution, and the
  stages are ordered by what has been established: the working directory, then
  the plan snapshot, then the workspace binding, then the prompt, then the
  harness invocation. Each stage may refuse, and a refusal carries exactly what
  the stages before it resolved — which is why ``cwd`` and ``plan_dir`` appear
  on the later error payloads and not on the first.
* **The refusals.** :func:`_print_doctor_error` is the single printer every
  stage returns through, so a caller parsing ``--json`` sees one failure shape
  regardless of which stage refused, always with the selected model and effort
  attached.

Durable records this boundary writes: **none**. Doctor resolves paths that a
run would later write — ``state.plan_state_dir(...) / "last-message-doctor.txt"``
most visibly — and writes to none of them; ``tests/test_asl_run_doctor.py``
pins that as a flat prohibition so a first writer appearing here is a test
failure rather than a review catch. :func:`_print_doctor_error` resolves
``sys.stderr`` directly, which few surfaces in the package may do: like
``run_outcome``'s text printer it runs where no ``run_reporting`` coordinator
exists to race, because no run is live.

Nothing here imports the runner, and this boundary needs no ``Protocol`` seam:
the parsed namespace is the only thing that enters, and every collaborator is
read at its true owner — the plan snapshot, the registry and the session-kind
constants at the ``plans_*`` modules, the workspace binding and its bounded
rows at ``workspaces``, the verification context at ``verification``, the worker mode
and the packet at ``task_packet``, the review-session test at
``review_session_contract``, the prompt and the invocation at ``run_prompts``,
the model, effort and unsafe-bypass selection at ``run_selection``, the
invocation payload at ``run_commit_invocation``, the model/effort lines at
``run_outcome``,
the harness config at ``config``, the adapter at ``harnesses``, the state
directory at ``state`` and the response envelopes at ``responses``.

Invariants that survive the move:

* **A doctor that cannot bind the prepared workspace refuses.** A registered
  plan whose recorded metadata names a different workspace root or a different
  registry digest is an error, not a note in the report: reporting it would be
  a green light for a run that cannot start, and the digest travels on the
  refusal so the operator can see which binding they have.
* **The preview goes through the builders a turn uses.** The prompt is the task
  packet when ``task_packet._worker_mode()`` selects packet mode and
  ``run_prompts.build_session_prompt`` otherwise, and the invocation is
  ``run_prompts.build_harness_invocation`` with the same resolved root, model,
  effort and unsafe-bypass a turn would pass. A preview built any other way
  could drift from the thing it previews.
* **No pending session means no scope, not a failure.** A plan with nothing
  left to run still reports its harness, its executable and its workspace; the
  prompt is built with no workspace and no session scope rather than the
  command refusing.
* **The turn plan is printed whenever the transport carries one.** Under a
  transport whose argv no longer states the access profile, a report that
  printed only argv would no longer show what the turn is allowed to do.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import (
    config,
    harnesses,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_payloads,
    plans_progress,
    plans_registry,
    plans_snapshot,
    responses,
    review_session_contract,
    run_commit_invocation,
    run_outcome,
    run_prompts,
    run_selection,
    state,
    task_packet,
    task_packet_lookups,
    verification_models,
    workspaces,
)
from .cli_defaults import program_name
from .cli_parser_support import flag_refusal


def _print_doctor_error(
    args: argparse.Namespace,
    *,
    reason: str,
    error: Exception,
    cwd: Path | None = None,
    plan_dir: Path | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    if args.json:
        payload = responses.error_payload(
            "doctor",
            reason=reason,
            error=str(error),
            paused=False,
            details=details,
            harness=args.harness,
            cwd=str(cwd) if cwd is not None else str(args.cwd),
            plan_dir=str(plan_dir) if plan_dir is not None else str(args.plan_dir),
        )
        if args.harness in harnesses.HARNESS_NAMES:
            # A refused harness has no model or effort to resolve.
            payload.update(run_selection._selected_model_effort_payload(args))
        responses.print_json(payload)
    else:
        print(f"{program_name()} doctor: {error}", file=sys.stderr)
    return 1


def doctor_from_args(args: argparse.Namespace) -> int:
    """Check local harness prerequisites from CLI arguments."""
    harness_name = getattr(args, "harness", None)
    if harness_name not in harnesses.HARNESS_NAMES:
        # Validated here rather than by argparse, so a missing or unknown
        # harness keeps its JSON `input-error` under --json instead of
        # argparse's plain-text exit 2.
        requirement = f"must be one of {', '.join(harnesses.HARNESS_NAMES)}"
        message = (
            f"--harness is required and {requirement}"
            if harness_name is None
            else flag_refusal("--harness", requirement, harness_name)
        )
        return _print_doctor_error(
            args, reason="input-error", error=ValueError(message)
        )
    refusal = plans_errors.empty_plan_argument_refusal(args.plan_dir)
    if refusal is not None:
        return _print_doctor_error(
            args, reason="input-error", error=ValueError(refusal)
        )
    try:
        cwd = plans_files.canonicalize_path(args.cwd)
        if not cwd.is_dir():
            raise plans_errors.PlanError(
                f"cwd does not exist or is not a directory: {cwd}"
            )
    except (plans_errors.PlanError, OSError) as error:
        return _print_doctor_error(args, reason="input-error", error=error)

    try:
        repository_registry = plans_registry.read_repository_registry(args.plan_dir)
        snapshot = plans_snapshot.inspect_plan(
            args.plan_dir,
            repository_registry=repository_registry,
        )
    except plans_errors.PlanError as error:
        return _print_doctor_error(
            args,
            reason=plans_errors.plan_error_reason(error, "malformed-plan"),
            error=error,
            cwd=cwd,
        )

    verification_context = verification_models.VerificationContext.legacy(cwd)
    if repository_registry is not None:
        try:
            verification_context = (
                verification_models.VerificationContext.resolved_workspace(
                    workspaces.resolve_workspace(cwd, repository_registry)
                )
            )
        except workspaces.WorkspaceError as error:
            return _print_doctor_error(
                args,
                reason="workspace-error",
                error=error,
                cwd=cwd,
                plan_dir=snapshot.plan_dir,
                details={"registry_digest": repository_registry.digest},
            )
        metadata = plans_metadata.load_plan_metadata(snapshot.plan_dir)
        if metadata is not None and (
            not isinstance(metadata, plans_metadata.WorkspacePlanMetadata)
            or metadata.workspace_root != str(verification_context.root)
            or metadata.registry_digest != repository_registry.digest
        ):
            return _print_doctor_error(
                args,
                reason="workspace-error",
                error=plans_errors.PlanError(
                    "doctor workspace does not match the prepared workspace binding"
                ),
                cwd=cwd,
                plan_dir=snapshot.plan_dir,
                details={"registry_digest": repository_registry.digest},
            )

    prompt_workspace = (
        verification_context.workspace if snapshot.next_pending is not None else None
    )
    prompt_scope = (
        run_prompts._session_scope(snapshot.next_pending, prompt_workspace)
        if snapshot.next_pending is not None and prompt_workspace is not None
        else None
    )
    if (
        prompt_workspace is not None
        and prompt_scope is not None
        and task_packet._worker_mode() == task_packet.WORKER_MODE_PACKET
    ):
        assert snapshot.next_pending is not None
        prompt = task_packet.build_task_packet(
            args.harness,
            snapshot.plan_dir,
            session_id=snapshot.next_pending.id,
            session_kind=(
                plans_payloads.SESSION_KIND_REVIEW
                if review_session_contract.is_generated_review_session_file(
                    snapshot.next_pending.path
                )
                else plans_payloads.SESSION_KIND_IMPLEMENTATION
            ),
            predecessor_id=task_packet_lookups._predecessor_session_id(
                snapshot, snapshot.next_pending.id
            ),
            has_expansion=task_packet_lookups._session_has_expansion(
                snapshot.plan_dir, snapshot.next_pending.id
            ),
            has_progress=(
                snapshot.plan_dir / plans_progress.PROGRESS_FILE_NAME
            ).is_file(),
            workspace=prompt_workspace,
            session_scope=prompt_scope,
        ).render()
    else:
        prompt = run_prompts.build_session_prompt(
            args.harness,
            snapshot.plan_dir,
            workspace=prompt_workspace,
            session_scope=prompt_scope,
        )

    try:
        harness_config = config.harness_config_payload()
        adapter = harnesses.get_adapter(args.harness)
        executable_path = adapter.resolve_executable()
        if executable_path is None:
            raise harnesses.HarnessError(
                f"{args.harness} executable not found on PATH: {adapter.executable}"
            )

        last_message_path = (
            state.plan_state_dir(snapshot.plan_dir) / "last-message-doctor.txt"
        )
        model_effort = run_selection._selected_model_effort_payload(args)
        invocation = run_prompts.build_harness_invocation(
            args.harness,
            cwd=verification_context.root,
            plan_dir=snapshot.plan_dir,
            unsafe_bypass=run_selection._unsafe_bypass(args),
            last_message_path=last_message_path,
            model=run_selection._requested_model(args, harness=args.harness),
            effort=args.effort,
            prompt=prompt,
        )
    except config.ConfigError as error:
        return _print_doctor_error(
            args,
            reason="config-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )
    except harnesses.HarnessError as error:
        return _print_doctor_error(
            args,
            reason="harness-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )
    except OSError as error:
        return _print_doctor_error(
            args,
            reason="os-error",
            error=error,
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
        )

    if args.json:
        doctor_fields: dict[str, Any] = {}
        if verification_context.workspace is not None:
            doctor_fields = {
                "workspace_root": str(verification_context.root),
                "registry_digest": verification_context.registry_digest,
                "repositories": [
                    {
                        "alias": repository.alias,
                        "path": repository.repository.path,
                        "origin": repository.origin,
                        "resolved_root": str(repository.root),
                    }
                    for repository in verification_context.workspace.repositories
                ],
            }
        responses.print_json(
            responses.success_payload(
                "doctor",
                reason="ok",
                harness=adapter.name,
                **model_effort,
                harness_config=harness_config,
                executable=str(executable_path),
                resolved_executable=str(executable_path),
                cwd=str(cwd),
                plan_dir=str(snapshot.plan_dir),
                next_pending=(
                    plans_payloads.session_compat_value(snapshot.next_pending.id)
                    if snapshot.next_pending is not None
                    else None
                ),
                next_pending_id=(
                    snapshot.next_pending.id
                    if snapshot.next_pending is not None
                    else None
                ),
                prompt=prompt,
                invocation=run_commit_invocation._build_invocation_payload(
                    invocation,
                    model_effort=model_effort,
                    verification_context=verification_context,
                ),
                invocation_argv=invocation.argv,
                **doctor_fields,
            )
        )
        return 0

    print(f"Harness: {adapter.name}")
    run_outcome._print_model_effort_lines(model_effort)
    print(f"Harness config: {harness_config['path']}")
    configured_harnesses = harness_config["configured_harnesses"]
    if configured_harnesses:
        print(f"Configured harnesses: {', '.join(configured_harnesses)}")
    else:
        print("Configured harnesses: none")
    print(f"Executable: {executable_path}")
    if verification_context.workspace is None:
        print(f"CWD: {cwd}")
    else:
        print(f"Workspace root: {cwd}")
        print("Repositories:")
        visible, omitted = workspaces.bounded_repository_rows(
            verification_context.workspace.repositories
        )
        for repository in visible:
            print(
                f"  {repository.alias}: {repository.repository.path} -> "
                f"{repository.root} ({repository.origin})"
            )
        if omitted:
            print(f"  ... {omitted} more repositories")
    print(f"Plan: {snapshot.plan_dir}")
    if snapshot.next_pending is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {snapshot.next_pending.id}")
    print(f"Prompt: {prompt}")
    print(f"Transport: {invocation.transport}")
    print("Argv:")
    for arg in invocation.argv:
        print(f"  {arg}")
    if invocation.turn_plan is not None:
        # Argv no longer states the access profile under this transport, so a
        # doctor report that printed only argv would no longer show what the
        # turn is allowed to do.
        print("Turn plan:")
        payload = json.dumps(
            invocation.turn_plan.to_payload(), indent=2, sort_keys=True
        )
        for line in payload.splitlines():
            print(f"  {line}")
    return 0
