"""Resolver prompt and command rendering for Q&A contexts."""

from __future__ import annotations

import json
import shlex

from . import events, mailbox_vocabulary
from .qna_models import (
    LEGACY_WAIT_CEILING_FIELD,
    WAIT_CEILING_FIELD,
    QnAContext,
)
from .qna_values import _wait_ceiling_field


def artifact_paths_payload(context: QnAContext) -> dict[str, str | None]:
    """Render the selected turn's artifact paths as a JSON-friendly dict."""
    return {
        "run_dir": str(context.run_dir) if context.run_dir is not None else None,
        "artifact_dir": (
            str(context.artifact_dir) if context.artifact_dir is not None else None
        ),
        "stdout_path": (
            str(context.stdout_path) if context.stdout_path is not None else None
        ),
        "stderr_path": (
            str(context.stderr_path) if context.stderr_path is not None else None
        ),
        "last_message_path": str(context.last_message_path),
        "result_path": (
            str(context.result_path) if context.result_path is not None else None
        ),
    }


def requires_conversation_recovery(context: QnAContext) -> bool:
    """Report whether this pause is a lost conversation rather than a question.

    The distinction matters because every other paused context has a question
    in it. This one does not: the run stopped because it cannot resume the
    harness conversation at all, so an operator told to "ask the user the
    question the agent needed answered" would go looking for words nobody
    wrote.
    """
    return (
        context.context_kind != "waiting"
        and context.pause_reason == events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
    )


def recovery_command_text(context: QnAContext) -> str:
    """``asl run <plan> --resume [--state-dir X] --fresh-conversation`` guidance.

    The authorization is one-use and is never persisted, so this is rendered
    rather than performed: the operator has to decide, each time, that starting
    a fresh conversation on the retained files is the right trade.
    """
    return shlex.join([*_resume_command_base(context), "--fresh-conversation"])


def render_resolver_prompt(context: QnAContext) -> str:
    """Render a selected Q&A context as a ready-to-send resolver prompt.

    The output is plain Markdown suitable for handing to another AI agent.
    The agent is expected to ask the user the question(s) the original
    harness asked, then return a concise reply that the appropriate paused or
    live reply command can consume verbatim -- except for a lost conversation,
    which has no question and gets a recovery prompt instead.
    """
    if context.context_kind == "waiting":
        return _render_waiting_resolver_prompt(context)
    if requires_conversation_recovery(context):
        return _render_recovery_resolver_prompt(context)

    lines: list[str] = []
    lines.append("# Paused Agent Session - Resolver Prompt")
    lines.append("")
    lines.append(
        "An agent harness paused mid-session waiting on a clarifying answer. "
        "Read the context below, ask the user the question(s) the previous "
        "agent needed answered, then return a concise reply suitable for "
        "`asl run --resume --reply`."
    )
    lines.append("")

    lines.append("## Plan")
    lines.append(f"- Plan directory: {context.plan_dir}")
    lines.append(f"- State file: {context.state_path}")
    lines.append(f"- Run ID: {_display(context.run_id)}")
    lines.append(f"- Session ID: {context.session_id}")
    lines.append(f"- Turn index: {_display(context.turn_index)}")
    lines.append("")

    lines.append("## Harness")
    lines.append(f"- Harness: {_display(context.harness)}")
    lines.append(
        f"- Model: {_display(context.model)}{_source_suffix(context.model_source)}"
    )
    lines.append(
        f"- Effort: {_display(context.effort)}{_source_suffix(context.effort_source)}"
    )
    lines.append("")

    lines.append("## Pause")
    lines.append(f"- Reason: {_display(context.pause_reason)}")
    lines.append(f"- Message: {_display(context.pause_message)}")
    if context.pause_details:
        details_json = json.dumps(context.pause_details, sort_keys=True)
        lines.append(f"- Details: {details_json}")
    else:
        lines.append("- Details: (none)")
    lines.append("")

    lines.append("## Artifacts")
    artifacts = artifact_paths_payload(context)
    lines.append(f"- Run dir: {_display(artifacts['run_dir'])}")
    lines.append(f"- Artifact dir: {_display(artifacts['artifact_dir'])}")
    lines.append(f"- Stdout: {_display(artifacts['stdout_path'])}")
    lines.append(f"- Stderr: {_display(artifacts['stderr_path'])}")
    lines.append(f"- Last message: {artifacts['last_message_path']}")
    lines.append(f"- Result: {_display(artifacts['result_path'])}")
    lines.append("")

    lines.append("## Previous Agent Message")
    lines.append("")
    lines.append(context.last_message.rstrip("\n"))
    lines.append("")

    lines.append("## Instructions")
    lines.append("")
    lines.append(
        "1. Ask the user the question(s) the previous agent needed answered. "
        "If the message contains multiple questions, ask each in order."
    )
    lines.append(
        "2. Keep the reply concise. One short answer per question is best "
        "(e.g., `1a, 2a, 3a`)."
    )
    lines.append(
        "3. Return ONLY the user's answer text. Do not add commentary, "
        "headings, or quoted context."
    )
    lines.append(
        "4. The answer will be passed verbatim to "
        "`asl run --resume --reply` to resume the paused session "
        "(--resume restores the original harness, model, and effort)."
    )
    lines.append("")

    return "\n".join(lines)


def _render_recovery_resolver_prompt(context: QnAContext) -> str:
    """Render the one pause that must not be read as a question.

    Deliberately different from the paused prompt in the only way that matters:
    it never tells the resolver to ask the user anything. There is no question
    here, so an instruction to find one produces an invented one, and a reply
    built from it would be handed to a session that cannot use it.
    """
    lines: list[str] = []
    lines.append("# Unresumable Conversation - Recovery Prompt")
    lines.append("")
    lines.append(
        "This run did not stop on a question. It stopped because the harness "
        "conversation it would resume can no longer be selected, so there is "
        "nothing for the user to answer and no reply that would restart it."
    )
    lines.append("")

    lines.append("## Plan")
    lines.append(f"- Plan directory: {context.plan_dir}")
    lines.append(f"- State file: {context.state_path}")
    lines.append(f"- Run ID: {_display(context.run_id)}")
    lines.append(f"- Session ID: {context.session_id}")
    lines.append(f"- Turn index: {_display(context.turn_index)}")
    lines.append("")

    lines.append("## Harness")
    lines.append(f"- Harness: {_display(context.harness)}")
    lines.append(
        f"- Model: {_display(context.model)}{_source_suffix(context.model_source)}"
    )
    lines.append(
        f"- Effort: {_display(context.effort)}{_source_suffix(context.effort_source)}"
    )
    lines.append("")

    lines.append("## Pause")
    lines.append(f"- Reason: {_display(context.pause_reason)}")
    lines.append(f"- Message: {_display(context.pause_message)}")
    if context.pause_details:
        details_json = json.dumps(context.pause_details, sort_keys=True)
        lines.append(f"- Details: {details_json}")
    else:
        lines.append("- Details: (none)")
    lines.append("")

    lines.append("## Artifacts")
    artifacts = artifact_paths_payload(context)
    lines.append(f"- Run dir: {_display(artifacts['run_dir'])}")
    lines.append(f"- Artifact dir: {_display(artifacts['artifact_dir'])}")
    lines.append(f"- Stdout: {_display(artifacts['stdout_path'])}")
    lines.append(f"- Stderr: {_display(artifacts['stderr_path'])}")
    lines.append(f"- Last message: {artifacts['last_message_path']}")
    lines.append(f"- Result: {_display(artifacts['result_path'])}")
    lines.append("")

    lines.append("## Recovery")
    lines.append("")
    lines.append(
        "1. Read the evidence first: "
        f"`{shlex.join(_recovery_inspect_command(context))}` reports whether a "
        "fresh conversation is required and which corrections a fresh start "
        "would still carry."
    )
    lines.append(
        "2. If a fresh start is the right trade, authorize exactly one: "
        f"`{recovery_command_text(context)}`. The session keeps its files, its "
        "recovery context, and any outstanding feedback; it loses only the "
        "prior conversation."
    )
    lines.append(
        "3. That authorization is one-use. It is never persisted, never "
        "rehydrated by a later `--resume`, and never replayed by a retry, so a "
        "second lost conversation has to be authorized again."
    )
    lines.append(
        "4. Do not ask the user to answer a question here, and do not resume "
        "with `--reply`: there is no pending question this session can consume."
    )
    lines.append("")

    return "\n".join(lines)


def _recovery_inspect_command(context: QnAContext) -> list[str]:
    parts = ["asl", "control", "recover-conversation", str(context.plan_dir)]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    return parts


def _render_waiting_resolver_prompt(context: QnAContext) -> str:
    waiting = (
        dict(context.waiting_for_reply) if context.waiting_for_reply is not None else {}
    )
    lines: list[str] = []
    lines.append("# Live Agent Session - Resolver Prompt")
    lines.append("")
    lines.append(
        "An agent harness is waiting in-process on a mailbox question. "
        "Read the context below and choose whether to submit a factual reply, "
        "continue without an answer, or end the live wait for ordinary "
        "paused-run recovery."
    )
    lines.append("")

    lines.append("## Plan")
    lines.append(f"- Plan directory: {context.plan_dir}")
    lines.append(f"- State file: {context.state_path}")
    lines.append(f"- Run ID: {_display(context.run_id)}")
    lines.append(f"- Session ID: {context.session_id}")
    lines.append(f"- Turn index: {_display(context.turn_index)}")
    lines.append("")

    lines.append("## Harness")
    lines.append(f"- Harness: {_display(context.harness)}")
    lines.append(
        f"- Model: {_display(context.model)}{_source_suffix(context.model_source)}"
    )
    lines.append(
        f"- Effort: {_display(context.effort)}{_source_suffix(context.effort_source)}"
    )
    lines.append("")

    lines.append("## Waiting for Reply")
    lines.append(f"- Question ID: {_display(context.question_id)}")
    lines.append(f"- Started: {_display(waiting.get('started_at'))}")
    lines.append(f"- Base deadline: {_display(waiting.get('base_deadline'))}")
    if _wait_ceiling_field(waiting) == LEGACY_WAIT_CEILING_FIELD:
        lines.append(f"- Hard deadline: {_display(waiting.get('hard_deadline'))}")
    else:
        lines.append(
            "- Unsupervised deadline: "
            f"{_display(waiting.get(WAIT_CEILING_FIELD))} "
            "(rolls forward with every lease that proves a supervisor alive)"
        )
        lines.append(
            "- Unsupervised ceiling: "
            f"{_display(waiting.get('unsupervised_ceiling_seconds'))}s"
        )
    lines.append(
        f"- Last lease activity: {_display(waiting.get('last_lease_activity_at'))}"
    )
    lines.append("")

    lines.append("## Artifacts")
    artifacts = artifact_paths_payload(context)
    lines.append(f"- Run dir: {_display(artifacts['run_dir'])}")
    lines.append(f"- Artifact dir: {_display(artifacts['artifact_dir'])}")
    lines.append(f"- Stdout: {_display(artifacts['stdout_path'])}")
    lines.append(f"- Stderr: {_display(artifacts['stderr_path'])}")
    lines.append(f"- Last message: {artifacts['last_message_path']}")
    lines.append(f"- Result: {_display(artifacts['result_path'])}")
    lines.append("")

    lines.append("## Resolution Choices")
    lines.append("")
    lines.append(
        "- Reply when the worker needs a factual operator answer: "
        f"`{mailbox_reply_command_text(context)}`"
    )
    lines.append(
        "- Continue without an answer only when the message is plainly "
        "nonblocking: "
        f"`{mailbox_continue_command_text(context)}`"
    )
    lines.append(
        "- Pause when the live wait should become ordinary paused-run "
        "recovery: "
        f"`{mailbox_pause_command_text(context)}`"
    )
    lines.append("")

    lines.append("## Previous Agent Message")
    lines.append("")
    lines.append(context.last_message.rstrip("\n"))
    lines.append("")

    lines.append("## Instructions")
    lines.append("")
    lines.append(
        "1. Choose reply, continue, or pause from the three outcomes above. "
        "Do not fabricate an answer merely to clear the live wait."
    )
    lines.append(
        "2. For reply, ask the user the question(s) the previous agent needed "
        "answered. If the message contains multiple questions, ask each in "
        "order. Keep the answer concise; one short answer per question is "
        "best (e.g., `1a, 2a, 3a`)."
    )
    lines.append(
        "3. For continue, proceed only when no factual answer or implementation "
        "choice is required. For pause, preserve the question for ordinary "
        "paused-run recovery."
    )
    lines.append(
        "4. Submit the selected command while the wait is still live. A reply "
        "continues with the operator's answer; continue supplies no answer; "
        "pause ends the live wait without inventing one."
    )
    lines.append("")

    return "\n".join(lines)


def _resume_command_base(context: QnAContext) -> list[str]:
    parts = ["asl", "run", str(context.plan_dir), "--resume"]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    return parts


def resume_command_text(context: QnAContext) -> str:
    """``asl run <plan> --resume [--state-dir X] --reply TEXT`` guidance.

    ``--resume`` rehydrates the paused run's harness/model/effort from the
    persisted run_config, so a non-default-harness run never resumes on the
    ``codex`` default.
    """
    return shlex.join([*_resume_command_base(context), "--reply", "TEXT"])


def reply_stdin_command_text(context: QnAContext) -> str:
    """``asl run <plan> --resume [--state-dir X] --reply-stdin`` guidance.

    Same rehydration as :func:`resume_command_text`, but the operator answer is
    piped on stdin so it never appears in argv.
    """
    return shlex.join([*_resume_command_base(context), "--reply-stdin"])


def _mailbox_reply_command_base(context: QnAContext) -> list[str]:
    if context.question_id is None:
        raise ValueError("live Q&A context does not record a question id")
    parts = [
        "asl",
        "inbox",
        "reply",
        str(context.plan_dir),
        "--to",
        context.question_id,
    ]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    return parts


def mailbox_reply_command_text(context: QnAContext) -> str:
    """Return live mailbox reply guidance with an inline text placeholder."""
    return shlex.join(
        [
            *_mailbox_reply_command_base(context),
            "--text",
            "TEXT",
            "--wait-consumed",
            "--wait-timeout",
            "300",
        ]
    )


def mailbox_reply_stdin_command_text(context: QnAContext) -> str:
    """Return live mailbox reply guidance that reads the answer from stdin."""
    return shlex.join(
        [
            *_mailbox_reply_command_base(context),
            "--text-stdin",
            "--wait-consumed",
            "--wait-timeout",
            "300",
        ]
    )


def _mailbox_resolution_command_base(
    context: QnAContext,
    action: mailbox_vocabulary.ResolutionAction,
) -> list[str]:
    if context.question_id is None:
        raise ValueError("live Q&A context does not record a question id")
    parts = [
        "asl",
        "inbox",
        "resolve",
        str(context.plan_dir),
        "--to",
        context.question_id,
        "--action",
        action,
    ]
    if context.state_dir_override:
        parts.extend(["--state-dir", context.state_dir_override])
    parts.extend(["--wait-consumed", "--wait-timeout", "300"])
    return parts


def mailbox_continue_command_text(context: QnAContext) -> str:
    """Return a ready-to-run answerless continue command."""
    return shlex.join(
        _mailbox_resolution_command_base(
            context,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
        )
    )


def mailbox_pause_command_text(context: QnAContext) -> str:
    """Return a ready-to-run answerless pause command."""
    return shlex.join(
        _mailbox_resolution_command_base(
            context,
            mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
        )
    )


def _display(value: object) -> str:
    if value is None or value == "":
        return "(unknown)"
    return str(value)


def _source_suffix(source: str | None) -> str:
    if not source:
        return ""
    return f" ({source})"
