"""The ``asl questions`` command surface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import cli_common, pause_policy, plans_errors, responses
from .cli_defaults import program_name
from .qna_context import load_qna_context
from .qna_models import QnAContext, QnAError
from .qna_render import (
    artifact_paths_payload,
    mailbox_continue_command_text,
    mailbox_pause_command_text,
    mailbox_reply_command_text,
    mailbox_reply_stdin_command_text,
    recovery_command_text,
    render_resolver_prompt,
    reply_stdin_command_text,
    requires_conversation_recovery,
    resume_command_text,
)


def questions_payload(context: QnAContext) -> dict[str, Any]:
    """Render a paused or live-wait context as ``asl questions`` JSON."""
    if context.context_kind == "waiting":
        return responses.success_payload(
            "questions",
            context_kind="waiting",
            reply_command=mailbox_reply_command_text(context),
            reply_stdin_command=mailbox_reply_stdin_command_text(context),
            continue_command=mailbox_continue_command_text(context),
            pause_command=mailbox_pause_command_text(context),
            plan_dir=str(context.plan_dir),
            state_dir=str(context.state_dir),
            state_path=str(context.state_path),
            run_id=context.run_id,
            session_id=context.session_id,
            next_pending_id=context.next_pending_id,
            turn_index=context.turn_index,
            question_id=context.question_id,
            waiting_for_reply=(
                dict(context.waiting_for_reply)
                if context.waiting_for_reply is not None
                else None
            ),
            harness=context.harness,
            model=context.model,
            model_source=context.model_source,
            effort=context.effort,
            effort_source=context.effort_source,
            artifact_paths=artifact_paths_payload(context),
            last_message=context.last_message,
            rendered_prompt=render_resolver_prompt(context),
        )

    # The recovery keys are always present and ``None`` unless the pause is an
    # unresumable conversation: a consumer should branch on a key it can rely
    # on, not on whether the producer felt like emitting one. When recovery is
    # required the reply commands go to ``None`` for the same reason the prompt
    # stops asking for an answer -- a consumer that prints ``resume_command``
    # without reading ``recovery_required`` would otherwise hand the operator a
    # `--reply` for a question that does not exist.
    recovery_required = requires_conversation_recovery(context)
    return responses.success_payload(
        "questions",
        resume_command=None if recovery_required else resume_command_text(context),
        reply_stdin_command=(
            None if recovery_required else reply_stdin_command_text(context)
        ),
        recovery_required=recovery_required,
        recovery_command=recovery_command_text(context) if recovery_required else None,
        plan_dir=str(context.plan_dir),
        state_dir=str(context.state_dir),
        state_path=str(context.state_path),
        run_id=context.run_id,
        session_id=context.session_id,
        next_pending_id=context.next_pending_id,
        turn_index=context.turn_index,
        pause_reason=context.pause_reason,
        pause_message=context.pause_message,
        pause_details=dict(context.pause_details),
        pause_policy=pause_policy.classify_pause(
            context.pause_reason, context.pause_details
        ),
        harness=context.harness,
        model=context.model,
        model_source=context.model_source,
        effort=context.effort,
        effort_source=context.effort_source,
        artifact_paths=artifact_paths_payload(context),
        last_message=context.last_message,
        rendered_prompt=render_resolver_prompt(context),
    )


def questions_from_args(args: argparse.Namespace) -> int:
    """Run the ``questions`` subcommand from CLI arguments."""
    if not cli_common.resolve_plan_dir_argument(
        args, command_name="questions", text_label="questions"
    ):
        return 1
    plan_dir_arg = args.plan_dir
    state_dir_arg = args.state_dir
    want_json = bool(args.json)

    try:
        context = load_qna_context(plan_dir_arg, state_dir=state_dir_arg)
    except QnAError as error:
        return _emit_questions_error(
            want_json=want_json,
            reason=error.reason,
            error=error.error,
            plan_dir=plan_dir_arg,
            details=error.details,
        )
    except plans_errors.PlanError as error:
        return _emit_questions_error(
            want_json=want_json,
            reason="malformed-plan",
            error=str(error),
            plan_dir=plan_dir_arg,
            details={"plan_dir": str(plan_dir_arg)},
        )

    if want_json:
        responses.print_json(questions_payload(context))
    else:
        print(render_resolver_prompt(context))
    return 0


def _emit_questions_error(
    *,
    want_json: bool,
    reason: str,
    error: str,
    plan_dir: str | Path,
    details: dict[str, Any],
) -> int:
    if want_json:
        responses.print_json(
            responses.error_payload(
                "questions",
                reason=reason,
                error=error,
                paused=False,
                details=dict(details),
                plan_dir=str(plan_dir),
            )
        )
    else:
        print(f"{program_name()} questions: {error}", file=sys.stderr)
    return 1
