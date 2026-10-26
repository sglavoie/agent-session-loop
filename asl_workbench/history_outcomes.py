"""History outcomes."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from . import history_primitives as primitives
from .catalog import (
    project_names,
)

# -- Classification ---------------------------------------------------------


def classify_outcome(payload: Mapping[str, object]) -> str:
    """Name how a run ended, from the fields its terminal record recorded.

    The order is the point. A reconstructed record is classified as abandoned
    before its ``ok``/``paused`` flags are read, because those describe the state
    the dead run was last seen in rather than a decision it made. A paused run is
    classified before a failed one, because ASL records a pause as ``ok: false``
    and a pause is a run waiting for a person, not a run that failed.

    ``completed`` requires all three of ``ok``, ``paused``, and ASL's
    ``all-complete`` reason to agree. A record missing any of them, or carrying a
    reason this vocabulary does not hold, is ``unknown``: a cockpit can show an
    unknown ending and an operator can go and look, whereas a guessed completion
    is a run nobody ever looks at again.
    """
    if (
        payload.get("synthetic") is True
        or payload.get("outcome") == primitives.ASL_OUTCOME_ABANDONED
    ):
        return primitives.HISTORY_OUTCOME_ABANDONED
    paused = payload.get("paused")
    ok = payload.get("ok")
    reason = payload.get("reason")
    if paused is True:
        return primitives.HISTORY_OUTCOME_PAUSED
    if not isinstance(ok, bool) or not isinstance(paused, bool):
        return primitives.HISTORY_OUTCOME_UNKNOWN
    if ok:
        if reason == primitives.ASL_REASON_ALL_COMPLETE:
            return primitives.HISTORY_OUTCOME_COMPLETED
        if reason in primitives.ASL_CLEAN_STOP_REASONS:
            return primitives.HISTORY_OUTCOME_STOPPED
        return primitives.HISTORY_OUTCOME_UNKNOWN
    return primitives.HISTORY_OUTCOME_FAILED


def pause_reason_code(payload: Mapping[str, object], outcome: str) -> str | None:
    """The closed pause code for a paused run, and nothing for any other ending.

    ASL puts its pause reason in ``pause_reason`` and mirrors it into ``reason``,
    so both are consulted.
    """
    if outcome != primitives.HISTORY_OUTCOME_PAUSED:
        return None
    for key in ("pause_reason", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value in primitives.PAUSE_REASONS:
            return value
    return primitives.PAUSE_REASON_OTHER


def question_outcome(reason: object) -> str:
    """Map one ``wait-cleared`` reason onto the closed question vocabulary."""
    for wait_reason, outcome in primitives.WAIT_REASON_OUTCOMES:
        if reason == wait_reason:
            return outcome
    return primitives.QUESTION_OUTCOME_UNRESOLVED


def _review_outcome(payload: Mapping[str, object]) -> str | None:
    verdict = payload.get("review_verdict")
    if not isinstance(verdict, Mapping):
        return None
    outcome = verdict.get("outcome")
    return (
        outcome
        if isinstance(outcome, str) and outcome in primitives.REVIEW_OUTCOMES
        else None
    )


def _session_ids(payload: Mapping[str, object]) -> tuple[str, ...]:
    values = payload.get("completed_session_ids")
    if not isinstance(values, list):
        values = payload.get("completed_sessions")
    if not isinstance(values, list):
        return ()
    canonical: list[str] = []
    for value in values:
        session = primitives._session_id(value)
        if session is not None and session not in canonical:
            canonical.append(session)
        if len(canonical) >= primitives.MAX_COMPLETED_SESSION_IDS:
            break
    return tuple(canonical)


def _count(payload: Mapping[str, object], key: str) -> int | None:
    counts = payload.get("counts")
    if not isinstance(counts, Mapping):
        return None
    value = counts.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _duration(payload: Mapping[str, object]) -> float | None:
    value = payload.get("duration_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    # A negative or non-finite duration is a clock fault, not a duration. It is
    # dropped rather than clamped: zero would read as an instantaneous run.
    if not math.isfinite(number) or number < 0:
        return None
    return number


# -- Plan facts -------------------------------------------------------------


@dataclass(frozen=True)
class PlanFacts:
    """What a plan's ``state.json`` says about where and what the plan is.

    The last fallback for a plan directory, used when neither the terminal
    record nor a remembered ``run_started`` supplied one, and the source of the
    project names when the terminal record carries no repository list.
    """

    plan_dir: str | None = None
    workspace_root: str | None = None
    repository_count: int = 0
    projects: tuple[str, ...] = ()


NO_PLAN_FACTS = PlanFacts()


"""The facts of a plan that said nothing, shared because it is immutable."""


def plan_facts_from_state(document: object) -> PlanFacts:
    """Read plan facts from a parsed ``state.json`` document.

    Anything that is not a mapping produces empty facts rather than an error:
    one damaged plan must not stop the import of the other several hundred, and
    the caller has no better answer to give than "this plan said nothing".
    """
    if not isinstance(document, Mapping):
        return NO_PLAN_FACTS
    workspace_root = primitives._nonempty(
        document.get("workspace_root")
    ) or primitives._nonempty(document.get("cwd"))
    repositories = document.get("repositories")
    return PlanFacts(
        plan_dir=primitives._nonempty(document.get("plan_dir")),
        workspace_root=workspace_root,
        repository_count=primitives._repository_count(repositories),
        projects=project_names(
            repositories if isinstance(repositories, list) else (),
            workspace_root=workspace_root,
        ),
    )
