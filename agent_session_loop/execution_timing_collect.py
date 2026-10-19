from .execution_timing_ingest import *
from .execution_timing_models import *
from .execution_timing_reconcile import _run_timing


def collect_execution_timing(
    evidence: Sequence[RunEvidence],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
) -> ExecutionTimingReport:
    """Reconcile supplied evidence into per-run phase accounting.

    ``window_start`` is inclusive and ``window_end`` exclusive, both compared
    against a run's *finish* time, and either may be ``None`` for an unbounded
    edge. A run straddling the opening edge is included whole, because its
    duration is a property of the run rather than of the window.

    Records the collector cannot attribute to a run — no ``run_id``, or a
    payload that is not a mapping — are counted rather than dropped silently,
    so a caller can see that evidence went unread.
    """
    tolerance = float(tolerance_seconds)
    accumulated: dict[tuple[str, str], _RunEvidence] = {}
    unattributable_records = 0
    unattributable_turn_results = 0

    def slot(plan_key: str, run_id: str) -> _RunEvidence:
        key = (plan_key, run_id)
        found = accumulated.get(key)
        if found is None:
            found = _RunEvidence(
                plan_key=plan_key, run_id=run_id, tolerance_seconds=tolerance
            )
            accumulated[key] = found
        return found

    for supplied in evidence:
        for record in supplied.event_records:
            if not isinstance(record, Mapping):
                unattributable_records += 1
                continue
            name = _string_or_none(record.get("name"))
            payload = record.get("payload")
            if name is None or not isinstance(payload, Mapping):
                unattributable_records += 1
                continue
            run_id = _string_or_none(payload.get("run_id"))
            if run_id is None:
                unattributable_records += 1
                continue
            _ingest_event(
                slot(supplied.plan_key, run_id),
                name,
                payload,
                _parse_timestamp(record.get("created_at")),
            )
        for payload in supplied.turn_results:
            if not isinstance(payload, Mapping):
                unattributable_turn_results += 1
                continue
            run_id = _string_or_none(payload.get("run_id"))
            if run_id is None:
                unattributable_turn_results += 1
                continue
            run = slot(supplied.plan_key, run_id)
            attempt = _attempt_for(run, payload, SOURCE_RESULT_FILE)
            _record_timing(run, attempt, SOURCE_RESULT_FILE, payload)

    runs = tuple(
        _run_timing(accumulated[key], window_start=window_start, window_end=window_end)
        for key in sorted(accumulated)
    )
    return ExecutionTimingReport(
        window_start=window_start,
        window_end=window_end,
        tolerance_seconds=tolerance,
        runs=runs,
        unattributable_records=unattributable_records,
        unattributable_turn_results=unattributable_turn_results,
    )
