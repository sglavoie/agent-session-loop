"""History combine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import history_checkpoints as checkpoints_module
from . import history_consume as consume
from . import history_entries as entries_module
from . import history_primitives as primitives

# -- Import results ---------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    """Everything one import pass produced, and everything it could not."""

    entries: tuple[entries_module.HistoryEntry, ...] = ()
    questions: tuple[entries_module.QuestionEntry, ...] = ()
    checkpoints: tuple[checkpoints_module.SourceCheckpoint, ...] = ()
    window_start: float = 0.0
    window_end: float = 0.0
    sources: int = 0
    restarted_sources: int = 0
    unreadable_sources: int = 0
    unresolved_sources: int = 0
    malformed: int = 0
    duplicates: int = 0
    unattributed: int = 0
    expired: int = 0
    undatable: int = 0
    unsupported: int = 0
    oversized: int = 0
    deferred_bytes: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether this pass saw the whole window it claims to describe.

        False while any source is unread or unidentifiable, while any bytes
        remain to consume, and whenever a record was dropped -- unparseable,
        undatable, oversized, or naming no run. Every one of those is a run
        whose ending may be missing, and a receiver treats an incomplete import
        the way it treats a truncated snapshot: as proof of nothing about what
        is not in it.

        Expired records are deliberately not counted. Being outside the
        retention window is the filter working, not evidence going missing.
        """
        return (
            self.unreadable_sources == 0
            and self.unresolved_sources == 0
            and self.deferred_bytes == 0
            and self.malformed == 0
            and self.unattributed == 0
            and self.undatable == 0
            and self.unsupported == 0
            and self.oversized == 0
        )


def _entry_order(entry: entries_module.HistoryEntry) -> tuple[float, str]:
    """Oldest ending first, with the entry id breaking ties deterministically."""
    return (entry.recorded_at, entry.entry_id)


def _question_order(entry: entries_module.QuestionEntry) -> tuple[float, str]:
    return (entry.recorded_at, entry.entry_id)


def combine(
    reads: Sequence[consume.SourceRead],
    *,
    base: Mapping[str, checkpoints_module.SourceCheckpoint],
    window_start: float,
    window_end: float,
    sources: int,
) -> ImportResult:
    """Fold every source's read into one pass result.

    Deduplication happens across sources as well as within one: two state
    directories can describe the same plan after a plan directory moves, and an
    entry id is a function of the run rather than of the file it was read from.

    ``base`` is the checkpoint set the pass started from. Checkpoints for
    sources this pass did not see are carried forward rather than dropped, so a
    state directory that disappears and comes back does not re-deliver the
    history it already delivered.
    """
    checkpoints = dict(base)
    entries: list[entries_module.HistoryEntry] = []
    questions: list[entries_module.QuestionEntry] = []
    totals = {
        "restarted": 0,
        "unreadable": 0,
        "unresolved": 0,
        "malformed": 0,
        "duplicates": 0,
        "unattributed": 0,
        "expired": 0,
        "undatable": 0,
        "unsupported": 0,
        "oversized": 0,
        "deferred": 0,
    }
    for read in reads:
        checkpoints[read.checkpoint.source_key] = read.checkpoint
        entries.extend(read.entries)
        questions.extend(read.questions)
        totals["restarted"] += 1 if read.restarted else 0
        totals["unreadable"] += 1 if read.unreadable else 0
        totals["unresolved"] += 1 if read.unresolved else 0
        totals["malformed"] += read.malformed
        totals["duplicates"] += read.duplicates
        totals["unattributed"] += read.unattributed
        totals["expired"] += read.expired
        totals["undatable"] += read.undatable
        totals["unsupported"] += read.unsupported
        totals["oversized"] += read.oversized
        totals["deferred"] += read.deferred_bytes

    seen: set[str] = set()
    unique_entries: list[entries_module.HistoryEntry] = []
    for entry in entries:
        if entry.entry_id in seen:
            totals["duplicates"] += 1
            continue
        seen.add(entry.entry_id)
        unique_entries.append(entry)
    unique_questions: list[entries_module.QuestionEntry] = []
    for question in questions:
        if question.entry_id in seen:
            totals["duplicates"] += 1
            continue
        seen.add(question.entry_id)
        unique_questions.append(question)
    unique_entries.sort(key=_entry_order)
    unique_questions.sort(key=_question_order)

    warnings: list[str] = []
    for label, count in (
        ("could not be read", totals["unreadable"]),
        ("could not be identified", totals["unresolved"]),
    ):
        if count:
            warnings.append(f"{count} ASL plan(s) {label} during this import")
    if totals["malformed"]:
        warnings.append(f"{totals['malformed']} event record(s) were unreadable")
    if totals["unattributed"]:
        warnings.append(f"{totals['unattributed']} terminal record(s) named no run")
    if totals["oversized"]:
        warnings.append(f"{totals['oversized']} event record(s) were too large to read")
    if totals["deferred"]:
        warnings.append("more history remains to be imported")

    return ImportResult(
        entries=tuple(unique_entries),
        questions=tuple(unique_questions),
        checkpoints=tuple(
            checkpoint
            for _, checkpoint in sorted(checkpoints.items(), key=lambda item: item[0])
        ),
        window_start=window_start,
        window_end=window_end,
        sources=sources,
        restarted_sources=totals["restarted"],
        unreadable_sources=totals["unreadable"],
        unresolved_sources=totals["unresolved"],
        malformed=totals["malformed"],
        duplicates=totals["duplicates"],
        unattributed=totals["unattributed"],
        expired=totals["expired"],
        undatable=totals["undatable"],
        unsupported=totals["unsupported"],
        oversized=totals["oversized"],
        deferred_bytes=totals["deferred"],
        warnings=tuple(warnings[: primitives.MAX_WARNINGS]),
    )
