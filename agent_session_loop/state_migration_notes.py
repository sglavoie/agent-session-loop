"""Conversation and redirect-journal notes attached to a migration report."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import conversations
from .state_schema_upgrade import MigrationError


def _conversation_note(report: conversations.RelocationReport) -> str | None:
    """Describe what relocation did to the conversation ledger, if anything."""
    parts: list[str] = []
    if report.records:
        parts.append(
            f"{report.rewritten} of {report.records} conversation reference(s) "
            f"{'were' if report.applied else 'would be'} repointed at the new "
            "plan directory"
        )
    if report.rejected:
        parts.append(
            f"{report.rejected} unreadable ledger line(s) were preserved "
            "unchanged; conversations cannot be resumed until they are resolved"
        )
    if not parts:
        return None
    return "; ".join(parts) + "."


@dataclass(frozen=True)
class _RedirectJournalSurvey:
    """What the redirect journal holds, counted without reading a word of it."""

    requests: int
    unfinished: int
    unreadable_lines: int
    torn_tail: bool


def _survey_redirect_journal(state_dir: Path) -> _RedirectJournalSurvey:
    """Count the redirect journal's requests, or refuse if it cannot be read.

    Imported locally because :mod:`redirect_store` imports this module; a
    top-level import would close the cycle. Migration needs only counts, so
    nothing here reaches ``read_feedback`` and no operator correction can travel
    into a report, an event, or a printed line.
    """
    from . import redirect_store  # local: it imports state

    try:
        view = redirect_store.read_journal_at(state_dir)
    except redirect_store.RedirectStoreError as error:
        raise MigrationError(
            "redirect-journal-error",
            f"source redirect journal could not be read: {error}",
            {"from_state_dir": str(state_dir)},
        ) from error
    return _RedirectJournalSurvey(
        requests=len(view.requests),
        unfinished=sum(
            1
            for request in view.requests
            if not request.terminal or (request.accepted and not request.carried)
        ),
        unreadable_lines=len(view.rejected),
        torn_tail=view.torn_tail,
    )


def _redirect_note(survey: _RedirectJournalSurvey, *, applied: bool) -> str | None:
    """Describe what relocation does to the redirect journal, if anything.

    Relocation moves the journal with its directory and rewrites nothing inside
    it, because a redirect request names a run, a session and a turn — none of
    which is a path. What the note reports is therefore what an operator is
    carrying to the new location rather than what changed, which matters
    because an unfinished request is a correction still owed an answer and the
    run that owed it is, by the time anyone is migrating, long gone.
    """
    if not survey.requests and not survey.unreadable_lines and not survey.torn_tail:
        return None
    parts = [
        f"{survey.requests} redirect request(s) "
        f"{'moved' if applied else 'would move'} with the state directory; "
        "their run, session and turn identities are unchanged by relocation"
    ]
    if survey.unfinished:
        parts.append(
            f"{survey.unfinished} of them {'is' if survey.unfinished == 1 else 'are'} "
            "still unfinished and will be reconciled by the next run"
        )
    if survey.unreadable_lines or survey.torn_tail:
        parts.append(
            f"{survey.unreadable_lines} unreadable line(s)"
            f"{' and a torn tail' if survey.torn_tail else ''} "
            "were preserved unchanged"
        )
    return "; ".join(parts) + "."
