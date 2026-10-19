"""Agent mux contract formats."""

from __future__ import annotations

import re

from . import agent_mux_contract_models as models

# ---------------------------------------------------------------------------
# The closed schema vocabulary
# ---------------------------------------------------------------------------

SCHEMA_MARKER = "https://agent-session-loop.invalid/schemas/asl-amx-contract-1"


_SCHEMA_TOP_LEVEL_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "component",
        "version",
        "max_document_bytes",
        "forbidden_property_names",
        "body_bearing_pointers",
        "roots",
        "definitions",
    }
)


_SCHEMA_TOP_LEVEL_REQUIRED = frozenset(_SCHEMA_TOP_LEVEL_KEYS - {"description"})


_NODE_KEYS = frozenset(
    {
        "$ref",
        "description",
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "propertyNames",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
    }
)


_JSON_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


#: Closed set of semantic string formats. Each is a rule a consumer in another
#: language must implement identically, so the list stays deliberately short.
FORMAT_TIMESTAMP = "timestamp"


FORMAT_EPOCH_SECONDS = "epoch-seconds"


FORMAT_SEQUENCE = "sequence"


FORMAT_CANONICAL_PATH = "canonical-path"


FORMAT_QUALIFIED_PATH = "qualified-path"


FORMAT_SHA256 = "sha256"


FORMAT_CONTRACT_SET_ID = "contract-set-id"


FORMATS: tuple[str, ...] = (
    FORMAT_TIMESTAMP,
    FORMAT_EPOCH_SECONDS,
    FORMAT_SEQUENCE,
    FORMAT_CANONICAL_PATH,
    FORMAT_QUALIFIED_PATH,
    FORMAT_SHA256,
    FORMAT_CONTRACT_SET_ID,
)


#: Canonical UTC spelling: an RFC 3339 timestamp with an explicit ``+00:00``
#: offset. ``Z``, a naive timestamp, and any non-zero offset are all refused, so
#: a timestamp that appears in the projection, in a hook payload, and in a
#: bridge frame compares byte for byte instead of needing three parsers.
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00$"
)


_EPOCH_SECONDS_RE = re.compile(r"^(?:0|[1-9]\d{0,11})$")


_SEQUENCE_RE = re.compile(r"^(?:0|[1-9]\d{0,17})$")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


_QUALIFIED_PATH_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_-]{0,63}:[^/\x00][^\x00]{0,1023}$"
)


_MAX_PATH_CHARS = 1024


def _timestamp_ok(value: str) -> bool:
    if _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    month = int(value[5:7])
    day = int(value[8:10])
    hour = int(value[11:13])
    minute = int(value[14:16])
    second = int(value[17:19])
    return (
        1 <= month <= 12
        and 1 <= day <= 31
        and hour < 24
        and minute < 60
        and second < 61
    )


def _canonical_path_ok(value: str) -> bool:
    """Absolute POSIX path, no traversal, no trailing slash, no NUL."""
    if not value.startswith("/") or "\x00" in value:
        return False
    if len(value) > _MAX_PATH_CHARS:
        return False
    if value != "/" and value.endswith("/"):
        return False
    if "//" in value:
        return False
    return all(segment not in {".", ".."} for segment in value.split("/")[1:])


_FORMAT_CHECKS = {
    FORMAT_TIMESTAMP: _timestamp_ok,
    FORMAT_EPOCH_SECONDS: lambda value: _EPOCH_SECONDS_RE.fullmatch(value) is not None,
    FORMAT_SEQUENCE: lambda value: _SEQUENCE_RE.fullmatch(value) is not None,
    FORMAT_CANONICAL_PATH: _canonical_path_ok,
    FORMAT_QUALIFIED_PATH: lambda value: (
        _QUALIFIED_PATH_RE.fullmatch(value) is not None
    ),
    FORMAT_SHA256: lambda value: _SHA256_RE.fullmatch(value) is not None,
    FORMAT_CONTRACT_SET_ID: lambda value: (
        models.CONTRACT_SET_ID_PATTERN.fullmatch(value) is not None
    ),
}
