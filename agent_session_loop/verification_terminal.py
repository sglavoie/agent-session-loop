# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F821
"""Event-log verification history and terminal run adjudication."""

from __future__ import annotations

from . import verification_repair as _repair
from . import verification_schedule as _schedule

globals().update(
    {
        name: value
        for name, value in vars(_schedule).items()
        if not name.startswith("__")
    }
)
globals().update(
    {name: value for name, value in vars(_repair).items() if not name.startswith("__")}
)
del _repair, _schedule


def read_event_records(event_path: str | Path) -> list[dict[str, Any]]:
    """Every well-formed JSON object in an append-only event log, oldest first.

    Malformed lines are skipped the way every other reader of this log skips
    them: a log a previous build wrote, or one truncated by a crash, still has to
    yield the records it does hold.
    """

    try:
        # Bytes, decoded line by line below: a stray non-UTF-8 byte then
        # spoils only its own line, the way a torn line does, not the file.
        data = Path(event_path).read_bytes()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError, RecursionError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def read_verification_finished_events(
    event_path: str | Path,
) -> list[dict[str, Any]]:
    """Return every ``verification_finished`` payload this plan recorded."""

    return [
        record["payload"]
        for record in read_event_records(event_path)
        if record.get("name") == events.VERIFICATION_FINISHED_EVENT
        and isinstance(record.get("payload"), dict)
    ]


def accepted_broad_failure_deferral(
    verification_payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return a structurally valid broad-failure deferral, else ``None``.

    Validated rather than trusted even though this process wrote it: a run
    resumed over an older event log reads acceptances a previous build
    produced, and an acceptance whose shape cannot be checked is not evidence
    that the failures under it were ever owed to a repair loop.
    """

    acceptance = verification_payload.get("acceptance")
    if not isinstance(acceptance, Mapping):
        return None
    names = acceptance.get("deferred_failure_names")
    if not isinstance(names, (list, tuple)) or not names:
        return None
    prefix = f"{BROAD_REVIEW_CHECK_PREFIX}:"
    if any(not isinstance(name, str) or not name.startswith(prefix) for name in names):
        return None
    if not (
        acceptance.get("accepted") is True
        and acceptance.get("reason") == REVIEW_BROAD_FAILURES_DEFERRED_REASON
        and acceptance.get("review_outcome") == review.REVIEW_OUTCOME_FINDINGS
        and acceptance.get("deferred_failure_count") == len(names)
    ):
        return None
    return acceptance


def recorded_review_cycles(
    payload: Mapping[str, Any], event_path: Path
) -> list[dict[str, Any]]:
    """Every review cycle this plan has recorded, oldest first.

    The append-only event log is authoritative because it spans runs: a
    deferral opened by one run is discharged by the all-clear a *later* run
    records, and reading only this run's projection would call that deferral
    unearned forever. The live projection is the fallback for a run whose
    events are not being persisted (a dry run, a reply-only invocation).
    """

    try:
        cycles = review_continuation.read_prior_review_cycles(event_path)
    except OSError:
        cycles = []
    if cycles:
        return cycles
    projected = payload.get("review_cycles")
    if not isinstance(projected, list):
        return []
    return [dict(item) for item in projected if isinstance(item, Mapping)]


def unresolved_deferred_verifications(
    payload: Mapping[str, Any], event_path: Path
) -> list[dict[str, Any]]:
    """Broad-check failures a review deferred and no later review closed.

    A findings review is allowed to finish with the plan's own gates still
    failing: those failures *are* the repair specification the findings
    describe. What the deferral buys is a delay, redeemed only by a later
    all-clear review of the repaired tree. This finds the deferrals that were
    never redeemed, which is the one way a run can reach a terminal ending with
    a recorded required-check failure and no pause naming it.

    Deferrals are read from the event log rather than from this run's turns, so
    a run resumed after the deferral was opened re-evaluates the same blocker
    instead of forgetting it.
    """

    cycles = recorded_review_cycles(payload, event_path)
    unresolved: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for record in read_verification_finished_events(event_path):
        acceptance = accepted_broad_failure_deferral(record)
        if acceptance is None or record.get("passed") is not False:
            continue
        session_id = str(record.get("session_id") or record.get("session") or "")
        run_id = str(record.get("run_id") or "")
        if (run_id, session_id) in seen:
            continue
        seen.add((run_id, session_id))
        if review_continuation.deferred_review_is_cleared(
            cycles, review_session_id=session_id
        ):
            continue
        unresolved.append(
            {
                "run_id": run_id or None,
                "session_id": session_id or None,
                "turn_index": record.get("turn_index"),
                "checks": list(acceptance.get("deferred_failure_names") or ()),
                "review_outcome": acceptance.get("review_outcome"),
            }
        )
    return unresolved


def unpassed_run_verifications(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """This run's turn verifications that neither passed nor were deferred.

    Every such failure already pauses the run where it happens, so this is the
    backstop rather than the detector: it exists so that a future path which
    forgets to pause still cannot exit reporting success.
    """

    turns = payload.get("turns")
    if not isinstance(turns, list):
        return []
    unpassed: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        verification_payload = turn.get("verification")
        if not isinstance(verification_payload, Mapping):
            continue
        if verification_payload.get("passed") is not False:
            continue
        if accepted_broad_failure_deferral(verification_payload) is not None:
            continue
        unpassed.append(
            {
                "session_id": verification_payload.get("session")
                or turn.get("session_id"),
                "turn_index": turn.get("turn_index"),
                "reason": verification_payload.get("reason"),
            }
        )
    return unpassed


def final_review_is_clear(payload: Mapping[str, Any]) -> bool:
    """Whether the run holds a review verdict that closed every finding.

    The single definition of a cleared review. Both the terminal blocker below
    and the review lane's own terminal reason read it, so a run cannot report a
    clear review to one and an unresolved one to the other.
    """

    verdict = payload.get("review_verdict")
    verdict = verdict if isinstance(verdict, Mapping) else {}
    return (
        payload.get("review_fix_disposition")
        == review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
        and verdict.get("outcome") == review.REVIEW_OUTCOME_ALL_CLEAR
        and verdict.get("total_findings") == 0
    )


@dataclass(frozen=True)
class TerminalBlocker:
    """One reason a terminating run may not report the success it reached."""

    reason: str
    message: str
    details: dict[str, Any]


def terminal_blocker(
    payload: Mapping[str, Any], event_path: Path
) -> TerminalBlocker | None:
    """The first thing a terminating run cannot show it earned.

    Required checks are asked about first because they are the broader claim:
    a run whose gates never passed is not made honest by a clear review. Only
    that first clause can fire on a run that is already failing — the review
    clause is scoped to the endings that assert the plan is finished, and a run
    reporting unresolved findings asserts the opposite.
    """

    unresolved = unresolved_deferred_verifications(payload, event_path)
    unpassed = unpassed_run_verifications(payload)
    if unresolved or unpassed:
        deferred_checks = sorted(
            {check for entry in unresolved for check in entry["checks"]}
        )
        summary = ", ".join(deferred_checks) or ", ".join(
            str(entry["session_id"]) for entry in unpassed
        )
        return TerminalBlocker(
            reason=REQUIRED_CHECKS_INCOMPLETE_REASON,
            message=(
                "the run cannot report success: required verification did not "
                f"reach passed for every session ({summary})"
            ),
            details={
                "deferred_unresolved": unresolved,
                "unpassed_verifications": unpassed,
            },
        )
    if (
        payload.get("review_requested")
        and str(payload.get("reason") or "") in PLAN_COMPLETION_REASONS
        and not final_review_is_clear(payload)
    ):
        verdict = payload.get("review_verdict")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        return TerminalBlocker(
            reason=REVIEW_NOT_CLEAR_REASON,
            message=(
                "the run cannot report the plan complete: its final review is "
                "missing or did not come back all-clear"
            ),
            details={
                "review_completed": payload.get("review_completed"),
                "review_fix_disposition": payload.get("review_fix_disposition"),
                "review_outcome": verdict.get("outcome"),
                "unresolved_findings": verdict.get("total_findings"),
                "findings_by_severity": verdict.get("findings_by_severity"),
                "review_skipped_reason": payload.get("review_skipped_reason"),
            },
        )
    return None
