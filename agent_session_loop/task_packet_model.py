"""The structured, frozen record behind one runner-managed session's task packet."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import (
    mailbox_models,
    plans_payloads,
    plans_session_scope,
    task_packet_render,
    workspaces,
)


@dataclass(frozen=True)
class SessionTaskPacket:
    """Structured, harness-neutral packet for one runner-managed session.

    The packet is a compact autonomous-worker prompt that stands in for the
    large manual ``session-run`` skill: the Python runner owns readiness,
    locking, session selection, and verification, so none of that policy is
    embedded here. Plan contents are referenced as paths to read rather than
    inlined. :meth:`render` produces the prompt string; the structured fields
    let the runner (packet mode) reason about the selected session without
    re-parsing the prompt.
    """

    harness: str
    plan_dir: str
    session_id: str
    session_path: str
    context_path: str
    progress_path: str | None = None
    expansion_path: str | None = None
    predecessor_id: str | None = None
    predecessor_path: str | None = None
    repair_references: tuple[str, ...] = ()
    mailbox_enabled: bool = False
    mailbox_read_command: str | None = None
    mailbox_notify_command: str | None = None
    mailbox_ask_command: str | None = None
    mailbox_resume_ask_command: str | None = None
    pending_guidance: tuple[mailbox_models.MailboxMessage, ...] = ()
    correction_block: tuple[str, ...] = field(default_factory=tuple)
    reply_block: tuple[str, ...] = field(default_factory=tuple)
    resolution_block: tuple[str, ...] = field(default_factory=tuple)
    workspace: workspaces.ResolvedWorkspace | None = None
    session_scope: plans_session_scope.SessionScope | None = None
    session_kind: str = plans_payloads.SESSION_KIND_IMPLEMENTATION

    @property
    def has_correction_context(self) -> bool:
        return bool(self.correction_block)

    @property
    def has_reply_context(self) -> bool:
        return bool(self.reply_block)

    @property
    def has_resolution_context(self) -> bool:
        return bool(self.resolution_block)

    @property
    def is_review(self) -> bool:
        return self.session_kind == plans_payloads.SESSION_KIND_REVIEW

    def render(self) -> str:
        return task_packet_render.render_task_packet(self)
