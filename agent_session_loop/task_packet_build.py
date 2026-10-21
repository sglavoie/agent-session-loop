"""The factory that validates inputs and builds a :class:`SessionTaskPacket`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from . import (
    harnesses,
    mailbox_models,
    mailbox_vocabulary,
    plans_files,
    plans_payloads,
    plans_progress,
    plans_session_ids,
    plans_session_scope,
    qna,
    redirect_ack_carry,
    run_prompts,
    workspaces,
)
from .task_packet_model import SessionTaskPacket


def build_task_packet(
    harness: str,
    plan_dir: str | Path,
    *,
    session_id: str | plans_session_ids.SessionId,
    predecessor_id: str | plans_session_ids.SessionId | None = None,
    has_expansion: bool = False,
    has_progress: bool = True,
    reply: str | None = None,
    reply_context: qna.QnAContext | None = None,
    resolution_action: mailbox_vocabulary.ResolutionAction | str | None = None,
    resolution_note: str | None = None,
    resolution_context: qna.QnAContext | None = None,
    repair_references: Sequence[str] = (),
    mailbox_enabled: bool = False,
    state_dir: str | Path | None = None,
    pending_guidance: Sequence[mailbox_models.MailboxMessage] = (),
    workspace: workspaces.ResolvedWorkspace | None = None,
    session_scope: plans_session_scope.SessionScope | None = None,
    session_kind: str = plans_payloads.SESSION_KIND_IMPLEMENTATION,
    corrections: redirect_ack_carry.CarriedCorrections | None = None,
) -> SessionTaskPacket:
    """Build the compact autonomous-worker packet for one selected session.

    ``harness`` is validated (Codex, Claude, and Copilot all yield the same
    harness-neutral body). The packet names the exact ``session_id`` and the
    artifact paths to read; it embeds neither manual-mode readiness/lock
    policy nor any session-selection logic. Optional inputs are included only
    when applicable:

    - ``predecessor_id`` adds the immediate predecessor handoff reference.
    - ``has_expansion`` adds the ``session-<id>.expand.md`` correction path.
    - ``has_progress`` adds the ``progress.md`` digest path (default on).
    - ``repair_references`` add verification-repair references.
    - ``mailbox_enabled`` adds the scoped live-guidance protocol, shell-safe
      commands, and any ``pending_guidance`` captured for this turn.
    - ``reply`` + ``reply_context`` append the operator-reply block; the
      runner supplies these only on the first resumed turn, so reply context
      never leaks onto later turns.
    - ``resolution_action`` + ``resolution_context`` append a distinct
      answerless-resolution block. ``resolution_note`` preserves omitted and
      empty notes without ever sharing the operator-reply representation.
    - ``session_kind`` selects the worker role: a ``review`` session renders
      reviewer duties and read-only mutation rules instead of the
      implementation-worker text.
    - ``corrections`` prepend the urgent-correction block an interrupted
      attempt resumes with. Unlike a reply or a resolution it is not mutually
      exclusive with either: an operator may correct a turn that was itself
      resuming from a pause.

    The returned :class:`SessionTaskPacket` carries structured data and a
    :meth:`~SessionTaskPacket.render` method producing the prompt string.
    """
    # Validate the harness even though the body is harness-neutral, so an
    # unknown harness fails fast the same way the other prompt builders do.
    harnesses.get_adapter(harness)

    if session_kind not in plans_payloads.SESSION_KINDS:
        raise ValueError(f"unknown session kind: {session_kind!r}")
    canonical_dir = plans_files.canonicalize_path(plan_dir)
    selected = plans_session_ids.parse_session_id(session_id)
    if (workspace is None) != (session_scope is None):
        raise ValueError("workspace and session_scope must be provided together")
    pending_guidance_tuple = tuple(pending_guidance)
    if pending_guidance_tuple and not mailbox_enabled:
        raise ValueError("pending_guidance requires mailbox_enabled")

    predecessor_value: str | None = None
    predecessor_path: str | None = None
    if predecessor_id is not None:
        predecessor = plans_session_ids.parse_session_id(predecessor_id)
        predecessor_value = predecessor.value
        predecessor_path = str(canonical_dir / predecessor.filename)

    correction_block: tuple[str, ...] = ()
    if corrections:
        correction_block = (
            run_prompts.CORRECTION_CONTEXT_HEADING,
            "",
            *run_prompts._correction_context_body(
                corrections,
                plan_dir=canonical_dir,
                state_dir=state_dir,
            ),
        )
    reply_block: tuple[str, ...] = ()
    resolution_block: tuple[str, ...] = ()
    if reply_context is not None or reply is not None:
        if reply_context is None or reply is None:
            raise ValueError("reply and reply_context must be provided together")
        reply_text = reply.strip()
        if not reply_text:
            raise ValueError("reply must contain non-whitespace content")
        reply_block = (
            run_prompts.REPLY_CONTEXT_HEADING,
            "",
            *run_prompts._reply_context_body(reply_text, reply_context),
        )
    resolution_requested = (
        resolution_action is not None
        or resolution_context is not None
        or resolution_note is not None
    )
    if resolution_requested:
        if reply_block:
            raise ValueError("reply and resolution contexts are mutually exclusive")
        if resolution_action is None or resolution_context is None:
            raise ValueError(
                "resolution_action and resolution_context must be provided together"
            )
        resolution_block = (
            run_prompts.RESOLUTION_CONTEXT_HEADING,
            "",
            *run_prompts._resolution_context_body(
                resolution_action,
                resolution_note,
                resolution_context,
            ),
        )

    return SessionTaskPacket(
        harness=harnesses.get_adapter(harness).name,
        plan_dir=run_prompts._prompt_plan_dir(canonical_dir),
        session_id=selected.value,
        session_path=str(canonical_dir / selected.filename),
        context_path=str(canonical_dir / "context.md"),
        progress_path=(
            str(canonical_dir / plans_progress.PROGRESS_FILE_NAME)
            if has_progress
            else None
        ),
        expansion_path=(
            str(canonical_dir / selected.expansion_filename) if has_expansion else None
        ),
        predecessor_id=predecessor_value,
        predecessor_path=predecessor_path,
        repair_references=tuple(repair_references),
        mailbox_enabled=mailbox_enabled,
        mailbox_read_command=(
            run_prompts._mailbox_read_command_text(canonical_dir, state_dir)
            if mailbox_enabled
            else None
        ),
        mailbox_notify_command=(
            run_prompts._mailbox_notify_command_text(canonical_dir, state_dir)
            if mailbox_enabled
            else None
        ),
        mailbox_ask_command=(
            run_prompts._mailbox_ask_command_text(
                canonical_dir, state_dir, resume=False
            )
            if mailbox_enabled
            else None
        ),
        mailbox_resume_ask_command=(
            run_prompts._mailbox_ask_command_text(canonical_dir, state_dir, resume=True)
            if mailbox_enabled
            else None
        ),
        pending_guidance=pending_guidance_tuple,
        correction_block=correction_block,
        reply_block=reply_block,
        resolution_block=resolution_block,
        workspace=workspace,
        session_scope=session_scope,
        session_kind=session_kind,
    )
