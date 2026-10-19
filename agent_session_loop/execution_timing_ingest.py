from .execution_timing_models import *


@dataclass
class _AttemptEvidence:
    """Every statement supplied about one attempt, before reconciliation."""

    plan_key: str
    run_id: str
    turn_index: int | None
    #: source -> (started_at, finished_at, duration_seconds), in arrival order.
    timing: list[tuple[str, datetime | None, datetime | None, float | None]] = field(
        default_factory=list
    )
    session_ids: list[tuple[str, str]] = field(default_factory=list)
    session_kinds: list[tuple[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    started: bool = False
    accepted: bool = False
    rejected: bool = False
    executed: bool = False


@dataclass
class _PhaseEvidence:
    """One non-turn phase occurrence: verification, a commit, a finalization."""

    phase: str
    label: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    start_seen: bool = False
    finish_seen: bool = False
    finish_count: int = 0


@dataclass
class _CheckEvidence:
    """One verification check execution, however many records restate it."""

    duration_seconds: float | None = None
    finish_count: int = 0


@dataclass
class _RunEvidence:
    """Accumulated evidence for one ``(plan, run)`` pair."""

    plan_key: str
    run_id: str
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS
    run_started: Mapping[str, Any] | None = None
    run_finished: Mapping[str, Any] | None = None
    run_finished_created_at: datetime | None = None
    run_finished_count: int = 0
    attempts: dict[Any, _AttemptEvidence] = field(default_factory=dict)
    phases: dict[Any, _PhaseEvidence] = field(default_factory=dict)
    checks: dict[Any, _CheckEvidence] = field(default_factory=dict)
    anomalies: list[Observation] = field(default_factory=list)
    gaps: list[Observation] = field(default_factory=list)


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _hashable(value: Any) -> Any:
    """A dict key for a persisted value of unproven type.

    Mirrors ``metrics._hashable`` deliberately: the two collectors must agree
    on when two check records name the same check, or the same report would
    total a different number of checks in each of its two places.
    """
    return value if isinstance(value, (str, int, type(None))) else repr(value)


def _attempt_key(run: _RunEvidence, turn_index: Any, fallback: str) -> Any:
    """Key an attempt by its turn index, or uniquely when it has none.

    A record with no turn index cannot be reconciled against anything, so it
    gets a key nothing else can collide with rather than being merged into a
    shared ``None`` bucket where two unrelated attempts would cancel out.
    """
    if isinstance(turn_index, int) and not isinstance(turn_index, bool):
        return turn_index
    run.gaps.append(
        Observation(GAP_MISSING_IDENTITY, {"record": fallback, "field": "turn_index"})
    )
    return (fallback, len(run.attempts))


def _record_timing(
    run: _RunEvidence,
    attempt: _AttemptEvidence,
    source: str,
    payload: Mapping[str, Any],
) -> None:
    started_at = _parse_timestamp(payload.get("started_at"))
    finished_at = _parse_timestamp(payload.get("finished_at"))
    raw_duration = payload.get("duration_seconds")
    duration = _finite_seconds(raw_duration)
    if raw_duration is not None and duration is None:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": source,
                    "field": "duration_seconds",
                    "value": repr(raw_duration),
                    "turn_index": attempt.turn_index,
                },
            )
        )
    elif duration is not None and duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": source,
                    "field": "duration_seconds",
                    "value": duration,
                    "reason": "negative",
                    "turn_index": attempt.turn_index,
                },
            )
        )
        duration = None
    attempt.timing.append((source, started_at, finished_at, duration))
    session_id = _string_or_none(payload.get("session_id")) or _string_or_none(
        payload.get("session")
    )
    if session_id is not None:
        attempt.session_ids.append((source, session_id))
    session_kind = _string_or_none(payload.get("session_kind"))
    if session_kind is not None:
        attempt.session_kinds.append((source, session_kind))
    if source not in attempt.sources:
        attempt.sources.append(source)


def _phase_key(phase: str, discriminator: Any, ordinal: int) -> Any:
    if discriminator is None:
        return (phase, "unkeyed", ordinal)
    return (phase, discriminator)


def _phase_slot(
    run: _RunEvidence, phase: str, discriminator: Any, label: str
) -> _PhaseEvidence:
    key = _phase_key(phase, discriminator, len(run.phases))
    slot = run.phases.get(key)
    if slot is None:
        slot = _PhaseEvidence(phase=phase, label=label)
        run.phases[key] = slot
    return slot


def _record_phase_duration(
    run: _RunEvidence, slot: _PhaseEvidence, payload: Mapping[str, Any]
) -> None:
    raw_duration = payload.get("duration_seconds")
    duration = _finite_seconds(raw_duration)
    if raw_duration is not None and duration is None:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": slot.label,
                    "field": "duration_seconds",
                    "value": repr(raw_duration),
                },
            )
        )
    elif duration is not None and duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": slot.label,
                    "field": "duration_seconds",
                    "value": duration,
                    "reason": "negative",
                },
            )
        )
        duration = None
    finished_at = _parse_timestamp(payload.get("finished_at"))
    started_at = _parse_timestamp(payload.get("started_at"))
    if slot.finish_seen:
        slot.finish_count += 1
        previous = slot.duration_seconds
        if previous is not None and duration is not None:
            run.anomalies.append(
                Observation(
                    ANOMALY_DUPLICATE_EVIDENCE
                    if abs(previous - duration) <= run.tolerance_seconds
                    else ANOMALY_CONFLICTING_EVIDENCE,
                    {
                        "source": slot.label,
                        "field": "duration_seconds",
                        "values": [previous, duration],
                    },
                )
            )
        return
    slot.finish_seen = True
    slot.finish_count = 1
    slot.duration_seconds = duration
    if finished_at is not None:
        slot.finished_at = finished_at
    if started_at is not None and slot.started_at is None:
        slot.started_at = started_at


def _record_check(run: _RunEvidence, payload: Mapping[str, Any]) -> None:
    """Reconcile one check finish against the execution it reports on.

    ``(turn_index, check_index)`` is the check's durable identity within a run.
    A second finish for that identity is another copy of one execution, so its
    duration is compared against the first rather than summed with it: summing
    would report two checks and twice the work for a single replayed record,
    and would disagree with the per-scope totals the metrics collector derives
    from the same events.
    """
    raw_duration = payload.get("duration_seconds")
    duration = _finite_seconds(raw_duration)
    if duration is None or duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": events.VERIFICATION_CHECK_FINISHED_EVENT,
                    "field": "duration_seconds",
                    "value": repr(raw_duration),
                },
            )
        )
        return
    turn_index = payload.get("turn_index")
    check_index = payload.get("check_index")
    if turn_index is None or check_index is None:
        # Unidentified records cannot be told apart from each other. They share
        # a slot — the same one the metrics collector shares — so the two never
        # disagree, and the gap says the count is a floor rather than a fact.
        run.gaps.append(
            Observation(
                GAP_MISSING_IDENTITY,
                {
                    "record": events.VERIFICATION_CHECK_FINISHED_EVENT,
                    "field": "turn_index" if turn_index is None else "check_index",
                },
            )
        )
    slot = run.checks.get((_hashable(turn_index), _hashable(check_index)))
    if slot is None:
        run.checks[(_hashable(turn_index), _hashable(check_index))] = _CheckEvidence(
            duration_seconds=duration, finish_count=1
        )
        return
    slot.finish_count += 1
    previous = slot.duration_seconds
    agrees = previous is not None and abs(previous - duration) <= run.tolerance_seconds
    run.anomalies.append(
        Observation(
            ANOMALY_DUPLICATE_EVIDENCE if agrees else ANOMALY_CONFLICTING_EVIDENCE,
            {
                "source": events.VERIFICATION_CHECK_FINISHED_EVENT,
                "field": "duration_seconds",
                "turn_index": turn_index,
                "check_index": check_index,
                "values": [previous, duration],
                "count": slot.finish_count,
            },
        )
    )


def _ingest_event(
    run: _RunEvidence,
    name: str,
    payload: Mapping[str, Any],
    created_at: datetime | None,
) -> None:
    if name == events.RUN_STARTED_EVENT:
        run.run_started = payload
        return
    if name == events.RUN_FINISHED_EVENT:
        run.run_finished_count += 1
        if run.run_finished is None:
            run.run_finished = payload
            run.run_finished_created_at = created_at
        return
    if name == TURN_STARTED_EVENT:
        attempt = _attempt_for(run, payload, TURN_STARTED_EVENT)
        attempt.started = True
        if SOURCE_TURN_STARTED not in attempt.sources:
            attempt.sources.append(SOURCE_TURN_STARTED)
        session_id = _string_or_none(payload.get("session_id")) or _string_or_none(
            payload.get("session")
        )
        if session_id is not None:
            attempt.session_ids.append((SOURCE_TURN_STARTED, session_id))
        return
    if name == events.TURN_EXECUTION_FINISHED_EVENT:
        attempt = _attempt_for(run, payload, SOURCE_EXECUTION_FINISHED)
        if attempt.executed:
            run.anomalies.append(
                Observation(
                    ANOMALY_DUPLICATE_EVIDENCE,
                    {
                        "source": SOURCE_EXECUTION_FINISHED,
                        "turn_index": attempt.turn_index,
                    },
                )
            )
        attempt.executed = True
        _record_timing(run, attempt, SOURCE_EXECUTION_FINISHED, payload)
        return
    if name == events.TURN_FINISHED_EVENT:
        attempt = _attempt_for(run, payload, SOURCE_TURN_FINISHED)
        attempt.accepted = True
        _record_timing(run, attempt, SOURCE_TURN_FINISHED, payload)
        return
    if name == events.TURN_REJECTED_EVENT:
        attempt = _attempt_for(run, payload, SOURCE_TURN_REJECTED)
        attempt.rejected = True
        if SOURCE_TURN_REJECTED not in attempt.sources:
            attempt.sources.append(SOURCE_TURN_REJECTED)
        session_id = _string_or_none(payload.get("session_id")) or _string_or_none(
            payload.get("session")
        )
        if session_id is not None:
            attempt.session_ids.append((SOURCE_TURN_REJECTED, session_id))
        return
    if name == events.VERIFICATION_STARTED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_VERIFICATION,
            payload.get("turn_index"),
            events.VERIFICATION_STARTED_EVENT,
        )
        slot.start_seen = True
        slot.started_at = _parse_timestamp(payload.get("started_at"))
        return
    if name == events.VERIFICATION_FINISHED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_VERIFICATION,
            payload.get("turn_index"),
            events.VERIFICATION_FINISHED_EVENT,
        )
        _record_phase_duration(run, slot, payload)
        return
    if name == events.VERIFICATION_CHECK_FINISHED_EVENT:
        _record_check(run, payload)
        return
    if name == events.COMMIT_STARTED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_COMMITS,
            _commit_discriminator(payload),
            events.COMMIT_STARTED_EVENT,
        )
        slot.start_seen = True
        slot.started_at = _parse_timestamp(payload.get("started_at"))
        return
    if name == events.COMMIT_FINISHED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_COMMITS,
            _commit_discriminator(payload),
            events.COMMIT_FINISHED_EVENT,
        )
        _record_phase_duration(run, slot, payload)
        return
    if name == events.FINALIZATION_STARTED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_ASSURANCE,
            payload.get("phase"),
            events.FINALIZATION_STARTED_EVENT,
        )
        slot.start_seen = True
        slot.started_at = _parse_timestamp(payload.get("started_at"))
        return
    if name == events.FINALIZATION_FINISHED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_ASSURANCE,
            payload.get("phase"),
            events.FINALIZATION_FINISHED_EVENT,
        )
        _record_phase_duration(run, slot, payload)
        return


def _commit_discriminator(payload: Mapping[str, Any]) -> Any:
    index = payload.get("commit_index")
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    return (index, _string_or_none(payload.get("repository_key")))


def _attempt_for(
    run: _RunEvidence, payload: Mapping[str, Any], source: str
) -> _AttemptEvidence:
    key = _attempt_key(run, payload.get("turn_index"), source)
    attempt = run.attempts.get(key)
    if attempt is None:
        turn_index = payload.get("turn_index")
        attempt = _AttemptEvidence(
            plan_key=run.plan_key,
            run_id=run.run_id,
            turn_index=(
                turn_index
                if isinstance(turn_index, int) and not isinstance(turn_index, bool)
                else None
            ),
        )
        run.attempts[key] = attempt
    return attempt


__all__ = (
    "_AttemptEvidence",
    "_PhaseEvidence",
    "_CheckEvidence",
    "_RunEvidence",
    "_string_or_none",
    "_hashable",
    "_attempt_key",
    "_record_timing",
    "_phase_key",
    "_phase_slot",
    "_record_phase_duration",
    "_record_check",
    "_ingest_event",
    "_commit_discriminator",
    "_attempt_for",
)
