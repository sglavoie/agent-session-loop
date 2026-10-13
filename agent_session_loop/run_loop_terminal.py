"""Terminalizing a run and expiring its guards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import (
    commit_settlement,
    harnesses,
    plans_errors,
    plans_models,
    plans_session_scope,
    readiness_gate,
    run_coordination,
    run_prompts,
    run_termination,
)
from .run_loop_context import RunContext


def _finish_run_locked(
    ctx: RunContext,
    exit_code: int,
    result_payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    ctx.reply_state_preserver.restore_if_unconsumed()
    return exit_code, result_payload


def _current_session_scope_or_none(
    ctx: RunContext,
    session: plans_models.SessionStatus | None,
) -> plans_session_scope.SessionScope | None:
    """Read a session's durable repository scope, or ``None`` if it cannot be.

    The run-start refusal below judges a retained scope violation against the
    scope now on disk, and a scope it cannot read is one it cannot clear a
    violation with, so an unreadable or invalid one reads as absent and the
    refusal stands.
    """
    workspace = ctx.verification_context.workspace
    if workspace is None or session is None:
        return None
    try:
        return run_prompts._session_scope(session, workspace)
    except plans_errors.PlanError:
        return None


def _terminalize(
    ctx: RunContext,
    exit_code: int,
    *,
    cancel: harnesses.AbortSignal | None = None,
    poll: Callable[[], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Close this run through the one coordinator every ending shares.

    The refusals above the loop end runs as truly as the loop's own exits
    do, so they terminalize here rather than finalizing on their own; the
    exit code each one already decided passes through untouched. Those
    refusals run before the control queue has a poller, so only the loop's
    own exit supplies ``cancel`` and ``poll``.

    A commit failure no longer stops the run, so the last thing decided
    before the audit runs is whether any group is still owed. The answer is
    written into the payload here, ahead of
    ``run_termination._terminalize_run``, because every reader that must not
    miss it — terminal adjudication, the assurance audit, and the final
    summary — is downstream of this point.

    The exit code each caller passes is a proposal, not a verdict: the
    adjudicator inside ``run_termination._terminalize_run`` may refuse a
    success and return a failing code instead, which is why the returned
    value is used rather than the one passed in.
    """
    with ctx.persistence.lock:
        ctx.payload[commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY] = (
            commit_settlement._unresolved_commit_groups(ctx.payload)
        )
    return _finish_run_locked(
        ctx,
        run_termination._terminalize_run(
            exit_code=exit_code,
            event_path=ctx.event_path,
            payload=ctx.payload,
            write_readiness_state=readiness_gate.write_readiness_state,
            progress=ctx.progress,
            persistence=ctx.persistence,
            audit_context=ctx.audit_context,
            cancel=cancel,
            poll=poll,
        ),
        ctx.payload,
    )


def _expire_run_guards(ctx: RunContext) -> None:
    """Disable this run's command gate on every exit path."""
    run_coordination.expire_run_guards(
        controller=ctx.guard_controller,
        event_path=ctx.event_path,
        payload=ctx.payload,
        control_payload=ctx.control_payload,
        persistence=ctx.persistence,
    )
