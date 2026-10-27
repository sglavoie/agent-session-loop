"""Event log and mailbox readers for the audit evidence bundle."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import events, mailbox_vocabulary
from .supervisor_audit_evidence_collector import _Collector
from .supervisor_audit_evidence_constants import (
    _MAILBOX_JOIN_KEYS,
    _RUN_STARTED_EVENT,
    _TERMINAL_EVENT_NAMES,
    MAILBOX_OWNERSHIP_DELIVERED,
    REASON_MALFORMED,
    REASON_MISSING,
    REASON_UNREADABLE,
    UNPARSABLE_LINE_COUNT_KEY,
)
from .supervisor_audit_evidence_failures import _event_priority
from .supervisor_audit_evidence_helpers import _mapping


def _read_events(
    path: Path, run_id: str, collector: _Collector
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the run's slice of the shared event log, failing loudly on damage.

    The log is per-plan and append-only, so a run is the span from its own
    ``run_started`` to its terminal record, plus any record that names the run
    ID outright. Lines that will not parse are counted and digested rather than
    dropped: a damaged log is itself evidence about the run that wrote it.
    """
    summary: dict[str, Any] = {"available": False, UNPARSABLE_LINE_COUNT_KEY: 0}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        summary["reason"] = REASON_MISSING
        collector.omissions.record("events", REASON_MISSING, str(path))
        return [], summary
    except OSError:
        summary["reason"] = REASON_UNREADABLE
        collector.omissions.record("events", REASON_UNREADABLE, str(path))
        return [], summary

    summary["available"] = True
    records: list[dict[str, Any]] = []
    inside = False
    terminated = False
    rejected = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            rejected += 1
            collector.omissions.record("events", REASON_MALFORMED, line[:2000])
            continue
        if not isinstance(parsed, Mapping):
            rejected += 1
            collector.omissions.record("events", REASON_MALFORMED, line[:2000])
            continue
        payload = parsed.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        name = str(parsed.get("name") or "")
        named_run = payload.get("run_id")
        names_this_run = named_run == run_id
        if names_this_run and name == _RUN_STARTED_EVENT:
            inside = True
            terminated = False
        if not names_this_run and (not inside or terminated or named_run is not None):
            continue
        records.append(
            {
                "sequence": index,
                "name": name,
                "created_at": parsed.get("created_at"),
                "version": parsed.get("version"),
                "priority": _event_priority(name, payload),
                "payload": collector.redact(payload),
            }
        )
        if names_this_run and name in _TERMINAL_EVENT_NAMES:
            terminated = True
    summary[UNPARSABLE_LINE_COUNT_KEY] = rejected
    summary["reference"] = collector.reference(path, kind="event")
    return records, summary


def _delivered_mailbox_ids(
    event_records: Sequence[Mapping[str, Any]], run_id: str
) -> frozenset[str]:
    """Mailbox messages this run's own event stream says it took delivery of.

    Guidance is addressed to the plan, not to a run: a supervisor appends it
    without knowing which run — or which turn — will read it, so
    ``mailbox_appenders.append_guidance`` writes no ``run_id`` and the record cannot be
    claimed the way ``append_notice``'s is. Delivery is the tie, and delivery is
    already recorded: the runner appends a ``guidance-observed`` mailbox event
    naming the message it read into a turn. Joining on that accounts for the
    guidance a run acted on while leaving guidance meant for some other run
    where it belongs.
    """
    delivered: set[str] = set()
    for record in event_records:
        if record.get("name") != events.MAILBOX_EVENT:
            continue
        payload = _mapping(record.get("payload"))
        if payload.get("action") != events.MAILBOX_ACTION_GUIDANCE_OBSERVED:
            continue
        if payload.get("run_id") not in (None, run_id):
            continue
        message_id = payload.get("message_id")
        if message_id is not None:
            delivered.add(str(message_id))
    return frozenset(delivered)


def _read_mailbox(
    path: Path,
    run_id: str,
    collector: _Collector,
    *,
    delivered_ids: frozenset[str] = frozenset(),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read the run's slice of the shared operator mailbox.

    Without it an audit sees the contract a session was materialized with and
    the recipe it actually ran, but nothing saying a human ruled between the
    two, and reads an authorized narrowing as an evaded gate.

    Only a record that names a run outright can be claimed by run ID; a reply,
    a receipt and the wait records join their exchange through ``to``,
    ``wait_id`` or ``closure_id``, so ownership is seeded from the named
    records and walked forward. ``delivered_ids`` adds the third seed a
    plan-addressed record needs, since guidance names no run and answers no
    question: the run's own event stream saying it read that message. A record
    that never joins belongs to another run or to the plan, and is counted
    rather than reported as withheld: an omission means evidence was kept back,
    not that it was someone else's.

    Guidance-delivery receipts seed their targets before that forward walk:
    explicit inbox reads have receipts but need not have runner delivery events.
    Missing targets are recorded as omissions instead of silently unscoped.

    The file is read directly rather than through :mod:`mailbox`, which is
    addressed by plan directory and takes a shared lock that a live supervisor
    could stall collection behind.
    """
    summary: dict[str, Any] = {
        "available": False,
        "delivered_count": 0,
        UNPARSABLE_LINE_COUNT_KEY: 0,
        "unscoped_count": 0,
        "counts": {},
    }
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        summary["reason"] = REASON_MISSING
        collector.omissions.record("mailbox", REASON_MISSING, str(path))
        return [], summary
    except OSError:
        summary["reason"] = REASON_UNREADABLE
        collector.omissions.record("mailbox", REASON_UNREADABLE, str(path))
        return [], summary

    summary["available"] = True
    parsed_lines: list[tuple[int, Mapping[str, Any]]] = []
    rejected = 0
    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError, ValueError:
            rejected += 1
            collector.omissions.record("mailbox", REASON_MALFORMED, line[:2000])
            continue
        if not isinstance(parsed, Mapping):
            rejected += 1
            collector.omissions.record("mailbox", REASON_MALFORMED, line[:2000])
            continue
        parsed_lines.append((index, parsed))

    owned = {
        str(parsed["id"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id and parsed.get("id") is not None
    }
    # Delivery receipts point backwards to plan-addressed guidance. Seed those
    # targets before the forward exchange walk, including explicit inbox reads
    # that do not emit a guidance-observed event (ASI-131).
    referenced_ids = frozenset(
        str(parsed["to"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id
        and parsed.get("kind") == mailbox_vocabulary.KIND_GUIDANCE_DELIVERY
        and parsed.get("to") is not None
    )
    delivered_ids = delivered_ids | frozenset(
        str(parsed["to"])
        for _, parsed in parsed_lines
        if parsed.get("run_id") == run_id
        and parsed.get("kind") == mailbox_vocabulary.KIND_GUIDANCE_DELIVERY
        and parsed.get("delivery_state") in mailbox_vocabulary.GUIDANCE_DELIVERED_STATES
        and parsed.get("to") is not None
    )
    present_ids = {str(parsed["id"]) for _, parsed in parsed_lines if parsed.get("id")}
    missing_ids = sorted((referenced_ids | delivered_ids) - present_ids)
    summary["missing_reference_count"] = len(missing_ids)
    for missing_id in missing_ids:
        collector.omissions.record("mailbox", REASON_MISSING, missing_id)
    records: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    unscoped = 0
    delivered = 0
    for index, parsed in parsed_lines:
        identifier = parsed.get("id")
        was_delivered = identifier is not None and str(identifier) in delivered_ids
        was_referenced = identifier is not None and str(identifier) in referenced_ids
        # A join value is an id string; any other JSON shape joins nothing,
        # and a list or object would not even hash against ``owned``.
        joined = any(
            isinstance(parsed.get(key), str) and parsed.get(key) in owned
            for key in _MAILBOX_JOIN_KEYS
        )
        if (
            not joined
            and not was_delivered
            and not was_referenced
            and parsed.get("run_id") != run_id
        ):
            unscoped += 1
            continue
        if identifier is not None:
            owned.add(str(identifier))
        kind = str(parsed.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        # The prose lives under ``text``, which redaction already treats as
        # application content, so the ruling's digest and length survive and
        # its bytes do not.
        record = dict(collector.redact(parsed))
        record["sequence"] = index
        if was_delivered and parsed.get("run_id") != run_id:
            # Say how a record naming no run reached this run's slice, so a
            # reader is not left inferring it from the absence of a ``run_id``.
            delivered += 1
            record["ownership"] = MAILBOX_OWNERSHIP_DELIVERED
        elif was_referenced and parsed.get("run_id") != run_id:
            record["ownership"] = "delivery-referenced"
        records.append(record)
    summary["delivered_count"] = delivered
    summary[UNPARSABLE_LINE_COUNT_KEY] = rejected
    summary["unscoped_count"] = unscoped
    summary["counts"] = counts
    summary["reference"] = collector.reference(path, kind="event")
    return records, summary
