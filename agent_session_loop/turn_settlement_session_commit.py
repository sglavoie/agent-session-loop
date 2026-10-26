"""The per-session commit of the settlement pass.

Under ``--commit-per-session``, or owed by a repair child that owns its own
commit. A commit that fails does not end the run: it is recorded on the turn
and soft-failed onto the run's owed group.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import commit_settlement, durability, run_selection
from .run_errors import RunError
from .turn_settlement_types import RunContext, SoftFailCommit


def commit_session_if_owed(
    ctx: RunContext,
    *,
    session_id: str,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    is_review_session: bool,
    is_repair_session: bool,
    commit_mode: str | None,
    soft_fail_commit: SoftFailCommit,
) -> None:
    """Commit the session's changes when the commit mode or a repair owes it."""
    repair_owned_commit = is_repair_session and commit_mode is None
    if is_review_session or not (
        commit_mode == run_selection.COMMIT_MODE_PER_SESSION or repair_owned_commit
    ):
        return
    session_targets: list[dict[str, Any]] = []
    try:
        session_targets = (
            commit_settlement._registered_session_commit_targets(
                ctx.verification_context,
                turn_payload.get("workspace_write_guard", {}),
            )
            if ctx.verification_context.workspace is not None
            else commit_settlement._dirty_commit_targets(ctx.verification_context)
        )
        if repair_owned_commit:
            session_targets = [
                target
                for target in session_targets
                if not commit_settlement._commit_target_is_clean(target)
            ]
        if session_targets or not repair_owned_commit:
            commit_group, failed_commit = commit_settlement._run_commit_group(
                event_path=ctx.event_path,
                payload=ctx.payload,
                progress=ctx.progress,
                verification_context=ctx.verification_context,
                run_dir=ctx.run_dir,
                run_id=ctx.run_id,
                session_id=session_id,
                targets=session_targets,
                generation=ctx.commit_generation,
                persistence=ctx.persistence,
            )
        else:
            commit_group = None
            failed_commit = None
    except RunError as error:
        commit_group = None
        failed_commit = None
        commit_error: str | None = str(error)
    else:
        commit_error = None
    if repair_owned_commit:
        with ctx.persistence.lock:
            turn_payload["repair_commit"] = {
                "mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
                # A failure is checked first: one raised while the targets were
                # being read leaves none, which is not the same as nothing dirty.
                "result": (
                    "failed"
                    if failed_commit is not None or commit_error is not None
                    else ("no-changes" if not session_targets else "committed")
                ),
                "commit_group": commit_group,
            }
            durability.write_json_file(paths["result"], turn_payload, durable=True)
    if failed_commit is not None or commit_error is not None:
        # The turn itself stands: it executed, it verified, and the
        # session it completed is still complete. Only the commit
        # of its changes failed, so the failure is recorded on the
        # turn and on the run, and the turn goes on to be committed
        # to the plan. Recording a `turn_rejected` here instead
        # would say a guard refused a turn the very next call
        # accepts.
        with ctx.persistence.lock:
            turn_payload["session_commit"] = {
                "guard": (
                    "repair-session-commit" if repair_owned_commit else "session-commit"
                ),
                "reason": "commit-failed",
                "error": commit_error,
                "commit_group": commit_group,
                "commit": failed_commit,
            }
            durability.write_json_file(paths["result"], turn_payload, durable=True)
        soft_fail_commit(
            scope="session",
            session_id=session_id,
            group=commit_group,
            commit=failed_commit,
            error=commit_error,
            message=(f"auto-commit failed after session {session_id} completed"),
        )
