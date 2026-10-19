"""History entries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from . import history_checkpoints as checkpoints
from . import history_outcomes as outcomes
from . import history_primitives as primitives
from .catalog import (
    MAX_LABEL_CHARS,
    digest_of,
    display_label,
    plan_key,
    project_names,
    run_key,
    safe_label,
)

# -- Entries ----------------------------------------------------------------


@dataclass(frozen=True)
class HistoryEntry:
    """One finished run, as both the local view and the remote provider see it.

    Every field is a digest, a timestamp, a counter, or a member of a vocabulary
    declared in this module. There is no path, no operator-typed prose, and no
    harness output, because none of those has a name here to be copied into:
    :func:`project_entry` reads the terminal record field by field rather than
    copying it, so a future ASL that adds a field adds nothing to this entry.
    """

    entry_id: str
    source_key: str
    plan_key: str
    run_key: str
    outcome: str
    recorded_at: float
    event_schema_version: int
    project: str
    projects: tuple[str, ...] = ()
    run_id: str | None = None
    harness: str | None = None
    model: str | None = None
    pause_reason: str | None = None
    run_started_at: float | None = None
    run_finished_at: float | None = None
    duration_seconds: float | None = None
    turn_count: int | None = None
    completed_session_count: int | None = None
    pending_session_count: int | None = None
    completed_session_ids: tuple[str, ...] = ()
    review_completed: bool | None = None
    review_outcome: str | None = None
    reconstructed: bool = False

    @property
    def succeeded(self) -> bool:
        """Whether this entry may be rendered as a successful run."""
        return self.outcome in primitives.HISTORY_SUCCESS_OUTCOMES

    def to_payload(self) -> dict[str, object]:
        """The wire and storage shape, with absent facts absent rather than null."""
        payload: dict[str, object] = {
            "entry_id": self.entry_id,
            "source_key": self.source_key,
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "project": self.project,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "event_schema_version": self.event_schema_version,
        }
        if self.projects:
            payload["projects"] = list(self.projects)
        for key, value in (
            ("run_id", self.run_id),
            ("harness", self.harness),
            ("model", self.model),
            ("pause_reason", self.pause_reason),
            ("run_started_at", self.run_started_at),
            ("run_finished_at", self.run_finished_at),
            ("duration_seconds", self.duration_seconds),
            ("turn_count", self.turn_count),
            ("completed_session_count", self.completed_session_count),
            ("pending_session_count", self.pending_session_count),
            ("review_completed", self.review_completed),
            ("review_outcome", self.review_outcome),
        ):
            if value is not None:
                payload[key] = value
        if self.completed_session_ids:
            payload["completed_session_ids"] = list(self.completed_session_ids)
        if self.reconstructed:
            payload["reconstructed"] = True
        return payload


@dataclass(frozen=True)
class QuestionEntry:
    """One question a run asked, and how the wait for it ended.

    The question's text never appears, here or anywhere below it: what history
    records is that a run stopped to ask, when, and whether anybody answered.
    ``question_key`` is a digest rather than ASL's own question id, for the same
    reason ``plan_key`` is a digest rather than a path -- history publishes its
    own identifiers, not another program's.
    """

    entry_id: str
    source_key: str
    plan_key: str
    run_key: str
    question_key: str
    outcome: str
    recorded_at: float
    session_id: str | None = None
    asked_at: float | None = None
    resolved_at: float | None = None
    waited_seconds: float | None = None

    @property
    def answered(self) -> bool:
        return self.outcome == primitives.QUESTION_OUTCOME_ANSWERED

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "entry_id": self.entry_id,
            "source_key": self.source_key,
            "plan_key": self.plan_key,
            "run_key": self.run_key,
            "question_key": self.question_key,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
        }
        for key, value in (
            ("session_id", self.session_id),
            ("asked_at", self.asked_at),
            ("resolved_at", self.resolved_at),
            ("waited_seconds", self.waited_seconds),
        ):
            if value is not None:
                payload[key] = value
        return payload


def entry_id_for(plan: str, run: str) -> str:
    """The stable id of one run's history entry."""
    return digest_of(["history-entry", plan, run])


def question_key_for(plan: str, question_id: str) -> str:
    """The stable, opaque identity of one question of one plan."""
    return digest_of(["history-question-key", plan, question_id])


def question_entry_id_for(plan: str, run: str, question: str) -> str:
    """The stable id of one question's history entry."""
    return digest_of(["history-question", plan, run, question])


def project_entry(
    body: Mapping[str, object],
    *,
    source_key: str,
    plan_dir: str,
    facts: outcomes.PlanFacts,
    started: checkpoints.OpenRun | None,
    recorded_at: float,
    version: int,
) -> HistoryEntry:
    """Build one history entry from one terminal record.

    Every field is read by name. Nothing is copied wholesale, so the fields that
    carry prose -- ``pause_message``, ``pause_details``, the review verdict's
    status line, the assurance report -- have no name in the entry to be copied
    into.
    """
    plan = plan_key(plan_dir)
    run_id = primitives._nonempty(body.get("run_id"))
    run = run_key(plan, run_id)
    outcome = outcomes.classify_outcome(body)

    workspace_root = (
        primitives._nonempty(body.get("workspace_root")) or facts.workspace_root
    )
    repositories = body.get("repositories")
    count = primitives._repository_count(repositories) or facts.repository_count
    if isinstance(repositories, list) and repositories:
        projects = project_names(repositories, workspace_root=workspace_root)
    elif facts.projects:
        projects = facts.projects
    else:
        projects = project_names((), workspace_root=workspace_root)

    finished_at = primitives.epoch(body.get("run_finished_at"))
    if finished_at is None and outcome == primitives.HISTORY_OUTCOME_COMPLETED:
        # The one outcome rendered as a success has to say when it succeeded.
        # Without that it is a claim with no evidence behind it, and the honest
        # label for a claim with no evidence is already in the vocabulary.
        outcome = primitives.HISTORY_OUTCOME_UNKNOWN

    return HistoryEntry(
        entry_id=entry_id_for(plan, run),
        source_key=source_key,
        plan_key=plan,
        run_key=run,
        outcome=outcome,
        recorded_at=recorded_at,
        event_schema_version=version,
        project=display_label(
            workspace_root=workspace_root,
            plan_dir=plan_dir,
            repository_count=count,
        ),
        projects=projects,
        run_id=run_id,
        harness=primitives._nonempty(body.get("harness"))
        or (started.harness if started else None),
        model=safe_label(
            primitives._nonempty(body.get("observed_model"))
            or primitives._nonempty(body.get("model"))
            or (started.model if started else None),
            limit=MAX_LABEL_CHARS,
        ),
        pause_reason=outcomes.pause_reason_code(body, outcome),
        run_started_at=primitives.epoch(body.get("run_started_at"))
        or (started.started_at if started else None),
        run_finished_at=finished_at,
        duration_seconds=outcomes._duration(body),
        turn_count=_count_field(body.get("turn_count")),
        completed_session_count=outcomes._count(body, "complete"),
        pending_session_count=outcomes._count(body, "pending"),
        completed_session_ids=outcomes._session_ids(body),
        review_completed=_flag(body.get("review_completed")),
        review_outcome=outcomes._review_outcome(body),
        reconstructed=outcome == primitives.HISTORY_OUTCOME_ABANDONED,
    )


def _flag(value: object) -> bool | None:
    """A recorded boolean, or ``None`` when the field said anything else."""
    return value if isinstance(value, bool) else None


def _count_field(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def project_question(
    *,
    source_key: str,
    plan_dir: str,
    question_id: str,
    run_id: str | None,
    session_id: str | None,
    asked_at: float | None,
    resolved_at: float | None,
    recorded_at: float,
    outcome: str,
) -> QuestionEntry:
    """Build one question entry from a wait's opening and closing facts."""
    plan = plan_key(plan_dir)
    run = run_key(plan, run_id)
    question = question_key_for(plan, question_id)
    waited = (
        resolved_at - asked_at
        if asked_at is not None and resolved_at is not None and resolved_at >= asked_at
        else None
    )
    return QuestionEntry(
        entry_id=question_entry_id_for(plan, run, question),
        source_key=source_key,
        plan_key=plan,
        run_key=run,
        question_key=question,
        outcome=outcome,
        recorded_at=recorded_at,
        session_id=session_id,
        asked_at=asked_at,
        resolved_at=resolved_at,
        waited_seconds=waited,
    )
