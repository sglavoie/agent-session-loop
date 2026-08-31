"""Durable vocabulary for supervisor audits and the assurance inbox.

The supervisor audit judges ASL's own control plane after a persisted loop
ends. Its verdicts outlive the run that produced them: they are appended to an
immutable ledger, deduplicated across runs by fingerprint, and dispositioned by
a human weeks later. That makes the vocabulary itself the durable contract, so
it is defined here — before any storage, orchestration, or agent invocation —
and every other assurance module builds on these models rather than on ad-hoc
dictionaries.

Four rules shape the schema:

* **Fingerprints are deliberately narrow.** A finding's identity is its schema
  version, category, component, and stable invariant key — nothing else. Run
  IDs, timestamps, summaries, and evidence are all volatile, and folding any of
  them in would make the same defect look new on every run, which defeats the
  deduplication the inbox depends on.
* **All-clear is an assertion, not a default.** A terminal audit must carry
  lane-by-lane evidence for every rubric lane, and ``all-clear`` additionally
  requires every lane to have passed. An audit that simply failed to look at a
  lane cannot claim the lane is clean; it is ``unavailable`` instead.
* **Canonical artifacts are written exactly once.** :func:`write_new_bytes`
  uses ``O_CREAT | O_EXCL`` rather than the last-writer-wins
  :func:`agent_session_loop.artifacts.atomic_write_bytes`, because a second
  write to ``supervisor-audit.json`` means two audits disagreed about one run
  and silently overwriting one of them destroys the evidence of that conflict.
* **Everything is bounded and fails closed.** Text, collections, references,
  and serialized artifacts all have explicit caps, and every parser rejects
  unknown schema versions, unknown keys, duplicate lanes, malformed severity,
  traversal-capable references, and references to evidence the audit never
  declared.

Parsing mirrors :func:`agent_session_loop.contracts.parse_completion_contract`:
errors name the offending field and the rule it broke, so a malformed record is
quotable rather than merely rejected.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from . import durability
from .artifacts import sha256_hex
from .harnesses import HARNESS_NAMES, HarnessName
from .literals import as_literal

#: Bumped whenever the meaning of any model below changes. The version is part
#: of every finding fingerprint, so a schema change deliberately retires the
#: old identities instead of silently merging them with the new ones.
ASSURANCE_SCHEMA_VERSION = 1

#: Versions this build can read. Anything else is rejected before any side
#: effect, mirroring :func:`agent_session_loop.prepare.parse_validator_output`.
SUPPORTED_ASSURANCE_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

AuditState = Literal[
    "all-clear", "findings", "partial", "unavailable", "skipped", "pending"
]

#: Every state a run's assurance object can hold, in reporting order.
#: ``partial`` is the honest middle: the audit ran, found nothing wrong, and
#: could not judge every lane. Calling that ``all-clear`` overstates it and
#: calling it ``unavailable`` throws away the lanes that were judged.
AUDIT_STATES: tuple[AuditState, ...] = (
    "all-clear",
    "findings",
    "partial",
    "unavailable",
    "skipped",
    "pending",
)

#: States that assert a complete audit and therefore require full lane
#: coverage. ``partial`` and ``unavailable`` may carry partial deterministic
#: findings, ``skipped`` carries none, and ``pending`` has not run yet.
TERMINAL_AUDIT_STATES: frozenset[str] = frozenset({"all-clear", "findings"})

#: States the ledger files as a completed audit. ``partial`` belongs here and
#: not above: the audit ran to a verdict, so it completed, but it never claims
#: the full lane coverage the terminal states assert.
COMPLETED_AUDIT_STATES: frozenset[str] = TERMINAL_AUDIT_STATES | {"partial"}

RubricLane = Literal[
    "identity-authority",
    "task-packet-coherence",
    "lifecycle-retry-resume",
    "verification-review-independence",
    "mutation-commit-guards",
    "truthfulness-observability",
    "resources-stalls",
    "unknown-anomalies",
]

#: The rubric an audit must cover. The order is stable because it is rendered
#: to humans and compared in tests.
RUBRIC_LANES: tuple[RubricLane, ...] = (
    "identity-authority",
    "task-packet-coherence",
    "lifecycle-retry-resume",
    "verification-review-independence",
    "mutation-commit-guards",
    "truthfulness-observability",
    "resources-stalls",
    "unknown-anomalies",
)

CheckStatus = Literal["pass", "fail", "unavailable"]

#: A deterministic check either proved the invariant, disproved it, or could
#: not see enough to judge. Missing evidence is ``unavailable``, never ``pass``.
CHECK_STATUSES: tuple[CheckStatus, ...] = ("pass", "fail", "unavailable")

Severity = Literal["low", "medium", "high", "critical"]

#: Severities in ascending order of urgency.
SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high", "critical")

IssueState = Literal["open", "resolved", "dismissed", "accepted-risk"]

#: Lifecycle states an inbox issue can hold.
ISSUE_STATES: tuple[IssueState, ...] = (
    "open",
    "resolved",
    "dismissed",
    "accepted-risk",
)

DispositionKind = Literal["resolve", "dismiss", "accept-risk"]

#: Human dispositions, and the issue state each one produces. Reopening is not
#: a disposition: it is a consequence of a recurrence and belongs to the
#: projection, not to a human command.
DISPOSITION_KINDS: tuple[DispositionKind, ...] = ("resolve", "dismiss", "accept-risk")
DISPOSITION_RESULT_STATE: Mapping[str, IssueState] = {
    "resolve": "resolved",
    "dismiss": "dismissed",
    "accept-risk": "accepted-risk",
}

CloseoutOutcome = Literal["accepted", "reworked", "escaped"]

#: How a human closed out a run's assurance verdict.
CLOSEOUT_OUTCOMES: tuple[CloseoutOutcome, ...] = ("accepted", "reworked", "escaped")

IdentityBindingSource = Literal["allocation", "merge", "reconcile"]

#: Why an identity key names the issue it names. ``allocation`` is the key an
#: issue was minted with, ``merge`` is a key whose history moved to another
#: issue, and ``reconcile`` is an explicitly reviewed operator decision.
IDENTITY_BINDING_SOURCES: tuple[IdentityBindingSource, ...] = (
    "allocation",
    "merge",
    "reconcile",
)

AttemptOutcome = Literal[
    "ok",
    "timeout",
    "transport-error",
    "malformed-output",
    "missing-lanes",
    "invalid-reference",
    "interrupted",
    "mutation-detected",
    "unavailable",
]

#: Why one agent attempt ended. Everything except ``ok`` is a reason to retry
#: once or to declare the audit unavailable.
ATTEMPT_OUTCOMES: tuple[AttemptOutcome, ...] = (
    "ok",
    "timeout",
    "transport-error",
    "malformed-output",
    "missing-lanes",
    "invalid-reference",
    "interrupted",
    "mutation-detected",
    "unavailable",
)

EvidenceKind = Literal[
    "artifact",
    "event",
    "state",
    "status",
    "plan",
    "task-packet",
    "prompt",
    "harness",
    "verification",
    "review",
    "commit",
    "repository",
]

#: What an evidence reference points at. Kinds are closed so a reader can route
#: a reference without guessing from its path shape.
EVIDENCE_KINDS: tuple[EvidenceKind, ...] = (
    "artifact",
    "event",
    "state",
    "status",
    "plan",
    "task-packet",
    "prompt",
    "harness",
    "verification",
    "review",
    "commit",
    "repository",
)

AuditRoleSource = Literal["explicit", "review"]

#: Whether the audit role came from an explicit ``--audit-harness`` or was
#: inherited from the effective review harness.
AUDIT_ROLE_SOURCES: tuple[AuditRoleSource, ...] = ("explicit", "review")


# ---------------------------------------------------------------------------
# Canonical artifact names
# ---------------------------------------------------------------------------

AUDIT_INPUT_FILENAME = "supervisor-audit-input.json"
AUDIT_REPORT_FILENAME = "supervisor-audit.json"
AUDIT_MARKDOWN_FILENAME = "supervisor-audit.md"
ATTEMPT_DIR_PREFIX = "supervisor-audit-attempt-"

#: Canonical per-run audit artifacts, in the order they are produced.
CANONICAL_AUDIT_ARTIFACTS: tuple[str, ...] = (
    AUDIT_INPUT_FILENAME,
    AUDIT_REPORT_FILENAME,
    AUDIT_MARKDOWN_FILENAME,
)

#: Budget for attempts the agent *answered*: one retry in a fresh invocation
#: after a malformed or incomplete answer, and no more. A bad answer is the
#: agent's own doing, so the budget stays tight.
MAX_AUDIT_ATTEMPTS = 2

#: Budget for attempts that never reached the agent at all — a timeout or a
#: transport/capacity failure. Delivery is the provider's doing, not the
#: agent's, so it gets its own, more forgiving budget: a provider outage
#: lasting minutes must not consume the answer retries or fail the audit.
MAX_AUDIT_DELIVERY_ATTEMPTS = 4

#: Budget for attempts the provider refused before the agent ran because the
#: request itself was too large. Neither the agent nor the provider is at fault
#: — the caller sent too much — so it is charged here rather than to either of
#: the budgets above, which would let one oversized bundle spend the retries a
#: real bad answer or a real outage is owed. One retry only: the shed order
#: gives up the least useful evidence first, so a second, smaller bundle would
#: start trading away the terminal evidence the audit exists to read.
MAX_AUDIT_SHRINK_ATTEMPTS = 1

#: Upper bound on recorded attempts: every budget can be spent in full within
#: one audit, and every attempt is recorded.
MAX_AUDIT_ATTEMPT_RECORDS = (
    MAX_AUDIT_ATTEMPTS + MAX_AUDIT_DELIVERY_ATTEMPTS + MAX_AUDIT_SHRINK_ATTEMPTS
)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

#: Identifier-shaped fields: categories, components, invariant keys, check
#: names, run IDs, harness models, and reason classes.
MAX_TOKEN_CHARS = 200

#: Free text a human reads: summaries, recommendations, reasons, notes.
MAX_TEXT_CHARS = 2000

#: An evidence locator is a run-relative path or pointer, never a payload.
MAX_LOCATOR_CHARS = 512

#: References attached to a single finding, lane, or check result.
MAX_REFERENCES_PER_ITEM = 32

#: The declared evidence index of one audit.
MAX_EVIDENCE_ENTRIES = 512

#: Findings and deterministic results in one audit.
MAX_FINDINGS = 200
MAX_DETERMINISTIC_RESULTS = 200

#: Issue IDs attached to one summary or closeout.
MAX_ISSUE_REFERENCES = 200

#: Serialized size of any canonical audit artifact. Session collectors cap
#: their own input well below this; the writer refuses anything larger so an
#: unbounded payload cannot reach the run directory.
MAX_ARTIFACT_BYTES = 1024 * 1024


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
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

ISSUE_ID_PREFIX = "ASI-"

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
        return f"{ISSUE_ID_PREFIX}{value}"
    if not isinstance(value, str):
        raise AssuranceError(
            f"issue id must be {ISSUE_ID_PREFIX}N or a positive integer; got {value!r}"
        )
    match = _ISSUE_ID_RE.match(value.strip())
    if match is None:
        raise AssuranceError(
            f"issue id must be {ISSUE_ID_PREFIX}N or a positive decimal without "
            f"leading zeros; got {value!r}"
        )
    return f"{ISSUE_ID_PREFIX}{match.group(1)}"


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


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceRef:
    """One run-relative pointer at material the audit actually read."""

    kind: EvidenceKind
    locator: str
    digest: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        as_literal(self.kind, EVIDENCE_KINDS, "evidence 'kind'", error=AssuranceError)
        _require_locator(self.locator, label="evidence 'locator'")
        if self.digest is not None:
            _require_digest(self.digest, label="evidence 'digest'")
        if self.detail is not None:
            _require_text(self.detail, label="evidence 'detail'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "locator": self.locator,
            "digest": self.digest,
            "detail": self.detail,
        }


def parse_evidence_ref(payload: object, *, label: str = "evidence") -> EvidenceRef:
    """Parse one evidence reference, failing closed."""
    data = _require_object(
        payload, label=label, allowed_keys=("kind", "locator", "digest", "detail")
    )
    return EvidenceRef(
        kind=as_literal(
            data.get("kind"), EVIDENCE_KINDS, f"{label} 'kind'", error=AssuranceError
        ),
        locator=_require_locator(data.get("locator"), label=f"{label} 'locator'"),
        digest=(
            None
            if data.get("digest") is None
            else _require_digest(data.get("digest"), label=f"{label} 'digest'")
        ),
        detail=_optional_text(data.get("detail"), label=f"{label} 'detail'"),
    )


def _require_references(value: object, *, label: str) -> tuple[str, ...]:
    items = _require_sequence(value, label=label, maximum=MAX_REFERENCES_PER_ITEM)
    if not items:
        raise AssuranceError(f"{label} must name at least one evidence locator")
    locators = tuple(
        _require_locator(item, label=f"{label}[{index}]")
        for index, item in enumerate(items)
    )
    duplicates = _duplicates(locators)
    if duplicates:
        raise AssuranceError(
            f"{label} repeats evidence locator(s) "
            f"{', '.join(repr(item) for item in duplicates)}"
        )
    return locators


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


def finding_fingerprint(
    *,
    schema_version: int,
    category: str,
    component: str,
    invariant_key: str,
) -> str:
    """Return the stable deduplication key for a finding.

    Only these four inputs participate. Run IDs, timestamps, summaries,
    recommendations, severities, and evidence are all excluded on purpose: the
    same defect seen in two runs must produce one issue with two occurrences,
    and any volatile input would instead produce two issues.
    """
    version = _require_schema_version(
        schema_version, label="fingerprint schema version"
    )
    material = "\x1f".join(
        (
            "assurance-finding",
            str(version),
            _require_token(category, label="fingerprint 'category'"),
            _require_token(component, label="fingerprint 'component'"),
            _require_token(invariant_key, label="fingerprint 'invariant_key'"),
        )
    )
    return sha256_hex(material.encode("utf-8"))


@dataclass(frozen=True)
class Finding:
    """One control-plane defect the audit is willing to put a human's name on.

    ``localization`` points at the ASL or skill source the defect was traced to.
    It is deliberately not evidence: evidence is what the run recorded, while a
    localization is where the behavior comes from, so it is never required to
    appear in the audit's evidence index and never participates in the
    fingerprint.
    """

    category: str
    component: str
    invariant_key: str
    lane: RubricLane
    severity: Severity
    summary: str
    recommendation: str
    evidence: tuple[str, ...]
    localization: tuple[str, ...] = ()
    schema_version: int = ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "localization", tuple(self.localization))
        _require_schema_version(self.schema_version, label="finding 'schema_version'")
        _require_token(self.category, label="finding 'category'")
        _require_token(self.component, label="finding 'component'")
        _require_token(self.invariant_key, label="finding 'invariant_key'")
        as_literal(self.lane, RUBRIC_LANES, "finding 'lane'", error=AssuranceError)
        as_literal(
            self.severity, SEVERITIES, "finding 'severity'", error=AssuranceError
        )
        _require_text(self.summary, label="finding 'summary'")
        _require_text(self.recommendation, label="finding 'recommendation'")
        _require_references(self.evidence, label="finding 'evidence'")
        if self.localization:
            _require_references(self.localization, label="finding 'localization'")

    @property
    def fingerprint(self) -> str:
        """The stable identity this finding deduplicates by."""
        return finding_fingerprint(
            schema_version=self.schema_version,
            category=self.category,
            component=self.component,
            invariant_key=self.invariant_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "category": self.category,
            "component": self.component,
            "invariant_key": self.invariant_key,
            "lane": self.lane,
            "severity": self.severity,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "evidence": list(self.evidence),
            "localization": list(self.localization),
            "fingerprint": self.fingerprint,
        }


def parse_finding(payload: object, *, label: str = "finding") -> Finding:
    """Parse one finding, recomputing rather than trusting its fingerprint."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "schema_version",
            "category",
            "component",
            "invariant_key",
            "lane",
            "severity",
            "summary",
            "recommendation",
            "evidence",
            "localization",
            "fingerprint",
        ),
    )
    localization = data.get("localization") or ()
    finding = Finding(
        schema_version=_require_schema_version(
            data.get("schema_version"), label=f"{label} 'schema_version'"
        ),
        category=_require_token(data.get("category"), label=f"{label} 'category'"),
        component=_require_token(data.get("component"), label=f"{label} 'component'"),
        invariant_key=_require_token(
            data.get("invariant_key"), label=f"{label} 'invariant_key'"
        ),
        lane=as_literal(
            data.get("lane"), RUBRIC_LANES, f"{label} 'lane'", error=AssuranceError
        ),
        severity=as_literal(
            data.get("severity"),
            SEVERITIES,
            f"{label} 'severity'",
            error=AssuranceError,
        ),
        summary=_require_text(data.get("summary"), label=f"{label} 'summary'"),
        recommendation=_require_text(
            data.get("recommendation"), label=f"{label} 'recommendation'"
        ),
        evidence=_require_references(data.get("evidence"), label=f"{label} 'evidence'"),
        localization=(
            ()
            if not localization
            else _require_references(localization, label=f"{label} 'localization'")
        ),
    )
    declared = data.get("fingerprint")
    if declared is not None and declared != finding.fingerprint:
        raise AssuranceError(
            f"{label} 'fingerprint' {declared!r} does not match the fingerprint "
            f"derived from its schema version, category, component, and invariant "
            f"key ({finding.fingerprint})"
        )
    return finding


# ---------------------------------------------------------------------------
# Normalized finding text
# ---------------------------------------------------------------------------

#: The trailing line, or line and column, a localization pointer may carry. Two
#: sightings of one defect almost never name the same line, so the file is the
#: durable half of the pointer and the position is not.
_LOCALIZATION_POSITION_RE = re.compile(r":[0-9]+(?::[0-9]+)?$")

#: Split points between words. Punctuation, backticks, and path separators all
#: vanish, so ``runner.py`` and ``runner`` share a token, and the dots, dashes,
#: and slashes inside a category or invariant key become segment boundaries.
_WORD_SPLIT_RE = re.compile(r"[^0-9a-z]+")

_STOPWORD_TEXT = """
a an and are as at be been being but by can could did do does for from had
has have if in into is it its may might must of on or our should so than
that the their them then there these they this those to was were will with
would you your
"""

#: Function words carried by nearly every summary. They are dropped before
#: comparison so similarity measures the defect rather than English. Negations
#: are deliberately absent: dropping ``not`` would let a summary and its
#: opposite normalize to the same tokens.
SUMMARY_STOPWORDS: frozenset[str] = frozenset(_STOPWORD_TEXT.split())


def normalize_localization(value: str) -> str:
    """Return the source file a localization pointer names, without a position.

    Normalization is only ever used to compare or index; the stored pointer
    keeps its line, because that is what a human opening the file needs.
    """
    return _LOCALIZATION_POSITION_RE.sub("", value)


def normalize_summary_tokens(value: str) -> tuple[str, ...]:
    """Return the sorted, deduplicated content words of a summary.

    Sorted and deduplicated because the result is a set used for overlap: word
    order and repetition say nothing about whether two summaries describe one
    defect, and a stable order makes the token list itself comparable.
    """
    tokens = {
        token
        for token in _WORD_SPLIT_RE.split(value.casefold())
        if len(token) > 1 and token not in SUMMARY_STOPWORDS
    }
    return tuple(sorted(tokens))


def normalize_invariant_tokens(value: str) -> tuple[str, ...]:
    """Return the sorted, deduplicated segments of an invariant key.

    An invariant key is a dotted and dashed path, and renaming a defect usually
    rewrites one of its segments rather than all of them. Splitting the key into
    segments is what lets ``retry.budget-clamped`` be recognized as a rewording
    of ``retry.budget-respected`` instead of an unrelated key.

    Only the invariant key is split. A component path is mostly the directories
    every sibling module shares, and a category is a bucket name, so segments
    from either would report an overlap for defects that merely live in the same
    package. No stopword is dropped: these are ASL-shaped tokens, not English.
    """
    tokens = {
        token for token in _WORD_SPLIT_RE.split(value.casefold()) if len(token) > 1
    }
    return tuple(sorted(tokens))


# ---------------------------------------------------------------------------
# Deterministic checks and rubric lanes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeterministicResult:
    """The verdict of one reproducible check, with the evidence it read."""

    check: str
    lane: RubricLane
    status: CheckStatus
    invariant_key: str
    evidence: tuple[str, ...]
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        _require_token(self.check, label="deterministic result 'check'")
        as_literal(
            self.lane, RUBRIC_LANES, "deterministic result 'lane'", error=AssuranceError
        )
        as_literal(
            self.status,
            CHECK_STATUSES,
            "deterministic result 'status'",
            error=AssuranceError,
        )
        _require_token(self.invariant_key, label="deterministic result 'invariant_key'")
        _require_references(self.evidence, label="deterministic result 'evidence'")
        if self.detail is not None:
            _require_text(self.detail, label="deterministic result 'detail'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "lane": self.lane,
            "status": self.status,
            "invariant_key": self.invariant_key,
            "evidence": list(self.evidence),
            "detail": self.detail,
        }


def parse_deterministic_result(
    payload: object, *, label: str = "deterministic result"
) -> DeterministicResult:
    """Parse one deterministic check result, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("check", "lane", "status", "invariant_key", "evidence", "detail"),
    )
    return DeterministicResult(
        check=_require_token(data.get("check"), label=f"{label} 'check'"),
        lane=as_literal(
            data.get("lane"), RUBRIC_LANES, f"{label} 'lane'", error=AssuranceError
        ),
        status=as_literal(
            data.get("status"),
            CHECK_STATUSES,
            f"{label} 'status'",
            error=AssuranceError,
        ),
        invariant_key=_require_token(
            data.get("invariant_key"), label=f"{label} 'invariant_key'"
        ),
        evidence=_require_references(data.get("evidence"), label=f"{label} 'evidence'"),
        detail=_optional_text(data.get("detail"), label=f"{label} 'detail'"),
    )


@dataclass(frozen=True)
class LaneEvidence:
    """What the audit actually looked at for one rubric lane.

    A lane always names evidence, including when its status is ``unavailable``:
    the reference then points at the record proving the lane could not be
    judged, so a gap in coverage stays visible instead of reading as silence.
    """

    lane: RubricLane
    status: CheckStatus
    summary: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        as_literal(self.lane, RUBRIC_LANES, "lane 'lane'", error=AssuranceError)
        as_literal(self.status, CHECK_STATUSES, "lane 'status'", error=AssuranceError)
        _require_text(self.summary, label="lane 'summary'")
        _require_references(self.evidence, label=f"lane {self.lane!r} 'evidence'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "status": self.status,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }


def parse_lane_evidence(payload: object, *, label: str = "lane") -> LaneEvidence:
    """Parse one rubric-lane evidence record, failing closed."""
    data = _require_object(
        payload, label=label, allowed_keys=("lane", "status", "summary", "evidence")
    )
    return LaneEvidence(
        lane=as_literal(
            data.get("lane"), RUBRIC_LANES, f"{label} 'lane'", error=AssuranceError
        ),
        status=as_literal(
            data.get("status"),
            CHECK_STATUSES,
            f"{label} 'status'",
            error=AssuranceError,
        ),
        summary=_require_text(data.get("summary"), label=f"{label} 'summary'"),
        evidence=_require_references(data.get("evidence"), label=f"{label} 'evidence'"),
    )


def validate_lane_coverage(
    lanes: Sequence[LaneEvidence], *, require_complete: bool
) -> None:
    """Reject duplicate lanes and, for a terminal audit, missing ones.

    ``require_complete`` is the difference between an audit that reports what
    it saw and one that asserts a verdict over the whole rubric.
    """
    duplicates = _duplicates(lane.lane for lane in lanes)
    if duplicates:
        raise AssuranceError(
            f"audit repeats rubric lane(s) {', '.join(duplicates)}; each lane "
            "must appear exactly once"
        )
    if not require_complete:
        return
    covered = {lane.lane for lane in lanes}
    missing = [lane for lane in RUBRIC_LANES if lane not in covered]
    if missing:
        raise AssuranceError(
            f"a terminal audit must carry evidence for every rubric lane; "
            f"missing {', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# The per-run audit artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorAudit:
    """The canonical, immutable verdict written to ``supervisor-audit.json``."""

    run_id: str
    state: AuditState
    input_digest: str
    generated_at: str
    evidence: tuple[EvidenceRef, ...]
    lanes: tuple[LaneEvidence, ...] = ()
    findings: tuple[Finding, ...] = ()
    deterministic: tuple[DeterministicResult, ...] = ()
    reason: str | None = None
    schema_version: int = ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "lanes", tuple(self.lanes))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "deterministic", tuple(self.deterministic))
        _require_schema_version(self.schema_version, label="audit 'schema_version'")
        _require_identifier(self.run_id, label="audit 'run_id'")
        as_literal(self.state, AUDIT_STATES, "audit 'state'", error=AssuranceError)
        _require_digest(self.input_digest, label="audit 'input_digest'")
        _require_timestamp(self.generated_at, label="audit 'generated_at'")
        if len(self.evidence) > MAX_EVIDENCE_ENTRIES:
            raise AssuranceError(
                f"audit 'evidence' must hold at most {MAX_EVIDENCE_ENTRIES} entries; "
                f"got {len(self.evidence)}"
            )
        if len(self.findings) > MAX_FINDINGS:
            raise AssuranceError(
                f"audit 'findings' must hold at most {MAX_FINDINGS} entries; "
                f"got {len(self.findings)}"
            )
        if len(self.deterministic) > MAX_DETERMINISTIC_RESULTS:
            raise AssuranceError(
                f"audit 'deterministic' must hold at most "
                f"{MAX_DETERMINISTIC_RESULTS} entries; got {len(self.deterministic)}"
            )
        self._validate_evidence_index()
        self._validate_findings()
        self._validate_state()

    def _validate_evidence_index(self) -> None:
        duplicates = _duplicates(entry.locator for entry in self.evidence)
        if duplicates:
            raise AssuranceError(
                f"audit 'evidence' repeats locator(s) "
                f"{', '.join(repr(item) for item in duplicates)}"
            )
        declared = {entry.locator for entry in self.evidence}
        referenced: list[tuple[str, str]] = []
        for lane in self.lanes:
            referenced.extend((f"lane {lane.lane!r}", ref) for ref in lane.evidence)
        for finding in self.findings:
            referenced.extend(
                (f"finding {finding.invariant_key!r}", ref) for ref in finding.evidence
            )
        for result in self.deterministic:
            referenced.extend(
                (f"deterministic result {result.check!r}", ref)
                for ref in result.evidence
            )
        unknown = sorted({ref for owner, ref in referenced if ref not in declared})
        if unknown:
            raise AssuranceError(
                f"audit references evidence it never declared: "
                f"{', '.join(repr(item) for item in unknown)}"
            )

    def _validate_findings(self) -> None:
        duplicates = _duplicates(finding.fingerprint for finding in self.findings)
        if duplicates:
            raise AssuranceError(
                "audit 'findings' repeats fingerprint(s) "
                f"{', '.join(duplicates)}; one audit reports a defect once"
            )

    def _validate_state(self) -> None:
        terminal = self.state in TERMINAL_AUDIT_STATES
        validate_lane_coverage(self.lanes, require_complete=terminal)
        if self.state in {"skipped", "unavailable", "partial"}:
            if self.reason is None:
                raise AssuranceError(
                    f"an audit in state {self.state!r} must record why"
                )
            _require_token(self.reason, label="audit 'reason'")
        elif self.reason is not None:
            raise AssuranceError(
                f"an audit in state {self.state!r} must not record a reason"
            )
        if self.state == "partial" and self.findings:
            raise AssuranceError(
                "a partial audit cannot carry findings; its state is 'findings'"
            )
        if self.state == "all-clear":
            if self.findings:
                raise AssuranceError(
                    "an all-clear audit cannot carry findings; its state is 'findings'"
                )
            unproven = [lane.lane for lane in self.lanes if lane.status != "pass"]
            if unproven:
                raise AssuranceError(
                    "all-clear requires every rubric lane to pass on its own "
                    f"evidence; lane(s) {', '.join(unproven)} did not"
                )
        elif self.state == "findings" and not self.findings:
            raise AssuranceError(
                "an audit in state 'findings' must carry at least one finding"
            )
        elif self.state in {"skipped", "pending"}:
            if self.findings or self.lanes or self.deterministic:
                raise AssuranceError(
                    f"an audit in state {self.state!r} performed no analysis and "
                    "must carry no lanes, findings, or deterministic results"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "input_digest": self.input_digest,
            "generated_at": self.generated_at,
            "reason": self.reason,
            "evidence": [entry.to_dict() for entry in self.evidence],
            "lanes": [lane.to_dict() for lane in self.lanes],
            "findings": [finding.to_dict() for finding in self.findings],
            "deterministic": [result.to_dict() for result in self.deterministic],
        }


def parse_supervisor_audit(payload: object) -> SupervisorAudit:
    """Parse a ``supervisor-audit.json`` payload, failing closed."""
    label = "audit"
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "schema_version",
            "run_id",
            "state",
            "input_digest",
            "generated_at",
            "reason",
            "evidence",
            "lanes",
            "findings",
            "deterministic",
        ),
    )
    evidence = tuple(
        parse_evidence_ref(item, label=f"{label} evidence #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("evidence"),
                label=f"{label} 'evidence'",
                maximum=MAX_EVIDENCE_ENTRIES,
            )
        )
    )
    lanes = tuple(
        parse_lane_evidence(item, label=f"{label} lane #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("lanes", []),
                label=f"{label} 'lanes'",
                maximum=len(RUBRIC_LANES),
            )
        )
    )
    findings = tuple(
        parse_finding(item, label=f"{label} finding #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("findings", []),
                label=f"{label} 'findings'",
                maximum=MAX_FINDINGS,
            )
        )
    )
    deterministic = tuple(
        parse_deterministic_result(
            item, label=f"{label} deterministic result #{index + 1}"
        )
        for index, item in enumerate(
            _require_sequence(
                data.get("deterministic", []),
                label=f"{label} 'deterministic'",
                maximum=MAX_DETERMINISTIC_RESULTS,
            )
        )
    )
    return SupervisorAudit(
        schema_version=_require_schema_version(
            data.get("schema_version"), label=f"{label} 'schema_version'"
        ),
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        state=as_literal(
            data.get("state"), AUDIT_STATES, f"{label} 'state'", error=AssuranceError
        ),
        input_digest=_require_digest(
            data.get("input_digest"), label=f"{label} 'input_digest'"
        ),
        generated_at=_require_timestamp(
            data.get("generated_at"), label=f"{label} 'generated_at'"
        ),
        reason=(
            None
            if data.get("reason") is None
            else _require_token(data.get("reason"), label=f"{label} 'reason'")
        ),
        evidence=evidence,
        lanes=lanes,
        findings=findings,
        deterministic=deterministic,
    )


# ---------------------------------------------------------------------------
# Inbox records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueObservation:
    """One sighting of a finding in one run.

    The observation carries the run and the moment; the finding carries the
    identity. Keeping them apart is what lets the ledger record a hundred
    sightings of one defect without a hundred issues.
    """

    run_id: str
    finding: Finding
    observed_at: str
    audit_artifact: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.run_id, label="observation 'run_id'")
        if not isinstance(self.finding, Finding):
            raise AssuranceError("observation 'finding' must be a Finding")
        _require_timestamp(self.observed_at, label="observation 'observed_at'")
        if self.audit_artifact is not None:
            _require_locator(self.audit_artifact, label="observation 'audit_artifact'")

    @property
    def fingerprint(self) -> str:
        return self.finding.fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "observed_at": self.observed_at,
            "audit_artifact": self.audit_artifact,
            "finding": self.finding.to_dict(),
        }


def parse_issue_observation(
    payload: object, *, label: str = "observation"
) -> IssueObservation:
    """Parse one issue observation, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "run_id",
            "fingerprint",
            "observed_at",
            "audit_artifact",
            "finding",
        ),
    )
    finding = parse_finding(data.get("finding"), label=f"{label} 'finding'")
    declared = data.get("fingerprint")
    if declared is not None and declared != finding.fingerprint:
        raise AssuranceError(
            f"{label} 'fingerprint' {declared!r} does not match its finding's "
            f"fingerprint {finding.fingerprint}"
        )
    return IssueObservation(
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        finding=finding,
        observed_at=_require_timestamp(
            data.get("observed_at"), label=f"{label} 'observed_at'"
        ),
        audit_artifact=(
            None
            if data.get("audit_artifact") is None
            else _require_locator(
                data.get("audit_artifact"), label=f"{label} 'audit_artifact'"
            )
        ),
    )


@dataclass(frozen=True)
class Disposition:
    """A human's decision about an issue, appended rather than applied in place."""

    issue_id: str
    kind: DispositionKind
    recorded_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        as_literal(
            self.kind, DISPOSITION_KINDS, "disposition 'kind'", error=AssuranceError
        )
        _require_timestamp(self.recorded_at, label="disposition 'recorded_at'")
        if self.note is not None:
            _require_text(self.note, label="disposition 'note'")

    @property
    def result_state(self) -> IssueState:
        """The issue state this disposition produces."""
        return DISPOSITION_RESULT_STATE[self.kind]

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "kind": self.kind,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def parse_disposition(payload: object, *, label: str = "disposition") -> Disposition:
    """Parse one disposition record, failing closed."""
    data = _require_object(
        payload, label=label, allowed_keys=("issue_id", "kind", "recorded_at", "note")
    )
    return Disposition(
        issue_id=parse_issue_id(data.get("issue_id")),
        kind=as_literal(
            data.get("kind"), DISPOSITION_KINDS, f"{label} 'kind'", error=AssuranceError
        ),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class IssueMerge:
    """An irreversible redirection of one issue's history into another."""

    source_issue_id: str
    target_issue_id: str
    recorded_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_issue_id", parse_issue_id(self.source_issue_id)
        )
        object.__setattr__(
            self, "target_issue_id", parse_issue_id(self.target_issue_id)
        )
        if self.source_issue_id == self.target_issue_id:
            raise AssuranceError(
                f"merge source and target are both {self.source_issue_id}; an "
                "issue cannot merge into itself"
            )
        _require_timestamp(self.recorded_at, label="merge 'recorded_at'")
        if self.note is not None:
            _require_text(self.note, label="merge 'note'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_issue_id": self.source_issue_id,
            "target_issue_id": self.target_issue_id,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def parse_issue_merge(payload: object, *, label: str = "merge") -> IssueMerge:
    """Parse one merge record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("source_issue_id", "target_issue_id", "recorded_at", "note"),
    )
    return IssueMerge(
        source_issue_id=parse_issue_id(data.get("source_issue_id")),
        target_issue_id=parse_issue_id(data.get("target_issue_id")),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class IssueKeptDistinct:
    """A durable statement that two reviewed issues describe separate defects.

    A merge is an event, and its negative is not simply the absence of one: a
    reviewer read a suggested pair and ruled that it is two defects, and without
    that ruling on disk every later reconciliation asks the same question again.

    What the ruling cannot say is "never ask again". It was reached about issues
    as they read at the time, so the identity digest each named issue had then is
    stored beside it, and the ruling holds only while those digests still
    describe them. An issue that absorbs a materially different sighting is a
    different question, and gets asked again rather than answered by a verdict
    nobody gave about it.
    """

    issue_ids: tuple[str, ...]
    identity_digests: tuple[str, ...]
    recorded_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "issue_ids",
            _require_issue_ids(list(self.issue_ids), label="keep-distinct 'issue_ids'"),
        )
        if len(self.issue_ids) < 2:
            raise AssuranceError(
                "keep-distinct must name at least two issues; "
                f"got {len(self.issue_ids)}"
            )
        object.__setattr__(
            self,
            "identity_digests",
            tuple(
                _require_digest(digest, label="keep-distinct 'identity_digests' entry")
                for digest in self.identity_digests
            ),
        )
        if len(self.identity_digests) != len(self.issue_ids):
            raise AssuranceError(
                "keep-distinct must carry one identity digest per issue; got "
                f"{len(self.identity_digests)} digest(s) for "
                f"{len(self.issue_ids)} issue(s)"
            )
        _require_timestamp(self.recorded_at, label="keep-distinct 'recorded_at'")
        if self.note is not None:
            _require_text(self.note, label="keep-distinct 'note'")

    @property
    def reviewed_digests(self) -> tuple[tuple[str, str], ...]:
        """Each named issue beside the identity digest it was reviewed at."""
        return tuple(zip(self.issue_ids, self.identity_digests, strict=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_ids": list(self.issue_ids),
            "identity_digests": list(self.identity_digests),
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


def parse_issue_kept_distinct(
    payload: object, *, label: str = "keep-distinct"
) -> IssueKeptDistinct:
    """Parse one keep-distinct record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("issue_ids", "identity_digests", "recorded_at", "note"),
    )
    digests = _require_sequence(
        data.get("identity_digests"),
        label=f"{label} 'identity_digests'",
        maximum=MAX_ISSUE_REFERENCES,
    )
    return IssueKeptDistinct(
        issue_ids=_require_issue_ids(
            data.get("issue_ids"), label=f"{label} 'issue_ids'"
        ),
        identity_digests=tuple(
            _require_digest(digest, label=f"{label} 'identity_digests' entry")
            for digest in digests
        ),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class IssueIdentityBinding:
    """A durable statement that one identity key names one canonical issue.

    Keys and issue IDs are both minted by ASL, so a binding never introduces a
    new name: it says which allocated issue owns an allocated key today. That is
    what lets a merged-away key keep resolving without the merge record having to
    be reinterpreted, and what keeps an agent from selecting an identity by
    rewording the defect it describes.
    """

    identity_key: str
    issue_id: str
    source: IdentityBindingSource
    bound_at: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "identity_key", parse_identity_key(self.identity_key))
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        as_literal(
            self.source,
            IDENTITY_BINDING_SOURCES,
            "binding 'source'",
            error=AssuranceError,
        )
        _require_timestamp(self.bound_at, label="binding 'bound_at'")
        if self.note is not None:
            _require_text(self.note, label="binding 'note'")
        minted = identity_key_for_issue(self.issue_id)
        if self.source == "allocation" and self.identity_key != minted:
            raise AssuranceError(
                f"binding {self.identity_key!r} claims to be the key allocated "
                f"with {self.issue_id}, whose key is {minted}"
            )

    @property
    def named_issue_id(self) -> str:
        """The issue the key itself names, which a merge may have moved."""
        return issue_id_for_identity_key(self.identity_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "issue_id": self.issue_id,
            "source": self.source,
            "bound_at": self.bound_at,
            "note": self.note,
        }


def parse_issue_identity_binding(
    payload: object, *, label: str = "binding"
) -> IssueIdentityBinding:
    """Parse one identity-binding record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("identity_key", "issue_id", "source", "bound_at", "note"),
    )
    return IssueIdentityBinding(
        identity_key=parse_identity_key(data.get("identity_key")),
        issue_id=parse_issue_id(data.get("issue_id")),
        source=as_literal(
            data.get("source"),
            IDENTITY_BINDING_SOURCES,
            f"{label} 'source'",
            error=AssuranceError,
        ),
        bound_at=_require_timestamp(data.get("bound_at"), label=f"{label} 'bound_at'"),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


@dataclass(frozen=True)
class Closeout:
    """A human's verdict on what a run's audit was worth."""

    run_id: str
    outcome: CloseoutOutcome
    recorded_at: str
    issue_ids: tuple[str, ...] = ()
    rework_minutes: float | None = None
    note: str | None = None
    plan_id: str | None = None
    replaces: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", tuple(parse_issue_id(item) for item in self.issue_ids)
        )
        duplicates = _duplicates(self.issue_ids)
        if duplicates:
            raise AssuranceError(
                f"closeout repeats issue id(s) {', '.join(duplicates)}"
            )
        if len(self.issue_ids) > MAX_ISSUE_REFERENCES:
            raise AssuranceError(
                f"closeout 'issue_ids' must hold at most {MAX_ISSUE_REFERENCES} "
                f"entries; got {len(self.issue_ids)}"
            )
        _require_identifier(self.run_id, label="closeout 'run_id'")
        as_literal(
            self.outcome, CLOSEOUT_OUTCOMES, "closeout 'outcome'", error=AssuranceError
        )
        _require_timestamp(self.recorded_at, label="closeout 'recorded_at'")
        if self.rework_minutes is not None:
            object.__setattr__(
                self,
                "rework_minutes",
                _require_number(
                    self.rework_minutes,
                    label="closeout 'rework_minutes'",
                    minimum=0.0,
                ),
            )
        if self.note is not None:
            _require_text(self.note, label="closeout 'note'")
        if self.plan_id is not None:
            _require_identifier(self.plan_id, label="closeout 'plan_id'")
        if self.replaces is not None:
            _require_identifier(self.replaces, label="closeout 'replaces'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "issue_ids": list(self.issue_ids),
            "rework_minutes": self.rework_minutes,
            "note": self.note,
            "plan_id": self.plan_id,
            "replaces": self.replaces,
        }


def parse_closeout(payload: object, *, label: str = "closeout") -> Closeout:
    """Parse one closeout record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "run_id",
            "outcome",
            "recorded_at",
            "issue_ids",
            "rework_minutes",
            "note",
            "plan_id",
            "replaces",
        ),
    )
    return Closeout(
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        outcome=as_literal(
            data.get("outcome"),
            CLOSEOUT_OUTCOMES,
            f"{label} 'outcome'",
            error=AssuranceError,
        ),
        recorded_at=_require_timestamp(
            data.get("recorded_at"), label=f"{label} 'recorded_at'"
        ),
        issue_ids=_require_issue_ids(
            data.get("issue_ids", []), label=f"{label} 'issue_ids'"
        ),
        rework_minutes=(
            None
            if data.get("rework_minutes") is None
            else _require_number(
                data.get("rework_minutes"),
                label=f"{label} 'rework_minutes'",
                minimum=0.0,
            )
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        plan_id=(
            None
            if data.get("plan_id") is None
            else _require_identifier(data.get("plan_id"), label=f"{label} 'plan_id'")
        ),
        replaces=(
            None
            if data.get("replaces") is None
            else _require_identifier(data.get("replaces"), label=f"{label} 'replaces'")
        ),
    )


@dataclass(frozen=True)
class ImprovementPlanDraft:
    """A record that an improvement proposal was drafted for one issue.

    Drafting is deliberately inert: the record names the file and its digest so
    a reader can find and verify the proposal, and carries none of the prompt or
    response text that produced it.
    """

    issue_id: str
    harness: HarnessName
    output_path: str
    output_sha256: str
    drafted_at: str
    harness_model: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        as_literal(self.harness, HARNESS_NAMES, "draft 'harness'", error=AssuranceError)
        # The operator chooses ``--output`` freely, so an absolute path here is
        # ordinary and the run-relative locator rule does not apply.
        _require_text(
            self.output_path, label="draft 'output_path'", max_chars=MAX_LOCATOR_CHARS
        )
        _require_digest(self.output_sha256, label="draft 'output_sha256'")
        _require_timestamp(self.drafted_at, label="draft 'drafted_at'")
        if self.harness_model is not None:
            _require_text(
                self.harness_model,
                label="draft 'harness_model'",
                max_chars=MAX_TOKEN_CHARS,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "harness": self.harness,
            "output_path": self.output_path,
            "output_sha256": self.output_sha256,
            "drafted_at": self.drafted_at,
            "harness_model": self.harness_model,
        }


def parse_improvement_plan_draft(
    payload: object, *, label: str = "draft"
) -> ImprovementPlanDraft:
    """Parse one improvement-plan record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "issue_id",
            "harness",
            "output_path",
            "output_sha256",
            "drafted_at",
            "harness_model",
        ),
    )
    return ImprovementPlanDraft(
        issue_id=parse_issue_id(data.get("issue_id")),
        harness=as_literal(
            data.get("harness"),
            HARNESS_NAMES,
            f"{label} 'harness'",
            error=AssuranceError,
        ),
        output_path=_require_text(
            data.get("output_path"),
            label=f"{label} 'output_path'",
            max_chars=MAX_LOCATOR_CHARS,
        ),
        output_sha256=_require_digest(
            data.get("output_sha256"), label=f"{label} 'output_sha256'"
        ),
        drafted_at=_require_timestamp(
            data.get("drafted_at"), label=f"{label} 'drafted_at'"
        ),
        harness_model=_optional_text(
            data.get("harness_model"),
            label=f"{label} 'harness_model'",
            max_chars=MAX_TOKEN_CHARS,
        ),
    )


# ---------------------------------------------------------------------------
# Per-run summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRole:
    """The independent role the audit ran as.

    The audit harness must differ from the implementation harness — an agent
    grading its own turn is not a second opinion — but may equal the review
    harness, which was already independent of the implementation.
    """

    harness: HarnessName
    implementation_harness: HarnessName
    source: AuditRoleSource
    timeout_seconds: float
    model: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        as_literal(self.harness, HARNESS_NAMES, "role 'harness'", error=AssuranceError)
        as_literal(
            self.implementation_harness,
            HARNESS_NAMES,
            "role 'implementation_harness'",
            error=AssuranceError,
        )
        if self.harness == self.implementation_harness:
            raise AssuranceError(
                f"audit harness {self.harness!r} must differ from the "
                "implementation harness; an audit role that grades its own work "
                "is not independent"
            )
        as_literal(
            self.source, AUDIT_ROLE_SOURCES, "role 'source'", error=AssuranceError
        )
        object.__setattr__(
            self,
            "timeout_seconds",
            _require_number(
                self.timeout_seconds, label="role 'timeout_seconds'", minimum=1.0
            ),
        )
        if self.model is not None:
            _require_text(self.model, label="role 'model'", max_chars=MAX_TOKEN_CHARS)
        if self.effort is not None:
            _require_text(self.effort, label="role 'effort'", max_chars=MAX_TOKEN_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "implementation_harness": self.implementation_harness,
            "source": self.source,
            "timeout_seconds": self.timeout_seconds,
            "model": self.model,
            "effort": self.effort,
        }


def parse_audit_role(payload: object, *, label: str = "role") -> AuditRole:
    """Parse the effective audit role, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "harness",
            "implementation_harness",
            "source",
            "timeout_seconds",
            "model",
            "effort",
        ),
    )
    return AuditRole(
        harness=as_literal(
            data.get("harness"),
            HARNESS_NAMES,
            f"{label} 'harness'",
            error=AssuranceError,
        ),
        implementation_harness=as_literal(
            data.get("implementation_harness"),
            HARNESS_NAMES,
            f"{label} 'implementation_harness'",
            error=AssuranceError,
        ),
        source=as_literal(
            data.get("source"),
            AUDIT_ROLE_SOURCES,
            f"{label} 'source'",
            error=AssuranceError,
        ),
        timeout_seconds=_require_number(
            data.get("timeout_seconds"), label=f"{label} 'timeout_seconds'", minimum=1.0
        ),
        model=_optional_text(
            data.get("model"), label=f"{label} 'model'", max_chars=MAX_TOKEN_CHARS
        ),
        effort=_optional_text(
            data.get("effort"), label=f"{label} 'effort'", max_chars=MAX_TOKEN_CHARS
        ),
    )


@dataclass(frozen=True)
class AuditAttempt:
    """One bounded agent invocation and how it ended."""

    number: int
    outcome: AttemptOutcome
    duration_seconds: float | None = None
    artifact_dir: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_int(
            self.number,
            label="attempt 'number'",
            minimum=1,
            maximum=MAX_AUDIT_ATTEMPT_RECORDS,
        )
        as_literal(
            self.outcome, ATTEMPT_OUTCOMES, "attempt 'outcome'", error=AssuranceError
        )
        if self.duration_seconds is not None:
            object.__setattr__(
                self,
                "duration_seconds",
                _require_number(
                    self.duration_seconds,
                    label="attempt 'duration_seconds'",
                    minimum=0.0,
                ),
            )
        if self.artifact_dir is not None:
            _require_locator(self.artifact_dir, label="attempt 'artifact_dir'")
        if self.detail is not None:
            _require_text(self.detail, label="attempt 'detail'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "artifact_dir": self.artifact_dir,
            "detail": self.detail,
        }


def parse_audit_attempt(payload: object, *, label: str = "attempt") -> AuditAttempt:
    """Parse one audit attempt record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "number",
            "outcome",
            "duration_seconds",
            "artifact_dir",
            "detail",
        ),
    )
    return AuditAttempt(
        number=_require_int(
            data.get("number"),
            label=f"{label} 'number'",
            minimum=1,
            maximum=MAX_AUDIT_ATTEMPT_RECORDS,
        ),
        outcome=as_literal(
            data.get("outcome"),
            ATTEMPT_OUTCOMES,
            f"{label} 'outcome'",
            error=AssuranceError,
        ),
        duration_seconds=(
            None
            if data.get("duration_seconds") is None
            else _require_number(
                data.get("duration_seconds"),
                label=f"{label} 'duration_seconds'",
                minimum=0.0,
            )
        ),
        artifact_dir=(
            None
            if data.get("artifact_dir") is None
            else _require_locator(
                data.get("artifact_dir"), label=f"{label} 'artifact_dir'"
            )
        ),
        detail=_optional_text(data.get("detail"), label=f"{label} 'detail'"),
    )


@dataclass(frozen=True)
class AssuranceSummary:
    """The bounded assurance object attached to a run's state, events, and status.

    It is additive and advisory: nothing here changes an exit code, a pause
    reason, readiness, a commit, or an implementation artifact.
    """

    run_id: str
    state: AuditState
    eligible: bool
    reason: str | None = None
    role: AuditRole | None = None
    attempts: tuple[AuditAttempt, ...] = ()
    artifacts: tuple[EvidenceRef, ...] = ()
    issue_ids: tuple[str, ...] = ()
    new_issue_count: int = 0
    recurring_issue_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    schema_version: int = ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self, "issue_ids", tuple(parse_issue_id(item) for item in self.issue_ids)
        )
        _require_schema_version(self.schema_version, label="assurance 'schema_version'")
        _require_identifier(self.run_id, label="assurance 'run_id'")
        as_literal(self.state, AUDIT_STATES, "assurance 'state'", error=AssuranceError)
        _require_bool(self.eligible, label="assurance 'eligible'")
        _require_int(
            self.new_issue_count,
            label="assurance 'new_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        )
        _require_int(
            self.recurring_issue_count,
            label="assurance 'recurring_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        )
        if len(self.attempts) > MAX_AUDIT_ATTEMPT_RECORDS:
            raise AssuranceError(
                f"assurance 'attempts' must hold at most "
                f"{MAX_AUDIT_ATTEMPT_RECORDS} entries; got {len(self.attempts)}"
            )
        numbers = [attempt.number for attempt in self.attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise AssuranceError(
                "assurance 'attempts' must be numbered consecutively from 1; "
                f"got {numbers}"
            )
        duplicates = _duplicates(self.issue_ids)
        if duplicates:
            raise AssuranceError(
                f"assurance 'issue_ids' repeats {', '.join(duplicates)}"
            )
        if len(self.issue_ids) > MAX_ISSUE_REFERENCES:
            raise AssuranceError(
                f"assurance 'issue_ids' must hold at most {MAX_ISSUE_REFERENCES} "
                f"entries; got {len(self.issue_ids)}"
            )
        if self.new_issue_count + self.recurring_issue_count != len(self.issue_ids):
            raise AssuranceError(
                f"assurance new/recurring counts "
                f"({self.new_issue_count}/{self.recurring_issue_count}) must "
                f"account for exactly the {len(self.issue_ids)} issue id(s) listed"
            )
        for timestamp, field in (
            (self.started_at, "started_at"),
            (self.completed_at, "completed_at"),
        ):
            if timestamp is not None:
                _require_timestamp(timestamp, label=f"assurance '{field}'")
        if self.duration_seconds is not None:
            object.__setattr__(
                self,
                "duration_seconds",
                _require_number(
                    self.duration_seconds,
                    label="assurance 'duration_seconds'",
                    minimum=0.0,
                ),
            )
        if self.role is not None and not isinstance(self.role, AuditRole):
            raise AssuranceError("assurance 'role' must be an AuditRole")
        self._validate_state()

    def _validate_state(self) -> None:
        if self.state in {"skipped", "unavailable", "partial"}:
            if self.reason is None:
                raise AssuranceError(f"assurance state {self.state!r} must record why")
            _require_token(self.reason, label="assurance 'reason'")
        elif self.reason is not None:
            raise AssuranceError(
                f"assurance state {self.state!r} must not record a reason"
            )
        if not self.eligible and self.state != "skipped":
            raise AssuranceError(
                f"an ineligible run can only be 'skipped'; got {self.state!r}"
            )
        if self.state in {"all-clear", "partial"} and self.issue_ids:
            raise AssuranceError(
                f"a run in state {self.state!r} creates no issues; "
                f"got {', '.join(self.issue_ids)}"
            )
        if self.state == "skipped" and self.issue_ids:
            raise AssuranceError(
                f"skipping an audit creates no issue; got {', '.join(self.issue_ids)}"
            )
        if self.state == "pending" and (self.attempts or self.issue_ids):
            raise AssuranceError(
                "a pending audit has not run and carries no attempts or issues"
            )
        if self.state == "findings" and not self.issue_ids:
            raise AssuranceError(
                "an audit in state 'findings' must name the issue id(s) it opened "
                "or reopened"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "eligible": self.eligible,
            "reason": self.reason,
            "role": None if self.role is None else self.role.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "issue_ids": list(self.issue_ids),
            "new_issue_count": self.new_issue_count,
            "recurring_issue_count": self.recurring_issue_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
        }


def parse_assurance_summary(payload: object) -> AssuranceSummary:
    """Parse a persisted per-run assurance object, failing closed."""
    label = "assurance"
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "schema_version",
            "run_id",
            "state",
            "eligible",
            "reason",
            "role",
            "attempts",
            "artifacts",
            "issue_ids",
            "new_issue_count",
            "recurring_issue_count",
            "started_at",
            "completed_at",
            "duration_seconds",
        ),
    )
    attempts = tuple(
        parse_audit_attempt(item, label=f"{label} attempt #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("attempts", []),
                label=f"{label} 'attempts'",
                maximum=MAX_AUDIT_ATTEMPT_RECORDS,
            )
        )
    )
    artifacts = tuple(
        parse_evidence_ref(item, label=f"{label} artifact #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("artifacts", []),
                label=f"{label} 'artifacts'",
                maximum=MAX_EVIDENCE_ENTRIES,
            )
        )
    )
    return AssuranceSummary(
        schema_version=_require_schema_version(
            data.get("schema_version"), label=f"{label} 'schema_version'"
        ),
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        state=as_literal(
            data.get("state"), AUDIT_STATES, f"{label} 'state'", error=AssuranceError
        ),
        eligible=_require_bool(data.get("eligible"), label=f"{label} 'eligible'"),
        reason=(
            None
            if data.get("reason") is None
            else _require_token(data.get("reason"), label=f"{label} 'reason'")
        ),
        role=(
            None
            if data.get("role") is None
            else parse_audit_role(data.get("role"), label=f"{label} 'role'")
        ),
        attempts=attempts,
        artifacts=artifacts,
        issue_ids=_require_issue_ids(
            data.get("issue_ids", []), label=f"{label} 'issue_ids'"
        ),
        new_issue_count=_require_int(
            data.get("new_issue_count", 0),
            label=f"{label} 'new_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        ),
        recurring_issue_count=_require_int(
            data.get("recurring_issue_count", 0),
            label=f"{label} 'recurring_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        ),
        started_at=(
            None
            if data.get("started_at") is None
            else _require_timestamp(
                data.get("started_at"), label=f"{label} 'started_at'"
            )
        ),
        completed_at=(
            None
            if data.get("completed_at") is None
            else _require_timestamp(
                data.get("completed_at"), label=f"{label} 'completed_at'"
            )
        ),
        duration_seconds=(
            None
            if data.get("duration_seconds") is None
            else _require_number(
                data.get("duration_seconds"),
                label=f"{label} 'duration_seconds'",
                minimum=0.0,
            )
        ),
    )


# ---------------------------------------------------------------------------
# Reconciliation manifest
# ---------------------------------------------------------------------------

#: A reconciliation manifest is an operator artifact, not a ledger record: a
#: preview generates it, a human edits it, and one apply replays it. Its version
#: is therefore its own and moves independently of the ledger schema version.
RECONCILIATION_MANIFEST_KIND = "assurance-reconciliation-manifest"
RECONCILIATION_MANIFEST_VERSION = 1
SUPPORTED_RECONCILIATION_MANIFEST_VERSIONS: frozenset[int] = frozenset({1})

#: Give one canonical issue a durable identity key it does not have yet.
DECISION_BIND = "bind"
#: Redirect one issue's history into another, irreversibly.
DECISION_MERGE = "merge"
#: Record that a suggested pair was read and is two defects. Writes nothing:
#: the ledger has no record for "not the same", and inventing one to store a
#: negative would put a growing table of non-facts in every replay.
DECISION_KEEP_DISTINCT = "keep-distinct"
#: A pair the catalog suggested and nobody has ruled on yet. It is a member of
#: the schema so a preview can be applied without being rewritten first — and
#: apply refuses any manifest still carrying one, so the refusal names the
#: unread suggestion instead of a syntax error somewhere in the file.
DECISION_REVIEW = "review"

RECONCILIATION_DECISIONS: tuple[str, ...] = (
    DECISION_BIND,
    DECISION_MERGE,
    DECISION_KEEP_DISTINCT,
    DECISION_REVIEW,
)

#: Decisions one manifest may carry. A reconciliation pass is something a person
#: reads before running it, and a file past this size is being generated into
#: rather than reviewed.
MAX_MANIFEST_DECISIONS = 1000


@dataclass(frozen=True)
class BindDecision:
    """Bind one identity key to the canonical issue that owns it."""

    issue_id: str
    identity_key: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issue_id", parse_issue_id(self.issue_id))
        object.__setattr__(self, "identity_key", parse_identity_key(self.identity_key))
        if self.note is not None:
            _require_text(self.note, label="bind decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_BIND

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_BIND,
            "issue_id": self.issue_id,
            "identity_key": self.identity_key,
            "note": self.note,
        }


@dataclass(frozen=True)
class MergeDecision:
    """Redirect one issue's history into another."""

    source_issue_id: str
    target_issue_id: str
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_issue_id", parse_issue_id(self.source_issue_id)
        )
        object.__setattr__(
            self, "target_issue_id", parse_issue_id(self.target_issue_id)
        )
        if self.source_issue_id == self.target_issue_id:
            raise AssuranceError(
                f"merge decision cannot merge {self.source_issue_id} into itself"
            )
        if self.note is not None:
            _require_text(self.note, label="merge decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_MERGE

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_MERGE,
            "source_issue_id": self.source_issue_id,
            "target_issue_id": self.target_issue_id,
            "note": self.note,
        }


@dataclass(frozen=True)
class KeepDistinctDecision:
    """Record that a suggested pair was read and left as separate defects."""

    issue_ids: tuple[str, ...]
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", _require_issue_ids(list(self.issue_ids), label="issues")
        )
        if len(self.issue_ids) < 2:
            raise AssuranceError(
                "keep-distinct decision must name at least two issues; "
                f"got {len(self.issue_ids)}"
            )
        if self.note is not None:
            _require_text(self.note, label="keep-distinct decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_KEEP_DISTINCT

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_KEEP_DISTINCT,
            "issue_ids": list(self.issue_ids),
            "note": self.note,
        }


@dataclass(frozen=True)
class ReviewDecision:
    """A pair the catalog suggested, carrying why, awaiting a human verdict."""

    issue_ids: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    similarity: float = 0.0
    note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "issue_ids", _require_issue_ids(list(self.issue_ids), label="issues")
        )
        if len(self.issue_ids) < 2:
            raise AssuranceError(
                "review decision must name at least two issues; "
                f"got {len(self.issue_ids)}"
            )
        object.__setattr__(
            self,
            "reasons",
            tuple(
                _require_token(reason, label="review decision 'reasons' entry")
                for reason in self.reasons
            ),
        )
        similarity = _require_number(
            self.similarity, label="review decision 'similarity'", minimum=0.0
        )
        if similarity > 1.0:
            raise AssuranceError(
                f"review decision 'similarity' must be <= 1.0; got {similarity}"
            )
        object.__setattr__(self, "similarity", similarity)
        if self.note is not None:
            _require_text(self.note, label="review decision 'note'")

    @property
    def decision(self) -> str:
        return DECISION_REVIEW

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": DECISION_REVIEW,
            "issue_ids": list(self.issue_ids),
            "reasons": list(self.reasons),
            "similarity": round(self.similarity, 2),
            "note": self.note,
        }


ReconciliationDecision = (
    BindDecision | MergeDecision | KeepDistinctDecision | ReviewDecision
)


@dataclass(frozen=True)
class ReconciliationManifest:
    """Every decision one reconciliation pass applies, and what it was read against.

    ``catalog_digest`` is the identity state the decisions were reviewed against.
    It is part of the manifest rather than a flag on the command because the
    review is what the digest qualifies: a file that outlived the catalog it was
    written from describes issues that may since have moved, and applying it
    would record a verdict nobody gave about the inbox as it stands.
    """

    catalog_digest: str
    decisions: tuple[ReconciliationDecision, ...] = ()
    generated_at: str | None = None

    def __post_init__(self) -> None:
        _require_digest(self.catalog_digest, label="manifest 'catalog_digest'")
        decisions = tuple(self.decisions)
        if len(decisions) > MAX_MANIFEST_DECISIONS:
            raise AssuranceError(
                f"manifest 'decisions' must hold at most {MAX_MANIFEST_DECISIONS} "
                f"entries; got {len(decisions)}"
            )
        object.__setattr__(self, "decisions", decisions)
        if self.generated_at is not None:
            _require_timestamp(self.generated_at, label="manifest 'generated_at'")
        bound_keys = _duplicates(
            item.identity_key for item in decisions if isinstance(item, BindDecision)
        )
        if bound_keys:
            raise AssuranceError(
                f"manifest binds identity key(s) {', '.join(bound_keys)} more than "
                "once; one key names one issue"
            )
        merged = _duplicates(
            item.source_issue_id
            for item in decisions
            if isinstance(item, MergeDecision)
        )
        if merged:
            raise AssuranceError(
                f"manifest merges issue(s) {', '.join(merged)} more than once; a "
                "merge is irreversible and only the first would take effect"
            )

    @property
    def unresolved(self) -> tuple[ReviewDecision, ...]:
        """Suggestions still awaiting a verdict, which apply refuses to act on."""
        return tuple(
            item for item in self.decisions if isinstance(item, ReviewDecision)
        )

    def of_kind(self, decision: str) -> tuple[ReconciliationDecision, ...]:
        return tuple(item for item in self.decisions if item.decision == decision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": RECONCILIATION_MANIFEST_KIND,
            "version": RECONCILIATION_MANIFEST_VERSION,
            "catalog_digest": self.catalog_digest,
            "generated_at": self.generated_at,
            "decisions": [item.to_dict() for item in self.decisions],
        }


def _parse_decision(payload: object, *, label: str) -> ReconciliationDecision:
    if not isinstance(payload, Mapping):
        raise AssuranceError(f"{label} must be a JSON object")
    kind = as_literal(
        payload.get("decision"),
        RECONCILIATION_DECISIONS,
        f"{label} 'decision'",
        error=AssuranceError,
    )
    if kind == DECISION_BIND:
        data = _require_object(
            payload,
            label=label,
            allowed_keys=("decision", "issue_id", "identity_key", "note"),
        )
        return BindDecision(
            issue_id=parse_issue_id(data.get("issue_id")),
            identity_key=parse_identity_key(data.get("identity_key")),
            note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        )
    if kind == DECISION_MERGE:
        data = _require_object(
            payload,
            label=label,
            allowed_keys=("decision", "source_issue_id", "target_issue_id", "note"),
        )
        return MergeDecision(
            source_issue_id=parse_issue_id(data.get("source_issue_id")),
            target_issue_id=parse_issue_id(data.get("target_issue_id")),
            note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        )
    if kind == DECISION_KEEP_DISTINCT:
        data = _require_object(
            payload, label=label, allowed_keys=("decision", "issue_ids", "note")
        )
        return KeepDistinctDecision(
            issue_ids=_require_issue_ids(
                data.get("issue_ids"), label=f"{label} 'issue_ids'"
            ),
            note=_optional_text(data.get("note"), label=f"{label} 'note'"),
        )
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("decision", "issue_ids", "reasons", "similarity", "note"),
    )
    reasons = _require_sequence(
        data.get("reasons", []),
        label=f"{label} 'reasons'",
        maximum=MAX_REFERENCES_PER_ITEM,
    )
    return ReviewDecision(
        issue_ids=_require_issue_ids(
            data.get("issue_ids"), label=f"{label} 'issue_ids'"
        ),
        reasons=tuple(
            _require_token(reason, label=f"{label} 'reasons' entry")
            for reason in reasons
        ),
        similarity=_require_number(
            data.get("similarity", 0.0), label=f"{label} 'similarity'", minimum=0.0
        ),
        note=_optional_text(data.get("note"), label=f"{label} 'note'"),
    )


def parse_reconciliation_manifest(
    payload: object, *, label: str = "manifest"
) -> ReconciliationManifest:
    """Parse one reconciliation manifest, failing closed.

    The ``kind`` and ``version`` fields are checked before anything else, so a
    file that is some other JSON document is refused as the wrong document
    rather than as a manifest missing every field it needs.
    """
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("kind", "version", "catalog_digest", "generated_at", "decisions"),
    )
    kind = _require_text(data.get("kind"), label=f"{label} 'kind'")
    if kind != RECONCILIATION_MANIFEST_KIND:
        raise AssuranceError(
            f"{label} 'kind' must be {RECONCILIATION_MANIFEST_KIND!r}; got {kind!r}"
        )
    version = _require_int(
        data.get("version"), label=f"{label} 'version'", minimum=1, maximum=1_000_000
    )
    if version not in SUPPORTED_RECONCILIATION_MANIFEST_VERSIONS:
        supported = ", ".join(
            str(item) for item in sorted(SUPPORTED_RECONCILIATION_MANIFEST_VERSIONS)
        )
        raise AssuranceError(
            f"{label} 'version' {version} is unsupported; this build reads "
            f"version(s) {supported}"
        )
    decisions = _require_sequence(
        data.get("decisions", []),
        label=f"{label} 'decisions'",
        maximum=MAX_MANIFEST_DECISIONS,
    )
    return ReconciliationManifest(
        catalog_digest=_require_digest(
            data.get("catalog_digest"), label=f"{label} 'catalog_digest'"
        ),
        decisions=tuple(
            _parse_decision(item, label=f"{label} decision {position}")
            for position, item in enumerate(decisions, start=1)
        ),
        generated_at=(
            None
            if data.get("generated_at") is None
            else _require_timestamp(
                data.get("generated_at"), label=f"{label} 'generated_at'"
            )
        ),
    )


# ---------------------------------------------------------------------------
# Exclusive-create artifact writers
# ---------------------------------------------------------------------------


def audit_artifact_name(name: str) -> str:
    """Validate ``name`` as a single, traversal-free artifact filename.

    Unlike :func:`agent_session_loop.artifacts.sanitize_artifact_name`, which
    rewrites an unsafe name into a safe one, this rejects it. A caller that
    produced ``../../etc/passwd`` has a bug, and quietly storing its bytes under
    a different name would hide that bug rather than stop it.
    """
    if not isinstance(name, str) or not _ARTIFACT_NAME_RE.match(name):
        raise AssuranceError(
            "audit artifact name must be a single filename of alphanumerics, "
            f"'.', '_', or '-' starting with an alphanumeric; got {name!r}"
        )
    if ".." in PurePosixPath(name).parts:
        raise AssuranceError(f"audit artifact name cannot traverse upward: {name!r}")
    return name


def attempt_dir_name(number: int) -> str:
    """Return the canonical directory name for audit attempt ``number``."""
    _require_int(
        number, label="attempt number", minimum=1, maximum=MAX_AUDIT_ATTEMPT_RECORDS
    )
    return f"{ATTEMPT_DIR_PREFIX}{number}"


def write_new_bytes(path: Path | str, data: bytes, *, durable: bool = True) -> Path:
    """Create ``path`` exclusively and write ``data`` to it.

    An existing destination is an error, never an overwrite: a second write to a
    canonical audit artifact means two audits disagree about one run, and the
    disagreement is the finding. On any failure the partial file is removed so a
    retry sees a clean slate. ``durable`` fullsyncs the file and its directory.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise AssuranceError("audit artifact data must be bytes")
    payload = bytes(data)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise AssuranceError(
            f"audit artifact {path} is {len(payload)} bytes, over the "
            f"{MAX_ARTIFACT_BYTES}-byte cap"
        )
    target = Path(path)
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as error:
        raise AssuranceError(
            f"audit artifact {target} already exists; canonical audit artifacts "
            "are written exactly once"
        ) from error
    except OSError as error:
        raise AssuranceError(
            f"could not create audit artifact {target}: {error}"
        ) from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if durable:
                durability.fullsync(handle.fileno())
    except OSError as error:
        with contextlib.suppress(OSError):
            target.unlink()
        raise AssuranceError(
            f"could not write audit artifact {target}: {error}"
        ) from error
    if durable:
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                durability.fullsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise AssuranceError(
                f"could not sync audit artifact directory {target.parent}: {error}"
            ) from error
    return target


def write_audit_artifact(
    run_dir: Path | str, name: str, data: bytes, *, durable: bool = True
) -> Path:
    """Write one canonical audit artifact into ``run_dir``, exactly once."""
    return write_new_bytes(
        Path(run_dir) / audit_artifact_name(name), data, durable=durable
    )


def write_audit_json(
    run_dir: Path | str, name: str, payload: Any, *, durable: bool = True
) -> Path:
    """Serialize ``payload`` canonically and write it as a new audit artifact."""
    return write_audit_artifact(
        run_dir, name, canonical_json_bytes(payload), durable=durable
    )


def create_attempt_dir(run_dir: Path | str, number: int) -> Path:
    """Create the attempt directory for ``number``, refusing to reuse one.

    A retry that lands in the previous attempt's directory would let the second
    invocation's artifacts sit beside — or on top of — the first's, which is
    exactly the evidence needed to explain why the retry happened.
    """
    target = Path(run_dir) / attempt_dir_name(number)
    try:
        target.mkdir(parents=True)
    except FileExistsError as error:
        raise AssuranceError(
            f"audit attempt directory {target} already exists; each attempt "
            "writes its own directory"
        ) from error
    except OSError as error:
        raise AssuranceError(
            f"could not create audit attempt directory {target}: {error}"
        ) from error
    return target
