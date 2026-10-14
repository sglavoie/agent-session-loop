"""Repair-group attempts and materialization-key reconciliation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_models,
    plans_session_ids,
    plans_snapshot,
    review,
    review_repair,
    session_overview,
)
from .review_continuation_events import (
    read_review_events,
)
from .review_continuation_models import (
    REVIEW_CYCLE_DISPOSITION_REPAIRS,
    ReviewEventRecord,
    _nonempty_str,
    _nonnegative_int,
    _string_tuple,
)


def _source_id_from_repair_ids(payload: Mapping[str, Any]) -> str | None:
    source_components: set[int] = set()
    for repair_id in _event_repair_ids(payload):
        try:
            parsed = plans_session_ids.parse_session_id(repair_id)
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
    snapshot_or_plan_dir: plans_models.PlanSnapshot | str | Path | None = None,
) -> dict[str, int]:
    """Return per-group repair attempt counts from events and durable children."""

    records = read_review_events(event_path)
    snapshot = (
        snapshot_or_plan_dir
        if isinstance(snapshot_or_plan_dir, plans_models.PlanSnapshot)
        else (
            plans_snapshot.inspect_plan(snapshot_or_plan_dir)
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
    generated_repairs: Mapping[str, Mapping[str, plans_models.SessionStatus]]
    | None = None,
    generated_reviews: Mapping[str, plans_models.SessionStatus] | None = None,
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
                key=lambda item: plans_session_ids.parse_session_id(item).sort_key,
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
    generated_repairs: Mapping[str, Mapping[str, plans_models.SessionStatus]]
    | None = None,
    generated_reviews: Mapping[str, plans_models.SessionStatus] | None = None,
) -> set[str]:
    """Return unique materializations across events and durable children."""

    return set(
        _materialization_key_times(records, generated_repairs, generated_reviews)
    )


def reconciled_materialization_keys(
    event_path: str | Path,
    snapshot_or_plan_dir: plans_models.PlanSnapshot | str | Path | None = None,
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
        if isinstance(snapshot_or_plan_dir, plans_models.PlanSnapshot)
        else (
            plans_snapshot.inspect_plan(snapshot_or_plan_dir)
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


def _generated_review_statuses(
    snapshot: plans_models.PlanSnapshot,
) -> dict[str, plans_models.SessionStatus]:
    statuses: dict[str, plans_models.SessionStatus] = {}
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
    snapshot: plans_models.PlanSnapshot,
) -> dict[str, dict[str, plans_models.SessionStatus]]:
    by_source: dict[str, dict[str, plans_models.SessionStatus]] = {}
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


def _repair_group_records_from_children(
    generated: Mapping[str, plans_models.SessionStatus],
) -> tuple[Mapping[str, Any], ...] | None:
    """Recover durable repair-group identity from generated child sessions."""
    if not generated:
        return None
    groups: list[Mapping[str, Any]] = []
    for session_id in sorted(
        generated, key=lambda item: plans_session_ids.parse_session_id(item).sort_key
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
