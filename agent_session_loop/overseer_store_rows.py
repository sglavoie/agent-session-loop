"""Column lists and row-to-record mappers for the overseer ledger.

Each mapper turns one ``sqlite3.Row`` into the frozen record the ledger hands
back, refusing a stored value this build does not recognise rather than
guessing at it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .overseer_store_models import (
    ACTION_STATUSES,
    INCIDENT_STATUSES,
    ActionRecord,
    ActionStatus,
    CheckpointGroupRecord,
    DecisionRecord,
    EvidenceRecord,
    IncidentRecord,
    IncidentStatus,
    LedgerError,
    ReceiptRecord,
    WatchLease,
)

INCIDENT_COLUMNS = (
    "incident_id, plan_dir, run_id, kind, status, summary, epoch, "
    "config_fingerprint, opened_at, settled_at"
)

EVIDENCE_COLUMNS = (
    "evidence_id, incident_id, plan_dir, alias, kind, locator, digest, epoch, "
    "config_fingerprint, sealed, recorded_at"
)

DECISION_COLUMNS = (
    "decision_id, plan_dir, access, allowed, available, failure, reason, summary, "
    "config_fingerprint, request_digest, recorded_at"
)

ACTION_COLUMNS = (
    "action_id, plan_dir, incident_id, operation, target, status, epoch, "
    "config_fingerprint, request_digest, progress_token, attempted_at, settled_at"
)

WATCH_LEASE_COLUMNS = (
    "plan_dir, watcher_id, run_id, host, pid, expires_at, claimed_at, "
    "renewed_at, released_at"
)

CHECKPOINT_GROUP_COLUMNS = (
    "group_key, plan_dir, incident_id, target, status, phase, epoch, "
    "config_fingerprint, request_digest, record, opened_at, updated_at"
)


def checkpoint_group_from_row(row: sqlite3.Row) -> CheckpointGroupRecord:
    """Read one stored group, refusing a payload that is not an object.

    A ``record`` that no longer parses is raised rather than defaulted to an
    empty group: an empty group reads as "nothing is owed", which is the one
    answer a torn row must never be allowed to give.
    """
    try:
        payload = json.loads(str(row["record"]))
    except ValueError as error:
        raise LedgerError(
            f"checkpoint group {row['group_key']} holds an unreadable record: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise LedgerError(
            f"checkpoint group {row['group_key']} holds a record that is not an object"
        )
    return CheckpointGroupRecord(
        group_key=str(row["group_key"]),
        plan_dir=str(row["plan_dir"]),
        incident_id=optional_int(row["incident_id"]),
        target=str(row["target"]),
        status=str(row["status"]),
        phase=str(row["phase"]),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        request_digest=str(row["request_digest"]),
        record=payload,
        opened_at=str(row["opened_at"]),
        updated_at=str(row["updated_at"]),
    )


def watch_lease_from_row(row: sqlite3.Row) -> WatchLease:
    return WatchLease(
        plan_dir=str(row["plan_dir"]),
        watcher_id=str(row["watcher_id"]),
        run_id=str(row["run_id"]),
        host=str(row["host"]),
        pid=int(row["pid"]),
        expires_at=float(row["expires_at"]),
        claimed_at=str(row["claimed_at"]),
        renewed_at=str(row["renewed_at"]),
        released_at=optional_text(row["released_at"]),
    )


def incident_from_row(row: sqlite3.Row) -> IncidentRecord:
    return IncidentRecord(
        incident_id=int(row["incident_id"]),
        plan_dir=str(row["plan_dir"]),
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        status=incident_status(str(row["status"])),
        summary=str(row["summary"]),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        opened_at=str(row["opened_at"]),
        settled_at=optional_text(row["settled_at"]),
    )


def evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=int(row["evidence_id"]),
        incident_id=int(row["incident_id"]),
        plan_dir=str(row["plan_dir"]),
        alias=str(row["alias"]),
        kind=str(row["kind"]),
        locator=str(row["locator"]),
        digest=str(row["digest"]),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        sealed=bool(row["sealed"]),
        recorded_at=str(row["recorded_at"]),
    )


def decision_from_row(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=int(row["decision_id"]),
        plan_dir=str(row["plan_dir"]),
        access=str(row["access"]),
        allowed=bool(row["allowed"]),
        available=bool(row["available"]),
        failure=optional_text(row["failure"]),
        reason=str(row["reason"]),
        summary=str(row["summary"]),
        config_fingerprint=str(row["config_fingerprint"]),
        request_digest=str(row["request_digest"]),
        recorded_at=str(row["recorded_at"]),
    )


def action_from_row(row: sqlite3.Row, receipt: ReceiptRecord | None) -> ActionRecord:
    return ActionRecord(
        action_id=int(row["action_id"]),
        plan_dir=str(row["plan_dir"]),
        incident_id=optional_int(row["incident_id"]),
        operation=str(row["operation"]),
        target=str(row["target"]),
        status=action_status(str(row["status"])),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        request_digest=str(row["request_digest"]),
        progress_token=str(row["progress_token"]),
        attempted_at=str(row["attempted_at"]),
        settled_at=optional_text(row["settled_at"]),
        receipt=receipt,
    )


def incident_status(value: str) -> IncidentStatus:
    """Read a stored status, refusing one this build does not know.

    A row written by a newer ASL naming a status this one has never heard of is
    an unreadable record, not an ``open`` one: guessing would put an incident
    into a bucket some caller then acts on.
    """
    if value not in INCIDENT_STATUSES:
        raise LedgerError(f"stored incident status {value!r} is not a known status")
    return value


def action_status(value: str) -> ActionStatus:
    """Read a stored action status, refusing one this build does not know."""
    if value not in ACTION_STATUSES:
        raise LedgerError(f"stored action status {value!r} is not a known status")
    return value


def optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
