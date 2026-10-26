"""Field-level validators shared by every payload check.

Part of :mod:`asl_workbench.transport`, which re-exports everything here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .catalog import (
    KEY_CHARS,
)
from .observation import (
    MAX_LABEL_CHARS,
    MAX_WARNINGS,
)
from .operations import (
    FORBIDDEN_PERSISTED_KEYS,
)
from .transport_shapes import (
    TARGET_SHAPE,
    _Shape,
)
from .transport_vocabulary import (
    ERROR_BODY_LEAK,
    ERROR_FRAME_TOO_LARGE,
    ERROR_INVALID_FIELD,
    ERROR_MISSING_FIELD,
    ERROR_UNKNOWN_FIELD,
    MAX_WARNING_CHARS,
    TransportError,
)

# --------------------------------------------------------------------------
# Field validation
# --------------------------------------------------------------------------

#: An identifier: something that may be logged, compared and used as a key.
#: No whitespace, no punctuation a path separator could hide in.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: The digests :mod:`asl_workbench.catalog` produces.
_KEY = re.compile(rf"^[0-9a-f]{{{KEY_CHARS}}}$")

#: ASL's dotted session numbering.
_SESSION_ID = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")


def refuse_non_finite(value: str) -> float:
    """Refuse ``NaN`` and the infinities rather than decoding them.

    :mod:`json` accepts all three by default. None survives a round trip
    through storage or a browser, so a frame containing one is malformed here
    rather than a row that fails later.
    """
    raise TransportError(ERROR_INVALID_FIELD, f"non-finite number: {value}")


def _fail(code: str, where: str, detail: str) -> TransportError:
    return TransportError(code, f"{where}: {detail}")


def check_shape(
    document: Mapping[str, Any], shape: _Shape, where: str
) -> Mapping[str, Any]:
    """Refuse any name the shape does not declare, and any it requires."""
    names = set(document)
    unknown = sorted(names - shape.allowed)
    if unknown:
        raise _fail(ERROR_UNKNOWN_FIELD, where, ", ".join(unknown))
    missing = sorted(shape.required - names)
    if missing:
        raise _fail(ERROR_MISSING_FIELD, where, ", ".join(missing))
    return document


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(ERROR_INVALID_FIELD, where, "expected an object")
    for name in value:
        if not isinstance(name, str):
            raise _fail(ERROR_INVALID_FIELD, where, "expected string keys")
    return value


def _sequence(value: Any, where: str, *, limit: int) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a list")
    if len(value) > limit:
        raise _fail(ERROR_FRAME_TOO_LARGE, where, f"more than {limit} entries")
    return value


def _text(value: Any, where: str, *, limit: int = MAX_LABEL_CHARS) -> str:
    if not isinstance(value, str):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a string")
    if len(value) > limit:
        raise _fail(ERROR_INVALID_FIELD, where, f"longer than {limit} characters")
    return value


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.match(value):
        raise _fail(ERROR_INVALID_FIELD, where, "expected an identifier")
    return value


def _key(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise _fail(ERROR_INVALID_FIELD, where, f"expected {KEY_CHARS} hex characters")
    return value


def _word(value: Any, where: str, vocabulary: Iterable[str]) -> str:
    allowed = tuple(vocabulary)
    if value not in allowed:
        raise _fail(
            ERROR_INVALID_FIELD, where, f"not one of {len(allowed)} known words"
        )
    assert isinstance(value, str)
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(ERROR_INVALID_FIELD, where, "expected true or false")
    return value


#: SQLite's largest INTEGER, which every count is eventually stored as.
_MAX_COUNT = 2**63 - 1


def _count(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(ERROR_INVALID_FIELD, where, "expected an integer")
    if value < minimum:
        raise _fail(ERROR_INVALID_FIELD, where, f"below {minimum}")
    if value > _MAX_COUNT:
        # Past what the store's integer columns can bind.
        raise _fail(ERROR_INVALID_FIELD, where, f"above {_MAX_COUNT}")
    return value


def _number(value: Any, where: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a number")
    try:
        number = float(value)
    except OverflowError:
        # An integer too large for a float is no more finite than ``inf``.
        raise _fail(ERROR_INVALID_FIELD, where, "expected a finite number") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise _fail(ERROR_INVALID_FIELD, where, "expected a finite number")
    if minimum is not None and number < minimum:
        raise _fail(ERROR_INVALID_FIELD, where, f"below {minimum}")
    return number


def _optional(document: Mapping[str, Any], name: str, where: str, check: Any) -> None:
    """Apply ``check`` to ``document[name]`` when it is present and not null.

    ``null`` is accepted for every optional field, and means "the Mac could not
    determine this" -- which is why the store's columns are nullable.
    """
    if name not in document:
        return
    value = document[name]
    if value is None:
        return
    check(value, f"{where}.{name}")


def _warnings(value: Any, where: str) -> None:
    for index, warning in enumerate(_sequence(value, where, limit=MAX_WARNINGS)):
        _text(warning, f"{where}[{index}]", limit=MAX_WARNING_CHARS)


def assert_no_body(document: Any, *, where: str) -> None:
    """Refuse any key that names prose, at any depth.

    :data:`asl_workbench.operations.FORBIDDEN_PERSISTED_KEYS` is the list the
    operations module already keeps for records on disk. The same walk runs on
    the wire so a leak is a refused frame rather than a refused write: by the
    time a write is attempted the prose has already crossed the network.
    """
    if isinstance(document, Mapping):
        for name, value in document.items():
            if isinstance(name, str) and name in FORBIDDEN_PERSISTED_KEYS:
                raise _fail(ERROR_BODY_LEAK, where, name)
            assert_no_body(value, where=f"{where}.{name}")
        return
    if isinstance(document, (str, bytes)) or not isinstance(document, Sequence):
        return
    for index, value in enumerate(document):
        assert_no_body(value, where=f"{where}[{index}]")


# --------------------------------------------------------------------------
# Payload validation
# --------------------------------------------------------------------------


def _validate_target(value: Any, where: str) -> None:
    target = _mapping(value, where)
    check_shape(target, TARGET_SHAPE, where)
    subjects = [
        name for name in ("plan_key", "archive", "archive_id") if name in target
    ]
    if len(subjects) != 1:
        raise _fail(ERROR_INVALID_FIELD, where, "exactly one subject is required")
    if "plan_key" in target:
        _key(target["plan_key"], f"{where}.plan_key")
    if "archive" in target:
        _word(target["archive"], f"{where}.archive", ("list", "settings"))
    if "archive_id" in target:
        _identifier(target["archive_id"], f"{where}.archive_id")
    if "plan_key" not in target and any(
        name in target for name in ("run_key", "run_id", "question_id", "session_id")
    ):
        raise _fail(ERROR_INVALID_FIELD, where, "run selectors require a plan_key")
    _optional(target, "run_key", where, _key)
    _optional(target, "run_id", where, _identifier)
    _optional(target, "question_id", where, _identifier)
    _optional(
        target,
        "session_id",
        where,
        lambda value, at: _word_matching(value, at, _SESSION_ID),
    )


def _word_matching(value: Any, where: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise _fail(ERROR_INVALID_FIELD, where, "malformed")
    return value


__all__ = [
    "assert_no_body",
    "check_shape",
    "refuse_non_finite",
]
