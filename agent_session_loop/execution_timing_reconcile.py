from .execution_timing_ingest import *
from .execution_timing_models import *


def _reconcile_attempt(run: _RunEvidence, attempt: _AttemptEvidence) -> TurnAttempt:
    tolerance = run.tolerance_seconds
    chosen: tuple[str, datetime | None, datetime | None, float | None] | None = None
    for source in TIMING_SOURCE_PRECEDENCE:
        for record in attempt.timing:
            if record[0] == source and record[3] is not None:
                chosen = record
                break
        if chosen is not None:
            break
    if chosen is None:
        for source in TIMING_SOURCE_PRECEDENCE:
            for record in attempt.timing:
                if record[0] != source:
                    continue
                if record[1] is not None and record[2] is not None:
                    chosen = record
                    break
            if chosen is not None:
                break

    started_at = chosen[1] if chosen is not None else None
    finished_at = chosen[2] if chosen is not None else None
    duration = chosen[3] if chosen is not None else None
    duration_source = chosen[0] if chosen is not None else None
    if duration is None and started_at is not None and finished_at is not None:
        duration = (finished_at - started_at).total_seconds()
    if (
        chosen is not None
        and chosen[3] is not None
        and started_at is not None
        and finished_at is not None
    ):
        spanned = (finished_at - started_at).total_seconds()
        if abs(spanned - chosen[3]) > tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_CONTRADICTORY_DURATION,
                    {
                        "source": chosen[0],
                        "turn_index": attempt.turn_index,
                        "duration_seconds": chosen[3],
                        "timestamp_span_seconds": _round(spanned),
                    },
                )
            )
    if duration is not None and duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": duration_source,
                    "field": "duration_seconds",
                    "value": _round(duration),
                    "reason": "negative-span",
                    "turn_index": attempt.turn_index,
                },
            )
        )

    for record in attempt.timing:
        if chosen is None or record is chosen:
            continue
        if record[3] is None or chosen[3] is None:
            continue
        if abs(record[3] - chosen[3]) > tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_CONFLICTING_EVIDENCE,
                    {
                        "field": "duration_seconds",
                        "turn_index": attempt.turn_index,
                        "preferred": {"source": chosen[0], "value": chosen[3]},
                        "conflicting": {"source": record[0], "value": record[3]},
                    },
                )
            )

    session_id = attempt.session_ids[0][1] if attempt.session_ids else None
    distinct_ids = {value for _, value in attempt.session_ids}
    if len(distinct_ids) > 1:
        run.anomalies.append(
            Observation(
                ANOMALY_CONFLICTING_EVIDENCE,
                {
                    "field": "session_id",
                    "turn_index": attempt.turn_index,
                    "values": sorted(distinct_ids),
                },
            )
        )
    session_kind: str | None = None
    for source in (SOURCE_TURN_FINISHED, SOURCE_RESULT_FILE, SOURCE_EXECUTION_FINISHED):
        for record_source, value in attempt.session_kinds:
            if record_source == source:
                session_kind = value
                break
        if session_kind is not None:
            break
    distinct_kinds = {value for _, value in attempt.session_kinds}
    if len(distinct_kinds) > 1:
        run.anomalies.append(
            Observation(
                ANOMALY_CONFLICTING_EVIDENCE,
                {
                    "field": "session_kind",
                    "turn_index": attempt.turn_index,
                    "values": sorted(distinct_kinds),
                },
            )
        )
    if session_kind is None:
        run.gaps.append(
            Observation(GAP_TURN_KIND_UNKNOWN, {"turn_index": attempt.turn_index})
        )

    if attempt.accepted:
        outcome = OUTCOME_ACCEPTED
    elif attempt.rejected:
        outcome = OUTCOME_REJECTED
    elif attempt.executed:
        outcome = OUTCOME_EXECUTED
    else:
        outcome = OUTCOME_STARTED

    if duration is None:
        if outcome == OUTCOME_STARTED:
            run.gaps.append(
                Observation(GAP_UNMATCHED_START, {"turn_index": attempt.turn_index})
            )
        else:
            run.gaps.append(
                Observation(
                    GAP_TURN_TIMING_UNKNOWN,
                    {"turn_index": attempt.turn_index, "outcome": outcome},
                )
            )
    elif outcome == OUTCOME_STARTED:
        run.gaps.append(
            Observation(GAP_UNMATCHED_START, {"turn_index": attempt.turn_index})
        )

    return TurnAttempt(
        plan_key=attempt.plan_key,
        run_id=attempt.run_id,
        turn_index=attempt.turn_index,
        session_id=session_id,
        session_kind=session_kind,
        outcome=outcome,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=None if duration is None else _round(duration),
        duration_source=duration_source,
        sources=tuple(attempt.sources),
    )


def _attempt_order(attempt: _AttemptEvidence) -> tuple[int, int]:
    """Order attempts by turn index, with unidentified ones last."""
    if attempt.turn_index is None:
        return (1, 0)
    return (0, attempt.turn_index)


def _phase_interval(
    slot: _PhaseEvidence,
) -> tuple[datetime, datetime] | None:
    if slot.finished_at is None:
        return None
    if slot.started_at is not None:
        return (slot.started_at, slot.finished_at)
    if slot.duration_seconds is None:
        return None
    inferred_start = slot.finished_at - timedelta(seconds=slot.duration_seconds)
    return (inferred_start, slot.finished_at)


def _detect_overlaps(
    run: _RunEvidence, intervals: Sequence[tuple[str, datetime, datetime]]
) -> None:
    """Flag phases that claim the same wall-clock seconds.

    A run executes one turn at a time, verifies after the harness exits, and
    commits after that, so two run-level phases sharing a second means at least
    one of their records is wrong — and their durations would then be counted
    twice against the same wall span.
    """
    tolerance = run.tolerance_seconds
    ordered = sorted(intervals, key=lambda item: (item[1], item[2]))
    # Sweep against the furthest end seen so far rather than the previous
    # interval, so a long phase that swallows several short ones is caught
    # against every one of them and not just its immediate neighbour.
    covering: tuple[str, datetime, datetime] | None = None
    for current in ordered:
        if covering is not None:
            overlap = (covering[2] - current[1]).total_seconds()
            if overlap > tolerance:
                run.anomalies.append(
                    Observation(
                        ANOMALY_OVERLAPPING_PHASES,
                        {
                            "phases": [covering[0], current[0]],
                            "overlap_seconds": _round(overlap),
                        },
                    )
                )
        if covering is None or current[2] > covering[2]:
            covering = current


def _run_timing(
    run: _RunEvidence,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
) -> RunTiming:
    tolerance = run.tolerance_seconds
    attempts = tuple(
        _reconcile_attempt(run, attempt)
        for attempt in sorted(run.attempts.values(), key=_attempt_order)
    )

    phase_seconds = dict.fromkeys(ATTRIBUTED_PHASES, 0.0)
    intervals: list[tuple[str, datetime, datetime]] = []
    for attempt in attempts:
        if attempt.duration_seconds is not None:
            phase_seconds[attempt.phase] += attempt.duration_seconds
        if attempt.started_at is not None and attempt.finished_at is not None:
            intervals.append(
                (f"turn-{attempt.turn_index}", attempt.started_at, attempt.finished_at)
            )

    for key, slot in run.phases.items():
        interval = _phase_interval(slot)
        if slot.finish_seen and slot.duration_seconds is not None:
            phase_seconds[slot.phase] += slot.duration_seconds
            if interval is not None:
                spanned_phase = (interval[1] - interval[0]).total_seconds()
                if abs(spanned_phase - slot.duration_seconds) > tolerance:
                    run.anomalies.append(
                        Observation(
                            ANOMALY_CONTRADICTORY_DURATION,
                            {
                                "phase": slot.phase,
                                "source": slot.label,
                                "duration_seconds": slot.duration_seconds,
                                "timestamp_span_seconds": _round(spanned_phase),
                            },
                        )
                    )
        elif slot.finish_seen and interval is not None:
            phase_seconds[slot.phase] += (interval[1] - interval[0]).total_seconds()
        elif slot.finish_seen:
            # The phase ran — both of its records exist — but nothing states how
            # long it took: no usable duration and not enough timestamps to span
            # one. Contributing nothing would file those seconds under
            # ``unattributed`` and still call the run complete, which asserts a
            # zero-length phase the evidence never claimed. The seconds this run
            # *does* know stay attributed; the run itself leaves the share basis.
            run.gaps.append(
                Observation(
                    GAP_PHASE_TIMING_UNKNOWN, {"phase": slot.phase, "key": str(key)}
                )
            )
        if slot.start_seen and not slot.finish_seen:
            run.gaps.append(
                Observation(GAP_UNMATCHED_START, {"phase": slot.phase, "key": str(key)})
            )
        if slot.finish_seen and not slot.start_seen:
            run.anomalies.append(
                Observation(
                    ANOMALY_UNMATCHED_FINISH, {"phase": slot.phase, "key": str(key)}
                )
            )
        if interval is not None:
            intervals.append((f"{slot.phase}:{key}", interval[0], interval[1]))

    _detect_overlaps(run, intervals)

    finished = run.run_finished
    started_at: datetime | None = None
    finished_at: datetime | None = None
    wall_seconds: float | None = None
    outcome: str | None = None
    paused: bool | None = None
    if finished is not None:
        started_at = _parse_timestamp(finished.get("run_started_at"))
        finished_at = (
            _parse_timestamp(finished.get("run_finished_at"))
            or run.run_finished_created_at
        )
        outcome = _string_or_none(finished.get("outcome")) or _string_or_none(
            finished.get("reason")
        )
        raw_paused = finished.get("paused")
        paused = raw_paused if isinstance(raw_paused, bool) else None
        raw_wall = finished.get("duration_seconds")
        wall_seconds = _finite_seconds(raw_wall)
        if raw_wall is not None and wall_seconds is None:
            run.anomalies.append(
                Observation(
                    ANOMALY_INVALID_NUMBER,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "field": "duration_seconds",
                        "value": repr(raw_wall),
                    },
                )
            )
        if wall_seconds is not None and wall_seconds < 0:
            run.anomalies.append(
                Observation(
                    ANOMALY_INVALID_NUMBER,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "field": "duration_seconds",
                        "value": wall_seconds,
                        "reason": "negative",
                    },
                )
            )
            wall_seconds = None
        spanned: float | None = None
        if started_at is not None and finished_at is not None:
            spanned = (finished_at - started_at).total_seconds()
        if wall_seconds is None:
            wall_seconds = spanned
        elif spanned is not None and abs(spanned - wall_seconds) > tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_CONTRADICTORY_DURATION,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "duration_seconds": wall_seconds,
                        "timestamp_span_seconds": _round(spanned),
                    },
                )
            )
        if run.run_finished_count > 1:
            run.anomalies.append(
                Observation(
                    ANOMALY_DUPLICATE_EVIDENCE,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "count": run.run_finished_count,
                    },
                )
            )
        if (
            finished.get("synthetic") is True
            or finished.get("outcome") == events.RUN_OUTCOME_ABANDONED
        ):
            # The terminal record was reconstructed from proof of death, so its
            # finish time is when the corpse was found rather than when the run
            # stopped working. The phases it did record are still real.
            run.gaps.append(
                Observation(
                    GAP_RECONSTRUCTED_TERMINAL,
                    {"outcome": _string_or_none(finished.get("outcome"))},
                )
            )
        if wall_seconds is None:
            run.gaps.append(Observation(GAP_WALL_DURATION_UNKNOWN, {}))

    dry_run: bool | None = None
    if run.run_started is None:
        run.gaps.append(Observation(GAP_RUN_START_ABSENT, {}))
    else:
        raw_dry = run.run_started.get("dry_run")
        dry_run = raw_dry if isinstance(raw_dry, bool) else None
        if started_at is None:
            started_at = _parse_timestamp(run.run_started.get("run_started_at"))

    # One slot per check execution, whatever the number of records that stated
    # it, so a replayed event never counts as extra work.
    check_seconds = sum(
        (
            slot.duration_seconds
            for slot in run.checks.values()
            if slot.duration_seconds is not None
        ),
        0.0,
    )

    attributed = sum(phase_seconds.values())
    unattributed: float | None = None
    if wall_seconds is not None:
        remainder = wall_seconds - attributed
        if remainder < -tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_ATTRIBUTED_EXCEEDS_WALL,
                    {
                        "attributed_seconds": _round(attributed),
                        "wall_seconds": _round(wall_seconds),
                        "excess_seconds": _round(-remainder),
                    },
                )
            )
            unattributed = _round(remainder)
        else:
            unattributed = _round(max(0.0, remainder))

    if finished is None:
        classification = CLASSIFICATION_ACTIVE
    elif run.anomalies:
        classification = CLASSIFICATION_INCONSISTENT
    elif run.gaps:
        classification = CLASSIFICATION_PARTIAL
    else:
        classification = CLASSIFICATION_COMPLETE

    included = True
    excluded_reason: str | None = None
    if finished is None:
        included = False
        excluded_reason = EXCLUDED_ACTIVE
    elif dry_run is True:
        included = False
        excluded_reason = EXCLUDED_DRY_RUN
    elif finished_at is None:
        included = False
        excluded_reason = EXCLUDED_NO_FINISH_TIME
    elif (window_start is not None and finished_at < window_start) or (
        window_end is not None and finished_at >= window_end
    ):
        included = False
        excluded_reason = EXCLUDED_OUTSIDE_WINDOW

    return RunTiming(
        plan_key=run.plan_key,
        run_id=run.run_id,
        classification=classification,
        included=included,
        excluded_reason=excluded_reason,
        outcome=outcome,
        paused=paused,
        dry_run=dry_run,
        started_at=started_at,
        finished_at=finished_at,
        wall_seconds=None if wall_seconds is None else _round(wall_seconds),
        phase_seconds={
            phase: _round(seconds) for phase, seconds in phase_seconds.items()
        },
        attributed_seconds=_round(attributed),
        unattributed_seconds=unattributed,
        verification_check_seconds=_round(check_seconds),
        verification_check_count=len(run.checks),
        attempts=attempts,
        anomalies=tuple(run.anomalies),
        gaps=tuple(run.gaps),
    )
