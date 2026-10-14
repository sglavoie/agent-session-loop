"""Review cycle state and durable repair-scope reconstruction."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_errors,
    plans_models,
    plans_registry,
    plans_session_ids,
    plans_session_scope,
    plans_snapshot,
    review,
    review_repair,
    workspaces,
)
from .review_continuation_events import (
    _reopen_repeat_keys,
    read_review_events,
)
from .review_continuation_groups import (
    _cycle_source_id,
    _event_repair_ids,
    _generated_repair_statuses,
    _generated_review_statuses,
    _reconciled_materialization_keys,
    _reopen_source_id,
    _repair_group_records_from_children,
    _review_id_for_cycle_position,
)
from .review_continuation_models import (
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    ReviewEventRecord,
    _nonempty_str,
    _nonnegative_int,
    _string_tuple,
)


def _post_reopen_baseline_payload(
    records: tuple[ReviewEventRecord, ...],
    latest_cycle: Mapping[str, Any],
    generated_reviews: Mapping[str, plans_models.SessionStatus] | None = None,
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
    positional_source_id: plans_session_ids.SessionId | None = None
    unresolved_source_reopen: (
        tuple[plans_session_ids.SessionId, Mapping[str, Any]] | None
    ) = None
    if source_id_text is None and generated_reviews:
        review_ids = sorted(
            generated_reviews,
            key=lambda item: plans_session_ids.parse_session_id(item).sort_key,
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

    latest_source_reopen: (
        tuple[plans_session_ids.SessionId, Mapping[str, Any]] | None
    ) = None
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
    snapshot_or_plan_dir: plans_models.PlanSnapshot | str | Path | None = None,
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


def _parse_session_id_or_none(value: str | None) -> plans_session_ids.SessionId | None:
    if value is None:
        return None
    try:
        return plans_session_ids.parse_session_id(value)
    except ValueError:
        return None


def _latest_source(
    cycles: list[Mapping[str, Any]],
    review_statuses: Mapping[str, plans_models.SessionStatus],
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
    plan_dir: Path, source_status: plans_models.SessionStatus
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
    generated: Mapping[str, plans_models.SessionStatus],
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
            return (0, plans_session_ids.parse_session_id(value).sort_key)
        except ValueError:
            return (1, value)

    return tuple(sorted(ids, key=sort_key))


def _resplit_repair_children(
    snapshot: plans_models.PlanSnapshot,
    repair_ids: Sequence[str],
    generated: Mapping[str, plans_models.SessionStatus],
) -> tuple[dict[str, plans_models.SessionStatus], frozenset[str]]:
    """Descendants standing in for a repair child the plan no longer holds.

    A repair session too large to run in one turn is resplit: the parent is
    archived out of the plan and nested children take its place. Event lineage
    still names the parent, and the children are authored fresh, so they need
    not carry the repair marker their parent had. Read literally, that lineage
    describes a child set the plan lost, which is what
    ``repair-sessions-missing`` is for -- and a plan whose every repair had in
    fact completed was reported terminal instead of being sent to the rereview
    its findings still required (ASI-146).

    The reconciliation stays fail-closed. A parent still present in the plan is
    never treated as superseded, whatever it looks like inside: a repair child
    that lost its own marker is a different failure and keeps the blocker it
    already had. Only a parent the plan no longer holds at all, and only when
    the plan holds sessions nested strictly beneath it, hands its place to
    those descendants -- which then have to satisfy the same completion
    accounting the parent would have.

    Returns the descendants to account for and the parent ids they stand in
    for; the parent ids stay in ``repair_session_ids`` because event lineage is
    what the projection reports, not what it re-derives.
    """
    present = {session.id for session in snapshot.sessions}
    children: dict[str, plans_models.SessionStatus] = {}
    superseded: set[str] = set()
    for repair_id in repair_ids:
        if repair_id in generated or repair_id in present:
            continue
        parsed = _parse_session_id_or_none(repair_id)
        if parsed is None:
            continue
        prefix = parsed.components
        descendants = {
            session.id: session
            for session in snapshot.sessions
            if session.session_id.components[: len(prefix)] == prefix
            and len(session.session_id.components) > len(prefix)
        }
        if not descendants:
            continue
        children.update(descendants)
        superseded.add(repair_id)
    return children, frozenset(superseded)


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
    generated: Mapping[str, plans_models.SessionStatus],
) -> _DurableRepairScopes | None:
    """Recover repository attribution from durable generated child sessions."""
    groups = _repair_group_records_from_children(generated)
    if groups is None:
        return None
    registry = plans_registry.read_repository_registry(plan_dir)
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
            scope = plans_session_scope.parse_session_scope(text, registry.aliases)
        except OSError, plans_errors.PlanError:
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
