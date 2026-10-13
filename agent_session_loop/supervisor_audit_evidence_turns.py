"""Turn evidence: rejection and deferral marking, sections, supersessions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import events, state
from .supervisor_audit_evidence_collector import _Collector
from .supervisor_audit_evidence_constants import REASON_MALFORMED
from .supervisor_audit_evidence_failures import _turn_shows_failure
from .supervisor_audit_evidence_helpers import _mapping, _read_json_file, _sequence


def _mark_rejected_turns(
    turns: list[dict[str, Any]], event_records: Sequence[Mapping[str, Any]]
) -> None:
    """Mark turns a post-execution guard refused after they had already run.

    A refusal is recorded in the event stream, not on the turn result the
    harness left behind, so the turn keeps claiming the clean exit it made
    before the guard reverted it. Correlating by turn index is what stops a
    refused turn from being projected as healthy.
    """
    rejected: set[int] = set()
    for record in event_records:
        if record.get("name") != events.TURN_REJECTED_EVENT:
            continue
        index = _mapping(record.get("payload")).get("turn_index")
        if isinstance(index, int):
            rejected.add(index)
    if not rejected:
        return
    for turn in turns:
        index = _mapping(turn.get("result")).get("turn_index")
        if isinstance(index, int) and index in rejected:
            turn["anomalous"] = True
            turn["rejected"] = True


def _unredeemed_deferral_turn_indexes(
    state_payload: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[int, list[str]]:
    """Turn indexes whose accepted deferral the run then declared unredeemed.

    Read from the pause the run actually took rather than re-derived: the
    ``required-checks-incomplete`` blocker already resolved every deferral
    against the review cycles that could have closed it, and it names the ones
    that reached no all-clear. Entries carrying another run's id are skipped,
    because this bundle's turn indexes mean nothing in that run's numbering.
    """
    sources: list[Mapping[str, Any]] = []
    status_details = _mapping(state_payload.get("pause_details"))
    if state_payload.get("pause_reason") == (
        events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE
    ):
        sources.append(status_details)
    for record in event_records:
        if record.get("name") != events.RUN_PAUSED_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("reason") == events.PAUSE_REASON_REQUIRED_CHECKS_INCOMPLETE:
            sources.append(_mapping(payload.get("details")))
    indexes: dict[int, list[str]] = {}
    for details in sources:
        for item in _sequence(details.get("deferred_unresolved")):
            entry = _mapping(item)
            entry_run = entry.get("run_id")
            if isinstance(entry_run, str) and entry_run and entry_run != run_id:
                continue
            index = entry.get("turn_index")
            if not isinstance(index, int):
                continue
            checks = [
                check
                for check in _sequence(entry.get("checks"))
                if isinstance(check, str)
            ]
            indexes.setdefault(index, [])
            for check in checks:
                if check not in indexes[index]:
                    indexes[index].append(check)
    return indexes


def _mark_unredeemed_deferral_turns(
    turns: list[dict[str, Any]],
    state_payload: Mapping[str, Any],
    event_records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> None:
    """Mark turns whose deferred broad-check failures were never redeemed.

    A findings review may finish with the plan's own gates failing, because
    those failures are the repair specification. What the deferral buys is a
    delay, redeemed only by a later all-clear review -- so an accepted deferral
    exempts the turn from :func:`_turn_shows_failure` on the promise that the
    redemption is still coming. When the run then pauses as
    ``required-checks-incomplete`` naming those same checks, the promise is
    broken and the exemption outlived its premise: the turn was projected
    healthy while the run it belongs to was refusing to report success for
    exactly its verification (ASI-15). Marked here rather than inside
    ``_turn_shows_failure`` for the same reason a guard refusal is: the fact
    that settles it is recorded after the turn, not on its result.
    """
    indexes = _unredeemed_deferral_turn_indexes(
        state_payload, event_records, run_id=run_id
    )
    if not indexes:
        return
    for turn in turns:
        result = _mapping(turn.get("result"))
        index = result.get("turn_index")
        if not isinstance(index, int) or index not in indexes:
            continue
        if _mapping(result.get("verification")).get("passed") is not False:
            continue
        turn["anomalous"] = True
        turn["deferral_unredeemed"] = True
        if indexes[index]:
            turn["unredeemed_checks"] = list(indexes[index])


def _turn_directories(run_dir: Path) -> list[Path]:
    try:
        entries = [
            entry
            for entry in run_dir.iterdir()
            if entry.is_dir() and entry.name.startswith(state.TURN_DIR_PREFIX)
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda entry: entry.name)


def _reference_verification_stderr(
    collector: _Collector, result: Mapping[str, Any]
) -> None:
    """Index the stderr artifact each failed verification check points at.

    That artifact is where a rejected gate's full diagnostic survives: the check
    record's own copy is a preview the content rules withhold, and its inline
    detail is bounded. Naming the path without indexing it left the auditor
    unable to tell evidence it could read from a path it merely saw.
    """
    for check in _sequence(_mapping(result.get("verification")).get("checks")):
        entry = _mapping(check)
        if entry.get("status") == "pass":
            continue
        path = entry.get("stderr_artifact_path")
        if isinstance(path, str) and path:
            collector.reference(
                path, kind="artifact", detail=f"{entry.get('name')} stderr"
            )


def _turn_section(collector: _Collector, directory: Path) -> dict[str, Any]:
    """One turn's packet, prompt, harness metadata, and bounded streams."""
    result, reason = _read_json_file(directory / state.RESULT_FILE_NAME)
    turn: dict[str, Any] = {
        "directory": collector.reference(directory, kind="artifact"),
        "name": directory.name,
    }
    if isinstance(result, Mapping):
        _reference_verification_stderr(collector, result)
        turn["result"] = collector.redact(result)
        turn["anomalous"] = _turn_shows_failure(result)
        # Beside ``anomalous``, never folded into it: a harness that lost an
        # auxiliary transport and still produced a usable turn is degraded, and
        # counting that as an anomaly would contradict every successful run.
        turn["degraded"] = bool(result.get("harness_degradation"))
    else:
        collector.omissions.record("turns", reason or REASON_MALFORMED, str(directory))
        turn["result"] = {"available": False, "reason": reason or REASON_MALFORMED}
        turn["anomalous"] = True
        turn["degraded"] = False
    turn["logs"] = {
        "stdout": collector.file_preview(directory / "stdout.log", kind="harness"),
        "stderr": collector.file_preview(directory / "stderr.log", kind="harness"),
    }
    turn["last_message"] = collector.file_preview(
        directory / "last-message.txt", kind="harness"
    )
    return turn


def _verification_section(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Check-level outcomes across the run, without any command output."""
    sessions = []
    for turn in turns:
        result = turn.get("result")
        if not isinstance(result, Mapping):
            continue
        session_verification = result.get("verification")
        if not isinstance(session_verification, Mapping):
            continue
        sessions.append(
            {
                "turn": turn.get("name"),
                "session_id": result.get("session_id"),
                "verification": session_verification,
            }
        )
    return {"sessions": sessions, "session_count": len(sessions)}


#: Turn key naming the later clean completion that replaced a turn, stamped
#: at collection time so supersession survives the retry being shed.
TURN_SUPERSEDED_BY_KEY = "superseded_by"


def _stamp_supersessions(turns: Sequence[dict[str, Any]]) -> None:
    """Record on each superseded turn the name of the retry that replaced it.

    Supersession is judged on the full turn list at collection time because
    the size cap protects anomalous turns but not the clean retries that
    supersede them: once a retry is shed, the bundle alone would read the
    recovered attempt as unanswered counter-evidence (ASI-23).
    """
    for name, replacement in _supersessions(turns).items():
        replacement_name = replacement.get("name")
        if not isinstance(replacement_name, str):
            continue
        for turn in turns:
            if turn.get("name") == name:
                turn[TURN_SUPERSEDED_BY_KEY] = replacement_name


def _supersessions(
    turns: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Map each superseded turn's name to the clean completion that replaced it."""

    completed: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for turn in turns:
        if turn.get("anomalous") is True:
            continue
        result = _mapping(turn.get("result"))
        session_id = result.get("session_id")
        index = result.get("turn_index")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        if result.get("status_result") != "complete":
            continue
        latest = completed.get(session_id)
        if latest is None or index >= latest[0]:
            completed[session_id] = (index, turn)
    superseded: dict[str, Mapping[str, Any]] = {}
    for turn in turns:
        result = _mapping(turn.get("result"))
        session_id = result.get("session_id")
        index = result.get("turn_index")
        name = turn.get("name")
        if not isinstance(session_id, str) or not isinstance(index, int):
            continue
        if not isinstance(name, str):
            continue
        latest = completed.get(session_id)
        if latest is not None and latest[0] > index:
            superseded[name] = latest[1]
    return superseded
