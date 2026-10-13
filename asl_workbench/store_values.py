"""Small decoding, clamping and SQL-building helpers shared by the store modules."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from .observation import (
    MAX_WARNINGS,
)
from .store_columns import (
    _OPERATION_COLUMNS,
    _quoted,
)
from .store_types import (
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    StoreError,
)


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    return int(value) if isinstance(value, (int, float)) else None


def _operation_record(row: sqlite3.Row | None) -> dict[str, Any]:
    """One journal row as a record, with the columns it never filled omitted.

    Omitting nulls rather than carrying them keeps ``dispatched_at`` absent on
    an operation nobody has collected, so "never dispatched" reads the same in
    the record as it does in the status.
    """
    if row is None:  # pragma: no cover - callers re-read a row they just wrote
        raise StoreError("the operation vanished inside its own transaction")
    return {
        column: row[column] for column in _OPERATION_COLUMNS if row[column] is not None
    }


def _decoded_list(value: Any) -> list[Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


def _decoded_mapping(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _warning_list(value: Any) -> tuple[str, ...]:
    decoded = _decoded_list(value) or []
    return tuple(entry for entry in decoded if isinstance(entry, str))[:MAX_WARNINGS]


def _clamp_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return DEFAULT_READ_LIMIT
    return max(1, min(limit, MAX_READ_LIMIT))


def _legacy_project(project: str) -> str:
    """Old installations used plan directory names as project names."""
    if re.search(r"[0-9a-f]{12}-[0-9a-f]{12}$", project) or project == "unnamed plan":
        return "Unknown project"
    return project


def _upsert_statement(
    table: str, columns: Sequence[str], identity: Sequence[str]
) -> str:
    """One named-parameter upsert, with every identifier quoted."""
    assignments = ", ".join(
        f'"{column}" = excluded."{column}"'
        for column in columns
        if column not in identity
    )
    return (
        f"INSERT INTO {table} ({_quoted(columns)}) "
        f"VALUES ({', '.join(f':{column}' for column in columns)}) "
        f"ON CONFLICT({_quoted(identity)}) DO UPDATE SET {assignments}"
    )


def _membership_clause(
    memberships: Mapping[str, set[str]], parameters: list[Any], *, prefix: str = ""
) -> str:
    """A SQL clause matching exactly these ``(connector, plan)`` pairs.

    ``0`` when there are none: a filter that matched nothing must return
    nothing, and an empty ``OR`` chain would return everything.
    """
    matches: list[str] = []
    for connector_id in sorted(memberships):
        keys = sorted(memberships[connector_id])
        marks = ",".join("?" for _ in keys)
        matches.append(f"({prefix}connector_id = ? AND {prefix}plan_key IN ({marks}))")
        parameters.extend([connector_id, *keys])
    return "(" + " OR ".join(matches) + ")" if matches else "0"
