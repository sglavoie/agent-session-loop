"""Fail-closed parsing of validator output into the prepare schema."""

from __future__ import annotations

import re

from .plans import PlanError, parse_session_id
from .prepare_models import (
    CLAIM_TYPES,
    DEPENDENCY_DIRECTIONS,
    ISSUE_TYPES,
    LEGACY_PREPARE_VALIDATION_VERSION,
    MAX_PATTERNS_PER_SESSION,
    MISSING_SPARK_SUITABILITY,
    PREPARE_VALIDATION_VERSION,
    TO_BE_CREATED_LOCATION,
    ConcreteDetail,
    ContractProblem,
    PrepareValidation,
    ProposedCorrection,
    ReusePattern,
    SessionDependency,
    SessionValidation,
    SparkSuitability,
    ValidationIssue,
    ValidatorRepository,
    VerifiedClaim,
)


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


def _require_choice(value: object, *, label: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        allowed = ", ".join(choices)
        raise _fail(f"{label} must be one of: {allowed}; got {value!r}")
    return str(value)


def _parse_line_number(value: object) -> int | None:
    """Read a claim's advisory line pointer, degrading anything unusable to null.

    The line is evidence, not a gate: null is always legal, so a validator that
    writes ``"42"``, ``"42-50"``, ``0``, or a line on to-be-created code loses
    only the pointer instead of failing its whole batch.
    """
    if isinstance(value, str):
        match = re.match(r"\s*(\d+)", value)
        value = int(match.group(1)) if match else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _parse_repo(data: dict, *, label: str, required: bool) -> str | None:
    value = data.get("repo")
    if not required and value is None:
        return None
    return _require_text(value, label=f"{label} 'repo'")


def _parse_claim(payload: object, *, label: str, repo_required: bool) -> VerifiedClaim:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "claim",
            "claim_type",
            "location",
            "line",
            "to_be_created",
        )
        + (("repo",) if repo_required else ()),
    )
    to_be_created = _require_bool(
        data.get("to_be_created"), label=f"{label} 'to_be_created'"
    )
    if to_be_created:
        # Code that does not exist yet has no line; drop one if supplied.
        location = str(data.get("location") or TO_BE_CREATED_LOCATION)
        line = None
    else:
        location = _require_text(data.get("location"), label=f"{label} 'location'")
        line = _parse_line_number(data.get("line"))
    return VerifiedClaim(
        claim=_require_text(data.get("claim"), label=f"{label} 'claim'"),
        claim_type=_require_choice(
            data.get("claim_type"), label=f"{label} 'claim_type'", choices=CLAIM_TYPES
        ),
        location=location,
        line=line,
        to_be_created=to_be_created,
        repo=_parse_repo(data, label=label, required=repo_required),
    )


def _parse_issue(
    payload: object, *, label: str, repo_required: bool
) -> ValidationIssue:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("issue_type", "summary", "session_ref", "actual", "fix")
        + (("repo",) if repo_required else ()),
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
        repo=_parse_repo(data, label=label, required=repo_required),
    )


def _parse_detail(
    payload: object, *, label: str, repo_required: bool
) -> ConcreteDetail:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("path", "notes") + (("repo",) if repo_required else ()),
    )
    notes = _require_list(data.get("notes"), label=f"{label} 'notes'")
    if not notes:
        raise _fail(f"{label} 'notes' must list at least one bullet")
    return ConcreteDetail(
        path=_require_text(data.get("path"), label=f"{label} 'path'"),
        notes=tuple(
            _require_text(note, label=f"{label} 'notes'[{index}]")
            for index, note in enumerate(notes)
        ),
        repo=_parse_repo(data, label=label, required=repo_required),
    )


def _parse_pattern(payload: object, *, label: str, repo_required: bool) -> ReusePattern:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("name", "location", "reason")
        + (("repo",) if repo_required else ()),
    )
    return ReusePattern(
        name=_require_text(data.get("name"), label=f"{label} 'name'"),
        location=_require_text(data.get("location"), label=f"{label} 'location'"),
        reason=_require_text(data.get("reason"), label=f"{label} 'reason'"),
        repo=_parse_repo(data, label=label, required=repo_required),
    )


def _parse_dependency(
    payload: object, *, label: str, repo_required: bool
) -> SessionDependency:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("direction", "session_id", "description")
        + (("repo",) if repo_required else ()),
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
        repo=_parse_repo(data, label=label, required=repo_required),
    )


def _parse_contract_problem(
    payload: object, *, label: str, repo_required: bool
) -> ContractProblem:
    # Registered contract checks carry a repo, so validators echo it here;
    # accept it like every other entry, though the problem does not record it.
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("message", "suggested_fix")
        + (("repo",) if repo_required else ()),
    )
    return ContractProblem(
        message=_require_text(data.get("message"), label=f"{label} 'message'"),
        suggested_fix=_require_text(
            data.get("suggested_fix"), label=f"{label} 'suggested_fix'"
        ),
    )


def _parse_correction(
    payload: object, *, label: str, repo_required: bool
) -> ProposedCorrection:
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("expected_text", "replacement_text", "reason")
        + (("repo",) if repo_required else ()),
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
        repo=_parse_repo(data, label=label, required=repo_required),
    )


def _parse_session_validation(
    payload: object, *, index: int, repo_required: bool
) -> SessionValidation:
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
            "spark",
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
        _parse_pattern(
            item,
            label=f"{label} pattern #{position + 1}",
            repo_required=repo_required,
        )
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
        _parse_contract_problem(
            item,
            label=f"{label} contract problem #{position + 1}",
            repo_required=repo_required,
        )
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

    raw_spark = data.get("spark")
    if raw_spark is None:
        spark = MISSING_SPARK_SUITABILITY
    elif (
        isinstance(raw_spark, dict)
        and set(raw_spark) == {"eligible", "reason"}
        and isinstance(raw_spark.get("eligible"), bool)
        and isinstance(raw_spark.get("reason"), str)
        and raw_spark["reason"].strip()
    ):
        spark = SparkSuitability(
            eligible=raw_spark["eligible"],
            reason=raw_spark["reason"].strip(),
            reported=True,
        )
    else:
        spark = SparkSuitability(
            eligible=False,
            reason="Spark eligibility marker malformed; using baseline",
            reported=True,
        )

    return SessionValidation(
        session_id=session_id,
        title=_require_text(data.get("title"), label=f"{label} 'title'"),
        claims=tuple(
            _parse_claim(
                item,
                label=f"{label} claim #{position + 1}",
                repo_required=repo_required,
            )
            for position, item in enumerate(
                _require_list(data.get("claims"), label=f"{label} 'claims'")
            )
        ),
        issues=tuple(
            _parse_issue(
                item,
                label=f"{label} issue #{position + 1}",
                repo_required=repo_required,
            )
            for position, item in enumerate(
                _require_list(data.get("issues"), label=f"{label} 'issues'")
            )
        ),
        details=tuple(
            _parse_detail(
                item,
                label=f"{label} detail #{position + 1}",
                repo_required=repo_required,
            )
            for position, item in enumerate(
                _require_list(data.get("details"), label=f"{label} 'details'")
            )
        ),
        patterns=patterns,
        dependencies=tuple(
            _parse_dependency(
                item,
                label=f"{label} dependency #{position + 1}",
                repo_required=repo_required,
            )
            for position, item in enumerate(
                _require_list(data.get("dependencies"), label=f"{label} 'dependencies'")
            )
        ),
        contract_checked=contract_checked,
        contract_problems=contract_problems,
        corrections=tuple(
            _parse_correction(
                item,
                label=f"{label} correction #{position + 1}",
                repo_required=repo_required,
            )
            for position, item in enumerate(
                _require_list(data.get("corrections"), label=f"{label} 'corrections'")
            )
        ),
        spark=spark,
    )


def _parse_validator_repository(payload: object, *, index: int) -> ValidatorRepository:
    label = f"repository #{index + 1}"
    data = _require_object(
        payload,
        label=label,
        allowed_keys=("alias", "path", "origin", "root"),
    )
    return ValidatorRepository(
        alias=_require_text(data.get("alias"), label=f"{label} 'alias'"),
        path=_require_text(data.get("path"), label=f"{label} 'path'"),
        origin=_require_text(data.get("origin"), label=f"{label} 'origin'"),
        root=_require_text(data.get("root"), label=f"{label} 'root'"),
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
    if not isinstance(payload, dict):
        raise _fail("validator output must be a JSON object")
    version = payload.get("version")
    if version == LEGACY_PREPARE_VALIDATION_VERSION:
        data = _require_object(
            payload,
            label="validator output",
            allowed_keys=("version", "codebase_root", "sessions"),
        )
        codebase_root = _require_text(
            data.get("codebase_root"), label="validator output 'codebase_root'"
        )
        repositories: tuple[ValidatorRepository, ...] = ()
        repo_required = False
    elif version == PREPARE_VALIDATION_VERSION:
        data = _require_object(
            payload,
            label="validator output",
            allowed_keys=("version", "workspace_root", "repositories", "sessions"),
        )
        codebase_root = _require_text(
            data.get("workspace_root"), label="validator output 'workspace_root'"
        )
        raw_repositories = data.get("repositories")
        if not isinstance(raw_repositories, list) or not raw_repositories:
            raise _fail("validator output 'repositories' must be a non-empty list")
        repositories = tuple(
            _parse_validator_repository(item, index=index)
            for index, item in enumerate(raw_repositories)
        )
        repo_required = True
    else:
        raise _fail(
            f"unsupported version {version!r}; expected "
            f"{LEGACY_PREPARE_VALIDATION_VERSION} or {PREPARE_VALIDATION_VERSION}"
        )
    raw_sessions = data.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise _fail("validator output 'sessions' must be a non-empty list")

    sessions: list[SessionValidation] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_sessions):
        session = _parse_session_validation(
            item, index=index, repo_required=repo_required
        )
        if session.session_id.value in seen:
            raise _fail(f"duplicate session id {session.session_id.value!r}")
        seen.add(session.session_id.value)
        sessions.append(session)

    return PrepareValidation(
        codebase_root=codebase_root,
        sessions=tuple(sessions),
        version=version,
        repositories=repositories,
    )
