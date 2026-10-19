# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Terminal run settlement after all commit work is accounted for."""

from . import commit_settlement_types as _types

globals().update(
    {name: value for name, value in vars(_types).items() if not name.startswith("__")}
)


def _finish_run(
    event_path: Path,
    payload: dict[str, Any],
    progress: run_reporting._ProgressReporter | None = None,
    persistence: run_persistence._RunPersistence | None = None,
    *,
    finalize_readiness: RunFinalizer,
) -> None:
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        finished_at = clock.utc_now()
        payload["run_finished_at"] = clock.isoformat(finished_at)
        started_at = clock.parse_datetime(payload.get("run_started_at"))
        payload["duration_seconds"] = (
            clock.duration_seconds(started_at, finished_at)
            if started_at is not None
            else None
        )
        payload["active_turn"] = None
        payload["active_verification"] = None
        if not payload.get("paused"):
            turn_execution_signals.clear_live_action_required(payload)
        finalize_readiness(payload)
        persistence.append_event(
            event_path,
            events.RUN_FINISHED_EVENT,
            run_persistence._run_finished_event_payload(payload),
        )
        persistence.write_state(payload)
    # A paused run already notified AgentSessionLoopPaused from _pause_run;
    # emitting Finished here would release the daemon's run lease and
    # overwrite the paused attention icon with the finished state.
    if not payload.get("paused"):
        run_persistence._notify_agent_mux_loop_finished(payload, persistence)
    if progress is not None:
        progress.run_finished(payload)
