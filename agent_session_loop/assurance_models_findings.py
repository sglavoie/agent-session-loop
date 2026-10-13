"""Evidence references, findings and fingerprints, normalized finding text.

Also holds deterministic check results and rubric-lane evidence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .artifacts import sha256_hex
from .assurance_models_primitives import (
    AssuranceError,
    _duplicates,
    _optional_text,
    _require_digest,
    _require_locator,
    _require_object,
    _require_schema_version,
    _require_sequence,
    _require_text,
    _require_token,
)
from .assurance_models_vocab import (
    ASSURANCE_SCHEMA_VERSION,
    CHECK_STATUSES,
    EVIDENCE_KINDS,
    MAX_REFERENCES_PER_ITEM,
    RUBRIC_LANES,
    SEVERITIES,
    CheckStatus,
    EvidenceKind,
    RubricLane,
    Severity,
)
from .literals import as_literal

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
