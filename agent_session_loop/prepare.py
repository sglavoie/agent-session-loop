"""Structured preparation validator schema and expansion renderer.

Session preparation previously relied on the ``session-expand`` skill free-form
editing plan files: an LLM researched the codebase and then hand-wrote both the
``session-<id>.expand.md`` markdown and any factual corrections directly into
session files. This module replaces that free-form mutation path with a
validated, versioned pipeline:

- **Versioned validator result schema** — claims, evidence, issues, patterns,
  dependencies, contract problems, and proposed factual replacements are parsed
  fail-closed (:func:`parse_validator_output`). Malformed or incomplete
  validator output raises :class:`~agent_session_loop.plans.PlanError` before
  any plan file is touched.
- **Compact validator task packets** — :func:`build_validation_packet` renders
  a bounded, harness-neutral worker prompt for a batch of sessions
  (:func:`batch_sessions`), mirroring the Session 9 task-packet conventions:
  plan contents are referenced as paths to read, never inlined.
- **Derived aggregate store** — validated output is merged and persisted as
  ``prepare-validation.json`` (:func:`ingest_validator_output`), a derived
  artifact excluded from the stable readiness hash by construction (the hash
  only walks ``context.md``, ``manifest.md``, session files, and expansion
  files).
- **Expansion rendering** — :func:`render_expansion` produces
  ``session-<id>.expand.md`` markdown compatible with the historical
  ``session-expand`` output schema, from validated data only, so expansion
  markdown contains only verified claims. :func:`write_expansions` commits the
  files plus the idempotent ``> **Expansion notes**`` reference line through a
  single atomic :class:`~agent_session_loop.authoring.PlanTransaction`.
- **Exact factual corrections** — :func:`apply_corrections` applies a proposed
  replacement only when its expected original text matches exactly once in the
  target session instructions. Ambiguous matches, unknown text, structural
  (heading-changing) edits, and corrections that land inside the
  ``## Completion Contract`` or ``## Handoff`` sections are reported as
  blockers, never applied. Re-applying the same corrections is idempotent.

The ``session-expand`` skill is a thin expert/manual wrapper over these same
entry points; ``asl prepare`` (:func:`prepare_plan` /
:func:`prepare_from_args`) drives the identical pipeline autonomously:
deterministic authoring through :mod:`agent_session_loop.authoring`,
validation through this module's packets and fail-closed ingest, exact
factual correction, baseline execution through
:mod:`agent_session_loop.baseline`, and a final ``--mark-loop-ready``
preflight through the canonical :mod:`agent_session_loop.preflight` gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import Any, Callable, Iterable

from . import authoring, baseline, contracts, harnesses, preflight, responses
from .authoring import PlanTransaction
from .baseline_cache import (
    BASELINE_RESULTS_FILENAME,
    BaselineCache,
    repository_fingerprint,
)
from .plans import (
    HANDOFF_HEADING,
    PlanError,
    ReadinessMetadata,
    SessionId,
    canonicalize_path,
    inspect_plan,
    parse_session_id,
)
from .contracts import COMPLETION_CONTRACT_HEADING


PREPARE_VALIDATION_VERSION = 1
PREPARE_VALIDATION_FILENAME = "prepare-validation.json"

VALIDATION_PACKET_HEADING_PREFIX = "# ASL preparation validation packet"

#: Sessions validated per worker packet; keeps each packet's research scope
#: bounded so one worker context window covers the whole batch.
DEFAULT_VALIDATION_BATCH_SIZE = 4

#: Claim taxonomy from the session-expand skill: Path, Signature,
#: Field/property, Dependency, Behavioral, Naming.
CLAIM_TYPES: tuple[str, ...] = ("P", "S", "F", "D", "B", "N")

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
)

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

    def to_dict(self) -> dict[str, object]:
        return {
            "claim": self.claim,
            "claim_type": self.claim_type,
            "location": self.location,
            "line": self.line,
            "to_be_created": self.to_be_created,
        }


@dataclass(frozen=True)
class ValidationIssue:
    """A factual mismatch between a session claim and the codebase."""

    issue_type: str
    summary: str
    session_ref: str
    actual: str
    fix: str

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_type": self.issue_type,
            "summary": self.summary,
            "session_ref": self.session_ref,
            "actual": self.actual,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class ConcreteDetail:
    """Per-file evidence bullets the session executor will need."""

    path: str
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "notes": list(self.notes)}


@dataclass(frozen=True)
class ReusePattern:
    """An existing codebase pattern the implementation should mirror."""

    name: str
    location: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "location": self.location, "reason": self.reason}


@dataclass(frozen=True)
class SessionDependency:
    """A requires/provides edge between the session and another session."""

    direction: str
    session_id: str
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "session_id": self.session_id,
            "description": self.description,
        }


@dataclass(frozen=True)
class ContractProblem:
    """An advisory contract finding (schema error, stale reference, placeholder)."""

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

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_text": self.expected_text,
            "replacement_text": self.replacement_text,
            "reason": self.reason,
        }


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

    @property
    def claims_checked(self) -> int:
        """Verified claims + factual issues + the parsed contract, when checked."""
        return len(self.claims) + len(self.issues) + (1 if self.contract_checked else 0)

    def to_dict(self) -> dict[str, object]:
        return {
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
            "corrections": [
                correction.to_dict() for correction in self.corrections
            ],
        }


@dataclass(frozen=True)
class PrepareValidation:
    """Aggregate validated preparation output for a plan."""

    codebase_root: str
    sessions: tuple[SessionValidation, ...]
    version: int = PREPARE_VALIDATION_VERSION

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
        return {
            "version": self.version,
            "codebase_root": self.codebase_root,
            "sessions": [session.to_dict() for session in self.sessions],
        }


# ---------------------------------------------------------------------------
# Fail-closed parsing
# ---------------------------------------------------------------------------


def _fail(message: str) -> PlanError:
    return PlanError(f"validator output rejected: {message}")


def _require_object(
    value: object, *, label: str, allowed_keys: tuple[str, ...]
) -> dict:
    if not isinstance(value, dict):
        raise _fail(f"{label} must be a JSON object")
    unknown = sorted(set(value) - set(allowed_keys))
    if unknown:
        raise _fail(f"{label} has unknown keys: {', '.join(unknown)}")
    return value


def _require_list(value: object, *, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail(f"{label} must be a list")
    return value


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise _fail(f"{label} must be a non-empty string")
    return value


def _require_bool(value: object, *, label: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _fail(f"{label} must be a boolean")
    return value


def _require_choice(
    value: object, *, label: str, choices: tuple[str, ...]
) -> str:
    if value not in choices:
        allowed = ", ".join(choices)
        raise _fail(f"{label} must be one of: {allowed}; got {value!r}")
    return str(value)


def _parse_line_number(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _fail(f"{label} must be a positive integer or null")
    return value


def _parse_claim(payload: object, *, label: str) -> VerifiedClaim:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("claim", "claim_type", "location", "line", "to_be_created"),
    )
    to_be_created = _require_bool(
        data.get("to_be_created"), label=f"{label} 'to_be_created'"
    )
    if to_be_created:
        if data.get("line") is not None:
            raise _fail(f"{label} is to-be-created and must not carry a line")
        location = str(data.get("location") or TO_BE_CREATED_LOCATION)
        line = None
    else:
        location = _require_text(data.get("location"), label=f"{label} 'location'")
        line = _parse_line_number(data.get("line"), label=f"{label} 'line'")
    return VerifiedClaim(
        claim=_require_text(data.get("claim"), label=f"{label} 'claim'"),
        claim_type=_require_choice(
            data.get("claim_type"), label=f"{label} 'claim_type'", choices=CLAIM_TYPES
        ),
        location=location,
        line=line,
        to_be_created=to_be_created,
    )


def _parse_issue(payload: object, *, label: str) -> ValidationIssue:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("issue_type", "summary", "session_ref", "actual", "fix"),
    )
    return ValidationIssue(
        issue_type=_require_choice(
            data.get("issue_type"), label=f"{label} 'issue_type'", choices=ISSUE_TYPES
        ),
        summary=_require_text(data.get("summary"), label=f"{label} 'summary'"),
        session_ref=_require_text(
            data.get("session_ref"), label=f"{label} 'session_ref'"
        ),
        actual=_require_text(data.get("actual"), label=f"{label} 'actual'"),
        fix=_require_text(data.get("fix"), label=f"{label} 'fix'"),
    )


def _parse_detail(payload: object, *, label: str) -> ConcreteDetail:
    data = _require_object(payload, label=label, allowed_keys=("path", "notes"))
    notes = _require_list(data.get("notes"), label=f"{label} 'notes'")
    if not notes:
        raise _fail(f"{label} 'notes' must list at least one bullet")
    return ConcreteDetail(
        path=_require_text(data.get("path"), label=f"{label} 'path'"),
        notes=tuple(
            _require_text(note, label=f"{label} 'notes'[{index}]")
            for index, note in enumerate(notes)
        ),
    )


def _parse_pattern(payload: object, *, label: str) -> ReusePattern:
    data = _require_object(
        payload, label=label, allowed_keys=("name", "location", "reason")
    )
    return ReusePattern(
        name=_require_text(data.get("name"), label=f"{label} 'name'"),
        location=_require_text(data.get("location"), label=f"{label} 'location'"),
        reason=_require_text(data.get("reason"), label=f"{label} 'reason'"),
    )


def _parse_dependency(payload: object, *, label: str) -> SessionDependency:
    data = _require_object(
        payload, label=label, allowed_keys=("direction", "session_id", "description")
    )
    raw_id = _require_text(data.get("session_id"), label=f"{label} 'session_id'")
    try:
        canonical = parse_session_id(raw_id)
    except (PlanError, ValueError) as error:
        raise _fail(f"{label} 'session_id' is invalid: {error}") from error
    return SessionDependency(
        direction=_require_choice(
            data.get("direction"),
            label=f"{label} 'direction'",
            choices=DEPENDENCY_DIRECTIONS,
        ),
        session_id=canonical.value,
        description=_require_text(
            data.get("description"), label=f"{label} 'description'"
        ),
    )


def _parse_contract_problem(payload: object, *, label: str) -> ContractProblem:
    data = _require_object(
        payload, label=label, allowed_keys=("message", "suggested_fix")
    )
    return ContractProblem(
        message=_require_text(data.get("message"), label=f"{label} 'message'"),
        suggested_fix=_require_text(
            data.get("suggested_fix"), label=f"{label} 'suggested_fix'"
        ),
    )


def _parse_correction(payload: object, *, label: str) -> ProposedCorrection:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("expected_text", "replacement_text", "reason"),
    )
    expected = _require_text(
        data.get("expected_text"), label=f"{label} 'expected_text'"
    )
    replacement = _require_text(
        data.get("replacement_text"), label=f"{label} 'replacement_text'"
    )
    if expected == replacement:
        raise _fail(f"{label} replacement text must differ from expected text")
    return ProposedCorrection(
        expected_text=expected,
        replacement_text=replacement,
        reason=_require_text(data.get("reason"), label=f"{label} 'reason'"),
    )


def _parse_session_validation(payload: object, *, index: int) -> SessionValidation:
    label = f"session #{index + 1}"
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "session_id",
            "title",
            "claims",
            "issues",
            "details",
            "patterns",
            "dependencies",
            "contract_checked",
            "contract_problems",
            "corrections",
        ),
    )
    raw_id = data.get("session_id")
    if not isinstance(raw_id, str) or raw_id.strip() == "":
        raise _fail(f"{label} 'session_id' must be a non-empty string")
    try:
        session_id = parse_session_id(raw_id)
    except (PlanError, ValueError) as error:
        raise _fail(f"{label} 'session_id' is invalid: {error}") from error
    label = f"session {session_id.value}"

    patterns = tuple(
        _parse_pattern(item, label=f"{label} pattern #{position + 1}")
        for position, item in enumerate(
            _require_list(data.get("patterns"), label=f"{label} 'patterns'")
        )
    )
    if len(patterns) > MAX_PATTERNS_PER_SESSION:
        raise _fail(
            f"{label} lists {len(patterns)} patterns; at most "
            f"{MAX_PATTERNS_PER_SESSION} are allowed"
        )

    contract_checked = _require_bool(
        data.get("contract_checked"), label=f"{label} 'contract_checked'"
    )
    contract_problems = tuple(
        _parse_contract_problem(item, label=f"{label} contract problem #{position + 1}")
        for position, item in enumerate(
            _require_list(
                data.get("contract_problems"), label=f"{label} 'contract_problems'"
            )
        )
    )
    if contract_problems and not contract_checked:
        raise _fail(
            f"{label} reports contract problems but 'contract_checked' is false"
        )

    return SessionValidation(
        session_id=session_id,
        title=_require_text(data.get("title"), label=f"{label} 'title'"),
        claims=tuple(
            _parse_claim(item, label=f"{label} claim #{position + 1}")
            for position, item in enumerate(
                _require_list(data.get("claims"), label=f"{label} 'claims'")
            )
        ),
        issues=tuple(
            _parse_issue(item, label=f"{label} issue #{position + 1}")
            for position, item in enumerate(
                _require_list(data.get("issues"), label=f"{label} 'issues'")
            )
        ),
        details=tuple(
            _parse_detail(item, label=f"{label} detail #{position + 1}")
            for position, item in enumerate(
                _require_list(data.get("details"), label=f"{label} 'details'")
            )
        ),
        patterns=patterns,
        dependencies=tuple(
            _parse_dependency(item, label=f"{label} dependency #{position + 1}")
            for position, item in enumerate(
                _require_list(data.get("dependencies"), label=f"{label} 'dependencies'")
            )
        ),
        contract_checked=contract_checked,
        contract_problems=contract_problems,
        corrections=tuple(
            _parse_correction(item, label=f"{label} correction #{position + 1}")
            for position, item in enumerate(
                _require_list(data.get("corrections"), label=f"{label} 'corrections'")
            )
        ),
    )


def parse_validator_output(payload: object) -> PrepareValidation:
    """Parse and validate a validator result payload, failing closed.

    ``payload`` is the decoded JSON object a validator worker produced (or the
    stored ``prepare-validation.json`` content). Any structural problem —
    wrong version, unknown keys, missing or empty required fields, unknown
    claim/issue vocabulary, invalid session ids, duplicate sessions, more than
    :data:`MAX_PATTERNS_PER_SESSION` patterns, or a correction whose texts are
    equal — raises :class:`PlanError` so malformed or incomplete validator
    output can never reach a plan mutation.
    """
    data = _require_object(
        payload,
        label="validator output",
        allowed_keys=("version", "codebase_root", "sessions"),
    )
    version = data.get("version")
    if version != PREPARE_VALIDATION_VERSION:
        raise _fail(
            f"unsupported version {version!r}; expected {PREPARE_VALIDATION_VERSION}"
        )
    codebase_root = _require_text(
        data.get("codebase_root"), label="validator output 'codebase_root'"
    )
    raw_sessions = data.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise _fail("validator output 'sessions' must be a non-empty list")

    sessions: list[SessionValidation] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_sessions):
        session = _parse_session_validation(item, index=index)
        if session.session_id.value in seen:
            raise _fail(f"duplicate session id {session.session_id.value!r}")
        seen.add(session.session_id.value)
        sessions.append(session)

    return PrepareValidation(
        codebase_root=codebase_root,
        sessions=tuple(sessions),
        version=PREPARE_VALIDATION_VERSION,
    )


# ---------------------------------------------------------------------------
# Bounded validator task packets
# ---------------------------------------------------------------------------


def batch_sessions(
    session_ids: Iterable[int | str | SessionId],
    batch_size: int = DEFAULT_VALIDATION_BATCH_SIZE,
) -> tuple[tuple[str, ...], ...]:
    """Split session ids into bounded, order-preserving validation batches."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise PlanError("validation batch size must be a positive integer")
    ordered = [parse_session_id(session_id).value for session_id in session_ids]
    return tuple(
        tuple(ordered[start : start + batch_size])
        for start in range(0, len(ordered), batch_size)
    )


@dataclass(frozen=True)
class ValidationTaskPacket:
    """Compact, harness-neutral worker packet for one validation batch.

    Mirrors the runner's session task packet conventions: plan contents are
    referenced as absolute paths to read, never inlined, and the packet embeds
    no readiness/lock/mutation policy — the caller owns parsing, storage, and
    every plan mutation through this module's fail-closed entry points.
    """

    plan_dir: str
    codebase_root: str
    session_ids: tuple[str, ...]
    session_paths: tuple[str, ...]
    context_path: str
    version: int = PREPARE_VALIDATION_VERSION

    def render(self) -> str:
        joined_ids = ", ".join(self.session_ids)
        lines: list[str] = [
            f"{VALIDATION_PACKET_HEADING_PREFIX} — sessions {joined_ids}",
            "",
            (
                "You are a preparation validator. Research the codebase at "
                f"{self.codebase_root} and validate the claims made by the "
                "session files below. Return ONLY a JSON object — no prose, "
                "no fences. Do not edit any file: the caller validates your "
                "output and owns every plan mutation."
            ),
            "",
            "## Read (open these paths; contents are not embedded)",
            f"- Shared context: {self.context_path}",
        ]
        for session_id, session_path in zip(self.session_ids, self.session_paths):
            lines.append(f"- Session {session_id}: {session_path}")
        lines.extend(
            [
                "",
                "## Output schema (fail-closed; unknown keys are rejected)",
                (
                    f'{{"version": {self.version}, '
                    f'"codebase_root": "{self.codebase_root}", "sessions": [...]}}'
                ),
                (
                    "- One sessions[] entry per session above: {session_id, "
                    "title, claims, issues, details, patterns, dependencies, "
                    "contract_checked, contract_problems, corrections}."
                ),
                (
                    "- claims[]: {claim, claim_type, location, line, "
                    "to_be_created} with claim_type in "
                    f"{'/'.join(CLAIM_TYPES)} "
                    "(path/signature/field/dependency/behavior/naming). Only "
                    "include claims you verified against the codebase; mark "
                    "code the session will create with to_be_created=true."
                ),
                (
                    "- issues[]: {issue_type, summary, session_ref, actual, fix} "
                    f"with issue_type in: {', '.join(ISSUE_TYPES)}."
                ),
                "- details[]: {path, notes[]} — evidence bullets per file.",
                (
                    "- patterns[]: {name, location, reason} — at most "
                    f"{MAX_PATTERNS_PER_SESSION} per session."
                ),
                (
                    "- dependencies[]: {direction: requires|provides, "
                    "session_id, description}."
                ),
                (
                    "- contract_problems[]: {message, suggested_fix}; set "
                    "contract_checked=true when you evaluated the session's "
                    "## Completion Contract."
                ),
                (
                    "- corrections[]: {expected_text, replacement_text, reason} "
                    "— exact factual text replacements only (wrong paths, "
                    "names, signatures). expected_text must appear exactly "
                    "once in the session instructions. Never propose design "
                    "changes or contract-body edits; report those as issues "
                    "or contract_problems instead."
                ),
            ]
        )
        return "\n".join(lines)


def build_validation_packet(
    plan_dir: str | Path,
    session_ids: Iterable[int | str | SessionId],
    *,
    codebase_root: str | Path,
) -> ValidationTaskPacket:
    """Build the compact validator packet for one bounded session batch.

    Every referenced session file must exist; a missing file raises
    :class:`PlanError` so a typoed batch fails before any worker is launched.
    """
    canonical_dir = canonicalize_path(plan_dir)
    ordered = [parse_session_id(session_id) for session_id in session_ids]
    if not ordered:
        raise PlanError("validation packet requires at least one session id")

    session_paths: list[str] = []
    for session_id in ordered:
        session_path = canonical_dir / session_id.filename
        if not session_path.is_file():
            raise PlanError(f"missing session file: {session_path}")
        session_paths.append(str(session_path))

    return ValidationTaskPacket(
        plan_dir=str(canonical_dir),
        codebase_root=os.fspath(codebase_root),
        session_ids=tuple(session_id.value for session_id in ordered),
        session_paths=tuple(session_paths),
        context_path=str(canonical_dir / "context.md"),
    )


# ---------------------------------------------------------------------------
# Derived aggregate store (prepare-validation.json)
# ---------------------------------------------------------------------------


def prepare_validation_path(plan_dir: str | Path) -> Path:
    return canonicalize_path(plan_dir) / PREPARE_VALIDATION_FILENAME


def load_prepare_validation(plan_dir: str | Path) -> PrepareValidation | None:
    """Load the stored aggregate, ``None`` when absent, fail-closed when malformed."""
    path = prepare_validation_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PlanError(f"cannot read {path}: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(
            f"{path} is not valid JSON: {error.msg} at line {error.lineno}"
        ) from error
    return parse_validator_output(payload)


def save_prepare_validation(
    plan_dir: str | Path, validation: PrepareValidation
) -> Path:
    """Atomically persist the aggregate as ``prepare-validation.json``."""
    path = prepare_validation_path(plan_dir)
    text = json.dumps(validation.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as error:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise PlanError(f"cannot write {path}: {error}") from error
    return path


def merge_validations(
    base: PrepareValidation | None, update: PrepareValidation
) -> PrepareValidation:
    """Merge a batch result into the aggregate; the update wins per session."""
    if base is None:
        return update
    if base.codebase_root != update.codebase_root:
        raise PlanError(
            "validator output codebase root "
            f"{update.codebase_root!r} does not match stored aggregate "
            f"{base.codebase_root!r}"
        )
    merged: dict[str, SessionValidation] = {
        session.session_id.value: session for session in base.sessions
    }
    for session in update.sessions:
        merged[session.session_id.value] = session
    ordered = sorted(merged.values(), key=lambda item: item.session_id.sort_key)
    return PrepareValidation(
        codebase_root=base.codebase_root,
        sessions=tuple(ordered),
        version=PREPARE_VALIDATION_VERSION,
    )


def ingest_validator_output(
    plan_dir: str | Path, payload: object
) -> PrepareValidation:
    """Parse one batch payload, merge it into the stored aggregate, and save.

    The parse happens before any disk write, so malformed or incomplete
    validator output raises :class:`PlanError` and leaves the plan directory
    untouched.
    """
    update = parse_validator_output(payload)
    aggregate = merge_validations(load_prepare_validation(plan_dir), update)
    save_prepare_validation(plan_dir, aggregate)
    return aggregate


# ---------------------------------------------------------------------------
# Expansion rendering
# ---------------------------------------------------------------------------


def _claim_row(claim: VerifiedClaim) -> str:
    if claim.to_be_created:
        location = claim.location or TO_BE_CREATED_LOCATION
        line = TO_BE_CREATED_LINE
        return f"| `{claim.claim}` | {claim.claim_type} | {location} | {line} |"
    line = str(claim.line) if claim.line is not None else TO_BE_CREATED_LINE
    return f"| `{claim.claim}` | {claim.claim_type} | `{claim.location}` | {line} |"


def render_expansion(
    session: SessionValidation,
    *,
    codebase_root: str,
    expanded: str | None = None,
) -> str:
    """Render a ``session-<id>.expand.md`` document from validated data.

    The output matches the historical ``session-expand`` schema: header
    metadata, optional ``## Issues`` / ``## Verified Claims`` /
    ``## Concrete Details`` / ``## Patterns to Reuse`` /
    ``## Session Dependencies`` sections (omitted entirely when empty), and a
    ``## Contract Issues`` section whenever the contract was evaluated, using
    the literal ``_no contract issues found_`` line when clean.
    """
    if expanded is None:
        expanded = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    display = session.session_id.display_label
    lines: list[str] = [
        f"# {display} Expansion: {session.title}",
        "",
        f"**Expanded**: {expanded}",
        f"**Codebase root**: `{codebase_root}`",
        (
            f"**Claims checked**: {session.claims_checked} | "
            f"**Issues found**: {len(session.issues)}"
        ),
    ]

    if session.issues:
        lines.extend(["", "## Issues"])
        for index, issue in enumerate(session.issues, start=1):
            lines.extend(
                [
                    "",
                    f"### ISSUE-{index}: {issue.summary}",
                    "",
                    f"- **Type**: {issue.issue_type}",
                    f'- **Session ref**: "{issue.session_ref}"',
                    f"- **Actual**: {issue.actual}",
                    f"- **Fix**: {issue.fix}",
                ]
            )

    if session.claims:
        lines.extend(
            [
                "",
                "## Verified Claims",
                "",
                "| Claim | Type | Location | Line |",
                "| --- | --- | --- | --- |",
            ]
        )
        lines.extend(_claim_row(claim) for claim in session.claims)

    if session.details:
        lines.extend(["", "## Concrete Details"])
        for detail in session.details:
            lines.extend(["", f"### File: `{detail.path}`", ""])
            lines.extend(f"- {note}" for note in detail.notes)

    if session.patterns:
        lines.extend(["", "## Patterns to Reuse", ""])
        lines.extend(
            f"- **{pattern.name}**: {pattern.location} — {pattern.reason}"
            for pattern in session.patterns
        )

    if session.dependencies:
        lines.extend(["", "## Session Dependencies", ""])
        for dependency in session.dependencies:
            direction = (
                "Requires from" if dependency.direction == "requires" else "Provides for"
            )
            lines.append(
                f"- **{direction} session {dependency.session_id}**: "
                f"{dependency.description}"
            )

    if session.contract_checked:
        lines.extend(["", "## Contract Issues", ""])
        if session.contract_problems:
            lines.extend(
                f"- `{session.session_id.value}: {problem.message}: "
                f"{problem.suggested_fix}`"
                for problem in session.contract_problems
            )
        else:
            lines.append(NO_CONTRACT_ISSUES_LINE)

    return "\n".join(lines) + "\n"


def expansion_reference_line(session_id: int | str | SessionId) -> str:
    filename = parse_session_id(session_id).expansion_filename
    return EXPANSION_REFERENCE_TEMPLATE.format(filename=filename)


def insert_expansion_reference(
    session_text: str, session_id: int | str | SessionId
) -> tuple[str, bool]:
    """Insert the ``> **Expansion notes**`` blockquote after ``> **Before starting**``.

    Idempotent: when the reference line for this session already exists the
    text is returned unchanged. Raises :class:`PlanError` when the session has
    no ``> **Before starting**:`` anchor line to insert after.
    """
    reference = expansion_reference_line(session_id)
    lines = session_text.splitlines()
    if any(line.strip() == reference for line in lines):
        return session_text, False

    for index, line in enumerate(lines):
        if line.startswith(_BEFORE_STARTING_PREFIX):
            updated = lines[: index + 1] + [reference] + lines[index + 1 :]
            suffix = "\n" if session_text.endswith("\n") else ""
            return "\n".join(updated) + suffix, True

    raise PlanError(
        f"session {parse_session_id(session_id).value} has no "
        f"'{_BEFORE_STARTING_PREFIX}' line to anchor the expansion reference"
    )


def write_expansions(
    plan_dir: str | Path,
    validation: PrepareValidation,
    *,
    expanded: str | None = None,
) -> tuple[Path, ...]:
    """Write ``.expand.md`` files and reference lines for every validated session.

    All writes (expansion documents plus updated session files) are staged in
    one :class:`PlanTransaction` and committed atomically, so a failure leaves
    no partially expanded plan. Raises :class:`PlanError` when a validated
    session has no session file on disk.
    """
    canonical_dir = canonicalize_path(plan_dir)
    transaction = PlanTransaction(canonical_dir)

    for session in validation.sessions:
        session_path = canonical_dir / session.session_id.filename
        try:
            session_text = session_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise PlanError(f"missing session file: {session_path}") from error
        except OSError as error:
            raise PlanError(f"cannot read {session_path}: {error}") from error

        transaction.stage(
            session.session_id.expansion_filename,
            render_expansion(
                session, codebase_root=validation.codebase_root, expanded=expanded
            ),
        )
        updated_text, changed = insert_expansion_reference(
            session_text, session.session_id
        )
        if changed:
            transaction.stage(session.session_id.filename, updated_text)

    return transaction.commit()


# ---------------------------------------------------------------------------
# Exact factual corrections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorrectionOutcome:
    """The result of attempting one proposed factual correction."""

    session_id: str
    expected_text: str
    replacement_text: str
    status: str
    blocker: str | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status == CORRECTION_APPLIED

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "expected_text": self.expected_text,
            "replacement_text": self.replacement_text,
            "status": self.status,
            "blocker": self.blocker,
            "detail": self.detail,
        }


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level == 0 or level == len(stripped) or stripped[level] != " ":
        return 0
    return level


def _protected_line_ranges(lines: list[str]) -> tuple[tuple[int, int, str], ...]:
    """Line ranges (start, end, blocker) covering the contract and handoff sections."""
    ranges: list[tuple[int, int, str]] = []

    handoff_start: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            handoff_start = index
            break
    if handoff_start is not None:
        ranges.append((handoff_start, len(lines), BLOCKER_HANDOFF_BODY))

    body_end = handoff_start if handoff_start is not None else len(lines)
    for index in range(body_end):
        if lines[index].strip() == COMPLETION_CONTRACT_HEADING:
            end = body_end
            for later in range(index + 1, body_end):
                level = _heading_level(lines[later])
                if 0 < level <= 2:
                    end = later
                    break
            ranges.append((index, end, BLOCKER_CONTRACT_BODY))
            break

    return tuple(ranges)


def _match_line_span(text: str, offset: int, length: int) -> tuple[int, int]:
    start_line = text.count("\n", 0, offset)
    end_line = text.count("\n", 0, offset + max(length - 1, 0))
    return start_line, end_line


def _blocked(
    session_id: str,
    correction: ProposedCorrection,
    blocker: str,
    detail: str,
) -> CorrectionOutcome:
    return CorrectionOutcome(
        session_id=session_id,
        expected_text=correction.expected_text,
        replacement_text=correction.replacement_text,
        status=CORRECTION_BLOCKED,
        blocker=blocker,
        detail=detail,
    )


def _apply_one_correction(
    text: str, session_id: str, correction: ProposedCorrection
) -> tuple[str, CorrectionOutcome]:
    if _HEADING_LINE_RE.search(correction.expected_text) or _HEADING_LINE_RE.search(
        correction.replacement_text
    ):
        return text, _blocked(
            session_id,
            correction,
            BLOCKER_STRUCTURAL,
            "correction touches markdown headings; structural or "
            "design-changing edits are not applied automatically",
        )

    occurrences = text.count(correction.expected_text)
    if occurrences == 0:
        if correction.replacement_text in text:
            return text, CorrectionOutcome(
                session_id=session_id,
                expected_text=correction.expected_text,
                replacement_text=correction.replacement_text,
                status=CORRECTION_ALREADY_APPLIED,
                detail="replacement text already present",
            )
        return text, _blocked(
            session_id,
            correction,
            BLOCKER_NOT_FOUND,
            "expected original text not found in session file",
        )
    if occurrences > 1:
        return text, _blocked(
            session_id,
            correction,
            BLOCKER_AMBIGUOUS,
            f"expected original text matches {occurrences} times; "
            "corrections require exactly one match",
        )

    offset = text.find(correction.expected_text)
    lines = text.splitlines()
    start_line, end_line = _match_line_span(
        text, offset, len(correction.expected_text)
    )
    for range_start, range_end, blocker in _protected_line_ranges(lines):
        if start_line < range_end and end_line >= range_start:
            section = (
                COMPLETION_CONTRACT_HEADING
                if blocker == BLOCKER_CONTRACT_BODY
                else HANDOFF_HEADING
            )
            return text, _blocked(
                session_id,
                correction,
                blocker,
                f"match lies inside the protected {section!r} section",
            )

    updated = text.replace(correction.expected_text, correction.replacement_text, 1)
    return updated, CorrectionOutcome(
        session_id=session_id,
        expected_text=correction.expected_text,
        replacement_text=correction.replacement_text,
        status=CORRECTION_APPLIED,
        detail=f"replaced at line {start_line + 1}",
    )


def apply_corrections(
    plan_dir: str | Path, validation: PrepareValidation
) -> tuple[CorrectionOutcome, ...]:
    """Apply every proposed factual correction that matches exactly once.

    Corrections are applied in order against the in-memory session text so
    multiple corrections to one file compose. A correction is applied only
    when its ``expected_text`` occurs exactly once and that occurrence lies
    outside the ``## Completion Contract`` and ``## Handoff`` sections;
    everything else is reported as a blocker outcome and left unapplied.
    Re-running with the same corrections yields ``already-applied`` outcomes
    and no file changes. All file writes commit through one atomic
    :class:`PlanTransaction`.
    """
    canonical_dir = canonicalize_path(plan_dir)
    outcomes: list[CorrectionOutcome] = []
    transaction = PlanTransaction(canonical_dir)

    for session in validation.sessions:
        if not session.corrections:
            continue
        session_path = canonical_dir / session.session_id.filename
        try:
            text = session_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise PlanError(f"missing session file: {session_path}") from error
        except OSError as error:
            raise PlanError(f"cannot read {session_path}: {error}") from error

        original = text
        for correction in session.corrections:
            text, outcome = _apply_one_correction(
                text, session.session_id.value, correction
            )
            outcomes.append(outcome)
        if text != original:
            transaction.stage(session.session_id.filename, text)

    transaction.commit()
    return tuple(outcomes)


# ---------------------------------------------------------------------------
# `asl prepare` end-to-end orchestration
# ---------------------------------------------------------------------------

PREPARE_EXIT_REASON_OK = "ok"
PREPARE_EXIT_REASON_NEEDS_PLAN_ITERATION = "needs-plan-iteration"
PREPARE_EXIT_REASON_VALIDATION_FAILED = "validation-failed"
PREPARE_EXIT_REASON_BASELINE_UNSATISFIED = "baseline-unsatisfied"
PREPARE_EXIT_REASON_NOT_LOOP_READY = "not-loop-ready"
PREPARE_EXIT_REASON_INPUT_ERROR = "input-error"
PREPARE_EXIT_REASON_CWD_ERROR = "cwd-error"
PREPARE_EXIT_REASON_PLAN_STRUCTURE = "plan-structure"

#: Pipeline stage names, in execution order.
PREPARE_STAGE_STRUCTURE = "structure"
PREPARE_STAGE_AUTHORING = "authoring"
PREPARE_STAGE_VALIDATION = "validation"
PREPARE_STAGE_CORRECTIONS = "corrections"
PREPARE_STAGE_BASELINE = "baseline"
PREPARE_STAGE_PREFLIGHT = "preflight"

PREPARE_STEP_OK = "ok"
PREPARE_STEP_FAILED = "failed"
PREPARE_STEP_SKIPPED = "skipped"

#: Baseline statuses that count as reusable recorded evidence. ``skip``,
#: ``timeout``, and ``error`` outcomes never satisfy an expectation and are
#: re-run rather than reused.
_REUSABLE_BASELINE_STATUSES = ("pass", "fail")


@dataclass(frozen=True)
class PrepareStep:
    """One pipeline stage outcome in a preparation run."""

    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class PrepareBlocker:
    """A structured reason a preparation could not claim readiness."""

    stage: str
    message: str
    session_id: str | None = None
    category: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "message": self.message,
            "session_id": self.session_id,
            "category": self.category,
        }


@dataclass(frozen=True)
class PrepareResult:
    """Aggregated outcome of one ``asl prepare`` pipeline run."""

    ok: bool
    reason: str
    plan_dir: Path | None
    source_path: str | None
    session_ids: tuple[str, ...]
    steps: tuple[PrepareStep, ...]
    blockers: tuple[PrepareBlocker, ...]
    corrections: tuple[CorrectionOutcome, ...] = ()
    baseline: dict[str, Any] | None = None
    marked_loop_ready: bool = False
    run_command: str | None = None
    resume_command: str | None = None


#: Callable that turns one validation packet into decoded validator JSON.
ValidatorRunner = Callable[[ValidationTaskPacket], object]


def _extract_json_object(text: str) -> object:
    """Decode a validator's final message as one JSON object, fail-closed.

    Tolerates a single surrounding markdown code fence (workers are told not
    to emit one, but a fenced JSON object is unambiguous); anything else that
    fails to decode raises :class:`PlanError`.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as error:
        raise _fail(
            f"validator returned invalid JSON: {error.msg} at line {error.lineno}"
        ) from error


def _launch_validator(
    harness_name: str,
    packet: ValidationTaskPacket,
    *,
    unsafe_bypass: bool = True,
    timeout: int | None = None,
) -> object:
    """Run one validation packet through a harness worker and decode its JSON."""
    with tempfile.TemporaryDirectory(prefix="asl-prepare-") as tmp:
        last_message_path = Path(tmp) / "validator-last-message.txt"
        try:
            result = harnesses.run_harness(
                harness_name,
                cwd=Path(packet.codebase_root),
                plan_dir=Path(packet.plan_dir),
                prompt=packet.render(),
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                timeout=timeout,
            )
        except harnesses.HarnessError as error:
            raise PlanError(
                f"validator harness {harness_name!r} failed to launch: {error}"
            ) from error
    if result.returncode != 0:
        snippet_lines = (result.stderr or result.stdout).strip().splitlines()
        detail = snippet_lines[-1].strip() if snippet_lines else ""
        message = f"validator harness {harness_name!r} exited {result.returncode}"
        if detail:
            message += f": {detail}"
        raise PlanError(message)
    if not result.last_message.strip():
        raise PlanError(f"validator harness {harness_name!r} returned no output")
    return _extract_json_object(result.last_message)


def _strip_contract_lines(lines: list[str]) -> list[str]:
    """Remove the ``## Completion Contract`` section from a session body."""
    start = contracts._find_heading_index(
        lines, COMPLETION_CONTRACT_HEADING, 0, len(lines)
    )
    if start is None:
        return lines
    end = contracts._section_end(lines, start, max_level=2, end=len(lines))
    return lines[:start] + lines[end:]


def _structure_blockers(
    parsed: authoring.ParsedPlan,
) -> tuple[list[PrepareBlocker], dict[str, contracts.CompletionContract]]:
    """Gate the source plan: >= 2 sessions, each with a parseable contract."""
    blockers: list[PrepareBlocker] = []
    contracts_by_id: dict[str, contracts.CompletionContract] = {}
    if len(parsed.sessions) < 2:
        blockers.append(
            PrepareBlocker(
                stage=PREPARE_STAGE_STRUCTURE,
                message=(
                    f"plan defines {len(parsed.sessions)} explicit session "
                    "heading(s); at least two supported H2-H6 "
                    "'Session <id>' headings are required"
                ),
            )
        )
    for session in parsed.sessions:
        try:
            contract = contracts.parse_completion_contract(
                "\n".join(session.lines), session_id=session.session_id
            )
        except PlanError as error:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_STRUCTURE,
                    session_id=session.session_id.value,
                    message=f"completion contract is malformed: {error}",
                )
            )
            continue
        if contract is None:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_STRUCTURE,
                    session_id=session.session_id.value,
                    message="session has no '## Completion Contract' section",
                )
            )
            continue
        contracts_by_id[session.session_id.value] = contract
    return blockers, contracts_by_id


def _author_plan_directory(
    source_path: str,
    text: str,
    parsed: authoring.ParsedPlan,
    contracts_by_id: dict[str, contracts.CompletionContract],
    *,
    base_dir: str | Path | None,
) -> Path:
    """Author the split plan directory deterministically, in one transaction."""
    resolver = authoring.resolve_plan_output(source_path, base_dir)
    output_dir = Path(str(resolver["output_dir"]))

    transaction = PlanTransaction(output_dir)
    transaction.stage(
        "context.md",
        authoring.render_context(parsed.context_lines, resolver["source_path"]),
    )
    transaction.stage("progress.md", authoring.render_progress_skeleton())

    manifest_sessions: list[authoring.ManifestSession] = []
    for index, session in enumerate(parsed.sessions):
        contract = contracts_by_id[session.session_id.value]
        transaction.stage(
            session.session_id.filename,
            authoring.render_session_file(
                session_id=session.session_id,
                title=session.title,
                instruction_lines=_strip_contract_lines(list(session.lines)),
                heading_level=session.heading_level,
                is_first=index == 0,
                previous_session_id=(
                    parsed.sessions[index - 1].session_id if index else None
                ),
                contract=contract,
            ),
        )
        manifest_sessions.append(
            authoring.ManifestSession(
                session_id=session.session_id,
                title=session.title or f"Session {session.session_id.value}",
            )
        )

    transaction.stage(
        "manifest.md",
        authoring.render_manifest(
            authoring.ManifestSpec(
                plan_title=authoring.title_from_plan(text),
                source_path=str(resolver["source_path"]),
                output_dir=str(output_dir),
                plan_id=str(resolver["plan_id"]),
                source_fingerprint=str(resolver["source_fingerprint"]),
                content_sha256=str(resolver["content_sha256"]),
                sessions=tuple(manifest_sessions),
                split_date=datetime.now(timezone.utc).date().isoformat(),
                readiness=ReadinessMetadata(
                    state="draft",
                    readiness_sha256="pending",
                    checked_at="pending",
                ),
            )
        ),
    )
    transaction.commit()
    authoring.refresh_readiness_metadata(output_dir, state="draft")
    return output_dir


def _refresh_draft_readiness(plan_dir: Path) -> None:
    """Best-effort readiness resync so a preserved directory stays consistent."""
    try:
        authoring.refresh_readiness_metadata(plan_dir, state="draft")
    except PlanError:
        pass


def _run_validation_stage(
    plan_dir: Path,
    session_ids: tuple[str, ...],
    *,
    codebase_root: str,
    validator: ValidatorRunner,
    batch_size: int,
) -> tuple[PrepareValidation, int]:
    """Validate sessions not yet covered by the stored aggregate.

    Returns the full aggregate plus the number of newly validated sessions.
    Each batch result must cover exactly the requested batch and match the
    preparation's codebase root; anything else fails closed before storage.
    """
    aggregate = load_prepare_validation(plan_dir)
    validated = set(aggregate.session_ids) if aggregate is not None else set()
    pending = [sid for sid in session_ids if sid not in validated]

    for batch in batch_sessions(pending, batch_size):
        packet = build_validation_packet(
            plan_dir, batch, codebase_root=codebase_root
        )
        payload = validator(packet)
        update = parse_validator_output(payload)
        if update.codebase_root != codebase_root:
            raise _fail(
                f"codebase_root {update.codebase_root!r} does not match the "
                f"preparation root {codebase_root!r}"
            )
        returned = set(update.session_ids)
        expected = set(batch)
        if returned != expected:
            raise _fail(
                "batch result covers sessions "
                f"{', '.join(sorted(returned)) or '(none)'}; expected "
                f"{', '.join(batch)}"
            )
        aggregate = merge_validations(aggregate, update)
        save_prepare_validation(plan_dir, aggregate)

    if aggregate is None:
        raise _fail("no validator output was produced for this plan")
    missing = [sid for sid in session_ids if aggregate.session(sid) is None]
    if missing:
        raise _fail(
            f"stored aggregate is missing sessions: {', '.join(missing)}"
        )
    return aggregate, len(pending)


def _baseline_check_entry(
    session_id: str,
    name: str,
    expectation: str,
    status: str,
    satisfied: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "name": name,
        "expectation": expectation,
        "status": status,
        "satisfied": satisfied,
        "detail": detail,
    }


def _cached_baseline_entries(
    plan_dir: Path,
    cwd: Path,
    unique_checks: list[tuple[str, str, Any]],
) -> list[dict[str, Any]] | None:
    """Return check entries from recorded evidence, or ``None`` when any
    check lacks valid (current-fingerprint, pass/fail) evidence."""
    cache = BaselineCache.load(plan_dir / BASELINE_RESULTS_FILENAME)
    fingerprint = repository_fingerprint(cwd)
    entries: list[dict[str, Any]] = []
    for key, session_id, check in unique_checks:
        record = cache.get(key)
        if record is None:
            return None
        result = record.result
        if str(result.get("repository_fingerprint", "")) != fingerprint:
            return None
        if str(result.get("status", "")) not in _REUSABLE_BASELINE_STATUSES:
            return None
        entries.append(
            _baseline_check_entry(
                session_id,
                check.name,
                getattr(check, "baseline_expectation", "skip"),
                str(result.get("status", "")),
                bool(result.get("satisfied", False)),
                str(result.get("detail", "")),
            )
        )
    return entries


def _run_baseline_stage(
    plan_dir: Path,
    cwd: Path,
    contracts_by_session: dict[str, Any],
) -> dict[str, Any]:
    """Execute (or reuse) baseline evidence and summarize the outcomes."""
    collected = baseline.collect_baseline_checks(contracts_by_session)
    if not collected:
        return {"check_count": 0, "reused": False, "checks": [], "unsatisfied": []}

    unique_checks: list[tuple[str, str, Any]] = []
    seen_keys: set[str] = set()
    for session_id, check in collected:
        key = baseline.baseline_identity_key(check, cwd)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_checks.append((key, session_id, check))

    cached = _cached_baseline_entries(plan_dir, cwd, unique_checks)
    if cached is not None:
        checks = cached
        reused = True
    else:
        run = baseline.run_baseline(plan_dir, cwd)
        checks = [
            _baseline_check_entry(
                outcome.session_id,
                outcome.name,
                outcome.expectation,
                outcome.status,
                outcome.satisfied,
                outcome.detail,
            )
            for outcome in run.outcomes
        ]
        reused = False

    return {
        "check_count": len(checks),
        "reused": reused,
        "checks": checks,
        "unsatisfied": [entry for entry in checks if not entry["satisfied"]],
    }


def _format_run_command(
    plan_dir: Path,
    *,
    cwd: Path,
    harness_name: str,
    harness_model: str | None,
    review_harness: str | None,
    review_harness_model: str | None,
) -> str:
    argv = [
        "asl",
        "run",
        str(plan_dir),
        "--cwd",
        str(cwd),
        "--harness",
        harness_name,
    ]
    if harness_model:
        argv.extend(["--harness-model", harness_model])
    if review_harness:
        argv.extend(["--review-harness", review_harness])
    if review_harness_model:
        argv.extend(["--review-harness-model", review_harness_model])
    return shlex.join(argv)


def _format_resume_command(
    plan_dir: Path,
    *,
    cwd: Path,
    harness_name: str,
    harness_model: str | None,
    validator_harness: str,
    review_harness: str | None,
    review_harness_model: str | None,
) -> str:
    argv = [
        "asl",
        "prepare",
        "--resume",
        str(plan_dir),
        "--cwd",
        str(cwd),
        "--harness",
        harness_name,
        "--validator-harness",
        validator_harness,
    ]
    if harness_model:
        argv.extend(["--harness-model", harness_model])
    if review_harness:
        argv.extend(["--review-harness", review_harness])
    if review_harness_model:
        argv.extend(["--review-harness-model", review_harness_model])
    return shlex.join(argv)


def prepare_plan(
    source: str | Path | None = None,
    *,
    cwd: str | Path,
    harness: str = "codex",
    harness_model: str | None = None,
    validator_harness: str = "codex",
    review_harness: str | None = None,
    review_harness_model: str | None = None,
    base_dir: str | Path | None = None,
    resume_plan_dir: str | Path | None = None,
    validator: ValidatorRunner | None = None,
    batch_size: int = DEFAULT_VALIDATION_BATCH_SIZE,
    unsafe_bypass: bool = True,
    validator_timeout: int | None = None,
    state_dir: str | Path | None = None,
) -> PrepareResult:
    """Run the full preparation pipeline for one source plan (or resume one).

    Stages run strictly in order: structure gate, deterministic authoring,
    structured validation, exact factual correction, baseline execution, and
    the final ``--mark-loop-ready`` preflight. A source plan without at least
    two explicit session headings and complete per-session contracts returns
    ``needs-plan-iteration`` before any output directory is created. Any later
    failure preserves the authored directory as ``draft`` with structured
    blockers and a resumable ``asl prepare --resume`` command.

    ``resume_plan_dir`` reruns validation (only for sessions missing from the
    stored aggregate) and baselines (reusing recorded evidence whose
    repository fingerprint still matches) against an existing split directory
    without creating another split.
    """
    canonical_cwd = canonicalize_path(cwd)
    if validator is None:
        selected_validator: ValidatorRunner = lambda packet: _launch_validator(
            validator_harness,
            packet,
            unsafe_bypass=unsafe_bypass,
            timeout=validator_timeout,
        )
    else:
        selected_validator = validator

    steps: list[PrepareStep] = []
    blockers: list[PrepareBlocker] = []

    if resume_plan_dir is None:
        if source is None:
            raise PlanError(
                "prepare requires a source plan file (or a resume plan directory)"
            )
        source_path = os.path.realpath(
            os.path.abspath(os.path.expanduser(os.fspath(source)))
        )
        try:
            text = Path(source_path).read_text(encoding="utf-8")
        except OSError as error:
            raise PlanError(
                f"cannot read source plan {source_path}: {error}"
            ) from error

        parsed: authoring.ParsedPlan | None
        try:
            parsed = authoring.parse_plan_document(text)
            structure_blockers, contracts_by_id = _structure_blockers(parsed)
        except PlanError as error:
            parsed = None
            structure_blockers = [
                PrepareBlocker(stage=PREPARE_STAGE_STRUCTURE, message=str(error))
            ]
            contracts_by_id = {}
        if structure_blockers:
            steps.append(
                PrepareStep(
                    PREPARE_STAGE_STRUCTURE,
                    PREPARE_STEP_FAILED,
                    (
                        "source plan is not preparation-ready; iterate on the "
                        "plan and rerun `asl prepare`"
                    ),
                )
            )
            return PrepareResult(
                ok=False,
                reason=PREPARE_EXIT_REASON_NEEDS_PLAN_ITERATION,
                plan_dir=None,
                source_path=source_path,
                session_ids=parsed.session_ids if parsed is not None else (),
                steps=tuple(steps),
                blockers=tuple(structure_blockers),
            )
        assert parsed is not None
        steps.append(
            PrepareStep(
                PREPARE_STAGE_STRUCTURE,
                PREPARE_STEP_OK,
                (
                    f"{len(parsed.sessions)} sessions with parseable "
                    "completion contracts"
                ),
            )
        )
        plan_dir = _author_plan_directory(
            source_path, text, parsed, contracts_by_id, base_dir=base_dir
        )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_AUTHORING,
                PREPARE_STEP_OK,
                f"split authored at {plan_dir}",
            )
        )
        session_ids = parsed.session_ids
    else:
        plan_dir = canonicalize_path(resume_plan_dir)
        source_path = None
        snapshot = inspect_plan(plan_dir)
        session_ids = tuple(status.id for status in snapshot.sessions)
        steps.append(
            PrepareStep(
                PREPARE_STAGE_STRUCTURE,
                PREPARE_STEP_SKIPPED,
                "resume mode: existing split directory reused",
            )
        )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_AUTHORING,
                PREPARE_STEP_SKIPPED,
                "resume mode: no new split created",
            )
        )

    resume_command = _format_resume_command(
        plan_dir,
        cwd=canonical_cwd,
        harness_name=harness,
        harness_model=harness_model,
        validator_harness=validator_harness,
        review_harness=review_harness,
        review_harness_model=review_harness_model,
    )

    def _failed(
        reason: str,
        *,
        corrections: tuple[CorrectionOutcome, ...] = (),
        baseline_summary: dict[str, Any] | None = None,
    ) -> PrepareResult:
        _refresh_draft_readiness(plan_dir)
        return PrepareResult(
            ok=False,
            reason=reason,
            plan_dir=plan_dir,
            source_path=source_path,
            session_ids=session_ids,
            steps=tuple(steps),
            blockers=tuple(blockers),
            corrections=corrections,
            baseline=baseline_summary,
            resume_command=resume_command,
        )

    try:
        aggregate, newly_validated = _run_validation_stage(
            plan_dir,
            session_ids,
            codebase_root=str(canonical_cwd),
            validator=selected_validator,
            batch_size=batch_size,
        )
        write_expansions(plan_dir, aggregate)
    except (PlanError, harnesses.HarnessError) as error:
        blockers.append(
            PrepareBlocker(stage=PREPARE_STAGE_VALIDATION, message=str(error))
        )
        steps.append(
            PrepareStep(PREPARE_STAGE_VALIDATION, PREPARE_STEP_FAILED, str(error))
        )
        return _failed(PREPARE_EXIT_REASON_VALIDATION_FAILED)
    validation_detail = (
        f"{newly_validated} session(s) validated"
        if newly_validated
        else "all sessions already validated; stored aggregate reused"
    )
    steps.append(
        PrepareStep(
            PREPARE_STAGE_VALIDATION,
            PREPARE_STEP_OK,
            f"{validation_detail}; expansions written",
        )
    )

    try:
        corrections = apply_corrections(plan_dir, aggregate)
    except PlanError as error:
        blockers.append(
            PrepareBlocker(stage=PREPARE_STAGE_CORRECTIONS, message=str(error))
        )
        steps.append(
            PrepareStep(PREPARE_STAGE_CORRECTIONS, PREPARE_STEP_FAILED, str(error))
        )
        return _failed(PREPARE_EXIT_REASON_VALIDATION_FAILED)
    applied = sum(1 for o in corrections if o.status == CORRECTION_APPLIED)
    already = sum(1 for o in corrections if o.status == CORRECTION_ALREADY_APPLIED)
    blocked = sum(1 for o in corrections if o.status == CORRECTION_BLOCKED)
    steps.append(
        PrepareStep(
            PREPARE_STAGE_CORRECTIONS,
            PREPARE_STEP_OK,
            f"{applied} applied, {already} already applied, {blocked} blocked",
        )
    )
    _refresh_draft_readiness(plan_dir)

    contracts_by_session = {
        sid: contracts.read_completion_contract(plan_dir, sid)
        for sid in session_ids
    }
    baseline_summary = _run_baseline_stage(
        plan_dir, canonical_cwd, contracts_by_session
    )
    if baseline_summary["unsatisfied"]:
        for entry in baseline_summary["unsatisfied"]:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_BASELINE,
                    session_id=entry["session_id"],
                    message=(
                        f"check {entry['name']!r} expected "
                        f"{entry['expectation']!r}, got {entry['status']!r}: "
                        f"{entry['detail']}"
                    ),
                )
            )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_BASELINE,
                PREPARE_STEP_FAILED,
                (
                    f"{len(baseline_summary['unsatisfied'])} of "
                    f"{baseline_summary['check_count']} baseline check(s) "
                    "unsatisfied"
                ),
            )
        )
        return _failed(
            PREPARE_EXIT_REASON_BASELINE_UNSATISFIED,
            corrections=corrections,
            baseline_summary=baseline_summary,
        )
    if baseline_summary["check_count"] == 0:
        baseline_detail = "no checks declare a baseline expectation"
    else:
        baseline_detail = (
            f"{baseline_summary['check_count']} baseline check(s) satisfied"
        )
        if baseline_summary["reused"]:
            baseline_detail += " (cached evidence reused)"
    steps.append(
        PrepareStep(PREPARE_STAGE_BASELINE, PREPARE_STEP_OK, baseline_detail)
    )

    report = preflight.run_preflight(
        plan_dir, cwd=canonical_cwd, state_dir=state_dir
    )
    mark_blockers = preflight._mark_loop_ready_blockers(report)
    if mark_blockers:
        for finding in mark_blockers:
            blockers.append(
                PrepareBlocker(
                    stage=PREPARE_STAGE_PREFLIGHT,
                    category=finding.category,
                    session_id=finding.session_id or None,
                    message=finding.message,
                )
            )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_PREFLIGHT,
                PREPARE_STEP_FAILED,
                preflight._mark_loop_ready_refusal_message(mark_blockers),
            )
        )
        return _failed(
            PREPARE_EXIT_REASON_NOT_LOOP_READY,
            corrections=corrections,
            baseline_summary=baseline_summary,
        )
    marked, mark_error = preflight._mark_loop_ready(report)
    if not marked:
        blockers.append(
            PrepareBlocker(
                stage=PREPARE_STAGE_PREFLIGHT,
                message=mark_error or "could not mark the plan loop-ready",
            )
        )
        steps.append(
            PrepareStep(
                PREPARE_STAGE_PREFLIGHT,
                PREPARE_STEP_FAILED,
                mark_error or "could not mark the plan loop-ready",
            )
        )
        return _failed(
            PREPARE_EXIT_REASON_NOT_LOOP_READY,
            corrections=corrections,
            baseline_summary=baseline_summary,
        )
    steps.append(
        PrepareStep(
            PREPARE_STAGE_PREFLIGHT, PREPARE_STEP_OK, "plan marked loop-ready"
        )
    )

    return PrepareResult(
        ok=True,
        reason=PREPARE_EXIT_REASON_OK,
        plan_dir=plan_dir,
        source_path=source_path,
        session_ids=session_ids,
        steps=tuple(steps),
        blockers=tuple(blockers),
        corrections=corrections,
        baseline=baseline_summary,
        marked_loop_ready=True,
        run_command=_format_run_command(
            plan_dir,
            cwd=canonical_cwd,
            harness_name=harness,
            harness_model=harness_model,
            review_harness=review_harness,
            review_harness_model=review_harness_model,
        ),
        resume_command=resume_command,
    )


def _result_payload_fields(result: PrepareResult) -> dict[str, Any]:
    return {
        "plan_dir": str(result.plan_dir) if result.plan_dir is not None else None,
        "source_path": result.source_path,
        "session_ids": list(result.session_ids),
        "steps": [step.to_dict() for step in result.steps],
        "blockers": [blocker.to_dict() for blocker in result.blockers],
        "corrections": [outcome.to_dict() for outcome in result.corrections],
        "baseline": result.baseline,
        "marked_loop_ready": result.marked_loop_ready,
        "run_command": result.run_command,
        "resume_command": result.resume_command,
    }


_PREPARE_STEP_MARKERS = {
    PREPARE_STEP_OK: "OK",
    PREPARE_STEP_FAILED: "FAIL",
    PREPARE_STEP_SKIPPED: "SKIP",
}


def _print_text_prepare(result: PrepareResult, *, plan_arg: str) -> None:
    print(f"Plan: {plan_arg}")
    if result.plan_dir is not None:
        print(f"Plan directory: {result.plan_dir}")
    for step in result.steps:
        marker = _PREPARE_STEP_MARKERS.get(step.status, step.status.upper())
        print(f"  [{marker}] {step.name}: {step.detail}")
    for blocker in result.blockers:
        location = f" (session {blocker.session_id})" if blocker.session_id else ""
        print(f"  blocker [{blocker.stage}]{location}: {blocker.message}")
    if result.ok and result.run_command is not None:
        print(f"Run command: {result.run_command}")
    elif result.resume_command is not None:
        print(f"Resume with: {result.resume_command}")


def prepare_from_args(args: Any) -> int:
    """Run the ``prepare`` subcommand from CLI arguments."""
    want_json = bool(args.json)
    plan_arg = str(args.plan)
    resume = bool(args.resume)
    harness_name = args.harness
    harness_model = args.harness_model
    validator_harness = args.validator_harness
    review_harness = args.review_harness
    review_harness_model = args.review_harness_model
    common_fields = {
        "plan": plan_arg,
        "cwd": str(args.cwd),
        "harness": harness_name,
        "harness_model": harness_model,
        "validator_harness": validator_harness,
        "review_harness": review_harness,
        "review_harness_model": review_harness_model,
        "resume": resume,
        "base_dir": str(args.base_dir) if args.base_dir else None,
    }

    def _emit_input_error(reason: str, error: str) -> int:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "prepare",
                    reason=reason,
                    error=error,
                    paused=False,
                    **common_fields,
                )
            )
        else:
            print(f"agent-session-loop prepare: {error}", file=sys.stderr)
        return 1

    if review_harness is not None and review_harness == harness_name:
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            "--review-harness must differ from --harness",
        )
    if review_harness_model is not None and review_harness is None:
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            "--review-harness-model requires --review-harness",
        )
    cwd_path = Path(os.path.expanduser(str(args.cwd)))
    if not cwd_path.is_dir():
        return _emit_input_error(
            PREPARE_EXIT_REASON_CWD_ERROR,
            f"cwd is not a directory: {args.cwd}",
        )
    plan_path = Path(os.path.expanduser(plan_arg))
    if resume:
        if (
            not plan_path.is_dir()
            or not (plan_path / "manifest.md").is_file()
            or not (plan_path / "context.md").is_file()
        ):
            return _emit_input_error(
                PREPARE_EXIT_REASON_INPUT_ERROR,
                (
                    "--resume requires an existing plan directory containing "
                    f"manifest.md and context.md: {plan_arg}"
                ),
            )
    elif not plan_path.is_file():
        return _emit_input_error(
            PREPARE_EXIT_REASON_INPUT_ERROR,
            f"source plan file does not exist: {plan_arg}",
        )

    try:
        result = prepare_plan(
            None if resume else plan_path,
            cwd=args.cwd,
            harness=harness_name,
            harness_model=harness_model,
            validator_harness=validator_harness,
            review_harness=review_harness,
            review_harness_model=review_harness_model,
            base_dir=args.base_dir,
            resume_plan_dir=plan_path if resume else None,
            unsafe_bypass=getattr(args, "unsafe_bypass", True),
            state_dir=getattr(args, "state_dir", None),
        )
    except PlanError as error:
        return _emit_input_error(
            PREPARE_EXIT_REASON_PLAN_STRUCTURE, str(error)
        )

    fields = {**common_fields, **_result_payload_fields(result)}
    if result.ok:
        if want_json:
            responses.print_json(
                responses.success_payload(
                    "prepare", reason=PREPARE_EXIT_REASON_OK, **fields
                )
            )
        else:
            _print_text_prepare(result, plan_arg=plan_arg)
        return 0

    error_message = (
        result.blockers[0].message
        if result.blockers
        else f"preparation failed: {result.reason}"
    )
    if want_json:
        responses.print_json(
            responses.error_payload(
                "prepare",
                reason=result.reason,
                error=error_message,
                paused=False,
                **fields,
            )
        )
    else:
        _print_text_prepare(result, plan_arg=plan_arg)
        print(f"agent-session-loop prepare: {error_message}", file=sys.stderr)
    return 1
