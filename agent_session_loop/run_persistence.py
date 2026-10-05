"""The run's durable write core: one lock, one state record, one event log.

A run is written to by more threads than the loop that drives it. The main
loop advances sessions, a turn heartbeat thread stamps liveness, a
verification heartbeat thread does the same during a long check, a control
poller records commands it observed, and the AgentMux notification seam
allocates a monotonic sequence for every lifecycle event any of them emits.
All of them write the *same* two durable records — ``state.json`` and the
run's event log — and every one of those writes has to be serialized against
the others or a reader finds a state torn between two transitions. This module
owns that serialization so the loop is left deciding *what* to record rather
than reimplementing *how* a record survives concurrency.

Four things live here:

* **The lock.** :class:`_RunPersistence` is the run's single reentrant mutex
  and the only sanctioned way to write. :func:`_run_persistence` resolves an
  absent handle into a real one so a caller never has to branch on ``None``.
* **The durable records.** :func:`_append_event` is the one event append, and
  :func:`_write_state_payload` is the one state write. Both are reached
  through the lock; the two thin event helpers here
  (:func:`_append_process_sweep_event`,
  :func:`_append_harness_degradation_event`) decide whether a record is worth
  writing at all rather than how to write it.
* **Compaction.** The projection that turns a live run payload into the bytes
  on disk: :func:`_compact_state_payload` and the snapshot, turn-summary, and
  verification-bounding helpers beneath it, plus
  :func:`_run_finished_event_payload` for the terminal event.
* **The AgentMux notify seam.** Every ``_notify_agent_mux_loop*`` entry point
  the loop announces its lifecycle through, the sequence allocation that keeps
  those announcements ordered, and :class:`_AgentMuxWaitNotifier`, which binds
  a coordination wait's lifecycle to this run's projection.

Nothing here imports the runner. Transport, payload shape, and outcome
recording for notifications belong to
:mod:`agent_session_loop.agent_mux_delivery`, which types this module's
persistence handle structurally as
:class:`~agent_session_loop.agent_mux_delivery.StatePersistence`; that is what
lets the turn boundary emit through exactly the same seam the loop does
without either side importing the other.

Four invariants survive the move and are load-bearing:

* **Lock ownership.** ``_RunPersistence.lock`` is the run's reentrant mutex
  and the outermost lock every durable write below takes. It is never held
  across a mailbox read, a lease read, a sleep, or a subprocess: the
  notification transport spawns its process *outside* the lock and only the
  sequence allocation and the state write it implies happen inside it. The
  mutex is reentrant because a write taken under the lock may legitimately
  reach another one — sequence allocation writes state — and must not
  self-deadlock.
* **Compaction bounds what it writes.** A v2 state record stores only compact
  turn summaries and never embeds raw verification output; the full per-turn
  detail lives in ``result.json`` behind ``result_path``. Stripping is
  metadata-preserving — ``*_artifact_path`` / ``*_byte_count`` / ``*_sha256``
  survive — so full diagnostics stay recoverable from the referenced artifact.
* **A record claims only what it contains.** :func:`_compact_state_payload`
  stamps v3 only when an ``agent_mux`` projection is already attached, because
  attaching one is a state transition's job. Stamping v3 on a
  projection-free record would tell a reader to expect a block that is not
  there.
* **Notification is never a run dependency.** Every ``_notify_agent_mux_loop*``
  path is fail-open: delivery errors are contained in
  ``agent_mux_delivery`` and returned as an outcome rather than raised, so no
  announcement can end a run — or end a wait a supervisor is still answering.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import agent_mux_delivery, agent_mux_projection, clock, durability, events

#: Source recorded on the run's terminal AgentMux notification. Identical to
#: the run-wide source today; kept separate so a daemon-side split of the
#: finish record from the rest never has to rename the live constant.
AGENT_MUX_FINISH_SOURCE = agent_mux_delivery.SOURCE

#: How much of a supervisor's question text a wait-start notification carries.
#: The preview is a locator, not the question: the mailbox holds the text.
AGENT_MUX_QUESTION_PREVIEW_CHARS = 160

#: Timeout for the terminal notification specifically. Identical to the
#: run-wide timeout today, and separate for the same reason as the source
#: above: the finish record is the one a run can never retry.
AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS = agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS

#: The version a record that has only been output-bounded may claim.
#: Compaction reaches this and no further; only a state transition or an
#: explicit ``asl migrate-state`` upgrade attaches the projection v3 promises.
COMPACT_STATE_SCHEMA_VERSION = 2

#: Keys carrying raw command output on a verification check payload. These are
#: stripped from every v2 state and event record; the accompanying
#: ``*_artifact_path`` / ``*_byte_count`` / ``*_sha256`` fields survive so full
#: diagnostics remain recoverable from the referenced artifact.
_VERIFICATION_OUTPUT_KEYS = ("stdout", "stderr")


def _strip_check_output(check: Any) -> Any:
    """Drop raw stdout/stderr from a verification check payload.

    Preserves every other field, including the bounded ``*_byte_count`` /
    ``*_sha256`` metadata and ``*_artifact_path`` references, so full
    verification output stays recoverable from the referenced artifact without
    embedding it in a compact state or event record.
    """
    if not isinstance(check, dict):
        return check
    return {
        key: value
        for key, value in check.items()
        if key not in _VERIFICATION_OUTPUT_KEYS
    }


def _bounded_verification_details(details: Any) -> Any:
    """Return ``details`` with raw verification output stripped from checks.

    Used for both persisted pause details and the ``run_paused`` event so a v2
    record references verification artifacts instead of embedding their output.
    """
    if not isinstance(details, dict):
        return details
    bounded = dict(details)
    for key in ("checks", "failures"):
        value = bounded.get(key)
        if isinstance(value, list):
            bounded[key] = [_strip_check_output(check) for check in value]
    return bounded


def _compact_snapshot(snapshot: Any) -> Any:
    """Compact a per-turn plan snapshot by dropping the full session list.

    The session list is the dominant size driver when many turns each embed a
    ``before``/``after`` snapshot. Counts, hashes, and next-pending pointers are
    retained; the full session list stays in ``result.json``.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    return {key: value for key, value in snapshot.items() if key != "sessions"}


def _compact_turn_summary(turn: Any) -> Any:
    """Project one detailed turn payload into a compact state summary.

    Keeps every scalar/path field readers rely on (paths, harness, model,
    status, timings) but replaces the heavy ``before``/``after`` snapshots with
    count-only summaries and strips raw verification output from the embedded
    verification summary. The detailed turn payload remains in ``result.json``.
    """
    if not isinstance(turn, dict):
        return turn
    summary = dict(turn)
    if "before" in summary:
        summary["before"] = _compact_snapshot(summary["before"])
    if "after" in summary:
        summary["after"] = _compact_snapshot(summary["after"])
    verification = summary.get("verification")
    if isinstance(verification, dict):
        compact_verification = dict(verification)
        checks = compact_verification.get("checks")
        if isinstance(checks, list):
            compact_verification["checks"] = [
                _strip_check_output(check) for check in checks
            ]
        summary["verification"] = compact_verification
    return summary


def _compact_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the compact ``state.json`` record persisted to disk.

    Returns a fresh dict so no mutable detailed turn dictionary stays aliased
    inside the persisted state payload. Detailed per-turn data (full snapshots
    and verification output) is dropped here and kept in ``result.json``.

    The stamped version describes what the record actually contains rather than
    what this build can produce. Compaction performs the v2 bounding itself, so
    it always claims at least v2; it claims v3 only when an ``agent_mux``
    projection is already attached, because attaching one is a state
    transition's job and ``asl compact``/``asl migrate-state`` are not that.
    Stamping v3 on a projection-free record would tell a consumer to expect a
    block that is not there.
    """
    compact = dict(payload)
    compact["version"] = (
        agent_mux_projection.STATE_SCHEMA_VERSION
        if isinstance(payload.get(agent_mux_projection.PROJECTION_KEY), dict)
        else COMPACT_STATE_SCHEMA_VERSION
    )
    turns = payload.get("turns")
    if isinstance(turns, list):
        compact["turns"] = [_compact_turn_summary(turn) for turn in turns]
    for key in ("pause_details", "details"):
        if key in compact:
            compact[key] = _bounded_verification_details(compact[key])
    return compact


def _run_finished_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact ``run_finished`` event payload.

    Carries the run outcome, duration, session counts, review summary, and
    artifact references instead of duplicating the entire state payload (which
    embeds every turn and, transitively, raw verification output). The assurance
    object is bounded the same way: issue IDs and counts, never findings.
    """
    counts: dict[str, Any] | None = None
    final_snapshot = payload.get("final_snapshot")
    if isinstance(final_snapshot, dict):
        snapshot_counts = final_snapshot.get("counts")
        if isinstance(snapshot_counts, dict):
            counts = snapshot_counts
    return {
        "run_id": payload.get("run_id"),
        "run_dir": payload.get("run_dir"),
        "ok": payload.get("ok"),
        "paused": payload.get("paused"),
        "reason": payload.get("reason"),
        "pause_reason": payload.get("pause_reason"),
        "pause_message": payload.get("pause_message"),
        "run_started_at": payload.get("run_started_at"),
        "run_finished_at": payload.get("run_finished_at"),
        "duration_seconds": payload.get("duration_seconds"),
        # `run_started` can only carry the configured model, since no harness
        # has reported one yet. The terminal record is where both fit.
        "model": payload.get("model"),
        "model_source": payload.get("model_source"),
        "observed_model": payload.get("observed_model"),
        "observed_model_source": payload.get("observed_model_source"),
        "model_projection": payload.get("model_projection"),
        "counts": counts,
        "completed_sessions": payload.get("completed_sessions"),
        "completed_session_ids": payload.get("completed_session_ids"),
        "turn_count": (
            len(payload["turns"]) if isinstance(payload.get("turns"), list) else 0
        ),
        "control": payload.get("control"),
        "review_completed": payload.get("review_completed"),
        "review_verdict": payload.get("review_verdict"),
        "review_output_path": payload.get("review_output_path"),
        "review_cycles": payload.get("review_cycles"),
        "review_fix_disposition": payload.get("review_fix_disposition"),
        "review_resume": payload.get("review_resume"),
        "review_reopen": payload.get("review_reopen"),
        "state_path": payload.get("state_path"),
        "events_path": payload.get("events_path"),
        "assurance": payload.get("assurance"),
        "assurance_report": payload.get("assurance_report"),
        **(
            {
                "cwd": payload.get("cwd"),
                "workspace_root": payload.get("workspace_root"),
                "registry_digest": payload.get("registry_digest"),
                "repositories": payload.get("repositories"),
            }
            if payload.get("workspace_root") is not None
            else {}
        ),
    }


def _write_state_payload(payload: dict[str, Any]) -> None:
    """Persist one state transition and the projection that describes it.

    The projection is attached to the live payload immediately before the
    compaction that produces the bytes on disk, so the ``agent_mux`` block a
    reader finds was built from exactly the state it is nested in. Callers
    reach this through :meth:`_RunPersistence.write_state`, which holds the
    run's lock across both the generation allocation and the atomic replace;
    that is what keeps generations monotonic when a heartbeat thread and the
    main loop both write.
    """
    state_path = payload.get("state_path")
    if not isinstance(state_path, str) or not state_path:
        return
    agent_mux_projection.attach_projection(
        payload, updated_at=clock.isoformat(clock.utc_now())
    )
    durability.write_json_file(
        Path(state_path),
        _compact_state_payload(payload),
        durable=True,
    )


def _append_event(event_path: Path, name: str, payload: dict[str, Any]) -> None:
    events.append_event(event_path, events.Event(name=name, payload=payload))


class _RunPersistence:
    """Serializes one run's mutable payload, state writes, and event appends.

    :attr:`lock` is the run's outermost durable-write mutex. It is reentrant
    because a write taken under it may legitimately reach another one — the
    AgentMux sequence allocation in :func:`_allocate_agent_mux_sequence` bumps
    the counter and writes state within a single hold — and it is never held
    across a mailbox read, a lease read, a sleep, or a subprocess, so a slow
    external actor can never stall the threads that only want to record what
    already happened.
    """

    def __init__(
        self,
        *,
        write_state: bool = True,
        append_events: bool = True,
    ) -> None:
        self.lock = threading.RLock()
        self._write_state = write_state
        self._append_events = append_events

    def append_event(
        self,
        event_path: Path,
        name: str,
        payload: dict[str, Any],
    ) -> None:
        if not self._append_events:
            return
        with self.lock:
            _append_event(event_path, name, payload)

    def write_state(self, payload: dict[str, Any]) -> None:
        if not self._write_state:
            return
        with self.lock:
            _write_state_payload(payload)


def _run_persistence(
    persistence: _RunPersistence | None,
) -> _RunPersistence:
    return _RunPersistence() if persistence is None else persistence


def _append_process_sweep_event(
    *,
    event_path: Path,
    persistence: _RunPersistence,
    report: dict[str, Any] | None,
    phase: str,
    identity: dict[str, Any],
) -> None:
    """Record a sweep that reaped (or failed to reap) something it outlived.

    Most sweeps find nothing, and a report is also absent whenever the sweep
    was opted out of or could not read the process table, so only the reports
    naming a signalled pid are worth an event.
    """
    if not isinstance(report, dict):
        return
    killed = report.get("killed") or []
    kill_failed = report.get("kill_failed") or []
    if not killed and not kill_failed:
        return
    persistence.append_event(
        event_path,
        events.PROCESS_SWEEP_EVENT,
        {
            "phase": phase,
            **identity,
            "killed": list(killed),
            "kill_failed": list(kill_failed),
            "survivors_after": list(report.get("survivors_after") or []),
            "enumeration": report.get("enumeration") or {},
        },
    )


def _append_harness_degradation_event(
    *,
    event_path: Path,
    persistence: _RunPersistence,
    degradation: dict[str, Any] | None,
    identity: dict[str, Any],
) -> None:
    """Record a harness that lost an auxiliary transport and ran the turn anyway.

    The payload deliberately carries no failure or pause reason: the turn keeps
    whatever status it earned, and this is the only record that the capability
    was lost at all.
    """
    if not degradation:
        return
    persistence.append_event(
        event_path,
        events.HARNESS_DEGRADED_EVENT,
        {**identity, **degradation},
    )


def _agent_mux_loop_payload(
    payload: dict[str, Any],
    *,
    reason: str,
    active_turn: dict[str, Any] | None = None,
    event_name: str | None = None,
    sequence: int | None = None,
    emitted_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return agent_mux_delivery.build_loop_payload(
        payload,
        reason=reason,
        active_turn=active_turn,
        event_name=event_name,
        sequence=sequence,
        emitted_at=emitted_at,
        extra=extra,
    )


def _agent_mux_notify_timeout() -> float:
    """Return the amx notification timeout in seconds.

    ``ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS`` overrides the default when it
    parses as a finite positive float; anything else fails safe onto
    ``agent_mux_delivery.NOTIFY_TIMEOUT_SECONDS`` so a bad override can never
    hang or break notifications.
    """
    return agent_mux_delivery.notify_timeout()


def _agent_mux_notify_enabled() -> bool:
    """Return whether AgentMux notification is enabled.

    ``ASL_AGENT_MUX_NOTIFY`` accepts ``auto`` (default) or ``off``; only an
    explicit ``off`` (case-insensitive) disables notification. Any other
    value behaves as ``auto`` so a typo can never silently kill delivery.
    """
    return agent_mux_delivery.notify_enabled()


def _allocate_agent_mux_sequence(
    payload: dict[str, Any],
    persistence: _RunPersistence,
) -> int:
    """Allocate the next per-run AgentMux sequence and persist it.

    Allocation happens at the ``_RunPersistence`` lock seam so the counter in
    ``state.json`` stays monotonic across every producer thread.
    """
    return agent_mux_delivery.allocate_sequence(payload, persistence)


def _notify_agent_mux_loop(
    event_name: str,
    payload: dict[str, Any],
    *,
    reason: str,
    active_turn: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    persistence: _RunPersistence | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Emit one run-lifecycle notification for this loop's payload.

    Sequence allocation, outcome recording and transport live in
    ``agent_mux_delivery`` so the turn boundary emits through exactly the
    same seam this loop does.
    """
    agent_mux_delivery.notify_loop(
        event_name,
        payload,
        reason=reason,
        active_turn=active_turn,
        extra=extra,
        persistence=persistence,
        cancelled=cancelled,
    )


def _notify_agent_mux_loop_started(
    payload: dict[str, Any],
    persistence: _RunPersistence | None = None,
) -> None:
    _notify_agent_mux_loop(
        agent_mux_delivery.START_EVENT,
        payload,
        reason=agent_mux_delivery.START_REASON,
        persistence=persistence,
    )


def _question_preview(text: str | None) -> str | None:
    """Collapse question text into one bounded line for notification payloads."""
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    return collapsed[:AGENT_MUX_QUESTION_PREVIEW_CHARS]


def _notify_agent_mux_loop_wait_started(
    payload: dict[str, Any],
    *,
    question_id: str,
    question_text: str | None,
    persistence: _RunPersistence | None = None,
) -> None:
    _notify_agent_mux_loop(
        agent_mux_delivery.WAIT_START_EVENT,
        payload,
        reason=agent_mux_delivery.WAIT_START_REASON,
        extra={
            "question_id": question_id,
            "question_preview": _question_preview(question_text),
        },
        persistence=persistence,
    )


def _notify_agent_mux_loop_wait_ended(
    payload: dict[str, Any],
    *,
    question_id: str,
    wait_reason: str,
    closure_id: str | None = None,
    persistence: _RunPersistence | None = None,
) -> None:
    _notify_agent_mux_loop(
        agent_mux_delivery.WAIT_END_EVENT,
        payload,
        reason=agent_mux_delivery.WAIT_END_REASON,
        extra={
            "question_id": question_id,
            "wait_reason": wait_reason,
            "delivery_state": ("consumed" if closure_id is not None else "unconsumed"),
            "closure_consumed": closure_id is not None,
            **({"closure_id": closure_id} if closure_id is not None else {}),
        },
        persistence=persistence,
    )


@dataclass(frozen=True)
class _AgentMuxWaitNotifier:
    """Bind a live wait's lifecycle to this run's AgentMux projection.

    The coordination boundary announces a wait through
    :class:`run_coordination.WaitNotifier` rather than reaching for the
    projection itself, because who hears about a wait is a runner concern and
    the delivery seam is already fail-open here: ``_notify_agent_mux_loop``
    swallows its own errors, so neither call can end a wait a supervisor is
    still answering.
    """

    payload: dict[str, Any]
    persistence: _RunPersistence

    def wait_started(self, *, question_id: str, question_text: str | None) -> None:
        _notify_agent_mux_loop_wait_started(
            self.payload,
            question_id=question_id,
            question_text=question_text,
            persistence=self.persistence,
        )

    def wait_ended(
        self, *, question_id: str, wait_reason: str, closure_id: str | None
    ) -> None:
        _notify_agent_mux_loop_wait_ended(
            self.payload,
            question_id=question_id,
            wait_reason=wait_reason,
            closure_id=closure_id,
            persistence=self.persistence,
        )


def _notify_agent_mux_loop_paused(
    payload: dict[str, Any],
    persistence: _RunPersistence | None = None,
) -> None:
    _notify_agent_mux_loop(
        agent_mux_delivery.PAUSE_EVENT,
        payload,
        reason=agent_mux_delivery.PAUSE_REASON,
        extra={
            "pause_reason": payload.get("pause_reason"),
            "pause_message": payload.get("pause_message"),
        },
        persistence=persistence,
    )


def _notify_agent_mux_loop_finished(
    payload: dict[str, Any],
    persistence: _RunPersistence | None = None,
) -> None:
    _notify_agent_mux_loop(
        agent_mux_delivery.FINISH_EVENT,
        payload,
        reason=agent_mux_delivery.FINISH_REASON,
        persistence=persistence,
    )
