"""Assemble session-start prompts and harness invocations."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from . import (
    harnesses,
    mailbox_models,
    plans_files,
    plans_session_scope,
    redirect_ack,
    workspaces,
)
from .run_prompt_mailbox import (
    CORRECTION_CONTEXT_HEADING,
    _correction_context_body,
    _mailbox_ask_command_text,
    _mailbox_notify_command_text,
    _mailbox_protocol_lines,
    _mailbox_read_command_text,
)
from .run_prompt_workspace import _prompt_plan_dir, _workspace_prompt_lines

RUNNER_MANAGED_SESSION_ARG = "--runner-managed"


SESSION_RUN_SKILL_NAME = "session-run"


def build_session_prompt(
    harness: str,
    plan_dir: str | Path,
    *,
    mailbox_enabled: bool = False,
    state_dir: str | Path | None = None,
    pending_guidance: Sequence[mailbox_models.MailboxMessage] = (),
    workspace: workspaces.ResolvedWorkspace | None = None,
    session_scope: plans_session_scope.SessionScope | None = None,
    corrections: redirect_ack.CarriedCorrections | None = None,
) -> str:
    """Build the legacy worker prompt, keeping the skill invocation first.

    When ``corrections`` carries anything, its block is placed immediately
    after the skill invocation — ahead of the repository authority block and
    the mailbox protocol. The invocation has to stay on line one for the
    harness adapters, so "feedback first" means first among the things the
    worker reads as content.
    """
    adapter = harnesses.get_adapter(harness)
    first_line = (
        f"{adapter.skill_prefix}{SESSION_RUN_SKILL_NAME} "
        f"{shlex.quote(_prompt_plan_dir(plan_dir))} "
        f"{RUNNER_MANAGED_SESSION_ARG}"
    )
    if (workspace is None) != (session_scope is None):
        raise ValueError("workspace and session_scope must be provided together")
    # An empty batch is treated as no batch: the runner asks for corrections on
    # every resumed turn and gets back a record whether or not any survived
    # carrying, and a heading over nothing would read as a lost correction.
    carried = corrections or None
    if not mailbox_enabled and workspace is None and carried is None:
        return first_line
    lines = [first_line, ""]
    if carried is not None:
        lines.extend([CORRECTION_CONTEXT_HEADING, ""])
        lines.extend(
            _correction_context_body(carried, plan_dir=plan_dir, state_dir=state_dir)
        )
        if mailbox_enabled or workspace is not None:
            lines.append("")
    if workspace is not None and session_scope is not None:
        lines.extend(_workspace_prompt_lines(workspace, session_scope))
        if mailbox_enabled:
            lines.append("")
    if not mailbox_enabled:
        return "\n".join(lines)
    lines.extend(
        _mailbox_protocol_lines(
            read_command=_mailbox_read_command_text(plan_dir, state_dir),
            notify_command=_mailbox_notify_command_text(plan_dir, state_dir),
            ask_command=_mailbox_ask_command_text(plan_dir, state_dir, resume=False),
            resume_ask_command=_mailbox_ask_command_text(
                plan_dir, state_dir, resume=True
            ),
            pending_guidance=pending_guidance,
        )
    )
    return "\n".join(lines)


def build_harness_invocation(
    harness: str,
    *,
    cwd: str | Path,
    plan_dir: str | Path,
    unsafe_bypass: bool = False,
    last_message_path: str | Path | None = None,
    model: str | None = None,
    effort: str | None = None,
    prompt: str | None = None,
) -> harnesses.HarnessInvocation:
    """Build the subprocess command for one pending-session harness turn.

    When ``prompt`` is provided, it overrides the default
    :func:`build_session_prompt` output. The override path is how reply
    resumes ship a prepended ``ASL reply context`` block without changing
    normal session execution.
    """
    canonical_cwd = plans_files.canonicalize_path(cwd)
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    if prompt is None:
        prompt = build_session_prompt(harness, canonical_plan_dir)
    adapter = harnesses.get_adapter(harness)
    return adapter.build_invocation(
        cwd=canonical_cwd,
        plan_dir=canonical_plan_dir,
        prompt=prompt,
        unsafe_bypass=unsafe_bypass,
        last_message_path=(
            plans_files.canonicalize_path(last_message_path)
            if last_message_path is not None
            else None
        ),
        model=model,
        effort=effort,
    )
