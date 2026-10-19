"""History views."""

from __future__ import annotations

from dataclasses import dataclass

from . import history_combine as combine
from . import history_entries as entries_module
from . import history_primitives as primitives
from .catalog import (
    digest_of,
)

# -- Projections ------------------------------------------------------------


@dataclass(frozen=True)
class PlanHistory:
    """One plan's runs in the window, rolled up for a local list."""

    plan_key: str
    project: str
    runs: int
    succeeded: int
    questions: int
    answered: int
    last_recorded_at: float
    outcomes: tuple[tuple[str, int], ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "plan_key": self.plan_key,
            "project": self.project,
            "runs": self.runs,
            "succeeded": self.succeeded,
            "questions": self.questions,
            "answered": self.answered,
            "last_recorded_at": self.last_recorded_at,
            "outcomes": {outcome: count for outcome, count in self.outcomes},
        }


@dataclass(frozen=True)
class HistoryView:
    """The local rendering of one import: the same entries, grouped by plan."""

    entries: tuple[entries_module.HistoryEntry, ...] = ()
    questions: tuple[entries_module.QuestionEntry, ...] = ()
    plans: tuple[PlanHistory, ...] = ()
    window_start: float = 0.0
    window_end: float = 0.0
    complete: bool = True
    warnings: tuple[str, ...] = ()

    def outcome_counts(self) -> dict[str, int]:
        """How many runs ended each way, with every outcome named even at zero."""
        counts = dict.fromkeys(primitives.HISTORY_OUTCOMES, 0)
        for entry in self.entries:
            counts[entry.outcome] = counts.get(entry.outcome, 0) + 1
        return counts

    def question_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(primitives.QUESTION_OUTCOMES, 0)
        for question in self.questions:
            counts[question.outcome] = counts.get(question.outcome, 0) + 1
        return counts

    def to_payload(self) -> dict[str, object]:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "complete": self.complete,
            "entry_count": len(self.entries),
            "question_count": len(self.questions),
            "outcomes": self.outcome_counts(),
            "questions": self.question_counts(),
            "plans": [plan.to_payload() for plan in self.plans],
            "entries": [entry.to_payload() for entry in self.entries],
            "question_entries": [question.to_payload() for question in self.questions],
        }


def build_view(result: combine.ImportResult) -> HistoryView:
    """Group one import's entries for local presentation.

    The entries are the objects :func:`batches` publishes, not copies of them,
    so the cockpit on the Mac and the cockpit on the Pi cannot disagree about
    what a run did.
    """
    plans: dict[str, list[entries_module.HistoryEntry]] = {}
    for entry in result.entries:
        plans.setdefault(entry.plan_key, []).append(entry)
    questions: dict[str, list[entries_module.QuestionEntry]] = {}
    for question in result.questions:
        questions.setdefault(question.plan_key, []).append(question)

    rollups: list[PlanHistory] = []
    for key in sorted(plans.keys() | questions.keys()):
        runs = plans.get(key, [])
        asked = questions.get(key, [])
        outcomes: dict[str, int] = {}
        for entry in runs:
            outcomes[entry.outcome] = outcomes.get(entry.outcome, 0) + 1
        moments = [entry.recorded_at for entry in runs]
        moments.extend(question.recorded_at for question in asked)
        rollups.append(
            PlanHistory(
                plan_key=key,
                project=runs[0].project if runs else "",
                runs=len(runs),
                succeeded=sum(1 for entry in runs if entry.succeeded),
                questions=len(asked),
                answered=sum(1 for question in asked if question.answered),
                last_recorded_at=max(moments) if moments else 0.0,
                outcomes=tuple(sorted(outcomes.items())),
            )
        )
    rollups.sort(key=lambda plan: (-plan.last_recorded_at, plan.plan_key))

    return HistoryView(
        entries=result.entries,
        questions=result.questions,
        plans=tuple(rollups),
        window_start=result.window_start,
        window_end=result.window_end,
        complete=result.complete,
        warnings=result.warnings,
    )


@dataclass(frozen=True)
class HistoryBatch:
    """One bounded, idempotent chunk of an import, ready for a transport.

    The batch id is a digest of the entry ids it carries, so re-sending the same
    import produces byte-identical batches rather than a new one the receiver
    has to decide about.
    """

    batch_id: str
    index: int
    generated_at: float
    window_start: float
    window_end: float
    complete: bool
    entries: tuple[entries_module.HistoryEntry, ...] = ()
    questions: tuple[entries_module.QuestionEntry, ...] = ()
    more: bool = False
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "batch_id": self.batch_id,
            "generated_at": self.generated_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "entry_count": len(self.entries),
            "question_count": len(self.questions),
            "complete": self.complete,
            "entries": [entry.to_payload() for entry in self.entries],
            "questions": [question.to_payload() for question in self.questions],
        }
        if self.more:
            payload["more"] = True
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


def batches(result: combine.ImportResult, *, now: float) -> tuple[HistoryBatch, ...]:
    """Split one import into bounded, idempotent upload batches.

    An import with nothing in it still produces one batch: "the window holds no
    history" and "the connector has not looked" are different claims, and only
    the first is worth knowing.
    """
    size = primitives.MAX_HISTORY_ENTRIES_PER_BATCH
    runs = [
        result.entries[index : index + size]
        for index in range(0, len(result.entries), size)
    ]
    asked = [
        result.questions[index : index + size]
        for index in range(0, len(result.questions), size)
    ]
    count = max(len(runs), len(asked), 1)
    built: list[HistoryBatch] = []
    for index in range(count):
        chunk = runs[index] if index < len(runs) else ()
        question_chunk = asked[index] if index < len(asked) else ()
        built.append(
            HistoryBatch(
                batch_id=digest_of(
                    [
                        "history-batch",
                        index,
                        [entry.entry_id for entry in chunk],
                        [question.entry_id for question in question_chunk],
                    ]
                ),
                index=index,
                generated_at=now,
                window_start=result.window_start,
                window_end=result.window_end,
                complete=result.complete,
                entries=tuple(chunk),
                questions=tuple(question_chunk),
                more=index + 1 < count,
                warnings=result.warnings,
            )
        )
    return tuple(built)
