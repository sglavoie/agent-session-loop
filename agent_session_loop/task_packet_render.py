"""Prompt rendering for the autonomous worker's task packet.

:func:`render_task_packet` turns a :class:`SessionTaskPacket` into the prompt
string; it only reads the packet and writes nothing. The review and
implementation roles share the heading, the selected-session and read-first
sections, and the runner signal protocol, and differ in the duty, mutation,
and future-notes sections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import plans_handoff, review, run_prompts

if TYPE_CHECKING:
    from .task_packet_model import SessionTaskPacket

TASK_PACKET_HEADING_PREFIX = "# ASL session task packet"


def justified_repeat_lines(packet: SessionTaskPacket) -> list[str]:
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
        if packet.is_review
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


def _review_sections(packet: SessionTaskPacket) -> list[str]:
    lines: list[str] = []
    lines.append("## Review")
    lines.append(
        "Execute the instructions above the `## Handoff` section of "
        f"{packet.session_path}. Judge the work; do not repair it. "
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
    lines.extend(justified_repeat_lines(packet))
    lines.append("")

    lines.append("## Allowed mutation (the only writes this session may make)")
    lines.append(
        f"- Write the review verdict `{review.FINAL_REVIEW_FILENAME}` and the "
        f"structured artifact `{review.FINAL_REVIEW_ARTIFACT_FILENAME}` into "
        f"{packet.plan_dir}, exactly as the session file specifies."
    )
    lines.append(
        f"- In {packet.session_path}, set `- **Status**: complete` and "
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
    return lines


def _implement_sections(packet: SessionTaskPacket) -> list[str]:
    lines: list[str] = []
    lines.append("## Implement")
    lines.append(
        "Execute the instructions above the `## Handoff` section of "
        f"{packet.session_path}. Make real changes. During implementation, "
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
    lines.extend(justified_repeat_lines(packet))
    lines.append("")

    lines.append("## Allowed mutation (the only write that completes this session)")
    lines.append(
        f"- In {packet.session_path}, set `- **Status**: complete` and fill "
        "the `## Handoff` fields for this session only."
    )
    lines.append(
        "- Record new facts under "
        f"`{plans_handoff.DISCOVERED_FACTS_HEADING}` and "
        f"open uncertainties under `{plans_handoff.OPEN_QUESTIONS_HEADING}` "
        "(flat, top-level bullets)."
    )
    lines.append(
        "- Do not edit progress.md, manifest readiness metadata, or any lock file."
    )
    lines.append(
        "- Do not edit any other session's `session-<id>.md` or "
        "`session-<id>.expand.md`; the runner hashes them before the "
        "turn and pauses the run if any of them changes."
    )
    lines.append("")
    return lines


def _future_notes_sections(packet: SessionTaskPacket) -> list[str]:
    lines: list[str] = []
    if packet.is_review:
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
    return lines


def render_task_packet(packet: SessionTaskPacket) -> str:
    lines: list[str] = [f"{TASK_PACKET_HEADING_PREFIX} — session {packet.session_id}"]
    lines.append("")
    if packet.is_review:
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

    if packet.correction_block:
        # First among the packet's content sections, ahead of the session it
        # is correcting: an operator who interrupts a turn is answered
        # before the worker re-reads what it was already doing.
        lines.extend(packet.correction_block)
        lines.append("")

    lines.append("## Selected session (fixed — do not reinterpret)")
    lines.append(f"- Session ID: {packet.session_id}")
    lines.append(f"- Session file: {packet.session_path}")
    if packet.expansion_path is not None:
        lines.append(
            "- Expansion notes (authoritative factual corrections): "
            f"{packet.expansion_path}"
        )
    lines.append("")

    lines.append(
        "## Read before reviewing (open these paths; not embedded)"
        if packet.is_review
        else "## Read before implementing (open these paths; not embedded)"
    )
    lines.append(f"- Shared context: {packet.context_path}")
    if packet.progress_path is not None:
        lines.append(
            f"- Progress journal (cross-session digest): {packet.progress_path}"
        )
    if packet.predecessor_path is not None:
        lines.append(
            f"- Predecessor handoff: {packet.predecessor_path} "
            "(read its `## Handoff` section)"
        )
    lines.append("")

    if packet.workspace is not None and packet.session_scope is not None:
        lines.extend(
            run_prompts._workspace_prompt_lines(packet.workspace, packet.session_scope)
        )
        lines.append("")

    if packet.repair_references:
        lines.append("## Verification repair references")
        for reference in packet.repair_references:
            lines.append(f"- {reference}")
        lines.append("")

    if packet.mailbox_enabled:
        assert packet.mailbox_read_command is not None
        assert packet.mailbox_notify_command is not None
        assert packet.mailbox_ask_command is not None
        assert packet.mailbox_resume_ask_command is not None
        lines.extend(
            run_prompts._mailbox_protocol_lines(
                read_command=packet.mailbox_read_command,
                notify_command=packet.mailbox_notify_command,
                ask_command=packet.mailbox_ask_command,
                resume_ask_command=packet.mailbox_resume_ask_command,
                pending_guidance=packet.pending_guidance,
            )
        )
        lines.append("")

    if packet.is_review:
        lines.extend(_review_sections(packet))
    else:
        lines.extend(_implement_sections(packet))
    lines.extend(_future_notes_sections(packet))

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

    if packet.reply_block:
        lines.append("")
        lines.extend(packet.reply_block)
    elif packet.resolution_block:
        lines.append("")
        lines.extend(packet.resolution_block)

    return "\n".join(lines)
