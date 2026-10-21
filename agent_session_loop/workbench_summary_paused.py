"""Summary classifier for paused runs and expired reply waits."""

from __future__ import annotations

from . import events, qna
from .workbench_summary_context import SummaryContext
from .workbench_summary_helpers import (
    _first_activity,
    _paused_activity,
    _truncate_preview,
)
from .workbench_summary_model import (
    REVIEW_CONTINUATION_ACTIVITY_LABEL,
    WorkbenchSummary,
)


def paused_summary(ctx: SummaryContext) -> WorkbenchSummary | None:
    """Classify a paused run or expired reply wait; None when neither applies."""
    runner_state = ctx.runner_state
    recovery = ctx.recovery
    review_resume = ctx.review_resume
    next_pending = ctx.next_pending

    if not (runner_state.get("paused") is True or ctx.expired_wait_context is not None):
        return None

    pause_reason = (
        runner_state.get("pause_reason") or runner_state.get("reason") or None
    )
    if (
        pause_reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR
        and ctx.active_turn is None
        and recovery.get("kind") == "review-continuation"
    ):
        failed_activity, failed_source = _first_activity(
            (
                (runner_state.get("run_finished_at"), "run-finished"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        if (
            recovery.get("severity") == "info"
            and review_resume.get("eligible") is True
            and review_resume.get("action")
            in {"materialize-repairs", "run-repairs", "rereview"}
        ):
            return ctx.make(
                primary_state="recent",
                recommended_action="run",
                next_action_hint=recovery["title"],
                detail=recovery["summary"],
                pause_reason=pause_reason,
                pause_message=runner_state.get("pause_message") or None,
                last_activity_at=failed_activity,
                last_activity_source=failed_source,
                concise_reason=recovery["summary"],
                activity_label=(
                    REVIEW_CONTINUATION_ACTIVITY_LABEL if next_pending is None else None
                ),
            )
        return ctx.make(
            primary_state="failed",
            recommended_action="inspect-failure",
            next_action_hint=recovery["title"],
            detail=recovery["summary"],
            pause_reason=pause_reason,
            pause_message=runner_state.get("pause_message") or None,
            last_activity_at=failed_activity,
            last_activity_source=failed_source,
            concise_reason=recovery["summary"],
        )

    warnings: list[str] = []
    pause_message = None
    preview = None
    qna_last_message_path = ctx.last_message_path
    paused_activity, paused_source = _paused_activity(runner_state)
    try:
        context = ctx.expired_wait_context or qna.load_qna_context(
            ctx.snapshot.plan_dir,
            state_dir=ctx.state_dir,
            reclaim=ctx.reclaim,
        )
    except qna.QnAError as error:
        # Not every pause is a question. A run that stopped on, say, an
        # unready review repair has no Q&A context, and recovery has
        # already named what it needs; offering "reply" there sends the
        # operator to a form that can only refuse them.
        if recovery.get("kind") not in {"none", "reply-needed"} and recovery.get(
            "title"
        ):
            return ctx.make(
                primary_state="failed",
                recommended_action="inspect-failure",
                next_action_hint=recovery["title"],
                detail=recovery.get("summary") or recovery["title"],
                pause_reason=pause_reason,
                pause_message=runner_state.get("pause_message") or None,
                last_activity_at=paused_activity,
                last_activity_source=paused_source,
                concise_reason=recovery.get("summary") or recovery["title"],
            )
        warnings.append(
            f"paused-q&a context unreadable ({error.reason}): {error.error}"
        )
        hint = (
            f"Reply needed for session {next_pending.id}"
            if next_pending is not None
            else "Reply needed"
        )
        detail = "Paused without readable last-message context"
    else:
        pause_reason = context.pause_reason or pause_reason
        pause_message = context.pause_message
        preview = _truncate_preview(context.last_message)
        qna_last_message_path = context.last_message_path
        session_label = context.session_id
        return ctx.make(
            primary_state="paused-for-reply",
            recommended_action="reply",
            next_action_hint=f"Reply needed for session {session_label}",
            detail=pause_message or pause_reason or "Paused awaiting reply",
            warnings=tuple(warnings),
            run_id=context.run_id or ctx.base_run_id,
            harness=context.harness or ctx.base_harness,
            artifact_dir=context.artifact_dir or ctx.artifact_dir,
            stdout_path=context.stdout_path or ctx.stdout_path,
            stderr_path=context.stderr_path or ctx.stderr_path,
            last_message_path=qna_last_message_path,
            result_path=context.result_path or ctx.result_path,
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=preview,
            last_activity_at=paused_activity,
            last_activity_source=paused_source,
            concise_reason=(pause_message or pause_reason or "Awaiting operator reply"),
        )

    return ctx.make(
        primary_state="paused-for-reply",
        recommended_action="reply",
        next_action_hint=hint,
        detail=detail,
        warnings=tuple(warnings),
        last_message_path=qna_last_message_path,
        pause_reason=pause_reason,
        pause_message=pause_message,
        last_message_preview=preview,
        last_activity_at=paused_activity,
        last_activity_source=paused_source,
        concise_reason=(pause_message or pause_reason or "Awaiting operator reply"),
    )
