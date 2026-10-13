"""Size-cap shedding that bounds the audit evidence bundle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import assurance_models_primitives
from .supervisor_audit_evidence_collector import _Collector
from .supervisor_audit_evidence_constants import (
    _LAST_SHED_EVENT_NAMES,
    _SHEDDABLE_MAILBOX_KINDS,
    MIN_PREVIEW_BYTES,
    REASON_SIZE_CAP,
    RECENT_TURN_COUNT,
)


def _shed(
    payload: dict[str, Any],
    collector: _Collector,
    *,
    max_bytes: int,
) -> None:
    """Shrink the bundle to ``max_bytes``, giving up the least useful evidence first.

    Ordering is the whole point: heartbeat chatter goes before ordinary
    records, ordinary records before old healthy turns, and the terminal,
    failing, and guard evidence goes last, so a bundle that had to shed
    something still explains how the run ended.
    """
    records: list[dict[str, Any]] = payload["events"]["records"]
    messages: list[dict[str, Any]] = payload["mailbox"]["records"]
    turns: list[dict[str, Any]] = payload["turns"]
    protected = {
        id(turn)
        for turn in turns[-RECENT_TURN_COUNT:]
        + [t for t in turns if t.get("anomalous")]
    }

    def size() -> int:
        payload["omissions"] = collector.omissions.to_list()
        return len(assurance_models_primitives.canonical_json_bytes(payload))

    def drop_events(priority: int, batch: int) -> bool:
        candidates = [
            index
            for index, record in enumerate(records)
            if record.get("priority") == priority
        ]
        # Within a tier, the terminal and rejection records go last.
        candidates.sort(key=lambda i: records[i].get("name") in _LAST_SHED_EVENT_NAMES)
        doomed = sorted(candidates[:batch])
        for index in reversed(doomed):
            collector.omissions.record("events", REASON_SIZE_CAP, records[index])
            del records[index]
        return bool(doomed)

    def drop_mailbox(batch: int) -> bool:
        doomed = [
            index
            for index, message in enumerate(messages)
            if message.get("kind") in _SHEDDABLE_MAILBOX_KINDS
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record("mailbox", REASON_SIZE_CAP, messages[index])
            del messages[index]
        return bool(doomed)

    def drop_turn(only_unprotected: bool, batch: int) -> bool:
        doomed = [
            index
            for index, turn in enumerate(turns)
            if not (only_unprotected and id(turn) in protected)
        ][:batch]
        for index in reversed(doomed):
            collector.omissions.record(
                "turns", REASON_SIZE_CAP, turns[index].get("name")
            )
            del turns[index]
        return bool(doomed)

    steps: list[Callable[[int], bool]] = [
        lambda batch: collector.shrink_previews(
            MIN_PREVIEW_BYTES,
            only=[
                preview
                for turn in turns
                if id(turn) not in protected
                for preview in _turn_previews(turn)
            ],
        ),
        lambda batch: drop_events(2, batch),
        drop_mailbox,
        lambda batch: drop_events(1, batch),
        lambda batch: drop_turn(True, batch),
        lambda batch: collector.shrink_previews(MIN_PREVIEW_BYTES),
        lambda batch: collector.shrink_previews(0),
        lambda batch: drop_events(0, batch),
        lambda batch: drop_turn(False, batch),
    ]
    # Batches grow geometrically so a log with thousands of records is bounded
    # in a logarithmic number of serializations rather than one per record.
    for step in steps:
        batch = 1
        while size() > max_bytes:
            if not step(batch):
                break
            batch = min(batch * 2, 4096)
        if size() <= max_bytes:
            return


def _turn_previews(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    logs = turn.get("logs")
    if isinstance(logs, Mapping):
        found.extend(item for item in logs.values() if isinstance(item, dict))
    last_message = turn.get("last_message")
    if isinstance(last_message, dict):
        found.append(last_message)
    return found
