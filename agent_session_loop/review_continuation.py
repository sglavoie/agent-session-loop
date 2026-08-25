"""Read-only reconstruction of review repair continuation state.

The runner records review completions in an append-only event log, while the
plan directory remains the durable authority for generated repair children and
their handoff statuses.  This module reconciles both sources without mutating
either one.  Status, the runner, the supervisor, and Workbench can therefore
share one cap and continuation projection.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal

from . import (
    events,
    pause_policy,
    plans,
    review,
    review_repair,
    session_overview,
    workspaces,
)

REVIEW_CYCLE_DISPOSITION_ALL_CLEAR = "all-clear"
REVIEW_CYCLE_DISPOSITION_REPAIRS = "repairs-materialized"
REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED = "cycles-disabled"
REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED = "cycle-cap-exhausted"
REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED = "repair-group-exhausted"
REVIEW_CYCLE_DISPOSITION_REPEATED = "findings-repeated"
REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED = "unstructured-findings"
REVIEW_CYCLE_DISPOSITION_MALFORMED = "malformed-review-repair"
REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY = "not-loop-ready"
REVIEW_CYCLE_DISPOSITIONS: tuple[str, ...] = (
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    REVIEW_CYCLE_DISPOSITION_REPAIRS,
    REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
    REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_REPEATED,
    REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
    REVIEW_CYCLE_DISPOSITION_MALFORMED,
    REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
)

TERMINAL_REVIEW_DISPOSITIONS = frozenset(
    {
        REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
        REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
        REVIEW_CYCLE_DISPOSITION_REPEATED,
        REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
    }
)
BUDGET_REVIEW_DISPOSITIONS = frozenset(
    {
        REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
        REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
    }
)
MATERIALIZED_REVIEW_DISPOSITIONS = frozenset(
    {
        REVIEW_CYCLE_DISPOSITION_REPAIRS,
        REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
    }
)


ReviewResumeAction = Literal[
    "initial-review",
    "materialize-repairs",
    "run-repairs",
    "rereview",
    "terminal",
]


@dataclass(frozen=True)
class ReviewEventRecord:
    """One relevant, well-formed append-only review event."""

    name: str
    payload: Mapping[str, Any]
    created_at: str | None = None


@dataclass(frozen=True)
class ReviewResume:
    """Deterministic review continuation projection for public JSON surfaces."""

    source_cycle: int | None
    review_session_id: str | None
    source_disposition: str | None
    action: ReviewResumeAction
    eligible: bool
    blocker: str | None
    historical_materializations: int
    minimum_required_cap: int
    selected_cap: int | None
    max_repair_group_attempts: int | None = None
    repair_group_attempts: Mapping[str, int] | None = None
    repair_session_ids: tuple[str, ...] = ()
    pending_repair_session_ids: tuple[str, ...] = ()
    completed_repair_session_ids: tuple[str, ...] = ()
    source_fingerprints: tuple[str, ...] | None = None
    current_fingerprints: tuple[str, ...] | None = None
    current_repeat_keys: tuple[str, ...] | None = None
    source_review_output_sha256: str | None = None
    source_review_artifact_sha256: str | None = None
    current_review_output_sha256: str | None = None
    current_review_artifact_sha256: str | None = None
    artifact_valid: bool | None = None
    artifact_error: str | None = None
    source_findings: tuple[Mapping[str, Any], ...] | None = None
    current_findings: tuple[Mapping[str, Any], ...] | None = None
    source_repositories: tuple[Mapping[str, Any], ...] | None = None
    current_repositories: tuple[Mapping[str, Any], ...] | None = None
    repair_groups: tuple[Mapping[str, Any], ...] | None = None
    repair_scope_gaps: tuple[Mapping[str, Any], ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "source_cycle": self.source_cycle,
            "review_session_id": self.review_session_id,
            "source_disposition": self.source_disposition,
            "action": self.action,
            "eligible": self.eligible,
            "blocker": self.blocker,
            "historical_materializations": self.historical_materializations,
            "minimum_required_cap": self.minimum_required_cap,
            "selected_cap": self.selected_cap,
            "repair_session_ids": list(self.repair_session_ids),
            "pending_repair_session_ids": list(self.pending_repair_session_ids),
            "completed_repair_session_ids": list(self.completed_repair_session_ids),
            "source_fingerprints": (
                list(self.source_fingerprints)
                if self.source_fingerprints is not None
                else None
            ),
            "current_fingerprints": (
                list(self.current_fingerprints)
                if self.current_fingerprints is not None
                else None
            ),
            "current_repeat_keys": (
                list(self.current_repeat_keys)
                if self.current_repeat_keys is not None
                else None
            ),
            "source_review_output_sha256": self.source_review_output_sha256,
            "source_review_artifact_sha256": (self.source_review_artifact_sha256),
            "current_review_output_sha256": self.current_review_output_sha256,
            "current_review_artifact_sha256": (self.current_review_artifact_sha256),
            "artifact_valid": self.artifact_valid,
            "artifact_error": self.artifact_error,
        }
        if self.max_repair_group_attempts is not None:
            payload["max_repair_group_attempts"] = self.max_repair_group_attempts
        if self.repair_group_attempts is not None:
            payload["repair_group_attempts"] = dict(self.repair_group_attempts)
        if self.source_findings is not None:
            payload["source_findings"] = [dict(item) for item in self.source_findings]
        if self.current_findings is not None:
            payload["current_findings"] = [dict(item) for item in self.current_findings]
        if self.source_repositories is not None:
            payload["source_repositories"] = [
                dict(item) for item in self.source_repositories
            ]
        if self.current_repositories is not None:
            payload["current_repositories"] = [
                dict(item) for item in self.current_repositories
            ]
        if self.repair_groups is not None:
            payload["repair_groups"] = [dict(item) for item in self.repair_groups]
        if self.repair_scope_gaps is not None:
            payload["repair_scope_gaps"] = [
                dict(item) for item in self.repair_scope_gaps
            ]
        return payload


def read_review_events(event_path: str | Path) -> tuple[ReviewEventRecord, ...]:
    """Read cycle/reopen records in append order, skipping malformed lines."""
    relevant = {
        events.REVIEW_CYCLE_EVENT,
        events.REVIEW_REPAIR_REOPENED_EVENT,
    }
    records: list[ReviewEventRecord] = []
    try:
        event_file = Path(event_path).open(  # noqa: SIM115 - opened here so OSError returns (); closed by the with below
            "r", encoding="utf-8"
        )
    except OSError:
        return ()
    with event_file:
        for line in event_file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError, ValueError:
                continue
            if not isinstance(record, dict):
                continue
            name = record.get("name")
            payload = record.get("payload")
            if name in relevant and isinstance(payload, dict):
                created_at = record.get("created_at")
                records.append(
                    ReviewEventRecord(
                        name=name,
                        payload=payload,
                        created_at=(
                            created_at if isinstance(created_at, str) else None
                        ),
                    )
                )
    return tuple(records)


def read_standing_pause_reason(event_path: str | Path) -> str | None:
    """The reason the newest run stands paused on, or ``None`` if it does not.

    The event log is per-plan and append-only, so the newest run is the span
    after the last ``run_started``. Within that span a ``run_paused`` record is
    terminal: every runner pause site leaves the session loop, and the paused
    flag it sets is never cleared inside the same run. A ``run_finished``
    therefore does not clear the pause -- the runner appends one for paused runs
    too -- but a later ``run_started`` does, which is what lets this projection
    heal when the operator simply reruns the plan rather than rewriting history.
    """
    reason: str | None = None
    try:
        event_file = Path(event_path).open(  # noqa: SIM115 - opened here so OSError returns None; closed by the with below
            "r", encoding="utf-8"
        )
    except OSError:
        return None
    with event_file:
        for line in event_file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError, ValueError:
                continue
            if not isinstance(record, dict):
                continue
            name = record.get("name")
            if name == events.RUN_STARTED_EVENT:
                reason = None
                continue
            if name != events.RUN_PAUSED_EVENT:
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                reason = _nonempty_str(payload.get("reason")) or reason
    return reason


def _standing_environment_pause_blocker(event_path: str | Path) -> str | None:
    """The environment precondition a pending repair run would re-hit at once.

    A pause blocked on the workspace rather than on an answer survives the run
    that hit it: the dirty repository, the unregistered repo, the plan marked
    draft are all still there when the next run opens. Calling the pending
    children eligible while that pause stands publishes a contradiction inside
    one ``state.json`` -- the same file says the run is paused on it. Pauses an
    operator can clear by replying are deliberately excluded: those resolve
    within the run and say nothing about whether the next one can proceed.
    """
    reason = read_standing_pause_reason(event_path)
    if reason is None or reason not in pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS:
        return None
    return reason


def read_prior_review_cycles(event_path: str | Path) -> list[dict[str, Any]]:
    """Return prior ``review_cycle`` payloads oldest first."""
    return [
        dict(record.payload)
        for record in read_review_events(event_path)
        if record.name == events.REVIEW_CYCLE_EVENT
    ]


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _nonnegative_int(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(item for item in value if isinstance(item, str))


def repeated_finding_positions(
    *,
    fingerprints: Sequence[str],
    repeat_keys: Sequence[str],
    previous_fingerprints: Sequence[str],
    previous_repeat_keys: Sequence[str],
) -> tuple[int, ...]:
    """Return the positions of current findings that a previous cycle raised.

    A finding repeats when its exact fingerprint returns *or* when its
    drift-tolerant repeat key does. The fingerprint lane keeps a genuinely
    unchanged finding detected; the repeat-key lane catches the same defect
    re-reported against a moved evidence path or a different repository alias,
    which the fingerprint alone reads as newly introduced. Single-sourced here
    so the live cycle breaker and its crash-recovery mirror cannot diverge.
    """
    previous = set(previous_fingerprints)
    previous_keys = set(previous_repeat_keys)
    return tuple(
        position
        for position, fingerprint in enumerate(fingerprints)
        if fingerprint in previous
        or (position < len(repeat_keys) and repeat_keys[position] in previous_keys)
    )


def _reopen_repeat_keys(payload: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Read a reopen record's repeat keys, recomputing them from its findings.

    Reopen records written before repeat keys existed carry no
    ``current_repeat_keys`` list but do carry ``current_findings``, so the keys
    are recomputed and an in-flight run keeps its repeat baseline across the
    upgrade.
    """
    stored = _string_tuple(payload.get("current_repeat_keys"))
    if stored is not None:
        return stored
    findings = payload.get("current_findings")
    if isinstance(findings, list):
        return review.finding_repeat_keys(findings)
    return None


def _mapping_tuple(value: Any) -> tuple[Mapping[str, Any], ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _source_id_from_repair_ids(payload: Mapping[str, Any]) -> str | None:
    source_components: set[int] = set()
    for repair_id in _event_repair_ids(payload):
        try:
            parsed = plans.parse_session_id(repair_id)
        except ValueError:
            continue
        if parsed.is_integer:
            continue
        source_components.add(parsed.components[0])
    if len(source_components) == 1:
        return str(next(iter(source_components)))
    return None


def _cycle_source_id(payload: Mapping[str, Any]) -> str | None:
    return _nonempty_str(
        payload.get("review_session_id")
    ) or _source_id_from_repair_ids(payload)


def _reopen_source_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("source_review_session_id", "review_session_id"):
        source_id = _nonempty_str(payload.get(key))
        if source_id is not None:
            return source_id
    return _source_id_from_repair_ids(payload)


def _event_repair_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("repair_session_ids")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item.strip())


def _repair_groups_from_payload(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    raw = payload.get("repair_groups")
    if not isinstance(raw, list):
        return ()
    groups: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        repair_group = _nonempty_str(item.get("repair_group"))
        repair_session_id = _nonempty_str(item.get("repair_session_id"))
        if repair_group is None or repair_session_id is None:
            continue
        group: dict[str, Any] = {
            "repair_group": repair_group,
            "repair_session_id": repair_session_id,
            "finding_ids": list(_string_tuple(item.get("finding_ids")) or ()),
        }
        read_repositories = _string_tuple(item.get("read_repositories"))
        write_repositories = _string_tuple(item.get("write_repositories"))
        if read_repositories is not None:
            group["read_repositories"] = list(read_repositories)
        if write_repositories is not None:
            group["write_repositories"] = list(write_repositories)
        groups.append(group)
    return tuple(groups)


def _materialization_key(
    record: ReviewEventRecord,
    *,
    ordinal: int,
) -> str | None:
    payload = record.payload
    if record.name == events.REVIEW_CYCLE_EVENT:
        disposition = _nonempty_str(payload.get("disposition"))
        repair_ids = _event_repair_ids(payload)
        if not repair_ids and disposition != REVIEW_CYCLE_DISPOSITION_REPAIRS:
            return None
        source_id = _cycle_source_id(payload)
        if source_id is not None:
            return f"review:{source_id}"
        cycle = _nonnegative_int(payload.get("cycle"))
        return f"legacy-cycle:{cycle if cycle is not None else ordinal}"

    repair_ids = _event_repair_ids(payload)
    materialized = payload.get("materialized")
    if not repair_ids and materialized is not True:
        return None
    source_id = _reopen_source_id(payload)
    if source_id is not None:
        return f"review:{source_id}"
    source_cycle = _nonnegative_int(payload.get("source_cycle"))
    return (
        f"legacy-cycle:{source_cycle}"
        if source_cycle is not None
        else f"legacy-reopen:{ordinal}"
    )


def review_event_materialization_key(
    name: str,
    payload: Mapping[str, Any],
    *,
    ordinal: int,
) -> str | None:
    """Return the cross-event materialization identity for metrics/readers.

    Ordinary ``review_cycle`` records and their corresponding
    ``review_repair_reopened`` lineage records intentionally resolve to the
    same key.  Readers can therefore count unique source-review
    materializations without duplicating the continuation evaluator's legacy
    fallback rules.
    """

    if name not in {
        events.REVIEW_CYCLE_EVENT,
        events.REVIEW_REPAIR_REOPENED_EVENT,
    }:
        return None
    return _materialization_key(
        ReviewEventRecord(name=name, payload=payload),
        ordinal=ordinal,
    )


def repair_group_attempts(
    event_path: str | Path,
    snapshot_or_plan_dir: plans.PlanSnapshot | str | Path | None = None,
) -> dict[str, int]:
    """Return per-group repair attempt counts from events and durable children."""

    records = read_review_events(event_path)
    snapshot = (
        snapshot_or_plan_dir
        if isinstance(snapshot_or_plan_dir, plans.PlanSnapshot)
        else (
            plans.inspect_plan(snapshot_or_plan_dir)
            if snapshot_or_plan_dir is not None
            else None
        )
    )
    counts: dict[str, int] = {}
    seen_repair_sessions: set[str] = set()

    def record_attempt(group: Mapping[str, Any]) -> None:
        repair_group = _nonempty_str(group.get("repair_group"))
        repair_session_id = _nonempty_str(group.get("repair_session_id"))
        if repair_group is None or repair_session_id is None:
            return
        if repair_session_id in seen_repair_sessions:
            return
        seen_repair_sessions.add(repair_session_id)
        counts[repair_group] = counts.get(repair_group, 0) + 1

    for record in records:
        for group in _repair_groups_from_payload(record.payload):
            record_attempt(group)

    if snapshot is not None:
        generated_repairs = _generated_repair_statuses(snapshot)
        durable_children = {
            repair_session_id: status
            for children in generated_repairs.values()
            for repair_session_id, status in children.items()
        }
        durable_groups = _repair_group_records_from_children(durable_children)
        if durable_groups is not None:
            for group in durable_groups:
                record_attempt(group)

    return {group: counts[group] for group in sorted(counts)}


def _repair_group_attempts_from_payload(
    payload: Mapping[str, Any],
) -> dict[str, int]:
    raw = payload.get("repair_group_attempts")
    if not isinstance(raw, Mapping):
        return {}
    counts: dict[str, int] = {}
    for repair_group, attempts in raw.items():
        if not isinstance(repair_group, str) or not repair_group.strip():
            continue
        count = _nonnegative_int(attempts)
        if count is None:
            continue
        counts[repair_group] = count
    return {group: counts[group] for group in sorted(counts)}


def exhausted_repair_group_details(
    review_resume: Mapping[str, Any] | None,
) -> tuple[str, int] | None:
    """Return the recurring repair group that already exhausted its budget."""

    if not isinstance(review_resume, Mapping):
        return None
    limit = _nonnegative_int(review_resume.get("max_repair_group_attempts"))
    if limit is None:
        return None
    counts = _repair_group_attempts_from_payload(review_resume)
    if not counts:
        return None
    repair_groups = _repair_groups_from_payload(review_resume)
    if repair_groups:
        seen_groups: set[str] = set()
        for group in repair_groups:
            repair_group = str(group["repair_group"])
            if repair_group in seen_groups:
                continue
            seen_groups.add(repair_group)
            attempts = counts.get(repair_group)
            if attempts is not None and attempts >= limit:
                return repair_group, attempts
        return None
    for repair_group, attempts in counts.items():
        if attempts >= limit:
            return repair_group, attempts
    return None


def exhausted_repair_group_guidance(
    review_resume: Mapping[str, Any] | None,
) -> str | None:
    exhausted = exhausted_repair_group_details(review_resume)
    if exhausted is None:
        return None
    repair_group, attempts = exhausted
    return (
        f'the recurring repair group "{repair_group}" already hit {attempts} '
        f"{'attempt' if attempts == 1 else 'attempts'}; fix it manually or "
        "re-split instead of raising --max-review-fix-cycles"
    )


def _note_materialization_time(
    times: dict[str, datetime | None],
    key: str,
    moment: datetime | None,
) -> None:
    """Track the newest dated event attributed to one materialization key."""
    if key not in times:
        times[key] = moment
        return
    existing = times[key]
    if moment is not None and (existing is None or moment > existing):
        times[key] = moment


def _event_materialization_key_times(
    records: tuple[ReviewEventRecord, ...],
) -> dict[str, datetime | None]:
    times: dict[str, datetime | None] = {}
    for ordinal, record in enumerate(records, start=1):
        key = review_event_materialization_key(
            record.name,
            record.payload,
            ordinal=ordinal,
        )
        if key is None:
            continue
        _note_materialization_time(
            times,
            key,
            session_overview.parse_iso_timestamp(record.created_at),
        )
    return times


def _review_id_for_cycle_position(
    payload: Mapping[str, Any],
    *,
    cycle_ordinal: int,
    review_ids: list[str],
) -> str | None:
    """Resolve a legacy cycle against ordered generated review sessions."""
    cycle = _nonnegative_int(payload.get("cycle"))
    review_position = cycle if cycle else cycle_ordinal
    if not 1 <= review_position <= len(review_ids):
        return None
    return review_ids[review_position - 1]


def _materialization_key_times(
    records: tuple[ReviewEventRecord, ...],
    generated_repairs: Mapping[str, Mapping[str, plans.SessionStatus]] | None = None,
    generated_reviews: Mapping[str, plans.SessionStatus] | None = None,
) -> dict[str, datetime | None]:
    """Return each unique materialization with the newest event that dated it.

    A materialization recovered from durable children alone maps to ``None``:
    the lost event append is what carried its timestamp, so nothing on disk
    can place it in time.
    """

    times = _event_materialization_key_times(records)
    if generated_repairs is not None:
        review_ids = (
            sorted(
                generated_reviews,
                key=lambda item: plans.parse_session_id(item).sort_key,
            )
            if generated_reviews is not None
            else []
        )
        lineage_sources = {
            source_id
            for record in records
            if record.name == events.REVIEW_CYCLE_EVENT
            for source_id in (_cycle_source_id(record.payload),)
            if source_id is not None
        }
        cycle_ordinal = 0
        for ordinal, record in enumerate(records, start=1):
            if record.name != events.REVIEW_CYCLE_EVENT:
                continue
            cycle_ordinal += 1
            if _cycle_source_id(record.payload) is not None:
                continue
            key = review_event_materialization_key(
                record.name,
                record.payload,
                ordinal=ordinal,
            )
            if key is None:
                continue
            source_id = _review_id_for_cycle_position(
                record.payload,
                cycle_ordinal=cycle_ordinal,
                review_ids=review_ids,
            )
            if source_id is None:
                continue
            if source_id in lineage_sources or not generated_repairs.get(source_id):
                continue
            _note_materialization_time(
                times, f"review:{source_id}", times.pop(key, None)
            )
        for source_id, children in generated_repairs.items():
            if children:
                times.setdefault(f"review:{source_id}", None)
    return times


def _reconciled_materialization_keys(
    records: tuple[ReviewEventRecord, ...],
    generated_repairs: Mapping[str, Mapping[str, plans.SessionStatus]] | None = None,
    generated_reviews: Mapping[str, plans.SessionStatus] | None = None,
) -> set[str]:
    """Return unique materializations across events and durable children."""

    return set(
        _materialization_key_times(records, generated_repairs, generated_reviews)
    )


def reconciled_materialization_keys(
    event_path: str | Path,
    snapshot_or_plan_dir: plans.PlanSnapshot | str | Path | None = None,
    *,
    since: datetime | None = None,
) -> frozenset[str]:
    """Return durable source-review materialization identities.

    Event history supplies ordinary and reopened materializations.  When a
    readable plan snapshot is available, generated repair children also
    recover materializations whose event append was lost and reconcile legacy
    cycle records that predate explicit source-review lineage.

    ``since`` narrows the result to materializations whose newest attributed
    event landed at or after that instant.  A materialization recovered from
    durable children alone carries no timestamp and is left out, the same way
    an undated turn or event record falls outside a window.
    """

    records = read_review_events(event_path)
    snapshot = (
        snapshot_or_plan_dir
        if isinstance(snapshot_or_plan_dir, plans.PlanSnapshot)
        else (
            plans.inspect_plan(snapshot_or_plan_dir)
            if snapshot_or_plan_dir is not None
            else None
        )
    )
    generated_repairs = (
        _generated_repair_statuses(snapshot) if snapshot is not None else None
    )
    generated_reviews = (
        _generated_review_statuses(snapshot) if snapshot is not None else None
    )
    times = _materialization_key_times(records, generated_repairs, generated_reviews)
    if since is None:
        return frozenset(times)
    return frozenset(
        key for key, moment in times.items() if moment is not None and moment >= since
    )


def _post_reopen_baseline_payload(
    records: tuple[ReviewEventRecord, ...],
    latest_cycle: Mapping[str, Any],
    generated_reviews: Mapping[str, plans.SessionStatus] | None = None,
) -> Mapping[str, Any] | None:
    """Return the later successful reopen whose evidence corrects a cycle.

    Only reopen records appended after ``latest_cycle`` can replace that
    cycle's baseline.  The last materialized reopen for the same or a newer
    source review wins; the newer-source case recovers an eventless review
    whose cycle record was lost.  Session-id ordering keeps an older reopen
    from affecting a newer review.  A lineage-less cycle uses its positional
    generated-review source when available.  If neither source can be
    reconstructed, the latest explicit reopen source wins by session-id order,
    with append order breaking ties.  The cycle-number fallback preserves
    legacy reopen records that lack a review session id.
    """

    source_id_text = _cycle_source_id(latest_cycle)
    source_id = _parse_session_id_or_none(source_id_text)
    positional_source_id: plans.SessionId | None = None
    unresolved_source_reopen: tuple[plans.SessionId, Mapping[str, Any]] | None = None
    if source_id_text is None and generated_reviews:
        review_ids = sorted(
            generated_reviews,
            key=lambda item: plans.parse_session_id(item).sort_key,
        )
        cycle_ordinal = (
            sum(
                1
                for record in records
                if record.name == events.REVIEW_CYCLE_EVENT
                and record.payload is not latest_cycle
            )
            + 1
        )
        positional_source_id = _parse_session_id_or_none(
            _review_id_for_cycle_position(
                latest_cycle,
                cycle_ordinal=cycle_ordinal,
                review_ids=review_ids,
            )
        )
    source_cycle = _nonnegative_int(latest_cycle.get("cycle"))
    for record in reversed(records):
        if record.name == events.REVIEW_CYCLE_EVENT and record.payload is latest_cycle:
            break
        if record.name != events.REVIEW_REPAIR_REOPENED_EVENT:
            continue
        if record.payload.get("materialized") is not True:
            continue
        reopen_source_id_text = _reopen_source_id(record.payload)
        reopen_source_id = _parse_session_id_or_none(reopen_source_id_text)
        if reopen_source_id_text is not None and reopen_source_id is None:
            continue
        if source_id_text is not None and source_id is None:
            continue
        if source_id is not None and reopen_source_id is not None:
            if reopen_source_id.sort_key < source_id.sort_key:
                continue
        elif source_id is None and reopen_source_id is not None:
            if (
                positional_source_id is not None
                and reopen_source_id.sort_key < positional_source_id.sort_key
            ):
                continue
            if positional_source_id is None:
                if (
                    unresolved_source_reopen is None
                    or reopen_source_id.sort_key > unresolved_source_reopen[0].sort_key
                ):
                    unresolved_source_reopen = (reopen_source_id, record.payload)
                continue
        elif source_cycle is not None:
            reopened_cycle = _nonnegative_int(record.payload.get("source_cycle"))
            if reopened_cycle != source_cycle:
                continue
        else:
            continue
        return record.payload
    if unresolved_source_reopen is not None:
        return unresolved_source_reopen[1]
    return None


def _latest_materialized_reopen_baseline_payload(
    records: tuple[ReviewEventRecord, ...],
) -> Mapping[str, Any] | None:
    """Return the durable reopen baseline without a cycle record.

    A crash after repair children are atomically written but before the first
    ``review_cycle`` append leaves no cycle record to associate with the
    recovered reopen.  In that zero-cycle history, use the highest valid
    source-review session as the baseline, with event order breaking ties.
    This keeps multiple recovered reopens independent of their append order.
    Legacy records without source lineage retain their newest-record fallback.
    """

    latest_source_reopen: tuple[plans.SessionId, Mapping[str, Any]] | None = None
    latest_legacy_reopen: Mapping[str, Any] | None = None
    for record in records:
        if (
            record.name != events.REVIEW_REPAIR_REOPENED_EVENT
            or record.payload.get("materialized") is not True
        ):
            continue
        source_id_text = _reopen_source_id(record.payload)
        source_id = _parse_session_id_or_none(source_id_text)
        if source_id_text is None:
            latest_legacy_reopen = record.payload
        elif source_id is not None and (
            latest_source_reopen is None
            or source_id.sort_key >= latest_source_reopen[0].sort_key
        ):
            latest_source_reopen = (source_id, record.payload)
    if latest_source_reopen is not None:
        return latest_source_reopen[1]
    return latest_legacy_reopen


def review_cycle_state(
    event_path: str | Path,
    snapshot_or_plan_dir: plans.PlanSnapshot | str | Path | None = None,
) -> dict[str, Any]:
    """Compute the cross-process state used for the next cycle record.

    Materialization accounting is by unique source review.  A legacy
    ``repairs-materialized`` event without source lineage still consumes one
    cycle, and a ``not-loop-ready`` record with repair ids consumes one too.
    When the current plan is available, generated repair children also consume
    a cycle even if their source event was lost in a materialize/record crash.
    """
    records = read_review_events(event_path)
    snapshot = (
        snapshot_or_plan_dir
        if isinstance(snapshot_or_plan_dir, plans.PlanSnapshot)
        else (
            plans.inspect_plan(snapshot_or_plan_dir)
            if snapshot_or_plan_dir is not None
            else None
        )
    )
    generated_repairs = (
        _generated_repair_statuses(snapshot) if snapshot is not None else None
    )
    generated_reviews = (
        _generated_review_statuses(snapshot) if snapshot is not None else None
    )
    cycles = [
        record.payload for record in records if record.name == events.REVIEW_CYCLE_EVENT
    ]
    previous_fingerprints: tuple[str, ...] = ()
    previous_repeat_keys: tuple[str, ...] = ()
    if cycles:
        previous_fingerprints = _string_tuple(cycles[-1].get("fingerprints")) or ()
        previous_repeat_keys = _string_tuple(cycles[-1].get("repeat_keys")) or ()
        reopened = _post_reopen_baseline_payload(records, cycles[-1], generated_reviews)
    else:
        reopened = _latest_materialized_reopen_baseline_payload(records)
    if reopened is not None:
        reopened_fingerprints = _string_tuple(reopened.get("current_fingerprints"))
        if reopened_fingerprints is not None:
            previous_fingerprints = reopened_fingerprints
        reopened_repeat_keys = _reopen_repeat_keys(reopened)
        if reopened_repeat_keys is not None:
            previous_repeat_keys = reopened_repeat_keys
    return {
        "cycle": len(cycles) + 1,
        "repairs_materialized": len(
            _reconciled_materialization_keys(
                records, generated_repairs, generated_reviews
            )
        ),
        "previous_fingerprints": previous_fingerprints,
        "previous_repeat_keys": previous_repeat_keys,
    }


def _generated_review_statuses(
    snapshot: plans.PlanSnapshot,
) -> dict[str, plans.SessionStatus]:
    statuses: dict[str, plans.SessionStatus] = {}
    for session in snapshot.sessions:
        if not session.session_id.is_integer:
            continue
        try:
            body = session.path.read_text(encoding="utf-8")
        except OSError:
            continue
        if review.contains_review_session_marker(body):
            statuses[session.id] = session
    return statuses


def _generated_repair_statuses(
    snapshot: plans.PlanSnapshot,
) -> dict[str, dict[str, plans.SessionStatus]]:
    by_source: dict[str, dict[str, plans.SessionStatus]] = {}
    for session in snapshot.sessions:
        if session.session_id.is_integer:
            continue
        try:
            body = session.path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not review_repair.contains_review_repair_session_marker(body):
            continue
        source_id = str(session.session_id.components[0])
        by_source.setdefault(source_id, {})[session.id] = session
    return by_source


def _parse_session_id_or_none(value: str | None) -> plans.SessionId | None:
    if value is None:
        return None
    try:
        return plans.parse_session_id(value)
    except ValueError:
        return None


def _latest_source(
    cycles: list[Mapping[str, Any]],
    review_statuses: Mapping[str, plans.SessionStatus],
) -> tuple[str | None, Mapping[str, Any] | None]:
    latest_cycle = cycles[-1] if cycles else None
    cycle_source = _cycle_source_id(latest_cycle) if latest_cycle is not None else None
    completed_reviews = [
        status for status in review_statuses.values() if status.status == "complete"
    ]
    latest_generated = (
        max(completed_reviews, key=lambda item: item.session_id.sort_key)
        if completed_reviews
        else None
    )
    if latest_generated is None:
        return cycle_source, latest_cycle
    cycle_id = _parse_session_id_or_none(cycle_source)
    if cycle_id is None:
        explicit_matching = next(
            (
                cycle
                for cycle in reversed(cycles)
                if _cycle_source_id(cycle) == latest_generated.id
            ),
            None,
        )
        if explicit_matching is not None:
            return latest_generated.id, explicit_matching
        review_ids = [
            status.id
            for status in sorted(
                completed_reviews,
                key=lambda item: item.session_id.sort_key,
            )
        ]
        legacy_matching = next(
            (
                cycle
                for cycle_ordinal, cycle in reversed(tuple(enumerate(cycles, start=1)))
                if _parse_session_id_or_none(_cycle_source_id(cycle)) is None
                and _review_id_for_cycle_position(
                    cycle,
                    cycle_ordinal=cycle_ordinal,
                    review_ids=review_ids,
                )
                == latest_generated.id
            ),
            None,
        )
        return latest_generated.id, legacy_matching
    if latest_generated.session_id.sort_key > cycle_id.sort_key:
        matching = next(
            (
                cycle
                for cycle in reversed(cycles)
                if _cycle_source_id(cycle) == latest_generated.id
            ),
            None,
        )
        return latest_generated.id, matching
    return cycle_source, latest_cycle


def _selected_cap_from_events(
    records: tuple[ReviewEventRecord, ...],
) -> int | None:
    cap_keys = (
        "max_review_fix_cycles",
        "selected_max_review_fix_cycles",
        "new_max_review_fix_cycles",
        "after_max_review_fix_cycles",
    )
    for record in reversed(records):
        for key in cap_keys:
            cap = _nonnegative_int(record.payload.get(key))
            if cap is not None:
                return cap
    return None


def _selected_group_attempt_cap_from_history(
    records: tuple[ReviewEventRecord, ...],
    event_path: str | Path,
) -> int | None:
    for record in reversed(records):
        cap = _nonnegative_int(record.payload.get("max_repair_group_attempts"))
        if cap is not None:
            return cap

    state_path = Path(event_path).with_name(events.STATE_FILE_NAME)
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError, OSError, json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    run_config = raw.get("run_config")
    if not isinstance(run_config, dict):
        return None
    return _nonnegative_int(run_config.get("max_repair_group_attempts"))


def _source_digests(
    cycle: Mapping[str, Any] | None,
) -> tuple[str | None, str | None]:
    if cycle is None:
        return None, None
    output_digest = _nonempty_str(cycle.get("review_output_sha256"))
    artifact_digest = _nonempty_str(cycle.get("review_artifact_sha256"))
    return output_digest, artifact_digest


def _eventless_review_disposition(
    plan_dir: Path, source_status: plans.SessionStatus
) -> str | None:
    """Recover a terminal disposition from a completed review's artifacts.

    A completed generated review can become durable before its ``review_cycle``
    event is appended.  In that crash window the persisted, validated verdict
    remains authoritative for an all-clear outcome.  Findings still flow
    through repair planning, which validates their full materializability.
    """
    try:
        body = source_status.path.read_text(encoding="utf-8")
    except OSError:
        return None
    version = review.review_session_marker_version(body)
    verdict = review.load_review_verdict(
        plan_dir,
        require_artifact=(
            version is None or version >= review.STRUCTURED_REVIEW_MIN_VERSION
        ),
    )
    if (
        verdict.get("ok") is True
        and verdict.get("outcome") == review.REVIEW_OUTCOME_ALL_CLEAR
    ):
        return REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
    return None


def _repair_ids_for_source(
    source_id: str,
    records: tuple[ReviewEventRecord, ...],
    generated: Mapping[str, plans.SessionStatus],
) -> tuple[str, ...]:
    ids = set(generated)
    for record in records:
        if record.name == events.REVIEW_CYCLE_EVENT:
            event_source = _cycle_source_id(record.payload)
        else:
            event_source = _reopen_source_id(record.payload)
        if event_source == source_id:
            ids.update(_event_repair_ids(record.payload))

    def sort_key(value: str) -> tuple[int, tuple[int, ...] | str]:
        try:
            return (0, plans.parse_session_id(value).sort_key)
        except ValueError:
            return (1, value)

    return tuple(sorted(ids, key=sort_key))


def _repair_group_records_from_children(
    generated: Mapping[str, plans.SessionStatus],
) -> tuple[Mapping[str, Any], ...] | None:
    """Recover durable repair-group identity from generated child sessions."""
    if not generated:
        return None
    groups: list[Mapping[str, Any]] = []
    for session_id in sorted(
        generated, key=lambda item: plans.parse_session_id(item).sort_key
    ):
        status = generated[session_id]
        try:
            text = status.path.read_text(encoding="utf-8")
        except OSError:
            return None
        repair_group = review_repair.repair_group_from_session_text(text)
        finding_match = re.search(
            r"(?m)^- Addresses [0-9]+ review finding\(s\): (.+)\.$", text
        )
        if repair_group is None or finding_match is None:
            return None
        groups.append(
            {
                "repair_group": repair_group,
                "repair_session_id": session_id,
                "finding_ids": [
                    item.strip()
                    for item in finding_match.group(1).split(",")
                    if item.strip()
                ],
            }
        )
    return tuple(groups)


@dataclass(frozen=True)
class _DurableRepairScopes:
    """One parse pass over the durable repair children of a review session.

    Both the ``repair_groups`` projection fallback and the pending-child write
    scope comparison read this, so neither re-reads the children or the
    manifest registry.
    """

    groups: tuple[Mapping[str, Any], ...]
    registry: workspaces.RepositoryRegistry | None


def _repair_groups_from_children(
    plan_dir: Path,
    generated: Mapping[str, plans.SessionStatus],
) -> _DurableRepairScopes | None:
    """Recover repository attribution from durable generated child sessions."""
    groups = _repair_group_records_from_children(generated)
    if groups is None:
        return None
    registry = plans.read_repository_registry(plan_dir)
    if registry is None:
        return _DurableRepairScopes(groups, None)
    attributed: list[Mapping[str, Any]] = []
    for group in groups:
        repair_session_id = str(group["repair_session_id"])
        status = generated.get(repair_session_id)
        if status is None:
            return None
        try:
            text = status.path.read_text(encoding="utf-8")
            scope = plans.parse_session_scope(text, registry.aliases)
        except OSError, plans.PlanError:
            return None
        entry = dict(group)
        entry["read_repositories"] = list(scope.reads)
        entry["write_repositories"] = list(scope.writes)
        attributed.append(entry)
    return _DurableRepairScopes(tuple(attributed), registry)


def _pending_repair_scope_gaps(
    durable: _DurableRepairScopes | None,
    pending_ids: Sequence[str],
    planning: review_repair.ReviewRepairPlan,
) -> tuple[Mapping[str, Any], ...]:
    """Pending children whose durable write scope is narrower than required.

    This compares record to record: the scope persisted in each pending child
    against the scope its own findings require today. The retained guard is
    deliberately not consulted, because it is derived from that same durable
    scope and therefore agrees with it even when both are too narrow.

    Only strict widening is reported, and only for pending children, so the
    blocker clears as soon as the child is rewritten and can never strand a
    completed one.
    """
    if durable is None or durable.registry is None:
        return ()
    grouped = dict(
        review_repair.group_findings_by_repair_group(list(planning.validated_findings))
    )
    pending = frozenset(pending_ids)
    gaps: list[Mapping[str, Any]] = []
    for record in durable.groups:
        repair_session_id = _nonempty_str(record.get("repair_session_id"))
        repair_group = _nonempty_str(record.get("repair_group"))
        durable_writes = _string_tuple(record.get("write_repositories"))
        if (
            repair_session_id is None
            or repair_session_id not in pending
            or repair_group is None
            or durable_writes is None
        ):
            continue
        group_findings = grouped.get(repair_group)
        if not group_findings:
            continue
        try:
            required = review_repair.repair_group_write_repository_sources(
                repair_group,
                group_findings,
                repository_registry=durable.registry,
            )
        except review_repair.ReviewRepairError:
            # Findings this malformed already surface as their own blocker;
            # staying silent here keeps that diagnosis rather than masking it.
            continue
        persisted = frozenset(durable_writes)
        missing = [alias for alias in required if alias not in persisted]
        if not missing:
            continue
        gaps.append(
            {
                "repair_session_id": repair_session_id,
                "repair_group": repair_group,
                "durable_write_repositories": list(durable_writes),
                "required_write_repositories": list(required),
                "missing_write_repositories": missing,
                "write_repository_sources": dict(required),
            }
        )
    return tuple(gaps)


def _resume(
    *,
    source_cycle: int | None,
    source_id: str | None,
    disposition: str | None,
    action: ReviewResumeAction,
    eligible: bool,
    blocker: str | None,
    materialization_count: int,
    selected_cap: int | None,
    max_repair_group_attempts: int | None = None,
    repair_group_attempts: Mapping[str, int] | None = None,
    repair_ids: tuple[str, ...] = (),
    pending_ids: tuple[str, ...] = (),
    completed_ids: tuple[str, ...] = (),
    source_fingerprints: tuple[str, ...] | None = None,
    source_findings: tuple[Mapping[str, Any], ...] | None = None,
    source_repositories: tuple[Mapping[str, Any], ...] | None = None,
    planning: review_repair.ReviewRepairPlan | None = None,
) -> ReviewResume:
    current_fingerprints: tuple[str, ...] | None = None
    current_repeat_keys: tuple[str, ...] | None = None
    current_output_digest: str | None = None
    current_artifact_digest: str | None = None
    artifact_valid: bool | None = None
    artifact_error: str | None = None
    current_findings: tuple[Mapping[str, Any], ...] | None = None
    current_repositories: tuple[Mapping[str, Any], ...] | None = None
    repair_groups: tuple[Mapping[str, Any], ...] | None = None
    if planning is not None:
        repository_aware = (
            bool(planning.repository_bases)
            or any(group.read_repositories for group in planning.repair_groups)
            or any(
                isinstance(finding.get("repo"), str)
                for finding in planning.validated_findings
            )
        )
        current_fingerprints = (
            planning.finding_fingerprints
            if planning.ok or planning.finding_fingerprints
            else None
        )
        if current_fingerprints is not None:
            current_repeat_keys = (
                review.finding_repeat_keys(list(planning.validated_findings)) or ()
            )
        current_output_digest = planning.review_output_sha256
        current_artifact_digest = planning.review_artifact_sha256
        artifact_valid = planning.ok
        artifact_error = planning.error
        if repository_aware and planning.validated_findings:
            current_findings = tuple(
                dict(finding) for finding in planning.validated_findings
            )
        if planning.repository_bases:
            current_repositories = tuple(
                dict(repository) for repository in planning.repository_bases
            )
        if planning.repair_groups:
            repair_groups = tuple(
                dict(
                    {
                        "repair_group": group.repair_group,
                        "finding_ids": list(group.finding_ids),
                        "repair_session_id": group.proposed_child_id.value,
                    },
                    **(
                        {
                            "read_repositories": list(group.read_repositories),
                            "write_repositories": list(group.write_repositories),
                        }
                        if repository_aware
                        else {}
                    ),
                )
                for group in planning.repair_groups
            )
        if action in {"run-repairs", "rereview"} and planning.existing_child_ids:
            # Existing children are authoritative for these lifecycle states.
            # Planning stops at that expected conflict before it can establish
            # full materializability, so do not present the conflict as proof
            # that the structured artifact itself is invalid.
            artifact_valid = None
            artifact_error = None
    return ReviewResume(
        source_cycle=source_cycle,
        review_session_id=source_id,
        source_disposition=disposition,
        action=action,
        eligible=eligible,
        blocker=blocker,
        historical_materializations=materialization_count,
        minimum_required_cap=materialization_count + 1,
        selected_cap=selected_cap,
        max_repair_group_attempts=max_repair_group_attempts,
        repair_group_attempts=(
            {
                group: repair_group_attempts[group]
                for group in sorted(repair_group_attempts)
            }
            if repair_group_attempts
            else None
        ),
        repair_session_ids=repair_ids,
        pending_repair_session_ids=pending_ids,
        completed_repair_session_ids=completed_ids,
        source_fingerprints=source_fingerprints,
        current_fingerprints=current_fingerprints,
        current_repeat_keys=current_repeat_keys,
        current_review_output_sha256=current_output_digest,
        current_review_artifact_sha256=current_artifact_digest,
        artifact_valid=artifact_valid,
        artifact_error=artifact_error,
        source_findings=source_findings,
        current_findings=current_findings,
        source_repositories=source_repositories,
        current_repositories=current_repositories,
        repair_groups=repair_groups,
    )


def _repair_scope_widenings(review_resume: Mapping[str, Any]) -> tuple[str, ...]:
    """Render one ``<child> needs <aliases>`` fragment per stale repair scope."""
    gaps = review_resume.get("repair_scope_gaps")
    if not isinstance(gaps, Sequence) or isinstance(gaps, str):
        return ()
    fragments: list[str] = []
    for gap in gaps:
        if not isinstance(gap, Mapping):
            continue
        repair_session_id = _nonempty_str(gap.get("repair_session_id"))
        missing = _string_tuple(gap.get("missing_write_repositories"))
        if repair_session_id is None or not missing:
            continue
        fragments.append(f"{repair_session_id} needs {', '.join(missing)}")
    return tuple(fragments)


def review_resume_guidance(
    review_resume: Mapping[str, Any] | None,
    *,
    review_harness_active: bool,
) -> str | None:
    """Return concise operator guidance for a continuation projection.

    ``review_resume`` is a :meth:`ReviewResume.to_payload` mapping (or ``None``
    when the surface carries no projection).  ``review_harness_active`` is True
    when the surfacing command already ran with a review harness, so the
    guidance omits the "resume with a review harness" prerequisite.  This is the
    single renderer shared by the runner, the CLI, and the autopilot helper so
    all three describe the same continuation identically.
    """
    if not isinstance(review_resume, Mapping):
        return None
    if (
        review_resume.get("blocker")
        in {REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED, "higher-cap-required"}
        or review_resume.get("action") == "materialize-repairs"
    ):
        exhausted_guidance = exhausted_repair_group_guidance(review_resume)
        if exhausted_guidance is not None:
            return exhausted_guidance
    needs_review_harness = not review_harness_active
    blocker = review_resume.get("blocker")
    if blocker == "higher-cap-required":
        minimum = review_resume.get("minimum_required_cap")
        if type(minimum) is int:
            requirements = []
            if needs_review_harness:
                requirements.append("a review harness")
            requirements.append(f"--max-review-fix-cycles {minimum} or higher")
            return "resume the same plan with " + " and ".join(requirements)
    if blocker == "artifact-invalid":
        artifact_error = review_resume.get("artifact_error")
        detail = f": {artifact_error}" if artifact_error else ""
        guidance = f"correct the review artifact{detail}"
        if needs_review_harness:
            guidance += ", then resume the same plan with a review harness"
        return guidance
    if blocker == "repair-scope-stale":
        widenings = _repair_scope_widenings(review_resume)
        if widenings:
            return (
                "widen **Writes repositories** in the pending repair children, "
                "then resume the same plan: " + "; ".join(widenings)
            )
    if review_resume.get("action") == "terminal" and blocker:
        if blocker in TERMINAL_REVIEW_DISPOSITIONS:
            return None
        return f"no automatic continuation ({blocker})"
    if review_resume.get("eligible") is True:
        action = review_resume.get("action") or "review"
        minimum = review_resume.get("minimum_required_cap")
        cap_guidance = (
            f" and --max-review-fix-cycles {minimum} or higher"
            if action == "materialize-repairs" and type(minimum) is int
            else ""
        )
        command_guidance = (
            "resume the same plan with a review harness"
            if needs_review_harness
            else "rerun the same review-enabled command"
        )
        return (
            f"{command_guidance}{cap_guidance} "
            f"to run the eligible {action} continuation"
        )
    return None


def _unresolved_materialization_blocker(
    plan_dir: Path, disposition: str | None
) -> str | None:
    """Whether the gate that refused the last materialization still stands.

    Repair children are written to disk before the executability gate judges
    them, so a refused materialization leaves runnable-looking pending sessions
    behind and the continuation would call them eligible while the run is still
    paused on the refusal. Readiness is the resolution signal: the refusal left
    the plan at ``draft``, and clearing it is what re-marks it loop-ready.
    """
    if disposition != REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY:
        return None
    try:
        readiness = plans.read_readiness_metadata(plan_dir)
        if readiness is None or readiness.state != "loop-ready":
            return REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
        if readiness.readiness_sha256 != plans.stable_readiness_sha256(plan_dir):
            return REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
    except plans.PlanError:
        return REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
    return None


def evaluate_review_continuation(
    snapshot_or_plan_dir: plans.PlanSnapshot | str | Path,
    event_path: str | Path,
    *,
    max_repair_group_attempts: int | None = None,
    max_review_fix_cycles: int | None = None,
    review_harness_requested: bool = False,
) -> ReviewResume:
    """Reconcile history, generated lineage, budget, and current artifacts.

    ``max_repair_group_attempts`` and ``max_review_fix_cycles`` are the selected
    caps for a prospective run. Read-only status consumers may omit either; the
    latest review history, then the persisted run_config, are used when
    available. ``review_harness_requested`` only affects the otherwise inert
    ``initial-review`` projection.
    """
    snapshot = (
        snapshot_or_plan_dir
        if isinstance(snapshot_or_plan_dir, plans.PlanSnapshot)
        else plans.inspect_plan(snapshot_or_plan_dir)
    )
    records = read_review_events(event_path)
    cycles = [
        record.payload for record in records if record.name == events.REVIEW_CYCLE_EVENT
    ]
    review_statuses = _generated_review_statuses(snapshot)
    generated_repairs = _generated_repair_statuses(snapshot)

    materialization_keys = _reconciled_materialization_keys(
        records, generated_repairs, review_statuses
    )
    materialization_count = len(materialization_keys)
    selected_cap = (
        max_review_fix_cycles
        if max_review_fix_cycles is not None
        else _selected_cap_from_events(records)
    )
    selected_group_attempt_cap = (
        max_repair_group_attempts
        if max_repair_group_attempts is not None
        else _selected_group_attempt_cap_from_history(records, event_path)
    )

    source_id, source_cycle_record = _latest_source(cycles, review_statuses)
    if source_id is None:
        return _resume(
            source_cycle=None,
            source_id=None,
            disposition=None,
            action="initial-review",
            eligible=review_harness_requested,
            blocker=None if review_harness_requested else "review-harness-required",
            materialization_count=materialization_count,
            selected_cap=selected_cap,
        )

    disposition = (
        _nonempty_str(source_cycle_record.get("disposition"))
        if source_cycle_record is not None
        else None
    )
    recovered_eventless_all_clear = False
    if source_cycle_record is None:
        source_status = review_statuses.get(source_id)
        if source_status is not None:
            disposition = _eventless_review_disposition(
                snapshot.plan_dir, source_status
            )
            recovered_eventless_all_clear = (
                disposition == REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            )
    source_cycle = (
        _nonnegative_int(source_cycle_record.get("cycle"))
        if source_cycle_record is not None
        else None
    )
    source_fingerprints = (
        _string_tuple(source_cycle_record.get("fingerprints"))
        if source_cycle_record is not None
        else None
    )
    source_findings = (
        _mapping_tuple(source_cycle_record.get("findings"))
        if source_cycle_record is not None
        else None
    )
    source_repositories = (
        _mapping_tuple(source_cycle_record.get("repositories"))
        if source_cycle_record is not None
        else None
    )
    source_output_digest, source_artifact_digest = _source_digests(source_cycle_record)
    generated_for_source = generated_repairs.get(source_id, {})
    repair_ids = _repair_ids_for_source(source_id, records, generated_for_source)
    pending_ids = tuple(
        sorted(
            (
                session_id
                for session_id, status in generated_for_source.items()
                if status.status == "pending"
            ),
            key=lambda item: plans.parse_session_id(item).sort_key,
        )
    )
    completed_ids = tuple(
        sorted(
            (
                session_id
                for session_id, status in generated_for_source.items()
                if status.status == "complete"
            ),
            key=lambda item: plans.parse_session_id(item).sort_key,
        )
    )
    repair_attempt_counts = repair_group_attempts(event_path, snapshot)

    @cache
    def durable_scopes() -> _DurableRepairScopes | None:
        return _repair_groups_from_children(snapshot.plan_dir, generated_for_source)

    def result(
        action: ReviewResumeAction,
        eligible: bool,
        blocker: str | None,
        *,
        planning: review_repair.ReviewRepairPlan | None = None,
        scope_gaps: tuple[Mapping[str, Any], ...] = (),
    ) -> ReviewResume:
        resume = _resume(
            source_cycle=source_cycle,
            source_id=source_id,
            disposition=disposition,
            action=action,
            eligible=eligible,
            blocker=blocker,
            materialization_count=materialization_count,
            selected_cap=selected_cap,
            max_repair_group_attempts=selected_group_attempt_cap,
            repair_group_attempts=repair_attempt_counts,
            repair_ids=repair_ids,
            pending_ids=pending_ids,
            completed_ids=completed_ids,
            source_fingerprints=source_fingerprints,
            source_findings=source_findings,
            source_repositories=source_repositories,
            planning=planning,
        )
        if resume.repair_groups is None:
            durable = durable_scopes()
            resume = replace(
                resume,
                repair_groups=None if durable is None else durable.groups,
            )
        return replace(
            resume,
            source_review_output_sha256=source_output_digest,
            source_review_artifact_sha256=source_artifact_digest,
            repair_scope_gaps=scope_gaps or None,
        )

    if disposition in TERMINAL_REVIEW_DISPOSITIONS and not (
        recovered_eventless_all_clear and repair_ids
    ):
        return result("terminal", False, disposition)

    if (
        generated_for_source
        and not pending_ids
        and len(completed_ids) != len(generated_for_source)
    ):
        return result("terminal", False, "repair-sessions-not-runnable")

    if not set(repair_ids).issubset(generated_for_source):
        # Event lineage is the durable record of every repair that must run.
        # A partially deleted child set must fail closed just like a fully
        # deleted one; otherwise the surviving children could incorrectly
        # advance this source to rereview.
        return result("terminal", False, "repair-sessions-missing")
    if not generated_for_source and disposition in MATERIALIZED_REVIEW_DISPOSITIONS:
        return result("terminal", False, "repair-sessions-missing")

    candidate = (
        disposition in BUDGET_REVIEW_DISPOSITIONS
        or disposition == REVIEW_CYCLE_DISPOSITION_MALFORMED
        or disposition is None
    )
    if not generated_for_source and not candidate:
        # Preserve an unfamiliar recorded value in ``source_disposition`` for
        # diagnostics, but keep the public blocker vocabulary stable for
        # operator-facing continuations.
        return result("terminal", False, "unknown-disposition")

    try:
        planning = review_repair.plan_review_repair_sessions(
            snapshot.plan_dir, review_session_id=source_id
        )
    except plans.PlanError as error:
        planning = review_repair.ReviewRepairPlan(
            ok=False,
            reason=review_repair.REVIEW_REPAIR_REASON_MALFORMED,
            review_session_id=_parse_session_id_or_none(source_id),
            error=f"could not inspect review repair inputs: {error}",
        )

    if pending_ids:
        blocker = _unresolved_materialization_blocker(snapshot.plan_dir, disposition)
        if blocker is not None:
            return result("run-repairs", False, blocker, planning=planning)
        standing_pause = _standing_environment_pause_blocker(event_path)
        if standing_pause is not None:
            return result("run-repairs", False, standing_pause, planning=planning)
        scope_gaps = _pending_repair_scope_gaps(durable_scopes(), pending_ids, planning)
        if scope_gaps:
            return result(
                "run-repairs",
                False,
                "repair-scope-stale",
                planning=planning,
                scope_gaps=scope_gaps,
            )
        return result("run-repairs", True, None, planning=planning)
    if generated_for_source and len(completed_ids) == len(generated_for_source):
        return result("rereview", True, None, planning=planning)
    if (
        selected_group_attempt_cap is not None
        and planning.ok
        and planning.reason == review_repair.REVIEW_REPAIR_REASON_OK
        and any(
            repair_attempt_counts.get(group.repair_group, 0)
            >= selected_group_attempt_cap
            for group in planning.repair_groups
        )
    ):
        return result(
            "terminal",
            False,
            REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            planning=planning,
        )

    if disposition is None and not generated_for_source and planning.ok:
        cycle_state = review_cycle_state(event_path, snapshot)
        repeats = repeated_finding_positions(
            fingerprints=planning.finding_fingerprints,
            repeat_keys=(
                review.finding_repeat_keys(list(planning.validated_findings)) or ()
            ),
            previous_fingerprints=cycle_state["previous_fingerprints"],
            previous_repeat_keys=cycle_state["previous_repeat_keys"],
        )
    else:
        repeats = ()
    if repeats:
        # A completed rereview can survive a crash before its review_cycle
        # append.  Its repair plan must still honor the same repeated-finding
        # breaker as an uninterrupted _advance_review_cycle invocation.
        return result(
            "terminal",
            False,
            REVIEW_CYCLE_DISPOSITION_REPEATED,
            planning=planning,
        )

    minimum_cap = materialization_count + 1
    if disposition in BUDGET_REVIEW_DISPOSITIONS and (
        selected_cap is None or selected_cap < minimum_cap
    ):
        return result(
            "materialize-repairs",
            False,
            "higher-cap-required",
            planning=planning,
        )

    if not planning.ok or planning.reason != review_repair.REVIEW_REPAIR_REASON_OK:
        error = planning.error
        if planning.reason == review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS:
            error = (
                "the completed findings review no longer has repair findings; "
                "a manually changed all-clear artifact cannot finalize it"
            )
        resume = result(
            "materialize-repairs",
            False,
            "artifact-invalid",
            planning=planning,
        )
        return replace(
            resume,
            artifact_valid=(
                False
                if planning.reason == review_repair.REVIEW_REPAIR_REASON_NO_FINDINGS
                else resume.artifact_valid
            ),
            artifact_error=error,
        )

    if selected_cap is None or selected_cap < minimum_cap:
        return result(
            "materialize-repairs",
            False,
            "higher-cap-required",
            planning=planning,
        )
    return result("materialize-repairs", True, None, planning=planning)
