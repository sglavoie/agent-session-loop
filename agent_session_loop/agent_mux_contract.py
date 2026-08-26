"""The ASL-owned compatibility contract for the AgentMux integration.

ASL is the sole run authority, so ASL — not AgentMux — owns the schemas the two
components exchange, the fixtures that pin them, and the identity a consumer
checks before it trusts either. This module is that ownership expressed as code:
it loads the canonical corpus under ``tests/fixtures/agent_mux_contract``,
validates documents against it, derives the contract-set ID from the corpus
bytes, and builds the compatibility manifest that maps that ID to component
schema versions and supported protocol capabilities.

Three components are covered:

- **projection v1** — the ``agent_mux`` block ASL writes into ``state.json``.
- **lifecycle hook v2** — the payload ASL pipes to ``amx hook-indicator``.
- **bridge protocol v2** — the NDJSON frames ``asl bridge serve`` writes.

Four properties are structural rather than incidental:

- **Every field is closed.** Objects declare ``additionalProperties: false``,
  every string carries an enum, a const, a format, or a ``maxLength``, and every
  array carries a ``maxItems``. A field nobody declared is a rejection, not a
  value to ignore, so a producer cannot widen the wire without moving the
  contract-set ID.
- **The corpus is body-free by construction.** Question and reply prose is
  refused by name anywhere in a document, at every depth, before any
  schema-guided walk runs. The single body-bearing frame declares its exemption
  as an explicit JSON pointer in its own schema, so the exception is data rather
  than a special case in the validator.
- **Identity is derived, never declared.** The contract-set ID is a digest over
  the canonical schema and fixture bytes in sorted path order. Editing any byte
  of the corpus moves it; nothing else can.
- **Generation is byte-identical.** Every corpus file is stored in one canonical
  JSON rendering, and the manifest is rebuilt from the corpus rather than
  maintained by hand, so regeneration is a no-op or a diff.

This module does not activate v2 anywhere. It publishes the contract; the
runtime components adopt it in later work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Corpus layout
# ---------------------------------------------------------------------------

#: Location of the canonical corpus relative to the repository root.
CORPUS_RELATIVE_PATH = Path("tests") / "fixtures" / "agent_mux_contract"

REPO_ROOT = Path(__file__).resolve().parents[1]

MANIFEST_FILENAME = "manifest.json"
SCHEMA_DIRNAME = "schemas"
FIXTURE_DIRNAME = "fixtures"

COMPONENT_PROJECTION = "projection"
COMPONENT_LIFECYCLE_HOOK = "lifecycle_hook"
COMPONENT_BRIDGE_PROTOCOL = "bridge_protocol"

#: Declared order. Later work appends; it never reorders, because the manifest
#: and every report below are compared across versions by a human reading a
#: diff.
COMPONENTS: tuple[str, ...] = (
    COMPONENT_PROJECTION,
    COMPONENT_LIFECYCLE_HOOK,
    COMPONENT_BRIDGE_PROTOCOL,
)

#: Schema file per component. The version is in the filename because a corpus
#: that carried two versions of one component would otherwise need a lookup to
#: read.
SCHEMA_FILENAMES: dict[str, str] = {
    COMPONENT_PROJECTION: "projection-v1.json",
    COMPONENT_LIFECYCLE_HOOK: "lifecycle-hook-v2.json",
    COMPONENT_BRIDGE_PROTOCOL: "bridge-protocol-v2.json",
}

#: Fixture subdirectory per component.
FIXTURE_DIRNAMES: dict[str, str] = {
    COMPONENT_PROJECTION: "projection-v1",
    COMPONENT_LIFECYCLE_HOOK: "lifecycle-hook-v2",
    COMPONENT_BRIDGE_PROTOCOL: "bridge-protocol-v2",
}

VALID_DIRNAME = "valid"
INVALID_DIRNAME = "invalid"

#: The contract family. It prefixes every contract-set ID so an operator can
#: tell an ASL/AgentMux identity from any other digest at a glance.
CONTRACT_SET_PREFIX = "asl-amx-1"

#: How much of the corpus digest the contract-set ID carries. Sixteen hex
#: characters is 64 bits: enough that an accidental collision between two
#: hand-edited corpora is not a thing that happens, and short enough to ride in
#: every hook payload and hello frame without being the largest field there.
CONTRACT_SET_ID_HEX_CHARS = 16

CONTRACT_SET_ID_PATTERN = re.compile(
    rf"^{re.escape(CONTRACT_SET_PREFIX)}\.[0-9a-f]{{{CONTRACT_SET_ID_HEX_CHARS}}}$"
)

MANIFEST_VERSION = 1

#: Status values a contract set may carry in the manifest.
CONTRACT_SET_STATUS_CURRENT = "current"
CONTRACT_SET_STATUS_RETIRED = "retired"
CONTRACT_SET_STATUSES: tuple[str, ...] = (
    CONTRACT_SET_STATUS_CURRENT,
    CONTRACT_SET_STATUS_RETIRED,
)


class ContractError(Exception):
    """The corpus itself is unusable: missing, unreadable, or not JSON."""


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    """Render ``value`` as the one JSON spelling this corpus is stored in.

    Sorted keys, two-space indent, no ASCII escaping, and a trailing newline.
    Every schema, fixture, and manifest byte in the corpus is this rendering of
    its own content, which is what makes regeneration a no-op instead of a
    whitespace diff and what makes the corpus digest a function of content.
    """
    text = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    )
    return (text + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Violation vocabulary
# ---------------------------------------------------------------------------

#: Closed set of rejection reasons. A consumer maps an outcome by reason rather
#: than by parsing a message, so this vocabulary is part of the contract and
#: moves the contract-set ID when it changes.
REASON_UNKNOWN_FIELD = "unknown-field"
REASON_MISSING_FIELD = "missing-field"
REASON_TYPE_MISMATCH = "type-mismatch"
REASON_NULL_NOT_ALLOWED = "null-not-allowed"
REASON_INVALID_ENUM = "invalid-enum"
REASON_CONST_MISMATCH = "const-mismatch"
REASON_STRING_TOO_LONG = "string-too-long"
REASON_STRING_TOO_SHORT = "string-too-short"
REASON_PATTERN_MISMATCH = "pattern-mismatch"
REASON_INVALID_FORMAT = "invalid-format"
REASON_NUMBER_OUT_OF_RANGE = "number-out-of-range"
REASON_ARRAY_TOO_LONG = "array-too-long"
REASON_ARRAY_TOO_SHORT = "array-too-short"
REASON_DUPLICATE_ITEM = "duplicate-item"
REASON_DOCUMENT_TOO_LARGE = "document-too-large"
REASON_BODY_NOT_ALLOWED = "body-not-allowed"
REASON_DIGEST_MISMATCH = "digest-mismatch"
REASON_UNKNOWN_ROOT = "unknown-root"
REASON_SCHEMA_INVALID = "schema-invalid"

VIOLATION_REASONS: tuple[str, ...] = (
    REASON_UNKNOWN_FIELD,
    REASON_MISSING_FIELD,
    REASON_TYPE_MISMATCH,
    REASON_NULL_NOT_ALLOWED,
    REASON_INVALID_ENUM,
    REASON_CONST_MISMATCH,
    REASON_STRING_TOO_LONG,
    REASON_STRING_TOO_SHORT,
    REASON_PATTERN_MISMATCH,
    REASON_INVALID_FORMAT,
    REASON_NUMBER_OUT_OF_RANGE,
    REASON_ARRAY_TOO_LONG,
    REASON_ARRAY_TOO_SHORT,
    REASON_DUPLICATE_ITEM,
    REASON_DOCUMENT_TOO_LARGE,
    REASON_BODY_NOT_ALLOWED,
    REASON_DIGEST_MISMATCH,
    REASON_UNKNOWN_ROOT,
    REASON_SCHEMA_INVALID,
)


@dataclass(frozen=True)
class Violation:
    """One rejection, addressed by JSON pointer."""

    reason: str
    pointer: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "pointer": self.pointer, "detail": self.detail}


def _pointer(parts: Sequence[str]) -> str:
    if not parts:
        return ""
    escaped = [part.replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped)


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
        CONTRACT_SET_ID_PATTERN.fullmatch(value) is not None
    ),
}


# ---------------------------------------------------------------------------
# Schema linting
# ---------------------------------------------------------------------------


def lint_schema(schema: Mapping[str, Any]) -> list[Violation]:
    """Return every way ``schema`` fails the closed-vocabulary rules.

    The linter is what makes "closed fields, bounded values" a property of the
    corpus rather than a habit of whoever last edited it: an object without
    ``additionalProperties: false``, a free-text string without a ceiling, or an
    unbounded array is a schema defect reported here, long before a document is
    validated against it.
    """
    problems: list[Violation] = []

    def fail(pointer: str, detail: str) -> None:
        problems.append(Violation(REASON_SCHEMA_INVALID, pointer, detail))

    keys = set(schema)
    for unknown in sorted(keys - _SCHEMA_TOP_LEVEL_KEYS):
        fail(_pointer([unknown]), f"unknown top-level schema key `{unknown}`")
    for missing in sorted(_SCHEMA_TOP_LEVEL_REQUIRED - keys):
        fail("", f"schema is missing required key `{missing}`")
    if problems:
        return problems

    if schema["$schema"] != SCHEMA_MARKER:
        fail("/$schema", f"schema marker must be `{SCHEMA_MARKER}`")
    if schema["component"] not in COMPONENTS:
        fail("/component", f"unknown component `{schema['component']}`")
    if not isinstance(schema["version"], int) or schema["version"] < 1:
        fail("/version", "version must be a positive integer")
    if not isinstance(schema["max_document_bytes"], int) or (
        schema["max_document_bytes"] < 1
    ):
        fail("/max_document_bytes", "max_document_bytes must be a positive integer")

    definitions = schema["definitions"]
    if not isinstance(definitions, dict) or not definitions:
        fail("/definitions", "definitions must be a non-empty object")
        return problems

    roots = schema["roots"]
    if not isinstance(roots, dict) or not roots:
        fail("/roots", "roots must be a non-empty object")
        return problems
    for name, definition in sorted(roots.items()):
        if definition not in definitions:
            fail(_pointer(["roots", name]), f"root points at unknown `{definition}`")

    forbidden = schema["forbidden_property_names"]
    if not isinstance(forbidden, list) or sorted(set(forbidden)) != forbidden:
        fail(
            "/forbidden_property_names",
            "forbidden_property_names must be a sorted, duplicate-free list",
        )
    exempt = schema["body_bearing_pointers"]
    if not isinstance(exempt, list) or sorted(set(exempt)) != exempt:
        fail(
            "/body_bearing_pointers",
            "body_bearing_pointers must be a sorted, duplicate-free list",
        )

    reachable: set[str] = set()
    for name in sorted(definitions):
        _lint_node(
            definitions[name],
            pointer=_pointer(["definitions", name]),
            definitions=definitions,
            fail=fail,
        )
    for definition in roots.values() if isinstance(roots, dict) else []:
        _collect_reachable(definition, definitions, reachable)
    for orphan in sorted(set(definitions) - reachable):
        fail(
            _pointer(["definitions", orphan]),
            f"definition `{orphan}` is unreachable from any root",
        )

    # The forbidden-name rule and its exemptions are reconciled against the
    # document pointers a root can actually produce, in both directions: a
    # schema may not declare a body-bearing property it did not exempt, and it
    # may not carry an exemption for a property no root reaches. A stale
    # exemption is the failure mode that matters — it is how a contract keeps
    # permitting prose long after the field carrying it is gone.
    declared = _body_bearing_declarations(schema)
    exempt_pointers = set(exempt) if isinstance(exempt, list) else set()
    for pointer, name in sorted(declared.items()):
        if pointer not in exempt_pointers:
            fail(
                "/body_bearing_pointers",
                f"`{name}` is declared at `{pointer}` without an exemption",
            )
    for pointer in sorted(exempt_pointers - set(declared)):
        fail(
            "/body_bearing_pointers",
            f"`{pointer}` exempts a property no root declares",
        )
    return problems


def _body_bearing_declarations(schema: Mapping[str, Any]) -> dict[str, str]:
    """Map every document pointer declaring a forbidden name to that name.

    Pointers are templates rather than instances: an array element or a map
    value is written ``-``, because a declaration covers every element the array
    can hold. Both remaining edge cases fail closed rather than open. A
    recursive definition is walked once and then abandoned, so a body-bearing
    property nested inside a cycle has no finite pointer and cannot be exempted
    at all; and the document-side sweep resolves only array indices, so an
    exemption beneath a map lints clean but never matches a real document.
    """
    definitions = schema["definitions"]
    roots = schema["roots"]
    declared_forbidden = schema["forbidden_property_names"]
    if not isinstance(declared_forbidden, list):
        return {}
    forbidden = set(declared_forbidden)
    found: dict[str, str] = {}

    def walk(node: Any, parts: list[str], seen: frozenset[str]) -> None:
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            name = ref[len("#/definitions/") :]
            if name not in seen and name in definitions:
                walk(definitions[name], parts, seen | {name})
            return
        types = _node_types(node)
        if "object" in types:
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name in sorted(properties):
                    child = parts + [name]
                    if name in forbidden:
                        found[_pointer(child)] = name
                    walk(properties[name], child, seen)
            additional = node.get("additionalProperties")
            if isinstance(additional, dict):
                walk(additional, parts + ["-"], seen)
        if "array" in types:
            walk(node.get("items"), parts + ["-"], seen)

    for definition in sorted(set(roots.values())):
        walk({"$ref": f"#/definitions/{definition}"}, [], frozenset())
    return found


def _collect_reachable(
    name: str, definitions: Mapping[str, Any], seen: set[str]
) -> None:
    if name in seen or name not in definitions:
        return
    seen.add(name)
    for referenced in _referenced_definitions(definitions[name]):
        _collect_reachable(referenced, definitions, seen)


def _referenced_definitions(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/definitions/"):
            yield ref[len("#/definitions/") :]
        for value in node.values():
            yield from _referenced_definitions(value)
    elif isinstance(node, list):
        for value in node:
            yield from _referenced_definitions(value)


def _node_types(node: Mapping[str, Any]) -> set[str]:
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared)
    return set()


def _lint_node(
    node: Any,
    *,
    pointer: str,
    definitions: Mapping[str, Any],
    fail: Any,
) -> None:
    if not isinstance(node, dict):
        fail(pointer, "schema node must be an object")
        return
    for unknown in sorted(set(node) - _NODE_KEYS):
        fail(pointer, f"unknown schema keyword `{unknown}`")

    ref = node.get("$ref")
    if ref is not None:
        if set(node) - {"$ref", "description"}:
            fail(pointer, "$ref may not be combined with other keywords")
        elif not isinstance(ref, str) or not ref.startswith("#/definitions/"):
            fail(pointer, f"$ref must be `#/definitions/NAME`, got `{ref}`")
        elif ref[len("#/definitions/") :] not in definitions:
            fail(pointer, f"$ref points at unknown definition `{ref}`")
        return

    if "const" in node or "enum" in node:
        if "enum" in node:
            values = node["enum"]
            if not isinstance(values, list) or not values:
                fail(pointer, "enum must be a non-empty list")
            elif len(set(map(repr, values))) != len(values):
                fail(pointer, "enum carries a duplicate value")
        return

    types = _node_types(node)
    if not types:
        fail(pointer, "node declares neither `type`, `enum`, `const`, nor `$ref`")
        return
    for unknown in sorted(types - _JSON_TYPES):
        fail(pointer, f"unknown JSON type `{unknown}`")

    if "object" in types:
        _lint_object_node(
            node,
            pointer=pointer,
            definitions=definitions,
            fail=fail,
        )
    if "array" in types:
        if "items" not in node:
            fail(pointer, "array node must declare `items`")
        else:
            _lint_node(
                node["items"],
                pointer=pointer + "/items",
                definitions=definitions,
                fail=fail,
            )
        if "maxItems" not in node:
            fail(pointer, "array node must declare `maxItems`")
    if "string" in types and "maxLength" not in node and "format" not in node:
        fail(pointer, "free-text string node must declare `maxLength`")
    declared_format = node.get("format")
    if declared_format is not None and declared_format not in FORMATS:
        fail(pointer, f"unknown format `{declared_format}`")


def _lint_object_node(
    node: Mapping[str, Any],
    *,
    pointer: str,
    definitions: Mapping[str, Any],
    fail: Any,
) -> None:
    additional = node.get("additionalProperties")
    properties = node.get("properties")
    if additional is None:
        fail(pointer, "object node must declare `additionalProperties`")
        return
    if additional is False:
        if not isinstance(properties, dict) or not properties:
            fail(pointer, "closed object node must declare `properties`")
            return
        required = node.get("required")
        if not isinstance(required, list) or sorted(set(required)) != required:
            fail(pointer, "`required` must be a sorted, duplicate-free list")
        else:
            for name in required:
                if name not in properties:
                    fail(pointer, f"required names undeclared property `{name}`")
        for name in sorted(properties):
            _lint_node(
                properties[name],
                pointer=pointer + "/properties/" + name,
                definitions=definitions,
                fail=fail,
            )
        return
    # A map node: keys are open, values are not, and the key space is itself
    # constrained so an operator-authored key cannot become the payload.
    if properties is not None or "required" in node:
        fail(pointer, "map node may not also declare `properties`/`required`")
    if "propertyNames" not in node:
        fail(pointer, "map node must declare `propertyNames`")
    else:
        _lint_node(
            node["propertyNames"],
            pointer=pointer + "/propertyNames",
            definitions=definitions,
            fail=fail,
        )
    _lint_node(
        additional,
        pointer=pointer + "/additionalProperties",
        definitions=definitions,
        fail=fail,
    )


# ---------------------------------------------------------------------------
# Document validation
# ---------------------------------------------------------------------------


def validate_document(
    schema: Mapping[str, Any],
    document: Any,
    *,
    root: str | None = None,
) -> list[Violation]:
    """Validate ``document`` against ``schema``; return every violation found.

    The privacy sweep runs first and structurally, over the document rather than
    over the schema walk, so a leaked body is reported as ``body-not-allowed``
    wherever it hides — including inside a subtree the schema-guided walk would
    have abandoned at the first unknown field.

    ``schema`` must already have passed :func:`lint_schema`; that is the check
    which establishes that references resolve, that closed objects declare their
    properties, and that every declared format exists. Validation trusts those
    guarantees rather than re-deriving them per document.
    """
    roots = schema["roots"]
    if root is None:
        if len(roots) != 1:
            return [
                Violation(
                    REASON_UNKNOWN_ROOT,
                    "",
                    f"schema declares {len(roots)} roots; name one",
                )
            ]
        root = next(iter(roots))
    if root not in roots:
        return [Violation(REASON_UNKNOWN_ROOT, "", f"unknown root `{root}`")]

    problems: list[Violation] = list(
        _privacy_violations(
            document,
            forbidden=set(schema["forbidden_property_names"]),
            exempt=set(schema["body_bearing_pointers"]),
        )
    )

    size = len(canonical_json(document))
    ceiling = int(schema["max_document_bytes"])
    if size > ceiling:
        problems.append(
            Violation(
                REASON_DOCUMENT_TOO_LARGE,
                "",
                f"document is {size} canonical bytes, ceiling is {ceiling}",
            )
        )

    problems.extend(
        _validate_node(
            {"$ref": f"#/definitions/{roots[root]}"},
            document,
            parts=[],
            definitions=schema["definitions"],
        )
    )
    return problems


def _privacy_violations(
    document: Any,
    *,
    forbidden: set[str],
    exempt: set[str],
) -> Iterator[Violation]:
    # Two pointers are tracked per node: the concrete one a reader needs to find
    # the leak, and the index-free template the exemption list is written in.
    stack: list[tuple[Any, list[str], list[str]]] = [(document, [], [])]
    while stack:
        node, parts, template = stack.pop()
        if isinstance(node, dict):
            for key in sorted(node):
                child = parts + [key]
                child_template = template + [key]
                pointer = _pointer(child)
                if key in forbidden and _pointer(child_template) not in exempt:
                    yield Violation(
                        REASON_BODY_NOT_ALLOWED,
                        pointer,
                        f"`{key}` may not appear here; this contract is body-free",
                    )
                stack.append((node[key], child, child_template))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                stack.append((item, parts + [str(index)], template + ["-"]))


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _validate_node(
    node: Mapping[str, Any],
    value: Any,
    *,
    parts: list[str],
    definitions: Mapping[str, Any],
) -> list[Violation]:
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = definitions[ref[len("#/definitions/") :]]
        return _validate_node(target, value, parts=parts, definitions=definitions)

    pointer = _pointer(parts)
    problems: list[Violation] = []

    if "const" in node:
        if value != node["const"]:
            problems.append(
                Violation(
                    REASON_CONST_MISMATCH,
                    pointer,
                    f"expected {node['const']!r}, got {value!r}",
                )
            )
        return problems
    if "enum" in node:
        if value not in node["enum"]:
            problems.append(
                Violation(
                    REASON_INVALID_ENUM,
                    pointer,
                    f"{value!r} is not one of {sorted(map(str, node['enum']))}",
                )
            )
        return problems

    types = _node_types(node)
    actual = _json_type(value)
    # `integer` satisfies a `number` declaration; the reverse is not true.
    if actual not in types and not (actual == "integer" and "number" in types):
        reason = REASON_NULL_NOT_ALLOWED if actual == "null" else REASON_TYPE_MISMATCH
        problems.append(
            Violation(
                reason,
                pointer,
                f"expected {sorted(types)}, got {actual}",
            )
        )
        return problems

    if actual == "string":
        problems.extend(_validate_string(node, value, pointer=pointer))
    elif actual in {"integer", "number"}:
        problems.extend(_validate_number(node, value, pointer=pointer))
    elif actual == "array":
        problems.extend(
            _validate_array(node, value, parts=parts, definitions=definitions)
        )
    elif actual == "object":
        problems.extend(
            _validate_object(node, value, parts=parts, definitions=definitions)
        )
    return problems


def _validate_string(
    node: Mapping[str, Any], value: str, *, pointer: str
) -> list[Violation]:
    problems: list[Violation] = []
    maximum = node.get("maxLength")
    if maximum is not None and len(value) > int(maximum):
        problems.append(
            Violation(
                REASON_STRING_TOO_LONG,
                pointer,
                f"{len(value)} characters exceeds maxLength {maximum}",
            )
        )
    minimum = node.get("minLength")
    if minimum is not None and len(value) < int(minimum):
        problems.append(
            Violation(
                REASON_STRING_TOO_SHORT,
                pointer,
                f"{len(value)} characters is below minLength {minimum}",
            )
        )
    pattern = node.get("pattern")
    if pattern is not None and re.compile(pattern).fullmatch(value) is None:
        problems.append(
            Violation(REASON_PATTERN_MISMATCH, pointer, f"does not match `{pattern}`")
        )
    declared_format = node.get("format")
    if declared_format is not None and not _FORMAT_CHECKS[declared_format](value):
        problems.append(
            Violation(
                REASON_INVALID_FORMAT,
                pointer,
                f"{value!r} is not a valid `{declared_format}`",
            )
        )
    return problems


def _validate_number(
    node: Mapping[str, Any], value: Any, *, pointer: str
) -> list[Violation]:
    problems: list[Violation] = []
    minimum = node.get("minimum")
    if minimum is not None and value < minimum:
        problems.append(
            Violation(REASON_NUMBER_OUT_OF_RANGE, pointer, f"{value} < {minimum}")
        )
    maximum = node.get("maximum")
    if maximum is not None and value > maximum:
        problems.append(
            Violation(REASON_NUMBER_OUT_OF_RANGE, pointer, f"{value} > {maximum}")
        )
    return problems


def _validate_array(
    node: Mapping[str, Any],
    value: list[Any],
    *,
    parts: list[str],
    definitions: Mapping[str, Any],
) -> list[Violation]:
    pointer = _pointer(parts)
    problems: list[Violation] = []
    maximum = node.get("maxItems")
    if maximum is not None and len(value) > int(maximum):
        problems.append(
            Violation(
                REASON_ARRAY_TOO_LONG,
                pointer,
                f"{len(value)} items exceeds maxItems {maximum}",
            )
        )
    minimum = node.get("minItems")
    if minimum is not None and len(value) < int(minimum):
        problems.append(
            Violation(
                REASON_ARRAY_TOO_SHORT,
                pointer,
                f"{len(value)} items is below minItems {minimum}",
            )
        )
    if node.get("uniqueItems") is True:
        rendered = [canonical_json(item) for item in value]
        if len(set(rendered)) != len(rendered):
            problems.append(
                Violation(REASON_DUPLICATE_ITEM, pointer, "items must be unique")
            )
    for index, item in enumerate(value):
        problems.extend(
            _validate_node(
                node["items"],
                item,
                parts=parts + [str(index)],
                definitions=definitions,
            )
        )
    return problems


def _validate_object(
    node: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    parts: list[str],
    definitions: Mapping[str, Any],
) -> list[Violation]:
    problems: list[Violation] = []
    additional = node.get("additionalProperties")
    if additional is False:
        properties = node["properties"]
        for name in sorted(set(value) - set(properties)):
            problems.append(
                Violation(
                    REASON_UNKNOWN_FIELD,
                    _pointer(parts + [name]),
                    f"`{name}` is not declared by this contract",
                )
            )
        for name in node.get("required", []):
            if name not in value:
                problems.append(
                    Violation(
                        REASON_MISSING_FIELD,
                        _pointer(parts + [name]),
                        f"`{name}` is required",
                    )
                )
        for name in sorted(set(value) & set(properties)):
            problems.extend(
                _validate_node(
                    properties[name],
                    value[name],
                    parts=parts + [name],
                    definitions=definitions,
                )
            )
        return problems

    names = node["propertyNames"]
    for name in sorted(value):
        child = parts + [name]
        problems.extend(
            _validate_node(names, name, parts=child, definitions=definitions)
        )
        problems.extend(
            _validate_node(
                additional, value[name], parts=child, definitions=definitions
            )
        )
    return problems


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def corpus_root(root: str | Path | None = None) -> Path:
    """Return the canonical corpus directory."""
    if root is None:
        return REPO_ROOT / CORPUS_RELATIVE_PATH
    return Path(root)


def _read_json(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path} is not valid JSON: {error}") from error


def load_schema(component: str, *, root: str | Path | None = None) -> dict[str, Any]:
    """Load one component's canonical schema."""
    if component not in COMPONENTS:
        raise ContractError(f"unknown component `{component}`")
    path = corpus_root(root) / SCHEMA_DIRNAME / SCHEMA_FILENAMES[component]
    schema = _read_json(path)
    if not isinstance(schema, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return schema


def corpus_files(root: str | Path | None = None) -> list[str]:
    """Return every canonical corpus file as a sorted repo-relative POSIX path.

    ``manifest.json`` is excluded: it carries the digest of everything else, so
    including it would make the identity depend on itself.
    """
    base = corpus_root(root)
    if not base.is_dir():
        raise ContractError(f"contract corpus directory is missing: {base}")
    return sorted(
        path.relative_to(base).as_posix()
        for path in base.rglob("*.json")
        if path.is_file() and path.name != MANIFEST_FILENAME
    )


def file_digests(root: str | Path | None = None) -> dict[str, str]:
    """Return ``{relative path: sha256}`` over every canonical corpus file."""
    base = corpus_root(root)
    digests: dict[str, str] = {}
    for name in corpus_files(base):
        try:
            digests[name] = sha256_hex((base / name).read_bytes())
        except OSError as error:
            raise ContractError(f"cannot read {base / name}: {error}") from error
    return digests


def corpus_digest(root: str | Path | None = None) -> str:
    """Digest the corpus bytes in stable path order.

    The pre-image is ``"<relative path>\\n<sha256 hex>\\n"`` per file in
    ascending path order, which is the same construction the protocol-1 bridge
    manifest uses. A renamed file moves the digest even when its bytes do not,
    because the path is part of the contract.
    """
    joined = "".join(
        f"{name}\n{digest}\n" for name, digest in sorted(file_digests(root).items())
    )
    return sha256_hex(joined.encode("utf-8"))


def contract_set_id(root: str | Path | None = None) -> str:
    """Return the contract-set ID derived from the corpus bytes."""
    return f"{CONTRACT_SET_PREFIX}.{corpus_digest(root)[:CONTRACT_SET_ID_HEX_CHARS]}"


# ---------------------------------------------------------------------------
# Compatibility manifest
# ---------------------------------------------------------------------------


def _capabilities(root: str | Path | None = None) -> dict[str, Any]:
    """Read the supported capabilities out of the bridge schema itself.

    The manifest never restates a vocabulary: it points at the one the schema
    already closed, so the two cannot disagree.
    """
    schema = load_schema(COMPONENT_BRIDGE_PROTOCOL, root=root)
    definitions = schema["definitions"]
    hook = load_schema(COMPONENT_LIFECYCLE_HOOK, root=root)
    return {
        "bridge_methods": list(definitions["Method"]["enum"]),
        "bridge_mutation_methods": list(definitions["MutationMethod"]["enum"]),
        "bridge_events": list(definitions["EventName"]["enum"]),
        "bridge_error_codes": list(definitions["ErrorCode"]["enum"]),
        "lifecycle_events": list(hook["definitions"]["EventName"]["enum"]),
        "lifecycle_phases": list(hook["definitions"]["Phase"]["enum"]),
        "projection_phases": list(
            load_schema(COMPONENT_PROJECTION, root=root)["definitions"]["Phase"]["enum"]
        ),
    }


def build_manifest(root: str | Path | None = None) -> dict[str, Any]:
    """Build the compatibility manifest from the corpus bytes.

    Everything here is derived. Nothing is transcribed, so the manifest cannot
    describe a corpus other than the one beside it.
    """
    base = corpus_root(root)
    digests = file_digests(base)
    identity = contract_set_id(base)
    components: dict[str, Any] = {}
    for component in COMPONENTS:
        relative = f"{SCHEMA_DIRNAME}/{SCHEMA_FILENAMES[component]}"
        schema = load_schema(component, root=base)
        fixture_prefix = f"{FIXTURE_DIRNAME}/{FIXTURE_DIRNAMES[component]}/"
        components[component] = {
            "schema_path": relative,
            "schema_version": schema["version"],
            "digest": digests[relative],
            "roots": sorted(schema["roots"]),
            "max_document_bytes": schema["max_document_bytes"],
            "fixture_count": sum(
                1 for name in digests if name.startswith(fixture_prefix)
            ),
        }
    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_by": "python3 -m agent_session_loop.agent_mux_contract --write",
        "current_contract_set_id": identity,
        "contract_sets": [
            {
                "contract_set_id": identity,
                "status": CONTRACT_SET_STATUS_CURRENT,
                "corpus_digest": corpus_digest(base),
                "components": components,
                "capabilities": _capabilities(base),
            }
        ],
        "files": digests,
    }


def read_manifest(root: str | Path | None = None) -> dict[str, Any]:
    """Load the checked-in compatibility manifest."""
    path = corpus_root(root) / MANIFEST_FILENAME
    manifest = _read_json(path)
    if not isinstance(manifest, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return manifest


def current_contract_set(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the manifest entry the current contract-set ID names."""
    identity = manifest.get("current_contract_set_id")
    for entry in manifest.get("contract_sets", []):
        if entry.get("contract_set_id") == identity:
            return dict(entry)
    raise ContractError(
        f"manifest names current contract set `{identity}` but does not define it"
    )


def manifest_violations(root: str | Path | None = None) -> list[Violation]:
    """Return every way the checked-in manifest disagrees with the corpus."""
    base = corpus_root(root)
    expected = build_manifest(base)
    try:
        actual = read_manifest(base)
    except ContractError as error:
        return [Violation(REASON_DIGEST_MISMATCH, "", str(error))]

    problems: list[Violation] = []
    expected_id = expected["current_contract_set_id"]
    actual_id = actual.get("current_contract_set_id")
    if actual_id != expected_id:
        problems.append(
            Violation(
                REASON_DIGEST_MISMATCH,
                "/current_contract_set_id",
                f"manifest claims `{actual_id}`; corpus derives `{expected_id}`",
            )
        )
    actual_files = actual.get("files")
    if actual_files != expected["files"]:
        expected_names = set(expected["files"])
        actual_names = set(actual_files or {})
        for name in sorted(expected_names - actual_names):
            problems.append(
                Violation(
                    REASON_DIGEST_MISMATCH,
                    _pointer(["files", name]),
                    "corpus file is absent from the manifest",
                )
            )
        for name in sorted(actual_names - expected_names):
            problems.append(
                Violation(
                    REASON_DIGEST_MISMATCH,
                    _pointer(["files", name]),
                    "manifest names a file the corpus does not carry",
                )
            )
        for name in sorted(expected_names & actual_names):
            if expected["files"][name] != (actual_files or {})[name]:
                problems.append(
                    Violation(
                        REASON_DIGEST_MISMATCH,
                        _pointer(["files", name]),
                        "manifest digest does not match the file beside it",
                    )
                )
    if not problems and canonical_json(actual) != canonical_json(expected):
        problems.append(
            Violation(
                REASON_DIGEST_MISMATCH,
                "",
                "manifest content differs from the corpus it describes",
            )
        )
    return problems


def write_manifest(root: str | Path | None = None) -> Path:
    """Rewrite ``manifest.json`` from the corpus; return the path written."""
    base = corpus_root(root)
    path = base / MANIFEST_FILENAME
    path.write_bytes(canonical_json(build_manifest(base)))
    return path


# ---------------------------------------------------------------------------
# Fixture corpus
# ---------------------------------------------------------------------------

_FIXTURE_KEYS = frozenset({"case", "description", "root", "document"})
_INVALID_FIXTURE_KEYS = _FIXTURE_KEYS | {"reason", "pointer"}


@dataclass(frozen=True)
class FixtureResult:
    """One fixture's verdict."""

    component: str
    path: str
    case: str
    expected_valid: bool
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "path": self.path,
            "case": self.case,
            "expected_valid": self.expected_valid,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CorpusReport:
    """The whole corpus checked against itself."""

    contract_set_id: str
    corpus_digest: str
    corpus_root: str
    schema_violations: dict[str, list[Violation]]
    manifest_violations: list[Violation]
    fixtures: list[FixtureResult]
    noncanonical: list[str]

    @property
    def fixture_failures(self) -> list[FixtureResult]:
        return [fixture for fixture in self.fixtures if not fixture.passed]

    @property
    def ok(self) -> bool:
        return (
            not any(self.schema_violations.values())
            and not self.manifest_violations
            and not self.fixture_failures
            and not self.noncanonical
        )


def fixture_paths(
    component: str, *, valid: bool, root: str | Path | None = None
) -> list[Path]:
    """Return one component's positive or negative fixture files, sorted."""
    directory = (
        corpus_root(root)
        / FIXTURE_DIRNAME
        / FIXTURE_DIRNAMES[component]
        / (VALID_DIRNAME if valid else INVALID_DIRNAME)
    )
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _check_fixture(
    schema: Mapping[str, Any],
    path: Path,
    *,
    component: str,
    relative: str,
    expected_valid: bool,
) -> FixtureResult:
    fixture = _read_json(path)
    allowed = _FIXTURE_KEYS if expected_valid else _INVALID_FIXTURE_KEYS
    if not isinstance(fixture, dict) or set(fixture) - allowed:
        return FixtureResult(
            component,
            relative,
            path.stem,
            expected_valid,
            False,
            "fixture envelope carries keys outside the declared set",
        )
    missing = (
        {"case", "root", "document"} | (set() if expected_valid else {"reason"})
    ) - set(fixture)
    if missing:
        return FixtureResult(
            component,
            relative,
            path.stem,
            expected_valid,
            False,
            f"fixture envelope is missing {sorted(missing)}",
        )

    case = str(fixture["case"])
    problems = validate_document(schema, fixture["document"], root=fixture["root"])
    if expected_valid:
        if problems:
            return FixtureResult(
                component,
                relative,
                case,
                True,
                False,
                "; ".join(
                    f"{p.reason} at {p.pointer or '/'}: {p.detail}" for p in problems
                ),
            )
        return FixtureResult(component, relative, case, True, True, "accepted")

    if not problems:
        return FixtureResult(
            component,
            relative,
            case,
            False,
            False,
            f"expected `{fixture['reason']}` but the document was accepted",
        )
    reasons = {problem.reason for problem in problems}
    if fixture["reason"] not in reasons:
        return FixtureResult(
            component,
            relative,
            case,
            False,
            False,
            f"expected `{fixture['reason']}`, got {sorted(reasons)}",
        )
    pointer = fixture.get("pointer")
    if pointer is not None and not any(
        problem.pointer == pointer and problem.reason == fixture["reason"]
        for problem in problems
    ):
        return FixtureResult(
            component,
            relative,
            case,
            False,
            False,
            f"expected `{fixture['reason']}` at `{pointer}`, got other locations",
        )
    return FixtureResult(
        component, relative, case, False, True, f"rejected: {fixture['reason']}"
    )


def check_corpus(root: str | Path | None = None) -> CorpusReport:
    """Check the corpus against itself and return the whole verdict.

    This is the operation ``asl contract-check --agent-mux`` reports and the one
    a release gate runs: schemas lint clean, the manifest matches the bytes
    beside it, every positive fixture is accepted, every negative fixture is
    rejected for the exact reason it declares, and every file is already the
    canonical rendering of its own content — which is regeneration being
    byte-identical, stated as a check rather than as a habit.
    """
    base = corpus_root(root)
    schema_problems: dict[str, list[Violation]] = {}
    fixtures: list[FixtureResult] = []
    for component in COMPONENTS:
        schema = load_schema(component, root=base)
        problems = lint_schema(schema)
        schema_problems[component] = problems
        if problems:
            continue
        for expected_valid in (True, False):
            for path in fixture_paths(component, valid=expected_valid, root=base):
                fixtures.append(
                    _check_fixture(
                        schema,
                        path,
                        component=component,
                        relative=path.relative_to(base).as_posix(),
                        expected_valid=expected_valid,
                    )
                )
    return CorpusReport(
        contract_set_id=contract_set_id(base),
        corpus_digest=corpus_digest(base),
        corpus_root=str(base),
        schema_violations=schema_problems,
        manifest_violations=manifest_violations(base),
        fixtures=fixtures,
        noncanonical=noncanonical_files(base),
    )


def noncanonical_files(root: str | Path | None = None) -> list[str]:
    """Return corpus files whose bytes are not the canonical rendering.

    This is the determinism property stated as a check: if every file already
    equals ``canonical_json`` of its own content, regenerating the corpus is a
    no-op and two people editing it independently produce comparable diffs.
    """
    base = corpus_root(root)
    drifted: list[str] = []
    for name in [*corpus_files(base), MANIFEST_FILENAME]:
        path = base / name
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if raw != canonical_json(json.loads(raw.decode("utf-8"))):
            drifted.append(name)
    return sorted(drifted)


def report_payload(
    report: CorpusReport, *, state_root: str | None = None
) -> dict[str, Any]:
    """Render a :class:`CorpusReport` as the JSON block contract-check emits.

    The manifest is read from the same corpus the report was built against, and
    a manifest that cannot be read is reported rather than raised: the report
    already carries the ``digest-mismatch`` that says so, and a caller asking
    what is wrong with the corpus deserves an answer instead of an exception.
    """
    try:
        manifest = read_manifest(report.corpus_root)
        entry = current_contract_set(manifest)
        manifest_version = manifest.get("manifest_version")
    except ContractError:
        entry = {"components": {}, "capabilities": {}}
        manifest_version = None
    components = [
        {
            "component": component,
            **{
                key: value
                for key, value in entry["components"].get(component, {}).items()
                if key
                in {"schema_path", "schema_version", "digest", "max_document_bytes"}
            },
            "schema_status": (
                "ok"
                if not report.schema_violations[component]
                else REASON_SCHEMA_INVALID
            ),
        }
        for component in COMPONENTS
    ]
    return {
        "contract_set_id": report.contract_set_id,
        "corpus_digest": report.corpus_digest,
        "corpus_root": report.corpus_root,
        "manifest_version": manifest_version,
        "state_root": state_root,
        "components": components,
        "capabilities": entry["capabilities"],
        "fixtures": {
            "total": len(report.fixtures),
            "accepted": sum(1 for fixture in report.fixtures if fixture.expected_valid),
            "rejected": sum(
                1 for fixture in report.fixtures if not fixture.expected_valid
            ),
            "failures": [fixture.to_dict() for fixture in report.fixture_failures],
        },
        "issues": [
            *(
                violation.to_dict()
                for component in COMPONENTS
                for violation in report.schema_violations[component]
            ),
            *(violation.to_dict() for violation in report.manifest_violations),
            *(
                {
                    "reason": "fixture-failed",
                    "pointer": fixture.path,
                    "detail": fixture.detail,
                }
                for fixture in report.fixture_failures
            ),
            *(
                {
                    "reason": "noncanonical-bytes",
                    "pointer": name,
                    "detail": "file is not the canonical rendering of its content",
                }
                for name in report.noncanonical
            ),
        ],
    }


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------


def _print_violations(label: str, violations: Iterable[Violation]) -> None:
    for violation in violations:
        location = violation.pointer or "/"
        print(f"  {label}: {violation.reason} at {location}: {violation.detail}")


def main(argv: list[str] | None = None) -> int:
    """Check or regenerate the canonical corpus."""
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_session_loop.agent_mux_contract",
        description=(
            "Check the ASL-owned AgentMux compatibility corpus, or regenerate "
            "its derived manifest from the schema and fixture bytes."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="report corpus problems without writing anything (default)",
    )
    group.add_argument(
        "--write",
        action="store_true",
        help="rewrite manifest.json from the corpus bytes",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="corpus directory (default: the checked-in canonical corpus)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON output")
    args = parser.parse_args(argv)

    try:
        if args.write:
            path = write_manifest(args.corpus)
            print(f"wrote {path}")
        report = check_corpus(args.corpus)
    except ContractError as error:
        print(f"agent-mux-contract: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report_payload(report), indent=2, sort_keys=True))
        return 0 if report.ok else 1

    print(f"Contract set: {report.contract_set_id}")
    print(f"Corpus digest: {report.corpus_digest}")
    print(f"Fixtures: {len(report.fixtures)}")
    for component in COMPONENTS:
        _print_violations(component, report.schema_violations[component])
    _print_violations("manifest", report.manifest_violations)
    for fixture in report.fixture_failures:
        print(f"  fixture: {fixture.path}: {fixture.detail}")
    for name in report.noncanonical:
        print(f"  canonical-json: {name} is not stored in canonical form")
    if report.ok:
        print("corpus ok")
        return 0
    print("corpus has issues", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
