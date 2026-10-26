"""Read event records and select the run displayed by the tail."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import (
    tail_models,
)


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise tail_models.TailError(f"cannot read {path}: {error}") from error
    if not isinstance(raw, dict):
        raise tail_models.TailError(f"{path} did not contain a JSON object")
    return raw


def _read_event_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        lines = path.read_bytes().splitlines()
    except OSError as error:
        raise tail_models.TailError(f"cannot read {path}: {error}") from error

    # Decoded line by line: a stray non-UTF-8 byte spoils only its own line,
    # the way a torn line does, not the whole history.
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line.decode("utf-8"))
        except ValueError, RecursionError:
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("payload")
    return raw if isinstance(raw, dict) else {}


def _record_run_id(record: dict[str, Any]) -> str | None:
    run_id = _payload(record).get("run_id")
    return str(run_id) if run_id is not None else None


def _state_run_id(state_payload: dict[str, Any] | None) -> str | None:
    if state_payload is None:
        return None
    run_id = state_payload.get("run_id")
    return str(run_id) if run_id is not None else None


def _latest_run_id(
    state_payload: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> str | None:
    state_run_id = _state_run_id(state_payload)
    if state_run_id is not None:
        return state_run_id

    for record in reversed(records):
        run_id = _record_run_id(record)
        if run_id is not None:
            return run_id
    return None


def _matches_run(record: dict[str, Any], run_id: str | None) -> bool:
    if run_id is None:
        return True
    record_run_id = _record_run_id(record)
    return record_run_id == run_id


def _event_records_for_run(
    records: list[dict[str, Any]],
    run_id: str | None,
) -> list[dict[str, Any]]:
    return [record for record in records if _matches_run(record, run_id)]
