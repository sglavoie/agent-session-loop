"""Agent mux contract schema."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from . import agent_mux_contract_formats as formats
from . import agent_mux_contract_models as models

# ---------------------------------------------------------------------------
# Schema linting
# ---------------------------------------------------------------------------


def lint_schema(schema: Mapping[str, Any]) -> list[models.Violation]:
    """Return every way ``schema`` fails the closed-vocabulary rules.

    The linter is what makes "closed fields, bounded values" a property of the
    corpus rather than a habit of whoever last edited it: an object without
    ``additionalProperties: false``, a free-text string without a ceiling, or an
    unbounded array is a schema defect reported here, long before a document is
    validated against it.
    """
    problems: list[models.Violation] = []

    def fail(pointer: str, detail: str) -> None:
        problems.append(models.Violation(models.REASON_SCHEMA_INVALID, pointer, detail))

    keys = set(schema)
    for unknown in sorted(keys - formats._SCHEMA_TOP_LEVEL_KEYS):
        fail(
            models._pointer([unknown]),
            f"unknown top-level schema key `{unknown}`",
        )
    for missing in sorted(formats._SCHEMA_TOP_LEVEL_REQUIRED - keys):
        fail("", f"schema is missing required key `{missing}`")
    if problems:
        return problems

    if schema["$schema"] != formats.SCHEMA_MARKER:
        fail(
            "/$schema",
            f"schema marker must be `{formats.SCHEMA_MARKER}`",
        )
    if schema["component"] not in models.COMPONENTS:
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
            fail(
                models._pointer(["roots", name]),
                f"root points at unknown `{definition}`",
            )

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
            pointer=models._pointer(["definitions", name]),
            definitions=definitions,
            fail=fail,
        )
    for definition in roots.values() if isinstance(roots, dict) else []:
        _collect_reachable(definition, definitions, reachable)
    for orphan in sorted(set(definitions) - reachable):
        fail(
            models._pointer(["definitions", orphan]),
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
                        found[models._pointer(child)] = name
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
    for unknown in sorted(set(node) - formats._NODE_KEYS):
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
    for unknown in sorted(types - formats._JSON_TYPES):
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
    if declared_format is not None and declared_format not in formats.FORMATS:
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
