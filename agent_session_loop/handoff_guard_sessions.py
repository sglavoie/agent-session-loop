"""Session reopening and progress-journal updates shared by handoff guards."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import plans_errors, plans_handoff, plans_models, plans_progress, run_termination

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class HandoffNoteAppender(Protocol):
    """Inserts one note bullet under a session handoff's ``- **Notes**`` list.

    Takes the session file's lines and the bounds of its handoff section, and
    returns the rewritten lines; it never touches the filesystem, so the caller
    stays the only writer. The concrete implementation is
    ``handoff_correction._append_handoff_note``, which this module cannot import:
    ``handoff_correction`` imports *this* module for the revert, so the edge only
    runs one way and the seam is what keeps it acyclic.
    """

    def __call__(
        self,
        lines: list[str],
        *,
        handoff_index: int,
        section_end: int,
        note: str,
    ) -> list[str]: ...


@dataclass(frozen=True)
class SessionRevert:
    """What :func:`_revert_session_status_to_pending` actually did.

    The failure note is appended whenever the session has a Handoff section,
    but the status only moves when it was not already ``pending``. Reporting a
    rewrite as a reopening would emit ``session_reopened`` (and
    ``reverted_to_pending``) for a session that never left ``pending``
    (ASI-30), so the transition is carried separately from the rewrite.
    """

    path: Path
    from_status: str | None

    @property
    def reopened(self) -> bool:
        """Whether the status line moved from something other than pending."""
        return (
            self.from_status is not None
            and self.from_status.strip().lower() != "pending"
        )


def _reopened(revert: SessionRevert | None) -> bool:
    """Whether a revert moved the session back to pending."""
    return revert is not None and revert.reopened


def session_status_reverter(
    append_handoff_note: HandoffNoteAppender,
) -> run_termination.SessionStatusReverter:
    """Bind the note appender into the reverter ``run_termination`` expects.

    ``SessionStatusReverter`` fixes the argument list at ``(snapshot,
    session_id, *, failure_note)``, leaving no room for the appender seam, so a
    reverter that needs one cannot be handed over directly. This is the one
    place that binding is made, mirroring ``run_termination.readiness_finalizer``
    and ``review_cycle.existing_review_verdict_recorder``.
    """

    def revert(
        snapshot: plans_models.PlanSnapshot,
        session_id: str,
        *,
        failure_note: str,
    ) -> Path | None:
        reverted = _revert_session_status_to_pending(
            snapshot,
            session_id,
            failure_note=failure_note,
            append_handoff_note=append_handoff_note,
        )
        return reverted.path if reverted is not None else None

    return revert


def _session_path_in_snapshot(
    snapshot: plans_models.PlanSnapshot, session_id: str
) -> Path | None:
    for session in snapshot.sessions:
        if session.id == session_id:
            return session.path
    return None


def _append_progress_entry(
    after: plans_models.PlanSnapshot,
    session_id: str,
    completed_at: str,
) -> bool:
    """Append a progress-journal entry for the just-completed session.

    Returns ``True`` when a new entry was written, ``False`` when the call
    was an idempotent no-op because the most recent journal entry already
    matches ``(session_id, completed_at)``.
    """
    session_status = next(
        (item for item in after.sessions if item.id == session_id),
        None,
    )
    if session_status is None:
        raise plans_errors.PlanError(
            f"session {session_id!r} not found in plan snapshot"
        )
    handoff = plans_handoff.read_session_handoff(session_status.session)
    files_modified: tuple[str, ...] = ()
    if handoff.files_modified:
        inline = handoff.files_modified[0]
        files_modified = tuple(
            part.strip() for part in inline.split(",") if part.strip()
        )
    entry = plans_models.ProgressEntry(
        session_id=handoff.session_id,
        status=handoff.status,
        completed_at=completed_at,
        files_modified=files_modified,
        key_decisions=handoff.key_decisions,
        discovered_facts=handoff.discovered_facts,
        open_questions=handoff.open_questions,
        blockers=handoff.blockers,
    )
    existing = plans_progress.read_progress_entries(after.plan_dir)
    if (
        existing
        and existing[-1].session_id == entry.session_id
        and existing[-1].completed_at == entry.completed_at
    ):
        return False
    plans_progress.append_progress_entry(after.plan_dir, entry)
    return True


def _revert_session_status_to_pending(
    snapshot: plans_models.PlanSnapshot,
    session_id: str,
    *,
    failure_note: str,
    append_handoff_note: HandoffNoteAppender,
) -> SessionRevert | None:
    """Return the session to pending and append ``failure_note``.

    Returns ``None`` when the session file or its Handoff section is missing.
    Otherwise the file was rewritten (the note is always appended) and the
    result records the status it held before, so callers can tell a real
    reopening from a session that was already pending.
    """
    path = _session_path_in_snapshot(snapshot, session_id)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines(keepends=False)
    handoff_index: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == plans_handoff.HANDOFF_HEADING:
            handoff_index = index
            break
    if handoff_index is None:
        return None
    section_end = len(lines)
    for index in range(handoff_index + 1, len(lines)):
        if lines[index].startswith("## "):
            section_end = index
            break
    from_status: str | None = None
    for index in range(handoff_index + 1, section_end):
        if lines[index].startswith(plans_handoff.STATUS_PREFIX):
            from_status = lines[index][len(plans_handoff.STATUS_PREFIX) :].strip()
            lines[index] = f"{plans_handoff.STATUS_PREFIX} pending"
            break
    lines = append_handoff_note(
        lines,
        handoff_index=handoff_index,
        section_end=section_end,
        note=failure_note,
    )
    suffix = "\n" if text.endswith("\n") else ""
    path.write_text("\n".join(lines) + suffix, encoding="utf-8")
    return SessionRevert(path=path, from_status=from_status)
