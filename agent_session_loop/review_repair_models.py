"""Repair plans, materialization results, and generated-session markers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    contracts,
)
from .plans_session_ids import SessionId

# Generated review-repair session markers. Every materialized repair child
# carries the base marker (so a version-agnostic reader recognizes it) plus the
# versioned marker on the following line. These are intentionally distinct from
# the ``asl:generated-review-session`` markers in :mod:`review` so a review
# session and its repair children never shadow one another.
REVIEW_REPAIR_SESSION_MARKER = "<!-- asl:generated-review-repair-session -->"


REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE = (
    "<!-- asl:generated-review-repair-session version={version} -->"
)


_REVIEW_REPAIR_SESSION_MARKER_RE = re.compile(
    r"<!--\s*asl:generated-review-repair-session"
    r"(?:\s+version=([0-9]+))?\s*-->"
)


GENERATED_REVIEW_REPAIR_SESSION_VERSION = 1


REPOSITORY_AWARE_GENERATED_REVIEW_REPAIR_SESSION_VERSION = 2


_REPAIR_GROUP_LINE_RE = re.compile(r"(?m)^- Repair group: `([^`]+)`\.$")


#: Maximum number of repair groups (child sessions) produced per cycle.
MAX_REPAIR_GROUPS = 8


# Provenance labels for a repair group's write repositories. A repository can be
# attributed by more than one source; the strongest attribution is reported.
WRITE_REPOSITORY_SOURCE_REPO = "repo"


WRITE_REPOSITORY_SOURCE_EVIDENCE = "evidence"


WRITE_REPOSITORY_SOURCE_CHECK = "check"


_WRITE_REPOSITORY_SOURCE_RANK = {
    WRITE_REPOSITORY_SOURCE_REPO: 0,
    WRITE_REPOSITORY_SOURCE_EVIDENCE: 1,
    WRITE_REPOSITORY_SOURCE_CHECK: 2,
}


_WRITE_REPOSITORY_SOURCE_LABELS = {
    WRITE_REPOSITORY_SOURCE_REPO: "finding `repo`",
    WRITE_REPOSITORY_SOURCE_EVIDENCE: "finding evidence paths",
    WRITE_REPOSITORY_SOURCE_CHECK: "finding verification checks",
}


# Stable ``reason`` vocabulary for materialization results.
REVIEW_REPAIR_REASON_OK = "ok"


REVIEW_REPAIR_REASON_NO_FINDINGS = "no-findings"


REVIEW_REPAIR_REASON_NOT_LOOP_READY = "not-loop-ready"


REVIEW_REPAIR_REASON_MALFORMED = "malformed-review-repair"


class ReviewRepairError(ValueError):
    """Raised internally when safe repair sessions cannot be produced."""


@dataclass(frozen=True)
class RepairChild:
    """One materialized repair child session."""

    session_id: SessionId
    repair_group: str
    title: str
    finding_ids: tuple[str, ...]
    path: Path
    read_repositories: tuple[str, ...] = ()
    write_repositories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewRepairResult:
    """Outcome of a review-repair materialization attempt.

    ``ok`` is ``True`` whenever valid pending child sessions were produced (even
    when the plan could not yet be marked loop-ready). ``ok`` is ``False`` only
    when no safe sessions could be produced; in that case ``reason`` is
    :data:`REVIEW_REPAIR_REASON_MALFORMED` and ``error`` explains why.

    ``children`` is empty with ``reason``
    :data:`REVIEW_REPAIR_REASON_NOT_LOOP_READY` when the pre-authoring gate
    refused the rendered children: the refusal is a gate verdict rather than a
    malformed artifact, and the plan is untouched.

    ``rolled_back`` reports that children *were* authored and then withdrawn
    because the executability gate refused them. The plan is back in the state
    it held before the materialization, so ``children`` is empty here too: a
    child a run may not select is not a child the plan has.
    """

    ok: bool
    reason: str
    review_session_id: SessionId | None = None
    children: tuple[RepairChild, ...] = ()
    loop_ready: bool = False
    error: str | None = None
    detail: str | None = None
    blockers: tuple[str, ...] = ()
    baseline_summary: dict[str, Any] | None = None
    non_bypassable_blockers: tuple[str, ...] = ()
    rolled_back: bool = False
    rollback: dict[str, Any] | None = None


@dataclass(frozen=True)
class RepairGroupPlan:
    """One validated, fully rendered child proposed by repair planning."""

    repair_group: str
    proposed_child_id: SessionId
    title: str
    finding_ids: tuple[str, ...]
    done_when: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    contract: contracts.CompletionContract
    rendered_text: str
    read_repositories: tuple[str, ...] = ()
    write_repositories: tuple[str, ...] = ()
    write_repository_sources: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewRepairPlan:
    """Read-only validation and child-specification result.

    Successful findings plans contain normalized structured findings,
    fingerprints, and fully rendered repair groups ready for atomic staging.
    Failed plans retain any artifact provenance and conflict information that
    could be established safely, while using the materializer's existing
    stable reason/error vocabulary.
    """

    ok: bool
    reason: str
    review_session_id: SessionId | None = None
    validated_findings: tuple[dict[str, Any], ...] = ()
    finding_fingerprints: tuple[str, ...] = ()
    repair_groups: tuple[RepairGroupPlan, ...] = ()
    proposed_child_ids: tuple[SessionId, ...] = ()
    existing_child_ids: tuple[SessionId, ...] = ()
    review_output_sha256: str | None = None
    review_artifact_sha256: str | None = None
    repository_bases: tuple[dict[str, Any], ...] = ()
    error: str | None = None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


def review_repair_session_version_marker(
    version: int = GENERATED_REVIEW_REPAIR_SESSION_VERSION,
) -> str:
    """Render the versioned marker line for a generated repair session."""
    return REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(version=version)


def contains_review_repair_session_marker(text: str) -> bool:
    """Return True when a session body carries any review-repair marker."""
    return _REVIEW_REPAIR_SESSION_MARKER_RE.search(text) is not None


def review_repair_session_marker_version(text: str) -> int | None:
    """Return the review-repair version of a session body, if any.

    ``None`` when the body carries no review-repair marker; ``1`` when only the
    base (unversioned) marker is present; otherwise the integer from the
    versioned marker.
    """
    match = _REVIEW_REPAIR_SESSION_MARKER_RE.search(text)
    if match is None:
        return None
    if match.group(1) is not None:
        return int(match.group(1))
    return 1


def is_generated_review_repair_session_file(path: str | Path) -> bool:
    """Return True when ``path`` is a materialized repair session file."""
    try:
        body = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return contains_review_repair_session_marker(body)
