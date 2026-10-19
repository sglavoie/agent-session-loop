"""Agent mux contract models."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
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
