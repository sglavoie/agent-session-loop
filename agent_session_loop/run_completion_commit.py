"""The end-of-run commit owed under ``--commit-mode end``."""

from __future__ import annotations

from . import commit_settlement, plans_models
from .run_completion_types import CompletionOutcome, RunContext, SoftFailCommit
from .run_errors import RunError


def settle_end_commit(
    ctx: RunContext,
    snapshot: plans_models.PlanSnapshot,
    soft_fail_commit: SoftFailCommit,
) -> CompletionOutcome:
    """Attempt the end-of-run commit once and hand the loop back around.

    ``commit_at_end_attempted`` is set before the attempt so a failed commit
    still marks it spent, and a failure soft-fails rather than ending the run.
    """
    ctx.commit_at_end_attempted = True
    ctx.record_final_snapshot(snapshot)
    try:
        end_targets = commit_settlement._registered_end_commit_targets(
            ctx.verification_context
        )
        commit_group, failed_commit = commit_settlement._run_commit_group(
            event_path=ctx.event_path,
            payload=ctx.payload,
            progress=ctx.progress,
            verification_context=ctx.verification_context,
            run_dir=ctx.run_dir,
            run_id=ctx.run_id,
            session_id=None,
            targets=end_targets,
            generation=ctx.commit_generation,
            persistence=ctx.persistence,
        )
    except RunError as error:
        commit_group = None
        failed_commit = None
        commit_error: str | None = str(error)
    else:
        commit_error = None
    if failed_commit is not None or commit_error is not None:
        soft_fail_commit(
            scope="end",
            session_id=None,
            group=commit_group,
            commit=failed_commit,
            error=commit_error,
            message=("final auto-commit failed after all sessions completed"),
        )
    return CompletionOutcome.again(snapshot)
