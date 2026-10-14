"""Per-kind payload validators: requests, observations, details, operations, receipts.

Part of :mod:`asl_workbench.transport`, which re-exports everything here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .catalog import (
    ACTIVITY_LABELS,
    COUNT_KEYS,
    DISCOVERY_SOURCES,
    LAST_ACTIVITY_SOURCES,
    PRIMARY_STATES,
    RECOMMENDED_ACTIONS,
    URGENCY_GROUPS,
)
from .details import (
    ARTIFACT_NAMES,
    DETAIL_KINDS,
)
from .history import (
    HISTORY_OUTCOMES,
    REVIEW_OUTCOMES,
)
from .observation import (
    COMPLETENESS_LABELS,
    MAX_WARNINGS,
)
from .operations import (
    MAX_OPERATION_ID_CHARS,
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    OPERATION_DETAIL_CODES,
    OPERATION_METHODS,
)
from .transport_checks import (
    _SESSION_ID,
    _count,
    _fail,
    _flag,
    _identifier,
    _key,
    _mapping,
    _number,
    _optional,
    _sequence,
    _text,
    _validate_target,
    _warnings,
    _word,
    _word_matching,
    check_shape,
)
from .transport_shapes import (
    CATALOG_ENTRY_SHAPE,
    DETAIL_SHAPE,
    HISTORY_ENTRY_SHAPE,
    OBSERVATION_SHAPES,
    OPERATION_SHAPE,
    RECEIPT_SHAPE,
    REQUEST_SHAPES,
    RUN_SHAPE,
    _Shape,
)
from .transport_vocabulary import (
    CONNECTOR_REPORTED_STATUSES,
    DETAIL_REFUSAL_REASONS,
    ERROR_FORBIDDEN_METHOD,
    ERROR_FRAME_TOO_LARGE,
    ERROR_INVALID_FIELD,
    ERROR_MISSING_FIELD,
    ERROR_WRONG_TARGET,
    MAX_CATALOG_ENTRIES_PER_PAGE,
    MAX_COMPLETED_SESSION_IDS,
    MAX_DETAIL_BYTES,
    MAX_DETAIL_ENTRIES,
    MAX_ENTRIES_PER_BATCH,
    MAX_RUNS_PER_SNAPSHOT,
    OBSERVATION_VIEWS,
    PAUSE_REASON_CODES,
    PHASES,
    RECEIPT_DELIVERED,
    RECEIPT_DETAIL_CODES,
    RECEIPT_OUTCOMES,
    REQUEST_DETAIL,
    REQUEST_HISTORY,
    REQUEST_METHODS,
    REQUEST_MUTATE,
    STREAM_NAMES,
    SUMMARY_OUTCOMES,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
)


def _validate_request(payload: Mapping[str, Any], where: str) -> str:
    request = _word(payload.get("request"), f"{where}.request", REQUEST_METHODS)
    check_shape(payload, REQUEST_SHAPES[request], where)
    _optional(payload, "target", where, _validate_target)
    _optional(payload, "reason", where, _text)
    _optional(payload, "generation", where, _identifier)
    _optional(payload, "offset", where, _count)
    _optional(payload, "limit", where, lambda v, at: _count(v, at, minimum=1))
    _optional(payload, "max_bytes", where, lambda v, at: _count(v, at, minimum=1))
    _optional(payload, "window_start", where, _number)
    _optional(payload, "window_end", where, _number)
    if request == REQUEST_HISTORY:
        start = payload.get("window_start")
        end = payload.get("window_end")
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and float(end) < float(start)
        ):
            raise _fail(ERROR_INVALID_FIELD, where, "window ends before it starts")
    if request == REQUEST_DETAIL:
        detail = _word(payload["detail"], f"{where}.detail", DETAIL_KINDS)
        target = payload["target"]
        if detail == "archive":
            if target.get("archive") != "list":
                raise _fail(
                    ERROR_INVALID_FIELD,
                    f"{where}.target",
                    "archive read requires archive=list",
                )
        elif "plan_key" not in target:
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.target", "plan read requires plan_key"
            )
        _optional(
            payload, "artifact", where, lambda v, at: _word(v, at, ARTIFACT_NAMES)
        )
        _optional(payload, "stream", where, lambda v, at: _word(v, at, STREAM_NAMES))
        max_bytes = payload.get("max_bytes")
        if isinstance(max_bytes, int) and max_bytes > MAX_DETAIL_BYTES:
            raise _fail(
                ERROR_FRAME_TOO_LARGE,
                f"{where}.max_bytes",
                f"above {MAX_DETAIL_BYTES}",
            )
    if request == REQUEST_MUTATE:
        _identifier(payload["operation_id"], f"{where}.operation_id")
        _text(
            payload["operation_id"],
            f"{where}.operation_id",
            limit=MAX_OPERATION_ID_CHARS,
        )
        method = payload["method"]
        if method not in OPERATION_METHODS:
            raise _fail(ERROR_FORBIDDEN_METHOD, f"{where}.method", str(method))
        target = payload["target"]
        if method in (METHOD_ARCHIVE_RESTORE, METHOD_ARCHIVE_DELETE):
            if "archive_id" not in target:
                raise _fail(
                    ERROR_WRONG_TARGET, f"{where}.target", "archive_id required"
                )
        elif method == METHOD_ARCHIVE_SETTINGS:
            if target.get("archive") != "settings":
                raise _fail(
                    ERROR_WRONG_TARGET, f"{where}.target", "archive=settings required"
                )
        elif "plan_key" not in target:
            raise _fail(ERROR_WRONG_TARGET, f"{where}.target", "plan_key required")
        days = payload.get("archive_retention_days")
        if method == METHOD_ARCHIVE_SETTINGS:
            _count(days, f"{where}.archive_retention_days", minimum=1)
        elif days is not None:
            raise _fail(
                ERROR_INVALID_FIELD,
                f"{where}.archive_retention_days",
                "only archive settings may carry this",
            )
        _optional(payload, "action_token", where, _identifier)
        _optional(payload, "reply_ref", where, _identifier)
        _optional(payload, "bridge_token", where, _identifier)
        _optional(payload, "harness", where, _identifier)
    return request


def _validate_run(value: Any, where: str) -> None:
    run = _mapping(value, where)
    check_shape(run, RUN_SHAPE, where)
    _key(run["run_key"], f"{where}.run_key")
    _key(run["plan_key"], f"{where}.plan_key")
    _text(run["project"], f"{where}.project")
    _identifier(run["run_id"], f"{where}.run_id")
    _word(run["primary_state"], f"{where}.primary_state", PRIMARY_STATES)
    _number(run["last_activity_at"], f"{where}.last_activity_at")
    if run["phase"] is not None:
        _word(run["phase"], f"{where}.phase", PHASES)
    if run["urgency_group"] is not None:
        _word(run["urgency_group"], f"{where}.urgency_group", URGENCY_GROUPS)
    actions = _sequence(
        run["actions"], f"{where}.actions", limit=len(OPERATION_METHODS)
    )
    for index, action in enumerate(actions):
        if action not in OPERATION_METHODS:
            raise _fail(
                ERROR_FORBIDDEN_METHOD, f"{where}.actions[{index}]", str(action)
            )
    _optional(run, "action_token", where, _identifier)
    _optional(
        run, "session_id", where, lambda v, at: _word_matching(v, at, _SESSION_ID)
    )
    _optional(run, "harness", where, _identifier)
    _optional(run, "question_id", where, _identifier)
    _optional(run, "question_asked_at", where, _number)
    _optional(run, "unanswered_count", where, _count)
    _optional(run, "run_started_at", where, _number)
    _optional(run, "run_finished_at", where, _number)
    _optional(
        run, "pause_reason", where, lambda v, at: _word(v, at, PAUSE_REASON_CODES)
    )
    _optional(run, "outcome", where, lambda v, at: _word(v, at, SUMMARY_OUTCOMES))
    _optional(run, "warnings", where, _warnings)


def _validate_history_entry(value: Any, where: str) -> None:
    entry = _mapping(value, where)
    check_shape(entry, HISTORY_ENTRY_SHAPE, where)
    _identifier(entry["entry_id"], f"{where}.entry_id")
    _identifier(entry["source_key"], f"{where}.source_key")
    _key(entry["plan_key"], f"{where}.plan_key")
    _key(entry["run_key"], f"{where}.run_key")
    _identifier(entry["run_id"], f"{where}.run_id")
    _word(entry["outcome"], f"{where}.outcome", HISTORY_OUTCOMES)
    _number(entry["recorded_at"], f"{where}.recorded_at")
    _optional(entry, "project", where, _text)
    _optional(
        entry,
        "projects",
        where,
        lambda v, at: [
            _text(name, f"{at}[{index}]")
            for index, name in enumerate(_sequence(v, at, limit=MAX_WARNINGS))
        ],
    )
    _optional(entry, "harness", where, _identifier)
    _optional(entry, "model", where, _text)
    _optional(
        entry, "pause_reason", where, lambda v, at: _word(v, at, PAUSE_REASON_CODES)
    )
    _optional(entry, "run_started_at", where, _number)
    _optional(entry, "run_finished_at", where, _number)
    _optional(entry, "duration_seconds", where, lambda v, at: _number(v, at, minimum=0))
    _optional(entry, "turn_count", where, _count)
    _optional(entry, "completed_session_count", where, _count)
    _optional(entry, "pending_session_count", where, _count)
    _optional(
        entry,
        "completed_session_ids",
        where,
        lambda v, at: [
            _word_matching(value, f"{at}[{index}]", _SESSION_ID)
            for index, value in enumerate(
                _sequence(v, at, limit=MAX_COMPLETED_SESSION_IDS)
            )
        ],
    )
    _optional(entry, "review_completed", where, _flag)
    _optional(
        entry, "review_outcome", where, lambda v, at: _word(v, at, REVIEW_OUTCOMES)
    )
    _optional(entry, "reconstructed", where, _flag)
    _optional(entry, "event_schema_version", where, _count)


def _validate_catalog_entry(value: Any, where: str) -> None:
    entry = _mapping(value, where)
    check_shape(entry, CATALOG_ENTRY_SHAPE, where)
    _key(entry["plan_key"], f"{where}.plan_key")
    _text(entry["title"], f"{where}.title")
    _text(entry["display_label"], f"{where}.display_label")
    _word(entry["primary_state"], f"{where}.primary_state", PRIMARY_STATES)
    _word(
        entry["recommended_action"], f"{where}.recommended_action", RECOMMENDED_ACTIONS
    )
    _count(entry["urgency_rank"], f"{where}.urgency_rank")
    _word(entry["urgency_group"], f"{where}.urgency_group", URGENCY_GROUPS)
    _word(entry["activity_label"], f"{where}.activity_label", ACTIVITY_LABELS)
    _flag(entry["exists"], f"{where}.exists")
    counts = _mapping(entry["counts"], f"{where}.counts")
    check_shape(counts, _Shape(required=frozenset(COUNT_KEYS)), f"{where}.counts")
    for name, total in counts.items():
        _count(total, f"{where}.counts.{name}")
    sources = _sequence(
        entry["sources"], f"{where}.sources", limit=len(DISCOVERY_SOURCES)
    )
    for index, source in enumerate(sources):
        _word(source, f"{where}.sources[{index}]", DISCOVERY_SOURCES)
    _optional(entry, "run_key", where, _key)
    _optional(
        entry,
        "projects",
        where,
        lambda v, at: [
            _text(name, f"{at}[{index}]")
            for index, name in enumerate(_sequence(v, at, limit=MAX_WARNINGS))
        ],
    )
    _optional(entry, "project_group", where, _text)
    _optional(entry, "pinned", where, _flag)
    _optional(
        entry,
        "next_pending_session_id",
        where,
        lambda v, at: _word_matching(v, at, _SESSION_ID),
    )
    _optional(entry, "last_activity_at", where, _text)
    _optional(
        entry,
        "last_activity_source",
        where,
        lambda v, at: _word(v, at, LAST_ACTIVITY_SOURCES),
    )
    _optional(entry, "completed_at", where, _text)
    _optional(
        entry, "pause_reason", where, lambda v, at: _word(v, at, PAUSE_REASON_CODES)
    )
    _optional(entry, "harness", where, _identifier)
    _optional(entry, "registry_digest", where, _identifier)
    _optional(entry, "repository_count", where, _count)
    _optional(entry, "multi_repository", where, _flag)
    _optional(entry, "warning_count", where, _count)
    _optional(entry, "search_text", where, lambda v, at: _text(v, at, limit=4096))


def _validate_observation(payload: Mapping[str, Any], where: str) -> str:
    view = _word(payload.get("view"), f"{where}.view", OBSERVATION_VIEWS)
    check_shape(payload, OBSERVATION_SHAPES[view], where)
    _optional(payload, "warnings", where, _warnings)
    _optional(payload, "contract_set_id", where, _identifier)
    _optional(payload, "source_generation", where, _count)
    _optional(payload, "projection_age_seconds", where, lambda v, at: _number(v, at))
    if view == VIEW_HEARTBEAT:
        _number(payload["observed_at"], f"{where}.observed_at")
        _flag(payload["bridge_alive"], f"{where}.bridge_alive")
        _optional(payload, "bridge_protocol", where, _count)
        _optional(payload, "asl_version", where, _identifier)
        _optional(payload, "degraded_reason", where, _text)
    elif view == VIEW_SNAPSHOT:
        _number(payload["observed_at"], f"{where}.observed_at")
        _identifier(payload["digest"], f"{where}.digest")
        row_count = _count(payload["row_count"], f"{where}.row_count")
        _flag(payload["truncated"], f"{where}.truncated")
        runs = _sequence(payload["runs"], f"{where}.runs", limit=MAX_RUNS_PER_SNAPSHOT)
        for index, run in enumerate(runs):
            _validate_run(run, f"{where}.runs[{index}]")
        if row_count != len(runs):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.row_count", "disagrees with runs"
            )
        _optional(payload, "source_digest", where, _identifier)
        _optional(payload, "failed_projections", where, _count)
        _optional(
            payload,
            "staleness_bound_seconds",
            where,
            lambda v, at: _number(v, at, minimum=0),
        )
    elif view == VIEW_HISTORY_BATCH:
        _identifier(payload["batch_id"], f"{where}.batch_id")
        _number(payload["generated_at"], f"{where}.generated_at")
        _flag(payload["complete"], f"{where}.complete")
        entries = _sequence(
            payload["entries"], f"{where}.entries", limit=MAX_ENTRIES_PER_BATCH
        )
        for index, entry in enumerate(entries):
            _validate_history_entry(entry, f"{where}.entries[{index}]")
        if _count(payload["entry_count"], f"{where}.entry_count") != len(entries):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.entry_count", "disagrees with entries"
            )
        _optional(payload, "window_start", where, _number)
        _optional(payload, "window_end", where, _number)
        _optional(payload, "more", where, _flag)
    else:
        _number(payload["observed_at"], f"{where}.observed_at")
        _identifier(payload["generation"], f"{where}.generation")
        complete = _flag(payload["complete"], f"{where}.complete")
        final = _flag(payload["final"], f"{where}.final")
        _count(payload["offset"], f"{where}.offset")
        entries = _sequence(
            payload["entries"],
            f"{where}.entries",
            limit=MAX_CATALOG_ENTRIES_PER_PAGE,
        )
        for index, entry in enumerate(entries):
            _validate_catalog_entry(entry, f"{where}.entries[{index}]")
        if _count(payload["entry_count"], f"{where}.entry_count") != len(entries):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.entry_count", "disagrees with entries"
            )
        _optional(payload, "total", where, _count)
        # A page that claims to close a generation has to claim the generation
        # was whole: retirement is decided from those two together, and
        # ``final`` alone would retire rows a partial publication never saw.
        if final and not complete:
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.final", "final page is not complete"
            )
    return view


def _validate_detail(payload: Mapping[str, Any], where: str) -> str:
    check_shape(payload, DETAIL_SHAPE, where)
    detail = _word(payload["detail"], f"{where}.detail", DETAIL_KINDS)
    _validate_target(payload["target"], f"{where}.target")
    if detail == "archive":
        if payload["target"].get("archive") != "list":
            raise _fail(
                ERROR_INVALID_FIELD,
                f"{where}.target",
                "archive answer requires archive=list",
            )
    elif "plan_key" not in payload["target"]:
        raise _fail(
            ERROR_INVALID_FIELD, f"{where}.target", "plan answer requires plan_key"
        )
    _number(payload["observed_at"], f"{where}.observed_at")
    _word(payload["complete"], f"{where}.complete", COMPLETENESS_LABELS)
    _optional(payload, "artifact", where, lambda v, at: _word(v, at, ARTIFACT_NAMES))
    _optional(payload, "stream", where, lambda v, at: _word(v, at, STREAM_NAMES))
    _optional(
        payload, "encoding", where, lambda v, at: _word(v, at, ("utf-8", "base64"))
    )
    _optional(payload, "truncated", where, _flag)
    _optional(payload, "offset", where, _count)
    _optional(payload, "next_offset", where, _count)
    _optional(payload, "total", where, _count)
    _optional(
        payload, "reason", where, lambda v, at: _word(v, at, DETAIL_REFUSAL_REASONS)
    )
    _optional(payload, "warnings", where, _warnings)
    content = payload.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise _fail(ERROR_INVALID_FIELD, f"{where}.content", "expected a string")
        measured = len(content.encode("utf-8", "surrogatepass"))
        if measured > MAX_DETAIL_BYTES:
            raise _fail(
                ERROR_FRAME_TOO_LARGE,
                f"{where}.content",
                f"{measured} bytes above {MAX_DETAIL_BYTES}",
            )
        declared = payload.get("content_bytes")
        if (
            declared is not None
            and _count(declared, f"{where}.content_bytes") != measured
        ):
            raise _fail(
                ERROR_INVALID_FIELD,
                f"{where}.content_bytes",
                "disagrees with content",
            )
    elif "content_bytes" in payload and payload["content_bytes"] is not None:
        raise _fail(ERROR_INVALID_FIELD, f"{where}.content_bytes", "no content")
    entries = payload.get("entries")
    if entries is not None:
        listed = _sequence(entries, f"{where}.entries", limit=MAX_DETAIL_ENTRIES)
        for index, entry in enumerate(listed):
            _mapping(entry, f"{where}.entries[{index}]")
        declared_count = payload.get("entry_count")
        if declared_count is not None and _count(
            declared_count, f"{where}.entry_count"
        ) != len(listed):
            raise _fail(
                ERROR_INVALID_FIELD, f"{where}.entry_count", "disagrees with entries"
            )
    elif "entry_count" in payload and payload["entry_count"] is not None:
        raise _fail(ERROR_INVALID_FIELD, f"{where}.entry_count", "no entries")
    return detail


def _validate_operation(payload: Mapping[str, Any], where: str) -> None:
    check_shape(payload, OPERATION_SHAPE, where)
    _identifier(payload["operation_id"], f"{where}.operation_id")
    _text(
        payload["operation_id"], f"{where}.operation_id", limit=MAX_OPERATION_ID_CHARS
    )
    _word(payload["status"], f"{where}.status", CONNECTOR_REPORTED_STATUSES)
    _number(payload["observed_at"], f"{where}.observed_at")
    _optional(payload, "target", where, _validate_target)
    _optional(payload, "dispatched_at", where, _number)
    _optional(payload, "settled_at", where, _number)
    _optional(payload, "deliver_by", where, _number)
    _optional(
        payload,
        "detail_code",
        where,
        lambda v, at: _word(v, at, OPERATION_DETAIL_CODES),
    )
    method = payload.get("method")
    if method is not None and method not in OPERATION_METHODS:
        raise _fail(ERROR_FORBIDDEN_METHOD, f"{where}.method", str(method))


def _validate_receipt(payload: Mapping[str, Any], where: str) -> None:
    check_shape(payload, RECEIPT_SHAPE, where)
    outcome = _word(payload["outcome"], f"{where}.outcome", RECEIPT_OUTCOMES)
    _number(payload["observed_at"], f"{where}.observed_at")
    _optional(payload, "operation_id", where, _identifier)
    _optional(
        payload, "complete", where, lambda v, at: _word(v, at, COMPLETENESS_LABELS)
    )
    detail_code = payload.get("detail_code")
    if detail_code is not None:
        _word(detail_code, f"{where}.detail_code", RECEIPT_DETAIL_CODES)
    # Anything other than delivery has to say why. "It did not work" without a
    # reason is the receipt that leaves an owner unable to decide what to do.
    if outcome != RECEIPT_DELIVERED and detail_code is None:
        raise _fail(ERROR_MISSING_FIELD, f"{where}.detail_code", outcome)


__all__ = []
