"""The typed boundary that builds the autonomous worker's task packet.

An autonomous ``asl run`` turn is opened by a packet, not by a skill call.
This module owns that packet end to end: the structured
:class:`SessionTaskPacket` record, the :func:`build_task_packet` factory that
validates its inputs, the two plan lookups the factory's callers need
(:func:`_predecessor_session_id` and :func:`_session_has_expansion`), the
worker-mode switch that decides whether a run sends a packet at all, and the
paused-run ``interactive_help`` payload that tells an operator how to answer.

This boundary writes **no durable record**. It returns a frozen record, a
string, or a plain payload dictionary; the run loop remains the only thing
that decides when a packet is built, which prompt is persisted into a turn
record, and which process it is handed to. The ``interactive_help`` payload is
likewise handed back to the caller to embed, never written here.

Four invariants survive the move out of ``runner.py`` unchanged:

* **The heading is the packet's signature.** Every rendered packet starts with
  ``TASK_PACKET_HEADING_PREFIX``, which is how tests and operators tell a
  packet-mode turn from the legacy skill invocation on sight.
* **Packet mode fails safe.** :func:`_worker_mode` resolves anything other
  than a case-insensitive ``skill`` to :data:`WORKER_MODE_PACKET`, so a
  mistyped override cannot silently disable the default.
* **A continuation block never leaks forward.** An operator reply and an
  answerless supervisor resolution are mutually exclusive, each requires its
  own context, and both are supplied only on the turn that resumes, so
  reply context cannot reappear on a later turn. The urgent-correction block is
  a third, independent one — an operator may correct a turn that was itself
  resuming from a pause — and it cannot repeat either, because the journal
  records which attempt carried each correction.
* **Resolution identity carries no body.** :func:`_resolution_source_metadata`
  returns a fixed-shape identity — ids, sequence numbers, and the note's
  *length* — so a durable record can name a resolution without copying
  operator prose into it.

Nothing here imports the runner. The harness name, plan directory, session
id, workspace, scope, mailbox records, and the parsed argument namespace all
enter as parameters, and the prompt fragments this module composes come from
:mod:`agent_session_loop.run_prompts`, which is likewise runner-free.
"""

from __future__ import annotations

import argparse
import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    harnesses,
    mailbox_models,
    mailbox_vocabulary,
    plans_errors,
    plans_files,
    plans_handoff,
    plans_models,
    plans_payloads,
    plans_progress,
    plans_session_ids,
    plans_session_scope,
    qna,
    redirect_ack_carry,
    review,
    run_prompts,
    workspaces,
)

# Worker mode selects how autonomous ``asl run`` turns prompt the harness.
# ``packet`` (the default) sends a compact Python-built task packet
# (:func:`build_task_packet`) so autonomous runs never invoke the manual
# ``session-run`` skill. ``skill`` is a temporary rollback that restores the
# legacy ``session-run`` skill invocation
# (:func:`agent_session_loop.run_prompts.build_session_prompt`). The
# manual ``$session-run`` path is unaffected by this switch either way.
WORKER_MODE_ENV_VAR = "ASL_WORKER_MODE"
WORKER_MODE_PACKET = "packet"
WORKER_MODE_SKILL = "skill"


def _worker_mode() -> str:
    """Return the active worker mode, defaulting to packet mode.

    Any value other than a case-insensitive ``skill`` resolves to
    :data:`WORKER_MODE_PACKET` so an unrecognized override fails safe onto the
    new default rather than silently disabling packet mode.
    """
    raw = os.environ.get(WORKER_MODE_ENV_VAR)
    if raw is None:
        return WORKER_MODE_PACKET
    if raw.strip().lower() == WORKER_MODE_SKILL:
        return WORKER_MODE_SKILL
    return WORKER_MODE_PACKET


TASK_PACKET_HEADING_PREFIX = "# ASL session task packet"


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

    def _justified_repeat_lines(self) -> list[str]:
        """Return the bullets that bound repeated and widened checking.

        "Do not rerun the full Completion Contract" is a rule about the *final
        gate*, not a ban on checking. These bullets name the three cases that
        justify running a check again or running a broader one, so a worker
        reading the sentence above cannot conclude that a check it genuinely
        needs is forbidden. They deliberately impose no cap on how many checks
        a turn runs or how long they take — the focused regression checks that
        cover changed behavior stay available — and they ask for a brief reason
        in the handoff so a repeat is an argued decision rather than a habit.
        """
        kept = (
            "keep the focused read-only checks that substantiate a finding"
            if self.is_review
            else "keep the focused checks that cover the behavior this session changed"
        )
        return [
            (
                "- Running a check again, or running a broader one, is "
                "justified when an edit since that result invalidated it, "
                "when a failure needs diagnosis, or when a concrete "
                "correctness concern none of your checks covered remains. "
                "Nothing here caps how many checks you run or how long they "
                f"take: {kept}."
            ),
            (
                "- When you repeat or widen a check on that basis, record the "
                "brief reason in this session's handoff `Notes`."
            ),
        ]

    def render(self) -> str:
        lines: list[str] = [f"{TASK_PACKET_HEADING_PREFIX} — session {self.session_id}"]
        lines.append("")
        if self.is_review:
            lines.append(
                "You are an autonomous ASL reviewer. Review the work of this "
                "plan's prior sessions exactly as this one review session "
                "specifies, then stop. Do not implement anything and do not "
                "modify any file in the repository under review. The Python "
                "runner owns readiness, locking, and session selection. The "
                "runner owns authoritative final verification of your review "
                "artifacts; you own focused validation while writing them."
            )
        else:
            lines.append(
                "You are an autonomous ASL worker. Implement exactly this one "
                "session, then stop. The Python runner owns readiness, locking, "
                "and session selection. The runner owns authoritative final "
                "verification; you own focused validation during implementation."
            )
        lines.append("")

        if self.correction_block:
            # First among the packet's content sections, ahead of the session it
            # is correcting: an operator who interrupts a turn is answered
            # before the worker re-reads what it was already doing.
            lines.extend(self.correction_block)
            lines.append("")

        lines.append("## Selected session (fixed — do not reinterpret)")
        lines.append(f"- Session ID: {self.session_id}")
        lines.append(f"- Session file: {self.session_path}")
        if self.expansion_path is not None:
            lines.append(
                "- Expansion notes (authoritative factual corrections): "
                f"{self.expansion_path}"
            )
        lines.append("")

        lines.append(
            "## Read before reviewing (open these paths; not embedded)"
            if self.is_review
            else "## Read before implementing (open these paths; not embedded)"
        )
        lines.append(f"- Shared context: {self.context_path}")
        if self.progress_path is not None:
            lines.append(
                f"- Progress journal (cross-session digest): {self.progress_path}"
            )
        if self.predecessor_path is not None:
            lines.append(
                f"- Predecessor handoff: {self.predecessor_path} "
                "(read its `## Handoff` section)"
            )
        lines.append("")

        if self.workspace is not None and self.session_scope is not None:
            lines.extend(
                run_prompts._workspace_prompt_lines(self.workspace, self.session_scope)
            )
            lines.append("")

        if self.repair_references:
            lines.append("## Verification repair references")
            for reference in self.repair_references:
                lines.append(f"- {reference}")
            lines.append("")

        if self.mailbox_enabled:
            assert self.mailbox_read_command is not None
            assert self.mailbox_notify_command is not None
            assert self.mailbox_ask_command is not None
            assert self.mailbox_resume_ask_command is not None
            lines.extend(
                run_prompts._mailbox_protocol_lines(
                    read_command=self.mailbox_read_command,
                    notify_command=self.mailbox_notify_command,
                    ask_command=self.mailbox_ask_command,
                    resume_ask_command=self.mailbox_resume_ask_command,
                    pending_guidance=self.pending_guidance,
                )
            )
            lines.append("")

        if self.is_review:
            lines.append("## Review")
            lines.append(
                "Execute the instructions above the `## Handoff` section of "
                f"{self.session_path}. Judge the work; do not repair it. "
                "During the review, run focused read-only checks that help "
                "you substantiate a finding. Do not rerun the full Completion "
                "Contract solely as a final gate: after you signal "
                "completion, the runner executes it authoritatively. If it "
                "fails, the runner restores this session to pending and "
                "supplies the failed-check evidence for repair."
            )
            lines.append(
                "Record a finding only for an unmet accepted requirement or a "
                "concrete defect supported by evidence. Optional improvements "
                "are not new acceptance criteria."
            )
            lines.append("")
            lines.extend(self._justified_repeat_lines())
            lines.append("")

            lines.append("## Allowed mutation (the only writes this session may make)")
            lines.append(
                f"- Write the review verdict `{review.FINAL_REVIEW_FILENAME}` and the "
                f"structured artifact `{review.FINAL_REVIEW_ARTIFACT_FILENAME}` into "
                f"{self.plan_dir}, exactly as the session file specifies."
            )
            lines.append(
                f"- In {self.session_path}, set `- **Status**: complete` and "
                "fill the `## Handoff` fields for this session only."
            )
            lines.append(
                f"- Record new facts under `{plans_handoff.DISCOVERED_FACTS_HEADING}` "
                "and open uncertainties under "
                f"`{plans_handoff.OPEN_QUESTIONS_HEADING}` "
                "(flat, top-level bullets)."
            )
            lines.append(
                "- Do not edit any file in the repository under review, any "
                "other session file, progress.md, manifest readiness "
                "metadata, or any lock file. A finding is how you report a "
                "defect; a fix is not yours to write."
            )
            lines.append("")
        else:
            lines.append("## Implement")
            lines.append(
                "Execute the instructions above the `## Handoff` section of "
                f"{self.session_path}. Make real changes. During implementation, "
                "run focused checks that help you iterate. Do not rerun the full "
                "Completion Contract solely as a final gate: after you signal "
                "completion, the runner executes it authoritatively. If it fails, "
                "the runner restores this session to pending and supplies the "
                "failed-check evidence for repair."
            )
            lines.append(
                "Reuse still-valid evidence for unchanged behavior; diagnose the "
                "actual failure before widening checks. If you discover an "
                "integration blocker, record it through the handoff or mailbox; "
                "do not redesign boundaries or remove required checks."
            )
            lines.append("")
            lines.extend(self._justified_repeat_lines())
            lines.append("")

            lines.append(
                "## Allowed mutation (the only write that completes this session)"
            )
            lines.append(
                f"- In {self.session_path}, set `- **Status**: complete` and fill "
                "the `## Handoff` fields for this session only."
            )
            lines.append(
                "- Record new facts under "
                f"`{plans_handoff.DISCOVERED_FACTS_HEADING}` and "
                f"open uncertainties under `{plans_handoff.OPEN_QUESTIONS_HEADING}` "
                "(flat, top-level bullets)."
            )
            lines.append(
                "- Do not edit progress.md, manifest readiness metadata, or any "
                "lock file."
            )
            lines.append(
                "- Do not edit any other session's `session-<id>.md` or "
                "`session-<id>.expand.md`; the runner hashes them before the "
                "turn and pauses the run if any of them changes."
            )
            lines.append("")

        if self.is_review:
            lines.append("## Notes for future sessions")
            lines.append(
                "- Every defect worth acting on belongs in the review verdict "
                "as a finding with its own repair group; do not write "
                "guidance into another session's file and do not leave it "
                "inline in chat."
            )
            lines.append("")
        else:
            lines.append("## Notes for future sessions")
            lines.append(
                "- Write guidance for a later session into this session's "
                "`## Handoff`, which the next session reads as its predecessor "
                "handoff; never inline in chat and never in another session's "
                "file."
            )
            lines.append("")

        lines.append("## Runner signal protocol")
        lines.append(
            "- Before printing either runner signal, wait for or explicitly stop "
            "every background command you started, including commands the harness "
            "auto-backgrounded. Reading a command's output file does not prove its "
            "process exited."
        )
        lines.append(
            "- After writing the `complete` handoff, print `Runner status: complete`."
        )
        lines.append(
            "- To stop without completing, print `Runner status: paused` then "
            "`RUNNER_PAUSE: <reason> - <detail>` reusing the existing "
            "vocabulary (blocked, unsafe, verify-failed, malformed-plan, ...)."
        )

        if self.reply_block:
            lines.append("")
            lines.extend(self.reply_block)
        elif self.resolution_block:
            lines.append("")
            lines.extend(self.resolution_block)

        return "\n".join(lines)


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


def _predecessor_session_id(
    snapshot: plans_models.PlanSnapshot, session_id: str
) -> str | None:
    """Return the id of the session immediately before ``session_id``.

    Uses the plan's declared session order (not integer arithmetic) so dotted
    ids resolve correctly. Returns ``None`` for the first session or when the
    session is not found in the snapshot.
    """
    index = next(
        (
            position
            for position, session in enumerate(snapshot.sessions)
            if session.id == session_id
        ),
        None,
    )
    if index is None or index == 0:
        return None
    return snapshot.sessions[index - 1].id


def _session_has_expansion(plan_dir: Path, session_id: str) -> bool:
    """Return True when ``session-<id>.expand.md`` exists in ``plan_dir``."""
    try:
        selected = plans_session_ids.parse_session_id(session_id)
    except plans_errors.PlanError:
        return False
    return (plan_dir / selected.expansion_filename).is_file()


def _resolution_source_metadata(
    pending: run_prompts._PendingResolution,
) -> dict[str, Any]:
    """Return fixed-shape, body-free identity for a pending resolution."""
    if pending.action not in mailbox_vocabulary.RESOLUTION_ACTIONS:
        raise ValueError("pending resolution has an invalid action")
    if pending.note is not None and not isinstance(pending.note, str):
        raise ValueError("pending resolution note must be a string or None")
    return {
        "run_id": pending.context.run_id,
        "session_id": pending.context.session_id,
        "turn_index": pending.context.turn_index,
        "question_id": pending.question_id,
        "resolution_message_id": pending.resolution_message_id,
        "resolution_seq": pending.resolution_seq,
        "action": pending.action,
        "note_length": (len(pending.note) if pending.note is not None else None),
    }


def _questions_command_text(plan_dir: str, state_dir_arg: str | None) -> str:
    parts = ["asl", "questions", plan_dir]
    if state_dir_arg:
        parts.extend(["--state-dir", state_dir_arg])
    return shlex.join(parts)


def _reply_command_text(plan_dir: str, state_dir_arg: str | None) -> str:
    # ``--resume`` rehydrates harness/model/effort from the persisted
    # run_config so a paused non-default-harness run never silently resumes on
    # the ``codex`` default.
    parts = ["asl", "run", plan_dir, "--resume"]
    if state_dir_arg:
        parts.extend(["--state-dir", state_dir_arg])
    parts.extend(["--reply", "TEXT"])
    return shlex.join(parts)


def _build_interactive_help(
    args: argparse.Namespace,
    plan_dir: str,
) -> dict[str, Any] | None:
    """Return a payload describing how an operator can resume a paused run.

    Returns ``None`` when paused-run Q&A context cannot be loaded (no paused
    state, missing artifacts, or session advanced underneath the pause), so
    the caller can keep its existing payload shape unchanged.
    """
    state_dir_arg = getattr(args, "state_dir", None)
    try:
        context = qna.load_qna_context(plan_dir, state_dir=state_dir_arg)
    except qna.QnAError, plans_errors.PlanError, OSError:
        return None
    # An unresumable conversation is the one pause with no question in it, so
    # it gets the recovery route in place of a reply command rather than
    # alongside one: an operator handed both would reasonably try the reply.
    recovery_required = qna.requires_conversation_recovery(context)
    return {
        "questions_command": _questions_command_text(plan_dir, state_dir_arg),
        "reply_command": (
            None if recovery_required else _reply_command_text(plan_dir, state_dir_arg)
        ),
        "recovery_required": recovery_required,
        "recovery_command": (
            qna.recovery_command_text(context) if recovery_required else None
        ),
        "session_id": context.session_id,
        "next_pending_id": context.next_pending_id,
        "turn_index": context.turn_index,
        "artifact_paths": qna.artifact_paths_payload(context),
        "rendered_prompt": qna.render_resolver_prompt(context),
    }


def _attach_interactive_help(
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Attach ``interactive_help`` to a paused payload when Q&A context loads."""
    if not payload.get("paused"):
        return payload
    plan_dir = payload.get("plan_dir") or getattr(args, "plan_dir", None)
    if not plan_dir:
        return payload
    help_payload = _build_interactive_help(args, str(plan_dir))
    if help_payload is not None:
        payload["interactive_help"] = help_payload
    return payload
