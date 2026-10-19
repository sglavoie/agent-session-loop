"""Parse ledger envelopes while retaining rejected physical lines."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .assurance_ledger_models import LedgerRecord, LedgerView, RejectedLedgerRecord
from .assurance_ledger_vocabulary import (
    _AUDIT_KIND_STATES,
    _PAYLOAD_PARSERS,
    ENVELOPE_KEYS,
    ISSUE_ALLOCATING_KINDS,
    KINDS_BY_SCHEMA_VERSION,
    MAX_REJECTED_RAW_CHARS,
    REJECT_EMPTY_LINE,
    REJECT_INVALID_JSON,
    REJECT_INVALID_RECORD,
    REJECT_NOT_OBJECT,
    REJECT_UNKNOWN_KIND,
    REJECT_UNSUPPORTED_VERSION,
    SUPPORTED_LEDGER_SCHEMA_VERSIONS,
)
from .assurance_models_primitives import AssuranceError, parse_issue_id


def _physical_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


def _require_envelope_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AssuranceError(
            f"record 'recorded_at' must be an unpadded ISO-8601 timestamp; got "
            f"{value!r}"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AssuranceError(
            f"record 'recorded_at' must be an ISO-8601 timestamp; got {value!r} "
            f"({error})"
        ) from error
    if parsed.tzinfo is None:
        raise AssuranceError(
            f"record 'recorded_at' must carry an explicit UTC offset; got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _record_from_envelope(envelope: Mapping[str, Any], *, seq: int) -> LedgerRecord:
    unknown = sorted(str(key) for key in envelope if key not in ENVELOPE_KEYS)
    if unknown:
        raise AssuranceError(
            f"record has unknown key(s) {', '.join(repr(key) for key in unknown)}; "
            f"allowed keys are {', '.join(repr(key) for key in ENVELOPE_KEYS)}"
        )
    kind = envelope["kind"]
    recorded_at = _require_envelope_timestamp(envelope.get("recorded_at"))
    payload = _PAYLOAD_PARSERS[kind](envelope.get("payload"))
    _require_kind_agreement(kind, payload)
    issue_id = envelope.get("issue_id")
    if kind in ISSUE_ALLOCATING_KINDS:
        if issue_id is None:
            raise AssuranceError(
                f"record kind {kind!r} must carry its allocated issue id"
            )
        issue_id = parse_issue_id(issue_id)
    elif issue_id is not None:
        raise AssuranceError(
            f"record kind {kind!r} must not carry an 'issue_id'; only "
            f"{', '.join(sorted(ISSUE_ALLOCATING_KINDS))} allocates one"
        )
    return LedgerRecord(
        seq=seq,
        kind=kind,
        recorded_at=recorded_at,
        payload=payload,
        issue_id=issue_id,
        version=envelope["version"],
    )


def _require_kind_agreement(kind: str, payload: Any) -> None:
    expected_states = _AUDIT_KIND_STATES.get(kind)
    if expected_states is None:
        return
    if payload.state not in expected_states:
        raise AssuranceError(
            f"record kind {kind!r} requires assurance state "
            f"{' or '.join(repr(item) for item in sorted(expected_states))}; "
            f"got {payload.state!r}"
        )


def _reject(
    rejected: list[RejectedLedgerRecord],
    *,
    seq: int,
    reason: str,
    raw: str,
    detail: str | None = None,
) -> None:
    if len(raw) > MAX_REJECTED_RAW_CHARS:
        raw = f"{raw[:MAX_REJECTED_RAW_CHARS]}…"
    rejected.append(
        RejectedLedgerRecord(seq=seq, reason=reason, raw=raw, detail=detail)
    )


def parse_ledger_bytes(data: bytes, path: Path) -> LedgerView:
    """Parse ledger bytes into accepted records and preserved rejections."""
    records: list[LedgerRecord] = []
    rejected: list[RejectedLedgerRecord] = []
    for seq, raw_bytes in enumerate(_physical_lines(data), start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if raw.strip() == "":
            _reject(rejected, seq=seq, reason=REJECT_EMPTY_LINE, raw=raw)
            continue
        try:
            envelope = json.loads(raw)
        except ValueError as error:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_JSON,
                raw=raw,
                detail=str(error),
            )
            continue
        if not isinstance(envelope, Mapping):
            _reject(rejected, seq=seq, reason=REJECT_NOT_OBJECT, raw=raw)
            continue
        version = envelope.get("version")
        if isinstance(version, bool) or version not in SUPPORTED_LEDGER_SCHEMA_VERSIONS:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNSUPPORTED_VERSION,
                raw=raw,
                detail=f"version {version!r} is not one this build reads",
            )
            continue
        if envelope.get("kind") not in KINDS_BY_SCHEMA_VERSION[version]:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNKNOWN_KIND,
                raw=raw,
                detail=(
                    f"kind {envelope.get('kind')!r} is not a ledger record kind "
                    f"at schema v{version}"
                ),
            )
            continue
        try:
            records.append(_record_from_envelope(envelope, seq=seq))
        except AssuranceError as error:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_RECORD,
                raw=raw,
                detail=str(error),
            )
    return LedgerView(path=path, records=tuple(records), rejected=tuple(rejected))
