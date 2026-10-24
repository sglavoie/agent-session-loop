"""The ``status`` payload and its command-line handler."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    events,
    harnesses,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_models,
    plans_registry,
    plans_snapshot,
    responses,
)
from .cli_defaults import program_name
from .state import (
    RUNS_DIR_NAME,
    AmbiguousPlanError,
    LockInfo,
    StateError,
    lock_path_for_plan,
    plan_state_dir,
    state_root,
)
from .state_abandonment import (
    ABANDONED_TRIGGER_STATUS,
    _live_run_id,
    _stale_runner_state,
    mark_run_abandoned,
    orphaned_state_candidates,
)
from .state_locks import _read_lock_path
from .state_mailbox_status import _mailbox_status_payload, _progress_journal_payload
from .state_payloads import (
    _lock_payload,
    _runner_state_payload,
    _session_compat_value,
    _session_payload,
    _status_counts,
)
from .state_plan_refs import resolve_plan_reference
from .state_status_text import _print_text_status


def _next_command(snapshot: plans_models.PlanSnapshot) -> dict[str, Any]:
    """Describe what to run next for ``asl status``.

    When a pending session remains, returns per-harness ``session-run``
    invocations under the shared ``next_steps`` envelope, ordered by best-effort
    invoking-harness detection so the most likely command surfaces first. When
    every session is complete, returns the exact ``{"complete": true,
    "message": ...}`` payload whose message matches what ``session-run`` prints.
    """
    next_pending = snapshot.next_pending
    if next_pending is None:
        return {
            "complete": True,
            "message": responses.ALL_SESSIONS_COMPLETE_MESSAGE,
        }
    step = responses.invocation_step(
        f"Implement pending session {next_pending.id} with session-run",
        harnesses.skill_invocations("session-run", f"{snapshot.plan_dir}/"),
    )
    return {
        "complete": False,
        "next_pending_id": next_pending.id,
        **responses.next_steps_payload([step]),
    }


def _command_guard_payload(
    runner_state: Mapping[str, Any],
    *,
    lock_info: LockInfo | None,
) -> dict[str, Any]:
    """Summarize the command gate a live run is enforcing.

    Guards belong to a run, so a plan with no live run reports an empty set
    rather than the last run's guards: enforcement expired with that run, and
    reporting it as active would describe protection nobody has. The coverage
    block travels with the summary because a guard's value is inseparable from
    what it does not cover.
    """
    from . import guards

    active: list[dict[str, Any]] = []
    run_active = bool(
        lock_info is not None
        and lock_info.active
        and runner_state.get("present")
        and not runner_state.get("paused")
        and not runner_state.get("run_finished_at")
    )
    control = runner_state.get("control")
    if run_active and isinstance(control, Mapping):
        recorded = control.get("guards")
        if isinstance(recorded, list):
            active = [dict(entry) for entry in recorded if isinstance(entry, Mapping)]
    return {
        "active": active,
        "count": len(active),
        "coverage": dict(guards.COMMAND_GATE_COVERAGE),
        "protected_path_coverage": dict(guards.PROTECTED_PATH_COVERAGE),
    }


def status_payload(
    snapshot: plans_models.PlanSnapshot,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    # Local imports avoid module cycles: repair planning depends on state for
    # its mutating materializer, and the assurance ledger depends on state for
    # its root, while this status path only uses their read-only projections
    # after state has finished importing. Neither can launch an audit.
    from . import (
        assurance_report,
        review_continuation,
        scope_recovery,
        workspace_evidence,
    )

    plan_state = plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    event_path = plan_state / events.EVENTS_FILE_NAME
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = _read_lock_path(lock_path, snapshot.plan_dir)
    next_pending = snapshot.next_pending
    runner_state = _runner_state_payload(state_path, lock_info=lock_info)
    stale_unfinished, stale_reason = _stale_runner_state(runner_state, lock_info)
    if stale_unfinished and mark_run_abandoned(
        plan_state,
        lock_info=lock_info,
        trigger=ABANDONED_TRIGGER_STATUS,
    ):
        # Recompute so this same call reports the terminal state it just wrote
        # instead of the orphan it found; this is also the only backfill path
        # for runs that died before abandonment tracking existed.
        runner_state = _runner_state_payload(state_path, lock_info=lock_info)
        stale_unfinished, stale_reason = _stale_runner_state(runner_state, lock_info)
    runner_state["stale_unfinished"] = stale_unfinished
    runner_state["stale_reason"] = stale_reason
    mailbox_status = _mailbox_status_payload(
        snapshot.plan_dir,
        state_dir,
        _live_run_id(runner_state, lock_info),
    )
    progress_journal = _progress_journal_payload(snapshot.plan_dir)
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, event_path
    ).to_payload()
    run_id = runner_state.get("run_id")
    assurance_projection_payload = assurance_report.build_run_report(
        runner_state.get("assurance"),
        run_dir=(
            plan_state / RUNS_DIR_NAME / str(run_id)
            if isinstance(run_id, str) and run_id
            else None
        ),
        state_dir=state_dir,
    )
    workspace_fields = {
        key: runner_state.get(key)
        for key in ("workspace_root", "registry_digest", "repositories")
        if runner_state.get(key) is not None
    }
    if not workspace_fields:
        registry = plans_registry.read_repository_registry(snapshot.plan_dir)
        metadata = plans_metadata.load_plan_metadata(snapshot.plan_dir)
        if registry is not None and isinstance(
            metadata, plans_metadata.WorkspacePlanMetadata
        ):
            workspace_root = plans_files.canonicalize_path(metadata.workspace_root)
            workspace_fields = {
                "workspace_root": str(workspace_root),
                "registry_digest": metadata.registry_digest,
                "repositories": [
                    {
                        "alias": repository.alias,
                        "path": repository.path,
                        "origin": repository.origin,
                        "resolved_root": str(
                            plans_files.canonicalize_path(
                                workspace_root / repository.path
                            )
                        ),
                    }
                    for repository in registry.repositories
                ],
            }
    cwd_fields = {"cwd": workspace_fields["workspace_root"]} if workspace_fields else {}
    retained = (
        workspace_evidence._latest_workspace_scope_repair_requirement(
            event_path, next_pending.id
        )
        if next_pending is not None and workspace_fields
        else None
    )
    recovery = (
        scope_recovery.recovery_diagnostics(
            snapshot.plan_dir, next_pending.id, retained, state_dir=state_dir
        )
        if retained is not None and next_pending is not None
        else None
    )
    if recovery is None:
        recovery = (
            scope_recovery.recovery_journal_projection(
                snapshot.plan_dir, state_dir=state_dir
            )
            or None
        )
    return responses.success_payload(
        "status",
        plan_dir=str(snapshot.plan_dir),
        plan_title=snapshot.plan_title,
        context_path=str(snapshot.context_path),
        manifest_path=str(snapshot.manifest_path),
        content_sha256=snapshot.content_sha256,
        path_hash=snapshot.path_hash,
        plan_hash=snapshot.plan_hash,
        state_dir=str(plan_state),
        state_path=str(state_path),
        events_path=str(event_path),
        runner_state=runner_state,
        external_change_recovery=recovery,
        spark=runner_state.get("spark"),
        active_turn=runner_state.get("active_turn"),
        active_verification=runner_state.get("active_verification"),
        waiting_for_reply=runner_state.get("waiting_for_reply"),
        waiting_for_reply_status=runner_state.get("waiting_for_reply_status"),
        mailbox=mailbox_status,
        counts=_status_counts(snapshot),
        next_pending=(
            _session_compat_value(next_pending.id) if next_pending is not None else None
        ),
        next_pending_id=next_pending.id if next_pending is not None else None,
        sessions=[_session_payload(session) for session in snapshot.sessions],
        lock=_lock_payload(lock_info, lock_path),
        progress_journal=progress_journal,
        review_resume=review_resume,
        assurance=runner_state.get("assurance"),
        command_guards=_command_guard_payload(runner_state, lock_info=lock_info),
        assurance_report=assurance_projection_payload,
        orphaned_state_candidates=orphaned_state_candidates(snapshot, state_dir),
        next_command=_next_command(snapshot),
        **cwd_fields,
        **workspace_fields,
    )


def status_from_args(args: argparse.Namespace) -> int:
    """Show parsed plan and runner status from CLI arguments.

    ``args.plan_dir`` (advertised as ``PLAN_DIR_OR_ID``) may be either an
    existing plan directory or a bare compact plan label printed after ``plan``
    by ``asl sessions``. Labels are resolved through the state corpus before
    inspection; direct paths always take precedence and never trigger a lookup.
    """
    try:
        resolved_plan_dir = resolve_plan_reference(
            args.plan_dir, state_root(args.state_dir)
        )
        snapshot = plans_snapshot.inspect_plan(resolved_plan_dir)
        payload = status_payload(snapshot, args.state_dir)
    except AmbiguousPlanError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "status",
                    reason="ambiguous-plan",
                    error=str(error),
                    paused=False,
                    details={
                        "state_root": str(error.state_root),
                        "candidates": [
                            str(candidate) for candidate in error.candidates
                        ],
                    },
                    plan_reference=str(args.plan_dir),
                )
            )
        else:
            print(
                f"{program_name()} status: {error}; "
                "pass a full plan directory path to disambiguate",
                file=sys.stderr,
            )
        return 1
    except (plans_errors.PlanError, StateError) as error:
        if args.json:
            reason = (
                "malformed-plan"
                if isinstance(error, plans_errors.PlanError)
                else "state-error"
            )
            responses.print_json(
                responses.error_payload(
                    "status",
                    reason=reason,
                    error=str(error),
                    paused=False,
                    plan_reference=str(args.plan_dir),
                )
            )
        else:
            print(f"{program_name()} status: {error}", file=sys.stderr)
        return 1

    if args.json:
        responses.print_json(payload)
    else:
        _print_text_status(payload)
    return 0
