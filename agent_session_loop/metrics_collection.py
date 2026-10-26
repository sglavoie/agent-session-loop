"""Collect operational metrics from the persisted plan-state corpus."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

from . import (
    assurance_report,
    cli_trace,
    compact_state,
    events,
    execution_timing,
    execution_timing_models,
    metrics_invocations,
    metrics_models,
    metrics_timing,
    metrics_timing_evidence,
    plans_errors,
    plans_snapshot,
    retention_projection,
    review_continuation,
    session_overview,
    state,
)


def collect_metrics(
    *,
    state_dir: str | Path | None = None,
    since_days: int = metrics_models.DEFAULT_SINCE_DAYS,
    now: datetime | None = None,
) -> metrics_models.MetricsReport:
    """Aggregate reliability and storage metrics for the given state root."""
    if since_days <= 0:
        raise ValueError("--since-days must be a positive integer")
    state_root = state.state_root(state_dir)
    now = now or datetime.now(UTC)
    # A window past what ``datetime`` can subtract covers the whole history,
    # the same clamp ``gc --max-age-days`` applies, rather than an error.
    cutoff = retention_projection._age_cutoff(now, since_days)

    turns_total = 0
    turns_successful = 0
    successful_runtime = 0.0
    unsuccessful_runtime = 0.0
    interrupted_turns = 0
    session_turns: dict[tuple[str, str], metrics_models._SessionTurns] = {}
    completion_times: dict[tuple[str, str], str] = {}
    # The earliest implementation turn each session ever had, window or not:
    # the window picks which sessions count, but "first pass" is about the
    # session's whole history, so a failure before the window still counts.
    first_turns: dict[tuple[str, str], tuple[str, bool]] = {}

    pause_counts: dict[str, int] = {}
    pauses_total = 0
    pauses_never_retry = 0
    pauses_retryable = 0

    verify_failures = 0
    verify_exhausted = 0
    verify_failed_sessions: dict[tuple[str, str], str] = {}
    paused_work_commits = 0
    paused_work_commit_results: dict[str, int] = {}

    cycle_disposition_counts: dict[str, int] = {}
    cycles_total = 0
    findings_introduced = 0
    findings_closed = 0
    findings_repeated = 0
    unique_materialization_keys: set[tuple[str, str]] = set()
    reopen_attempts = 0
    successful_reopen_materializations = 0
    reopen_failures = 0
    reopen_source_disposition_counts: dict[str, int] = {}
    reopen_failure_result_counts: dict[str, int] = {}

    agent_mux_deliveries = 0
    agent_mux_acknowledged = 0
    agent_mux_failed = 0
    agent_mux_legacy = 0
    agent_mux_status_counts: dict[str, int] = {}
    agent_mux_error_counts: dict[str, int] = {}

    artifact_total_bytes = 0
    artifact_grown_bytes = 0
    artifact_grown_files = 0

    compaction_bytes_before = 0
    compaction_bytes_after = 0
    compaction_artifact_bytes = 0
    compaction_projected = 0
    compaction_dirs = 0
    compaction_skipped: list[str] = []

    state_dirs_scanned = 0
    state_dirs_active = 0

    timing_runs: list[execution_timing.RunTiming] = []
    timing_evidence: dict[str, metrics_models._PlanTimingEvidence] = {}
    timing_unattributable_records = 0
    timing_unattributable_results = 0

    for plan_state_dir in sorted(session_overview._iter_state_plan_dirs(state_root)):
        state_dirs_scanned += 1
        dir_key = plan_state_dir.name
        total_bytes, grown_bytes, grown_files = metrics_models._directory_sizes(
            plan_state_dir, cutoff
        )
        artifact_total_bytes += total_bytes
        artifact_grown_bytes += grown_bytes
        artifact_grown_files += grown_files
        if grown_files == 0:
            # Nothing under this dir was written inside the window, so no turn
            # result or event record can carry an in-window timestamp either.
            # Skipping here is what keeps a narrow window off the full corpus.
            continue

        active = False
        plan_dir = metrics_models._plan_dir_for_state_dir(plan_state_dir)
        snapshot = None
        if plan_dir is not None:
            # Metrics remain best-effort for stale state directories. The
            # full event history still supplies an events-only fallback.
            with contextlib.suppress(plans_errors.PlanError, OSError):
                snapshot = plans_snapshot.inspect_plan(plan_dir)
        materialization_keys = review_continuation.reconciled_materialization_keys(
            plan_state_dir / events.EVENTS_FILE_NAME,
            snapshot,
            since=cutoff,
        )
        unique_materialization_keys.update(
            (dir_key, key) for key in materialization_keys
        )

        # Read once, then used twice: the windowed sections below filter these
        # record by record, while the timing collector needs the full history so
        # lineage and retry ordering can see an attempt that predates the window.
        turn_payloads = metrics_models._iter_turn_results(plan_state_dir)
        event_records = metrics_models._read_event_records(
            plan_state_dir / events.EVENTS_FILE_NAME
        )

        plan_timing = metrics_models._PlanTimingEvidence(
            plan_key=dir_key, plan_dir=plan_dir
        )
        plan_timing.repair_session_ids = metrics_timing_evidence._repair_session_ids(
            event_records, snapshot
        )
        metrics_timing_evidence._ingest_timing_evidence(plan_timing, event_records)
        timing_evidence[dir_key] = plan_timing
        # Collected per plan and reduced to per-run accounting immediately, so
        # the corpus is never held in memory all at once.
        plan_timing_report = execution_timing.collect_execution_timing(
            [
                execution_timing.RunEvidence(
                    plan_key=dir_key,
                    event_records=event_records,
                    turn_results=turn_payloads,
                )
            ],
            window_start=cutoff,
        )
        timing_runs.extend(plan_timing_report.runs)
        timing_unattributable_records += plan_timing_report.unattributable_records
        timing_unattributable_results += plan_timing_report.unattributable_turn_results

        for payload in turn_payloads:
            session_id = payload.get("session_id")
            started_at = payload.get("started_at")
            if (
                payload.get("session_kind") == metrics_models._IMPLEMENTATION_KIND
                and isinstance(session_id, str)
                and isinstance(started_at, str)
            ):
                turn = (
                    started_at,
                    payload.get("status_result") == metrics_models._COMPLETE_STATUS,
                )
                key = (dir_key, session_id)
                earliest = first_turns.get(key)
                if earliest is None or turn < earliest:
                    first_turns[key] = turn

        for payload in turn_payloads:
            finished_at = metrics_models._parse_timestamp(payload.get("finished_at"))
            if finished_at is None or finished_at < cutoff:
                continue
            active = True
            turns_total += 1
            duration = (
                execution_timing_models._finite_seconds(payload.get("duration_seconds"))
                or 0.0
            )
            successful = payload.get("status_result") == metrics_models._COMPLETE_STATUS
            if successful:
                turns_successful += 1
                successful_runtime += duration
            else:
                unsuccessful_runtime += duration
            if payload.get("interrupted") is True:
                interrupted_turns += 1

            session_id = payload.get("session_id")
            started_at = payload.get("started_at")
            if (
                payload.get("session_kind") == metrics_models._IMPLEMENTATION_KIND
                and isinstance(session_id, str)
                and isinstance(started_at, str)
            ):
                key = (dir_key, session_id)
                session_turns.setdefault(
                    key, metrics_models._SessionTurns()
                ).turns.append((started_at, successful))
                if successful:
                    recorded = completion_times.get(key)
                    if recorded is None or started_at > recorded:
                        completion_times[key] = started_at

        for record in event_records:
            name = record.get("name")
            raw_payload = record.get("payload")
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            created_at = metrics_models._parse_timestamp(record.get("created_at"))
            if created_at is None or created_at < cutoff:
                continue
            if name == events.RUN_PAUSED_EVENT:
                active = True
                pauses_total += 1
                reason = payload.get("reason")
                reason = reason if isinstance(reason, str) and reason else "unknown"
                pause_counts[reason] = pause_counts.get(reason, 0) + 1
                details = payload.get("details")
                details = details if isinstance(details, dict) else {}
                classification = details.get("failure_classification")
                if isinstance(classification, dict):
                    if classification.get("retryable") is True:
                        pauses_retryable += 1
                    else:
                        pauses_never_retry += 1
                if reason == metrics_models._VERIFY_FAILED_REASON:
                    verify_failures += 1
                    if details.get("verification_repair_exhausted") is True:
                        verify_exhausted += 1
                    session_id = details.get("session")
                    if isinstance(session_id, str):
                        key = (dir_key, session_id)
                        record_time = record.get("created_at")
                        if isinstance(record_time, str):
                            existing = verify_failed_sessions.get(key)
                            if existing is None or record_time < existing:
                                verify_failed_sessions[key] = record_time
            elif name == events.PAUSED_WORK_COMMITTED_EVENT:
                active = True
                # One event per settlement, not per commit: the run appends it
                # for every outcome the settlement reached, including finding
                # nothing to commit. The total answers "how often did a
                # recoverable pause settle its own dirt instead of leaving it
                # for a human", and the breakdown is what keeps that number
                # from reading as "how often did a commit land".
                paused_work_commits += 1
                result = payload.get("result")
                result = result if isinstance(result, str) and result else "unknown"
                paused_work_commit_results[result] = (
                    paused_work_commit_results.get(result, 0) + 1
                )
            elif name == events.REVIEW_CYCLE_EVENT:
                active = True
                cycles_total += 1
                disposition = payload.get("disposition")
                disposition = (
                    disposition
                    if isinstance(disposition, str) and disposition
                    else "unknown"
                )
                cycle_disposition_counts[disposition] = (
                    cycle_disposition_counts.get(disposition, 0) + 1
                )
                for field_name, counter in (
                    ("introduced_findings", "introduced"),
                    ("closed_findings", "closed"),
                    ("repeated_findings", "repeated"),
                ):
                    values = payload.get(field_name)
                    count = len(values) if isinstance(values, list) else 0
                    if counter == "introduced":
                        findings_introduced += count
                    elif counter == "closed":
                        findings_closed += count
                    else:
                        findings_repeated += count
            elif name == events.REVIEW_REPAIR_REOPENED_EVENT:
                active = True
                if payload.get("recovered") is True:
                    # Crash-recovery backfills preserve durable lineage but
                    # are not fresh reopen attempts in this time window.
                    continue
                reopen_attempts += 1
                source_disposition = payload.get("source_disposition")
                source_disposition = (
                    source_disposition
                    if isinstance(source_disposition, str) and source_disposition
                    else "unknown"
                )
                reopen_source_disposition_counts[source_disposition] = (
                    reopen_source_disposition_counts.get(source_disposition, 0) + 1
                )
                if payload.get("materialized") is True:
                    successful_reopen_materializations += 1
                else:
                    reopen_failures += 1
                    result = payload.get("result")
                    result = result if isinstance(result, str) and result else "unknown"
                    reopen_failure_result_counts[result] = (
                        reopen_failure_result_counts.get(result, 0) + 1
                    )
            elif name == events.AGENT_MUX_DELIVERY_EVENT:
                active = True
                agent_mux_deliveries += 1
                status = payload.get("final_status")
                status = status if isinstance(status, str) and status else "unknown"
                agent_mux_status_counts[status] = (
                    agent_mux_status_counts.get(status, 0) + 1
                )
                error_classification = payload.get("error_classification")
                error_classification = (
                    error_classification
                    if isinstance(error_classification, str) and error_classification
                    else "unknown"
                )
                agent_mux_error_counts[error_classification] = (
                    agent_mux_error_counts.get(error_classification, 0) + 1
                )
                if payload.get("acknowledged") is True:
                    agent_mux_acknowledged += 1
                elif status == "legacy_unacknowledged":
                    agent_mux_legacy += 1
                else:
                    agent_mux_failed += 1

        if active:
            state_dirs_active += 1
            if plan_dir is None:
                compaction_skipped.append(dir_key)
            else:
                try:
                    report = compact_state.compact_plan_state(
                        plan_dir, state_dir=state_root, apply=False
                    )
                except Exception:  # noqa: BLE001 - projection is best-effort
                    compaction_skipped.append(dir_key)
                else:
                    compaction_dirs += 1
                    compaction_bytes_before += report.bytes_before
                    compaction_bytes_after += report.bytes_after
                    compaction_artifact_bytes += report.artifact_bytes_added
                    compaction_projected += report.projected_bytes_reclaimed

    sessions_attempted = len(session_turns)
    sessions_completed = 0
    sessions_first_pass = 0
    for key, tracker in session_turns.items():
        ordered = tracker.sorted_turns()
        if any(ok for _, ok in ordered):
            sessions_completed += 1
            if first_turns.get(key, ordered[0])[1]:
                sessions_first_pass += 1

    verify_sessions_repaired = 0
    for key, first_failure_at in verify_failed_sessions.items():
        completed_at = completion_times.get(key)
        if completed_at is not None and completed_at >= first_failure_at:
            verify_sessions_repaired += 1

    # The per-plan reports are recombined rather than recollected: every run is
    # keyed by (plan, run id), so concatenating them is the same answer one
    # whole-corpus call would have given, without holding the corpus.
    timing_report = execution_timing.ExecutionTimingReport(
        window_start=cutoff,
        window_end=None,
        tolerance_seconds=execution_timing.DEFAULT_TOLERANCE_SECONDS,
        runs=tuple(sorted(timing_runs, key=lambda run: (run.plan_key, run.run_id))),
        unattributable_records=timing_unattributable_records,
        unattributable_turn_results=timing_unattributable_results,
    )

    first_pass_rate = (
        sessions_first_pass / sessions_attempted if sessions_attempted else None
    )
    repair_rate = (
        verify_sessions_repaired / len(verify_failed_sessions)
        if verify_failed_sessions
        else None
    )

    return metrics_models.MetricsReport(
        state_root=str(state_root),
        since_days=since_days,
        window_start=cutoff.isoformat(),
        generated_at=now.isoformat(),
        state_dirs_scanned=state_dirs_scanned,
        state_dirs_active=state_dirs_active,
        turns={
            "total": turns_total,
            "successful": turns_successful,
            "unsuccessful": turns_total - turns_successful,
            "interrupted": interrupted_turns,
            "successful_runtime_seconds": round(successful_runtime, 3),
            "unsuccessful_runtime_seconds": round(unsuccessful_runtime, 3),
        },
        first_pass={
            "sessions_attempted": sessions_attempted,
            "sessions_completed": sessions_completed,
            "sessions_completed_first_pass": sessions_first_pass,
            "first_pass_rate": first_pass_rate,
        },
        pauses={
            "total": pauses_total,
            "by_reason": dict(sorted(pause_counts.items())),
            "classified_never_retry": pauses_never_retry,
            "classified_retryable": pauses_retryable,
        },
        verification_repair={
            "verify_failed_pauses": verify_failures,
            "repair_exhausted_pauses": verify_exhausted,
            "sessions_with_verify_failures": len(verify_failed_sessions),
            "sessions_repaired": verify_sessions_repaired,
            "repair_success_rate": repair_rate,
            "paused_work_commits": paused_work_commits,
            "paused_work_commits_by_result": dict(
                sorted(paused_work_commit_results.items())
            ),
        },
        review_cycles={
            "cycles_recorded": cycles_total,
            "by_disposition": dict(sorted(cycle_disposition_counts.items())),
            # Keep the legacy key while making its cross-event semantics
            # idempotent. New consumers should prefer the explicit name.
            "repairs_materialized": len(unique_materialization_keys),
            "unique_materializations": len(unique_materialization_keys),
            "reopen_attempts": reopen_attempts,
            "successful_reopen_materializations": (successful_reopen_materializations),
            "reopen_failures": reopen_failures,
            "reopen_attempts_by_source_disposition": dict(
                sorted(reopen_source_disposition_counts.items())
            ),
            "reopen_failures_by_result": dict(
                sorted(reopen_failure_result_counts.items())
            ),
            "findings_introduced": findings_introduced,
            "findings_closed": findings_closed,
            "findings_repeated": findings_repeated,
        },
        agent_mux_delivery={
            "total": agent_mux_deliveries,
            "acknowledged": agent_mux_acknowledged,
            "failed": agent_mux_failed,
            "legacy_unacknowledged": agent_mux_legacy,
            "by_status": dict(sorted(agent_mux_status_counts.items())),
            "by_error_classification": dict(sorted(agent_mux_error_counts.items())),
        },
        artifacts={
            "total_bytes": artifact_total_bytes,
            "grown_bytes": artifact_grown_bytes,
            "grown_files": artifact_grown_files,
        },
        compaction={
            "dirs_projected": compaction_dirs,
            "dirs_skipped": sorted(compaction_skipped),
            "bytes_before": compaction_bytes_before,
            "bytes_after": compaction_bytes_after,
            "artifact_bytes_added": compaction_artifact_bytes,
            "projected_bytes_reclaimed": compaction_projected,
        },
        # The inbox spans every plan, so it is aggregated once from the shared
        # ledger rather than per state directory.
        assurance=assurance_report.collect_assurance_metrics(
            state_dir=state_root, since=cutoff
        ),
        # The trace is per-invocation, not per-plan, so it is read once from
        # the root itself. This report's own start record is already on disk
        # and would otherwise show up as an unmatched start.
        cli_invocations=metrics_invocations.collect_cli_invocations(
            state_root,
            cutoff=cutoff,
            exclude_invocation_id=cli_trace.current_invocation_id(),
        ),
        # Rendered from evidence already read above; the section adds no scan.
        execution_timing=metrics_timing.collect_execution_timing(
            timing_report, timing_evidence
        ),
    )
