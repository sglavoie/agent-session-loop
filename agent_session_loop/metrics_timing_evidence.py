"""Reconcile repair lineage, verification checks, and repeated timing attempts."""

from __future__ import annotations

from typing import Any

from . import (
    events,
    execution_timing,
    metrics_invocations,
    metrics_models,
    plans_models,
    review_repair,
)


def _hashable(value: Any) -> Any:
    """A dict key for a persisted value of unproven type."""
    return value if isinstance(value, (str, int, type(None))) else repr(value)


def _repair_session_ids(
    event_records: list[dict[str, Any]],
    snapshot: plans_models.PlanSnapshot | None,
) -> set[str]:
    """Session ids that are generated review-repair children.

    Two independent kinds of evidence answer this, and the *shape* of a session
    id is neither of them: an authored plan may legitimately use dotted ids, so
    a dotted id is a candidate, never a classification. Materialization lineage
    names the children a review created, and a plan's own session bodies carry
    the generated marker. Dotted ids only decide which bodies are worth reading.
    """
    found: set[str] = set()
    for record in event_records:
        if record.get("name") not in (
            events.REVIEW_CYCLE_EVENT,
            events.REVIEW_REPAIR_REOPENED_EVENT,
        ):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        raw_ids = payload.get("repair_session_ids")
        if isinstance(raw_ids, list):
            found.update(value for value in raw_ids if isinstance(value, str) and value)
        raw_groups = payload.get("repair_groups")
        if isinstance(raw_groups, list):
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                value = group.get("repair_session_id")
                if isinstance(value, str) and value:
                    found.add(value)
    if snapshot is not None:
        for session in snapshot.sessions:
            if session.session_id.is_integer:
                continue
            try:
                body = session.path.read_text(encoding="utf-8")
            except OSError:
                continue
            if review_repair.contains_review_repair_session_marker(body):
                found.add(session.id)
    return found


def _check_slot(
    evidence: metrics_models._PlanTimingEvidence, run_id: str, payload: dict[str, Any]
) -> metrics_models._CheckRun:
    key = (
        run_id,
        _hashable(payload.get("turn_index")),
        _hashable(payload.get("check_index")),
    )
    slot = evidence.checks.get(key)
    if slot is None:
        slot = metrics_models._CheckRun(run_id=run_id)
        evidence.checks[key] = slot
    return slot


def _ingest_timing_evidence(
    evidence: metrics_models._PlanTimingEvidence, event_records: list[dict[str, Any]]
) -> None:
    """Read check identity and review-scope evidence from the same records.

    Read from the whole history rather than the window: a check or a scope
    selection belongs to the report when its *run* does, and a run that finished
    inside the window may have started its checks before the window opened.
    """
    for record in event_records:
        name = record.get("name")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        run_id = metrics_invocations._string_or_none(payload.get("run_id"))
        if run_id is None:
            continue
        if name == events.VERIFICATION_CHECK_STARTED_EVENT:
            check = _check_slot(evidence, run_id, payload)
            check.name = (
                metrics_invocations._string_or_none(payload.get("name")) or check.name
            )
            check.kind = (
                metrics_invocations._string_or_none(payload.get("kind")) or check.kind
            )
            check.repository = (
                metrics_invocations._string_or_none(payload.get("repo"))
                or check.repository
            )
            check.working_directory = (
                metrics_invocations._string_or_none(payload.get("cwd"))
                or check.working_directory
            )
            argv = payload.get("argv")
            if isinstance(argv, list):
                command = tuple(item for item in argv if isinstance(item, str))
                if command:
                    check.argv = command
        elif name == events.VERIFICATION_CHECK_FINISHED_EVENT:
            check = _check_slot(evidence, run_id, payload)
            check.finish_seen = True
            check.name = (
                metrics_invocations._string_or_none(payload.get("name")) or check.name
            )
            check.kind = (
                metrics_invocations._string_or_none(payload.get("kind")) or check.kind
            )
            check.repository = (
                metrics_invocations._string_or_none(payload.get("repo"))
                or check.repository
            )
            check.status = (
                metrics_invocations._string_or_none(payload.get("status"))
                or check.status
            )
            duration = payload.get("duration_seconds")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                seconds = float(duration)
                # First usable reading wins, as it does in the execution timing
                # collector: a repeated finish restates one execution, and
                # letting the later copy overwrite would make the scoped seconds
                # here disagree with the cumulative check work beside them.
                if (
                    check.duration_seconds is None
                    and seconds >= 0
                    and seconds == seconds
                    and seconds != float("inf")
                ):
                    check.duration_seconds = seconds
        elif name == events.REVIEW_SCOPE_EVENT:
            evidence.review_scopes.append(
                (
                    run_id,
                    metrics_invocations._string_or_none(payload.get("scope"))
                    or metrics_models.UNKNOWN_LABEL,
                    metrics_invocations._string_or_none(payload.get("fallback_reason")),
                    payload.get("refreshed") is True,
                )
            )


def _check_scope(
    plan_key: str, check: metrics_models._CheckRun
) -> metrics_models._CheckScope:
    command = " ".join(check.argv) if check.argv else check.name
    if check.repository is None and check.working_directory is None:
        return metrics_models._CheckScope(
            None, None, plan_key, command or metrics_models.UNKNOWN_COMMAND_LABEL
        )
    return metrics_models._CheckScope(
        check.repository,
        check.working_directory,
        None,
        command or metrics_models.UNKNOWN_COMMAND_LABEL,
    )


def _attempt_phase(
    attempt: execution_timing.TurnAttempt, repair_session_ids: set[str]
) -> str:
    phase = attempt.phase
    if phase != execution_timing.PHASE_WORKER_EXECUTION:
        return phase
    if attempt.session_id is not None and attempt.session_id in repair_session_ids:
        return metrics_models.TIMING_PHASE_GENERATED_REPAIR
    return metrics_models.TIMING_PHASE_IMPLEMENTATION


def _run_phase_seconds(
    run: execution_timing.RunTiming, repair_session_ids: set[str]
) -> dict[str, float]:
    """One run's seconds under this report's phase vocabulary.

    Turn phases are re-derived from the run's attempts so the implementation and
    generated-repair figures add back up to the collector's single
    ``worker_execution`` total; the remaining phases are the collector's own.
    """
    seconds = dict.fromkeys(metrics_models.TIMING_PHASES, 0.0)
    for attempt in run.attempts:
        if attempt.duration_seconds is None:
            continue
        seconds[_attempt_phase(attempt, repair_session_ids)] += attempt.duration_seconds
    for phase in metrics_models._RUN_LEVEL_TIMING_PHASES:
        seconds[phase] += run.phase_seconds.get(phase, 0.0)
    if run.unattributed_seconds is not None:
        seconds[execution_timing.PHASE_UNATTRIBUTED] += run.unattributed_seconds
    return seconds


def _attempt_order_key(
    run: execution_timing.RunTiming, attempt: execution_timing.TurnAttempt
) -> tuple[int, str, str, int]:
    """Order a session's attempts oldest first, undated ones last."""
    moment = attempt.started_at or attempt.finished_at
    return (
        1 if moment is None else 0,
        moment.isoformat() if moment is not None else "",
        run.run_id,
        attempt.turn_index if attempt.turn_index is not None else -1,
    )


def _repeated_attempts(
    timing: execution_timing.ExecutionTimingReport,
    repair_ids: dict[str, set[str]],
    included_run_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    """Count in-window attempts that followed an earlier attempt.

    Ordering spans the *whole* supplied history, not the window, so a retry
    whose first attempt happened before the window opened is still recognized as
    a repeat rather than mistaken for a first try.

    The seconds are reported as what they are — time spent on attempts after the
    first. Some of it produced the work that finally landed, so it is never
    labelled discarded or wasted.
    """
    ordered: dict[
        tuple[str, str],
        list[
            tuple[
                tuple[int, str, str, int],
                execution_timing.RunTiming,
                execution_timing.TurnAttempt,
            ]
        ],
    ] = {}
    for run in timing.runs:
        for attempt in run.attempts:
            if attempt.session_id is None:
                continue
            ordered.setdefault((run.plan_key, attempt.session_id), []).append(
                (_attempt_order_key(run, attempt), run, attempt)
            )

    additional = 0
    additional_seconds = 0.0
    attempts_without_duration = 0
    sessions_with_additional = 0
    by_phase: dict[str, int] = {}
    per_plan: dict[str, int] = {}
    for (plan_key, _session_id), items in ordered.items():
        items.sort(key=lambda item: item[0])
        counted = 0
        for _key, run, attempt in items[1:]:
            if (run.plan_key, run.run_id) not in included_run_keys:
                continue
            counted += 1
            additional += 1
            phase = _attempt_phase(attempt, repair_ids.get(plan_key, set()))
            by_phase[phase] = by_phase.get(phase, 0) + 1
            if attempt.duration_seconds is None:
                attempts_without_duration += 1
            else:
                additional_seconds += attempt.duration_seconds
        if counted:
            sessions_with_additional += 1
            per_plan[plan_key] = per_plan.get(plan_key, 0) + counted

    return {
        "sessions_tracked": len(ordered),
        "sessions_with_additional_attempts": sessions_with_additional,
        "additional_attempts": additional,
        "additional_attempt_seconds": round(additional_seconds, 3),
        "additional_attempts_without_duration": attempts_without_duration,
        "additional_attempts_by_phase": dict(sorted(by_phase.items())),
        "_per_plan": per_plan,
    }
