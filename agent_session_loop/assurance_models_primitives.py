"""Fail-closed primitive validators and issue-identifier helpers.

Also holds the canonical JSON serialization for assurance records.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .assurance_models_vocab import (
    MAX_ISSUE_REFERENCES,
    MAX_LOCATOR_CHARS,
    MAX_TEXT_CHARS,
    MAX_TOKEN_CHARS,
    SUPPORTED_ASSURANCE_SCHEMA_VERSIONS,
)


class AssuranceError(Exception):
    """Raised when assurance data is malformed or an artifact cannot be written."""


# ---------------------------------------------------------------------------
# Primitive validation
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^[a-z0-9]+(?:[._\-/][a-z0-9]+)*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9]+(?:[._\-][A-Za-z0-9]+)*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ISSUE_ID_RE = re.compile(r"^(?:ASI-)?([1-9][0-9]*)$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: A lone surrogate arrives only as a JSON escape such as ``\ud800``. It
#: cannot be encoded as UTF-8, so storing or printing such text would raise.
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

ISSUE_ID_PREFIX = "ASI-"

#: Longest decimal an issue id may carry.
MAX_ISSUE_ID_DIGITS = 18

#: An identity key is the durable, machine-facing name of one canonical issue.
#: It is minted from an ID the ledger allocated, so the key space is ASL's and
#: cannot be entered by describing a defect differently.
IDENTITY_KEY_PREFIX = "issue.asi-"
_IDENTITY_KEY_RE = re.compile(r"^issue\.asi-([1-9][0-9]*)$")


def _require_object(
    payload: object, *, label: str, allowed_keys: Sequence[str]
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise AssuranceError(f"{label} must be a JSON object")
    unknown = sorted(str(key) for key in payload if key not in allowed_keys)
    if unknown:
        raise AssuranceError(
            f"{label} has unknown key(s) {', '.join(repr(key) for key in unknown)}; "
            f"allowed keys are {', '.join(repr(key) for key in allowed_keys)}"
        )
    return dict(payload)


def _require_text(value: object, *, label: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise AssuranceError(f"{label} must be a string")
    if not value.strip():
        raise AssuranceError(f"{label} must be a non-empty string")
    # Validation never rewrites its input. Accepting padded text and storing the
    # trimmed form would make a parsed model differ from the bytes it came from,
    # which is exactly what deterministic round-tripping rules out.
    if value != value.strip():
        raise AssuranceError(
            f"{label} must not have leading or trailing whitespace; got {value!r}"
        )
    if len(value) > max_chars:
        raise AssuranceError(
            f"{label} must be at most {max_chars} characters; got {len(value)}"
        )
    if _CONTROL_RE.search(value):
        raise AssuranceError(f"{label} must not contain control characters")
    if _SURROGATE_RE.search(value):
        raise AssuranceError(f"{label} must not contain lone surrogate code points")
    return value


def _optional_text(
    value: object, *, label: str, max_chars: int = MAX_TEXT_CHARS
) -> str | None:
    if value is None:
        return None
    return _require_text(value, label=label, max_chars=max_chars)


def _require_token(value: object, *, label: str) -> str:
    text = _require_text(value, label=label, max_chars=MAX_TOKEN_CHARS)
    if not _TOKEN_RE.match(text):
        raise AssuranceError(
            f"{label} must be a lowercase identifier of alphanumeric segments "
            f"joined by '.', '_', '-', or '/'; got {text!r}"
        )
    return text


def _require_identifier(value: object, *, label: str) -> str:
    """Validate a case-preserving external identifier such as a run or plan ID.

    Distinct from :func:`_require_token` because those identifiers are minted
    elsewhere — ``20260810T101112123456Z-4242`` for a run, a directory basename
    for a plan — and lowercasing them would name something that does not exist.
    """
    text = _require_text(value, label=label, max_chars=MAX_TOKEN_CHARS)
    if not _IDENTIFIER_RE.match(text):
        raise AssuranceError(
            f"{label} must be alphanumeric segments joined by '.', '_', or '-'; "
            f"got {text!r}"
        )
    return text


def _require_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssuranceError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise AssuranceError(
            f"{label} must be between {minimum} and {maximum}; got {value}"
        )
    return value


def _require_number(value: object, *, label: str, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssuranceError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise AssuranceError(f"{label} must be a finite number; got {value!r}")
    if number < minimum:
        raise AssuranceError(f"{label} must be >= {minimum}; got {number}")
    return number


def _require_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise AssuranceError(f"{label} must be a boolean")
    return value


def _require_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _HEX64_RE.match(value):
        raise AssuranceError(
            f"{label} must be a lowercase 64-character SHA-256 hex digest; got "
            f"{value!r}"
        )
    return value


def _require_timestamp(value: object, *, label: str) -> str:
    text = _require_text(value, label=label, max_chars=MAX_TOKEN_CHARS)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as error:
        raise AssuranceError(
            f"{label} must be an ISO-8601 timestamp; got {text!r} ({error})"
        ) from error
    if parsed.tzinfo is None:
        raise AssuranceError(f"{label} must carry an explicit UTC offset; got {text!r}")
    return text


def _require_locator(value: object, *, label: str) -> str:
    text = _require_text(value, label=label, max_chars=MAX_LOCATOR_CHARS)
    # ``PurePosixPath`` normalizes ``./`` and doubled separators away, so the
    # raw segments decide. A locator is stored and matched verbatim, and two
    # spellings of one path would break reference resolution.
    if (
        "\\" in text
        or text.startswith("~")
        or PurePosixPath(text).is_absolute()
        or PureWindowsPath(text).is_absolute()
        or any(segment in ("", ".", "..") for segment in text.split("/"))
    ):
        raise AssuranceError(
            f"{label} must be a run-relative reference in canonical form and "
            f"cannot be absolute or traverse upward; got {text!r}"
        )
    return text


def _require_sequence(value: object, *, label: str, maximum: int) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise AssuranceError(f"{label} must be a list")
    items = list(value)
    if len(items) > maximum:
        raise AssuranceError(
            f"{label} must hold at most {maximum} entries; got {len(items)}"
        )
    return items


def _require_schema_version(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssuranceError(f"{label} must be an integer schema version")
    if value not in SUPPORTED_ASSURANCE_SCHEMA_VERSIONS:
        supported = ", ".join(
            str(item) for item in sorted(SUPPORTED_ASSURANCE_SCHEMA_VERSIONS)
        )
        raise AssuranceError(
            f"{label} {value} is unsupported; this build reads version(s) {supported}"
        )
    return value


def parse_issue_id(value: object) -> str:
    """Return the canonical ``ASI-N`` form of an issue reference.

    Accepts the canonical form, a bare positive decimal such as ``42``, and the
    integer ``42``. Zero, negatives, leading zeros, and anything else are
    rejected so a shorthand can never resolve to a different issue than the
    canonical ID it abbreviates.
    """
    if isinstance(value, bool):
        raise AssuranceError(
            f"issue id must be {ISSUE_ID_PREFIX}N or a positive integer; got {value!r}"
        )
    if isinstance(value, int):
        if value < 1:
            raise AssuranceError(f"issue id must be a positive integer; got {value}")
        value = str(value)
    elif not isinstance(value, str):
        raise AssuranceError(
            f"issue id must be {ISSUE_ID_PREFIX}N or a positive integer; got {value!r}"
        )
    match = _ISSUE_ID_RE.match(value.strip())
    if match is None:
        raise AssuranceError(
            f"issue id must be {ISSUE_ID_PREFIX}N or a positive decimal without "
            f"leading zeros; got {value!r}"
        )
    digits = match.group(1)
    if len(digits) > MAX_ISSUE_ID_DIGITS:
        # Far past any number the ledger allocates, and short of the length at
        # which ``int()`` refuses the string, so ``issue_id_number`` cannot raise.
        raise AssuranceError(
            f"issue id must have at most {MAX_ISSUE_ID_DIGITS} digits; got "
            f"{len(digits)}"
        )
    return f"{ISSUE_ID_PREFIX}{digits}"


def issue_id_number(value: object) -> int:
    """Return the monotonic integer behind an issue reference."""
    return int(parse_issue_id(value)[len(ISSUE_ID_PREFIX) :])


def identity_key_for_issue(value: object) -> str:
    """Return the identity key minted for issue ``value``.

    The key is a pure function of an ID the ledger allocated. Deriving it from a
    category, component, invariant key, summary, or severity would let an agent
    that rewords a finding rename the issue that owns it.
    """
    return f"{IDENTITY_KEY_PREFIX}{issue_id_number(value)}"


def parse_identity_key(value: object) -> str:
    """Validate an identity key, failing closed."""
    text = _require_token(value, label="identity key")
    if _IDENTITY_KEY_RE.match(text) is None:
        raise AssuranceError(
            f"identity key must be {IDENTITY_KEY_PREFIX}N naming an issue ASL "
            f"minted; got {text!r}"
        )
    return text


def issue_id_for_identity_key(value: object) -> str:
    """Return the issue an identity key names, which a merge may have moved."""
    return f"{ISSUE_ID_PREFIX}{parse_identity_key(value)[len(IDENTITY_KEY_PREFIX) :]}"


def parse_reason_token(value: object) -> str:
    """Validate a reason class in the token form every artifact stores it as.

    Reasons are classes, not messages: an audit and a summary both store one so
    a recurring failure groups under a stable key, which raw exception text
    never does.
    """
    return _require_token(value, label="reason")


def _require_issue_ids(value: object, *, label: str) -> tuple[str, ...]:
    items = _require_sequence(value, label=label, maximum=MAX_ISSUE_REFERENCES)
    issue_ids = tuple(parse_issue_id(item) for item in items)
    duplicates = _duplicates(issue_ids)
    if duplicates:
        raise AssuranceError(f"{label} repeats issue id(s) {', '.join(duplicates)}")
    return issue_ids


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    repeated: dict[str, None] = {}
    for value in values:
        if value in seen:
            repeated[value] = None
        seen.add(value)
    return sorted(repeated)


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize ``payload`` to stable, newline-terminated UTF-8 JSON.

    Sorted keys and fixed separators make byte equality a usable assertion:
    the same model always produces the same artifact, so an artifact digest
    identifies content rather than a serialization run.
    """
    text = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return text.encode("utf-8") + b"\n"
