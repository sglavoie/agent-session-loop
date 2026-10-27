"""Agent mux contract validation."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

from . import (
    agent_mux_contract_formats as formats,
)
from . import (
    agent_mux_contract_models as models,
)
from . import (
    agent_mux_contract_schema as schema_module,
)

# ---------------------------------------------------------------------------
# Document validation
# ---------------------------------------------------------------------------


def validate_document(
    schema: Mapping[str, Any],
    document: Any,
    *,
    root: str | None = None,
) -> list[models.Violation]:
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
                models.Violation(
                    models.REASON_UNKNOWN_ROOT,
                    "",
                    f"schema declares {len(roots)} roots; name one",
                )
            ]
        root = next(iter(roots))
    if root not in roots:
        return [
            models.Violation(
                models.REASON_UNKNOWN_ROOT,
                "",
                f"unknown root `{root}`",
            )
        ]

    problems: list[models.Violation] = list(
        _privacy_violations(
            document,
            forbidden=set(schema["forbidden_property_names"]),
            exempt=set(schema["body_bearing_pointers"]),
        )
    )

    size = len(models.canonical_json(document))
    ceiling = int(schema["max_document_bytes"])
    if size > ceiling:
        problems.append(
            models.Violation(
                models.REASON_DOCUMENT_TOO_LARGE,
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
) -> Iterator[models.Violation]:
    # Two pointers are tracked per node: the concrete one a reader needs to find
    # the leak, and the index-free template the exemption list is written in.
    stack: list[tuple[Any, list[str], list[str]]] = [(document, [], [])]
    while stack:
        node, parts, template = stack.pop()
        if isinstance(node, dict):
            for key in sorted(node):
                child = parts + [key]
                child_template = template + [key]
                pointer = models._pointer(child)
                if key in forbidden and models._pointer(child_template) not in exempt:
                    yield models.Violation(
                        models.REASON_BODY_NOT_ALLOWED,
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


def _json_equal(left: Any, right: Any) -> bool:
    """JSON Schema equality: Python's ``True == 1`` is not JSON's.

    A boolean equals only a boolean; ``1`` and ``1.0`` are the same number.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return bool(left == right)


def _validate_node(
    node: Mapping[str, Any],
    value: Any,
    *,
    parts: list[str],
    definitions: Mapping[str, Any],
) -> list[models.Violation]:
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = definitions[ref[len("#/definitions/") :]]
        return _validate_node(target, value, parts=parts, definitions=definitions)

    pointer = models._pointer(parts)
    problems: list[models.Violation] = []

    if "const" in node:
        if not _json_equal(value, node["const"]):
            problems.append(
                models.Violation(
                    models.REASON_CONST_MISMATCH,
                    pointer,
                    f"expected {node['const']!r}, got {value!r}",
                )
            )
        return problems
    if "enum" in node:
        if not any(_json_equal(value, option) for option in node["enum"]):
            problems.append(
                models.Violation(
                    models.REASON_INVALID_ENUM,
                    pointer,
                    f"{value!r} is not one of {sorted(map(str, node['enum']))}",
                )
            )
        return problems

    types = schema_module._node_types(node)
    actual = _json_type(value)
    # `integer` satisfies a `number` declaration; the reverse is not true.
    if actual not in types and not (actual == "integer" and "number" in types):
        reason = (
            models.REASON_NULL_NOT_ALLOWED
            if actual == "null"
            else models.REASON_TYPE_MISMATCH
        )
        problems.append(
            models.Violation(
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
) -> list[models.Violation]:
    problems: list[models.Violation] = []
    maximum = node.get("maxLength")
    if maximum is not None and len(value) > int(maximum):
        problems.append(
            models.Violation(
                models.REASON_STRING_TOO_LONG,
                pointer,
                f"{len(value)} characters exceeds maxLength {maximum}",
            )
        )
    minimum = node.get("minLength")
    if minimum is not None and len(value) < int(minimum):
        problems.append(
            models.Violation(
                models.REASON_STRING_TOO_SHORT,
                pointer,
                f"{len(value)} characters is below minLength {minimum}",
            )
        )
    pattern = node.get("pattern")
    if pattern is not None and re.compile(pattern).fullmatch(value) is None:
        problems.append(
            models.Violation(
                models.REASON_PATTERN_MISMATCH,
                pointer,
                f"does not match `{pattern}`",
            )
        )
    declared_format = node.get("format")
    if declared_format is not None and not formats._FORMAT_CHECKS[declared_format](
        value
    ):
        problems.append(
            models.Violation(
                models.REASON_INVALID_FORMAT,
                pointer,
                f"{value!r} is not a valid `{declared_format}`",
            )
        )
    return problems


def _validate_number(
    node: Mapping[str, Any], value: Any, *, pointer: str
) -> list[models.Violation]:
    problems: list[models.Violation] = []
    minimum = node.get("minimum")
    if minimum is not None and value < minimum:
        problems.append(
            models.Violation(
                models.REASON_NUMBER_OUT_OF_RANGE,
                pointer,
                f"{value} < {minimum}",
            )
        )
    maximum = node.get("maximum")
    if maximum is not None and value > maximum:
        problems.append(
            models.Violation(
                models.REASON_NUMBER_OUT_OF_RANGE,
                pointer,
                f"{value} > {maximum}",
            )
        )
    return problems


def _validate_array(
    node: Mapping[str, Any],
    value: list[Any],
    *,
    parts: list[str],
    definitions: Mapping[str, Any],
) -> list[models.Violation]:
    pointer = models._pointer(parts)
    problems: list[models.Violation] = []
    maximum = node.get("maxItems")
    if maximum is not None and len(value) > int(maximum):
        problems.append(
            models.Violation(
                models.REASON_ARRAY_TOO_LONG,
                pointer,
                f"{len(value)} items exceeds maxItems {maximum}",
            )
        )
    minimum = node.get("minItems")
    if minimum is not None and len(value) < int(minimum):
        problems.append(
            models.Violation(
                models.REASON_ARRAY_TOO_SHORT,
                pointer,
                f"{len(value)} items is below minItems {minimum}",
            )
        )
    if node.get("uniqueItems") is True:
        rendered = [models.canonical_json(item) for item in value]
        if len(set(rendered)) != len(rendered):
            problems.append(
                models.Violation(
                    models.REASON_DUPLICATE_ITEM,
                    pointer,
                    "items must be unique",
                )
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
) -> list[models.Violation]:
    problems: list[models.Violation] = []
    additional = node.get("additionalProperties")
    if additional is False:
        properties = node["properties"]
        for name in sorted(set(value) - set(properties)):
            problems.append(
                models.Violation(
                    models.REASON_UNKNOWN_FIELD,
                    models._pointer(parts + [name]),
                    f"`{name}` is not declared by this contract",
                )
            )
        for name in node.get("required", []):
            if name not in value:
                problems.append(
                    models.Violation(
                        models.REASON_MISSING_FIELD,
                        models._pointer(parts + [name]),
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

    if additional is None:
        # `_lint_object_node` refuses a schema that omits `additionalProperties`,
        # but validation runs against whatever schema it is handed. Without this
        # the map branch below would pass `None` to `_validate_node` and die on
        # an attribute lookup several frames deeper, blaming the document for a
        # defect in the contract.
        raise models.ContractError(
            f"schema node at {models._pointer(parts)} declares no "
            "`additionalProperties`; an object node must be closed or a map"
        )
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
