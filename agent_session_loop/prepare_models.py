"""Validator result schema: constants and frozen dataclasses for ``asl prepare``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import preflight
from .plans import PlanError, SessionId, parse_session_id

LEGACY_PREPARE_VALIDATION_VERSION = 1
PREPARE_VALIDATION_VERSION = 2
PREPARE_VALIDATION_FILENAME = "prepare-validation.json"
#: Version 2 retires every entry written before the cache identity carried the
#: packet-prompt digest; those entries are indistinguishable from current ones
#: by identity alone even though they answered a different prompt.
PREPARE_VALIDATION_CACHE_VERSION = 2
PREPARE_VALIDATION_CACHE_FILENAME = "prepare-validation-cache.json"

VALIDATION_PACKET_HEADING_PREFIX = "# ASL preparation validation packet"

#: Sessions validated per worker packet; keeps each packet's research scope
#: bounded so one worker context window covers the whole batch.
DEFAULT_VALIDATION_BATCH_SIZE = 4

#: Defaults for bounded, read-only validation workers. Model and effort remain
#: ``None`` by default so each harness retains its own native selection.
DEFAULT_VALIDATOR_JOBS = 2
DEFAULT_VALIDATOR_TIMEOUT_SECONDS = 900
DEFAULT_VALIDATOR_HEARTBEAT_SECONDS = 30

#: Claim taxonomy from the session-expand skill: Path, Signature,
#: Field/property, Dependency, Behavioral, Naming, Removal. An ``R`` claim
#: asserts that the session removes or alters existing behavior and carries the
#: origin rationale — the commit, blame range, or covering test that explains
#: why the code exists — in its ``location``.
CLAIM_TYPES: tuple[str, ...] = ("P", "S", "F", "D", "B", "N", "R")

#: Factual issue vocabulary from the session-expand skill. Corrections are
#: derivable only from these factual categories; design changes have no
#: representation in the schema and therefore cannot be smuggled in.
ISSUE_TYPES: tuple[str, ...] = (
    "path-missing",
    "signature-mismatch",
    "field-missing",
    "dependency-missing",
    "behavior-incorrect",
    "name-missing",
    "repository-ambiguous",
    "removal-unjustified",
)

REPOSITORY_AMBIGUITY_ISSUE = "repository-ambiguous"
CONTRACT_PROBLEM_CATEGORY = "contract-problem"

DEPENDENCY_DIRECTIONS: tuple[str, ...] = ("requires", "provides")

#: Expansion table placeholders for claims about code the session will create.
TO_BE_CREATED_LOCATION = "N/A — to be created"
TO_BE_CREATED_LINE = "—"

EXPANSION_REFERENCE_TEMPLATE = (
    "> **Expansion notes**: Read `{filename}` for verified line numbers, "
    "corrected paths, and patterns to reuse."
)
_BEFORE_STARTING_PREFIX = "> **Before starting**:"

MAX_PATTERNS_PER_SESSION = 3

NO_CONTRACT_ISSUES_LINE = "_no contract issues found_"

#: Correction outcome statuses.
CORRECTION_APPLIED = "applied"
CORRECTION_ALREADY_APPLIED = "already-applied"
CORRECTION_BLOCKED = "blocked"

#: Stable blocker reasons for corrections that must not be applied.
BLOCKER_NOT_FOUND = "not-found"
BLOCKER_AMBIGUOUS = "ambiguous"
BLOCKER_CONTRACT_BODY = "contract-body"
BLOCKER_HANDOFF_BODY = "handoff-body"
BLOCKER_STRUCTURAL = "structural"

_HEADING_LINE_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s")


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedClaim:
    """One claim checked against the codebase and confirmed (or to-be-created)."""

    claim: str
    claim_type: str
    location: str
    line: int | None = None
    to_be_created: bool = False
    repo: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "claim": self.claim,
            "claim_type": self.claim_type,
            "location": self.location,
            "line": self.line,
            "to_be_created": self.to_be_created,
        }
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class ValidationIssue:
    """A factual mismatch between a session claim and the codebase."""

    issue_type: str
    summary: str
    session_ref: str
    actual: str
    fix: str
    repo: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "issue_type": self.issue_type,
            "summary": self.summary,
            "session_ref": self.session_ref,
            "actual": self.actual,
            "fix": self.fix,
        }
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class ConcreteDetail:
    """Per-file evidence bullets the session executor will need."""

    path: str
    notes: tuple[str, ...]
    repo: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"path": self.path, "notes": list(self.notes)}
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class ReusePattern:
    """An existing codebase pattern the implementation should mirror."""

    name: str
    location: str
    reason: str
    repo: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "location": self.location,
            "reason": self.reason,
        }
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class SessionDependency:
    """A requires/provides edge between the session and another session."""

    direction: str
    session_id: str
    description: str
    repo: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "direction": self.direction,
            "session_id": self.session_id,
            "description": self.description,
        }
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class ContractProblem:
    """A contract defect that must be repaired before readiness can publish."""

    message: str
    suggested_fix: str

    def to_dict(self) -> dict[str, object]:
        return {"message": self.message, "suggested_fix": self.suggested_fix}


@dataclass(frozen=True)
class ProposedCorrection:
    """A proposed exact factual text replacement in the session file."""

    expected_text: str
    replacement_text: str
    reason: str
    repo: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "expected_text": self.expected_text,
            "replacement_text": self.replacement_text,
            "reason": self.reason,
        }
        if self.repo is not None:
            payload["repo"] = self.repo
        return payload


@dataclass(frozen=True)
class SparkSuitability:
    """Conservative per-session Spark routing assessment."""

    eligible: bool
    reason: str
    reported: bool = True

    def to_dict(self) -> dict[str, object]:
        return {"eligible": self.eligible, "reason": self.reason}


MISSING_SPARK_SUITABILITY = SparkSuitability(
    eligible=False,
    reason="Spark eligibility marker missing; using baseline",
    reported=False,
)


@dataclass(frozen=True)
class SessionValidation:
    """Validated preparation findings for one session."""

    session_id: SessionId
    title: str
    claims: tuple[VerifiedClaim, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()
    details: tuple[ConcreteDetail, ...] = ()
    patterns: tuple[ReusePattern, ...] = ()
    dependencies: tuple[SessionDependency, ...] = ()
    contract_checked: bool = False
    contract_problems: tuple[ContractProblem, ...] = ()
    corrections: tuple[ProposedCorrection, ...] = ()
    spark: SparkSuitability = MISSING_SPARK_SUITABILITY

    @property
    def claims_checked(self) -> int:
        """Verified claims + factual issues + the parsed contract, when checked."""
        return len(self.claims) + len(self.issues) + (1 if self.contract_checked else 0)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "session_id": self.session_id.value,
            "title": self.title,
            "claims": [claim.to_dict() for claim in self.claims],
            "issues": [issue.to_dict() for issue in self.issues],
            "details": [detail.to_dict() for detail in self.details],
            "patterns": [pattern.to_dict() for pattern in self.patterns],
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "contract_checked": self.contract_checked,
            "contract_problems": [
                problem.to_dict() for problem in self.contract_problems
            ],
            "corrections": [correction.to_dict() for correction in self.corrections],
        }
        if self.spark.reported:
            payload["spark"] = self.spark.to_dict()
        return payload


@dataclass(frozen=True)
class PrepareValidation:
    """Aggregate validated preparation output for a plan."""

    codebase_root: str
    sessions: tuple[SessionValidation, ...]
    version: int = LEGACY_PREPARE_VALIDATION_VERSION
    repositories: tuple[ValidatorRepository, ...] = ()

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(session.session_id.value for session in self.sessions)

    def session(self, session_id: int | str | SessionId) -> SessionValidation | None:
        canonical = parse_session_id(session_id)
        for session in self.sessions:
            if session.session_id == canonical:
                return session
        return None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": self.version,
            "sessions": [session.to_dict() for session in self.sessions],
        }
        if self.version == LEGACY_PREPARE_VALIDATION_VERSION:
            payload["codebase_root"] = self.codebase_root
        else:
            payload["workspace_root"] = self.codebase_root
            payload["repositories"] = [repo.to_dict() for repo in self.repositories]
        return payload

    @property
    def workspace_root(self) -> str:
        """Canonical validator root, named by plan mode in serialized data."""
        return self.codebase_root


@dataclass(frozen=True)
class ValidatorRepository:
    """One exact resolved registry entry echoed by validator output v2."""

    alias: str
    path: str
    origin: str
    root: str

    def to_dict(self) -> dict[str, str]:
        return {
            "alias": self.alias,
            "path": self.path,
            "origin": self.origin,
            "root": self.root,
        }


@dataclass(frozen=True)
class PrepareValidationCacheEntry:
    """One independently reusable session-validation result.

    ``identity`` is intentionally retained alongside its digest.  It keeps the
    cache inspectable and lets the loader reject a hand-edited key rather than
    silently accepting a result under an identity it was not validated for.
    ``validation`` always contains exactly one session.
    """

    session_id: SessionId
    key: str
    identity: dict[str, object]
    validation: PrepareValidation

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id.value,
            "key": self.key,
            "identity": self.identity,
            "validation": self.validation.to_dict(),
        }


@dataclass(frozen=True)
class PrepareValidationCache:
    """Versioned cache of validator outputs, independent of the aggregate."""

    entries: tuple[PrepareValidationCacheEntry, ...] = ()
    version: int = PREPARE_VALIDATION_CACHE_VERSION

    def entry(
        self, session_id: int | str | SessionId
    ) -> PrepareValidationCacheEntry | None:
        canonical = parse_session_id(session_id)
        for entry in self.entries:
            if entry.session_id == canonical:
                return entry
        return None

    def with_entry(
        self, replacement: PrepareValidationCacheEntry
    ) -> PrepareValidationCache:
        entries = {entry.session_id.value: entry for entry in self.entries}
        entries[replacement.session_id.value] = replacement
        return PrepareValidationCache(
            entries=tuple(
                sorted(
                    entries.values(),
                    key=lambda entry: entry.session_id.sort_key,
                )
            ),
            version=self.version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "entries": [entry.to_dict() for entry in self.entries],
        }


class RepositoryFootprintError(PlanError):
    """A legacy plan reaches beyond the single root a v1 packet can describe.

    Carries the serialized verdict so the caller can hand the operator the
    suggested registry table instead of only a message.
    """

    def __init__(self, message: str, *, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


def _require_legacy_repository_footprint(
    plan_dir: str | Path, root: str | Path
) -> None:
    """Refuse a legacy plan whose reach exceeds one repository under ``root``."""
    _, payload = preflight.plan_repository_footprint(plan_dir, root)
    if payload is None:
        return
    raise RepositoryFootprintError(
        "plan reaches beyond the bound root "
        f"{root} but manifest.md declares no `## Repositories` registry; "
        "declare the registry and per-session repository scopes first",
        payload=payload,
    )


class RepositoryRegistryRequiredError(PlanError):
    """A legacy plan intends to reach more than one Git repository.

    Distinct from :class:`RepositoryFootprintError`: reach is judged against
    paths that already exist, while intent also counts locations the plan has
    still to create. Carries the serialized intent report so the caller can
    name the repositories instead of only the condition.
    """

    def __init__(self, message: str, *, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


def _require_legacy_repository_intent(plan_dir: str | Path, root: str | Path) -> None:
    """Refuse a legacy plan that intends to reach several repositories."""
    _, payload = preflight.plan_repository_intent(plan_dir, root)
    if payload is None:
        return
    git_roots = ", ".join(payload.get("git_roots", ()))
    raise RepositoryRegistryRequiredError(
        f"plan intends to reach {payload.get('git_root_count', 0)} git "
        f"repositories ({git_roots}) but manifest.md declares no top-level "
        "`## Repositories` table; declare the registry, give every session "
        "read and write repository scopes, and attribute every contract "
        "check to a repository before preparing this plan",
        payload=payload,
    )
