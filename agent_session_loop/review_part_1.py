# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405
from . import review_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .review_shared import *

FINAL_REVIEW_FILENAME = "final-review.md"
FINAL_REVIEW_ARTIFACT_FILENAME = "final-review.json"
REVIEW_STATUS_ALL_CLEAR = "Status: All clear, no findings"
_REVIEW_STATUS_FINDINGS_RE = re.compile(
    r"^Status: ([0-9]+) Findings "
    r"\(([0-9]+) high, ([0-9]+) med, ([0-9]+) low\)$"
)

# Generated review session markers. Every generated review session carries the
# base marker (v1 readers keep recognizing v2 sessions as review sessions); a
# session generated at version >= 2 additionally carries the version marker on
# the following line. Absence of a version marker means version 1.
REVIEW_SESSION_MARKER = "<!-- asl:generated-review-session -->"
REVIEW_SESSION_VERSION_MARKER_TEMPLATE = (
    "<!-- asl:generated-review-session version={version} -->"
)
_REVIEW_SESSION_VERSION_MARKER_RE = re.compile(
    r"<!--\s*asl:generated-review-session\s+version=([0-9]+)\s*-->"
)
GENERATED_REVIEW_SESSION_VERSION = 4

# Repository-aware reviews use an independent additive version. Readers accept
# both artifact schemas; artifact v3 is selected only when the plan declares a
# repository registry.
REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION = 5

# Lowest marker version that must produce ``final-review.json``. Pinned
# independently of GENERATED_REVIEW_SESSION_VERSION: raising the generated
# version must never drop artifact enforcement for review sessions already
# pending on disk at an older version.
STRUCTURED_REVIEW_MIN_VERSION = 2

# Schema version stamped on ``final-review.json``. The parser fail-closes on
# any other value so future schema changes cannot be half-read.
REVIEW_ARTIFACT_VERSION = 2
REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION = 3
SUPPORTED_REVIEW_ARTIFACT_VERSIONS = (
    REVIEW_ARTIFACT_VERSION,
    REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
)

REVIEW_OUTCOME_ALL_CLEAR = "all-clear"
REVIEW_OUTCOME_FINDINGS = "findings"
REVIEW_OUTCOMES = (REVIEW_OUTCOME_ALL_CLEAR, REVIEW_OUTCOME_FINDINGS)
REVIEW_SEVERITIES = ("high", "med", "low")

# Required keys on every structured finding. ``evidence`` entries carry a
# required ``path`` and an optional ``symbol``; ``verification_checks`` entries
# must be completion-contract check objects so a repair session materialized
# from the finding can embed them verbatim in its ``### Verify`` recipe.
FINDING_REQUIRED_KEYS = (
    "id",
    "severity",
    "title",
    "evidence",
    "explanation",
    "repair_group",
    "done_criteria",
    "verification_checks",
)
_EVIDENCE_ALLOWED_KEYS = frozenset({"path", "symbol"})
_ARTIFACT_ALLOWED_KEYS = frozenset({"version", "outcome", "findings"})

# Stable ``reason`` vocabulary for non-ok verdict payloads.
REVIEW_REASON_MALFORMED = "malformed-review"
REVIEW_REASON_MISSING = "review-missing"
REVIEW_REASON_MISSING_ARTIFACT = "missing-review-artifact"
REVIEW_REASON_MALFORMED_ARTIFACT = "malformed-review-artifact"
REVIEW_REASON_CONTRADICTORY_ARTIFACT = "contradictory-review-artifact"

# Message prefix contracts._parse_check interpolates; stripped when re-raising
# check-validation failures in finding context.
_CONTRACT_CHECK_MESSAGE_PREFIX_RE = re.compile(r"^session \S+ contract check #[0-9]+ ")
_CHECK_VALIDATION_SESSION_ID = plans_session_ids.parse_session_id("1")


class ReviewArtifactError(ValueError):
    """Raised when ``final-review.json`` is malformed or incomplete."""


class ReviewConsistencyError(ReviewArtifactError):
    """Raised when ``final-review.json`` contradicts ``final-review.md``."""


def final_review_output_path(plan_dir: str | Path) -> Path:
    """Return the path of the human review document."""
    return Path(plan_dir) / FINAL_REVIEW_FILENAME


def final_review_artifact_path(plan_dir: str | Path) -> Path:
    """Return the path of the structured review artifact."""
    return Path(plan_dir) / FINAL_REVIEW_ARTIFACT_FILENAME


def final_review_output_sha256(plan_dir: str | Path) -> str:
    """Return the full SHA-256 digest of ``final-review.md``.

    The file is streamed through :func:`plans_files.file_sha256`, whose fixed-size
    reads keep memory use bounded even when a malformed or manually edited
    review artifact is unexpectedly large.
    """
    return plans_files.file_sha256(final_review_output_path(plan_dir))


def final_review_artifact_sha256(plan_dir: str | Path) -> str:
    """Return the full SHA-256 digest of ``final-review.json``.

    This hashes the complete serialized artifact, intentionally covering
    repair grouping, done criteria, and verification checks that are excluded
    from :func:`finding_fingerprint` identity.
    """
    return plans_files.file_sha256(final_review_artifact_path(plan_dir))


def review_session_version_marker(
    version: int = GENERATED_REVIEW_SESSION_VERSION,
) -> str:
    """Render the versioned marker line for a generated review session."""
    return REVIEW_SESSION_VERSION_MARKER_TEMPLATE.format(version=version)


def contains_review_session_marker(text: str) -> bool:
    """Return True when a session body carries any generated-review marker."""
    return (
        REVIEW_SESSION_MARKER in text
        or _REVIEW_SESSION_VERSION_MARKER_RE.search(text) is not None
    )


def review_session_marker_version(text: str) -> int | None:
    """Return the generated-review version of a session body.

    ``None`` when the body carries no review marker; ``1`` when only the base
    (unversioned) marker is present; otherwise the integer from the version
    marker.
    """
    match = _REVIEW_SESSION_VERSION_MARKER_RE.search(text)
    if match is not None:
        return int(match.group(1))
    if REVIEW_SESSION_MARKER in text:
        return 1
    return None


def _verdict_error(error: str) -> dict[str, Any]:
    return {"ok": False, "reason": REVIEW_REASON_MALFORMED, "error": error}


def parse_review_verdict(text: str) -> dict[str, Any]:
    """Parse a ``final-review.md`` status line into a stable verdict payload.

    Returns ``ok: True`` with ``outcome`` ``all-clear``/``findings`` plus the
    status line and severity totals, or ``ok: False`` with ``reason:
    "malformed-review"`` for any grammar violation. There must be exactly one
    ``Status:`` line, it must be the final non-empty line, and a findings line
    must carry severity counts that sum to a positive total.
    """
    nonempty = [line.strip() for line in text.splitlines() if line.strip()]
    if not nonempty:
        return _verdict_error(f"{FINAL_REVIEW_FILENAME} is empty")

    status_lines = [line for line in nonempty if line.startswith("Status:")]
    if not status_lines:
        return _verdict_error("missing required final Status line")
    if len(status_lines) > 1:
        return _verdict_error("expected exactly one Status line")

    status_line = status_lines[0]
    if status_line != nonempty[-1]:
        return _verdict_error("Status line must be the final non-empty line")

    if status_line == REVIEW_STATUS_ALL_CLEAR:
        return {
            "ok": True,
            "outcome": REVIEW_OUTCOME_ALL_CLEAR,
            "status_line": status_line,
            "total_findings": 0,
            "findings_by_severity": {"high": 0, "med": 0, "low": 0},
        }

    match = _REVIEW_STATUS_FINDINGS_RE.fullmatch(status_line)
    if match is None:
        return _verdict_error("malformed Status line")

    total, high, med, low = (int(value) for value in match.groups())
    if total <= 0:
        return _verdict_error("finding total must be greater than zero")
    if high + med + low != total:
        return _verdict_error("finding severity counts do not add up")

    return {
        "ok": True,
        "outcome": REVIEW_OUTCOME_FINDINGS,
        "status_line": status_line,
        "total_findings": total,
        "findings_by_severity": {"high": high, "med": med, "low": low},
    }


def _require_finding_str(finding_id: str, value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewArtifactError(
            f"finding {finding_id!r} field {label!r} must be a non-empty string"
        )
    return value.strip()


def _validate_repository_alias(
    finding_id: str,
    value: object,
    *,
    label: str,
    repository_registry: workspaces.RepositoryRegistry,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewArtifactError(
            f"finding {finding_id!r} field {label!r} must be a non-empty "
            "repository alias"
        )
    alias = value.strip()
    try:
        repository_registry.get(alias)
    except workspaces.WorkspaceError as error:
        raise ReviewArtifactError(
            f"finding {finding_id!r} field {label!r} names unknown "
            f"repository alias {alias!r}"
        ) from error
    return alias


def _validate_evidence_for_registry(
    finding_id: str,
    value: object,
    *,
    repository_registry: workspaces.RepositoryRegistry | None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ReviewArtifactError(
            f"finding {finding_id!r} field 'evidence' must be a non-empty list"
        )
    evidence: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ReviewArtifactError(
                f"finding {finding_id!r} evidence #{index + 1} must be an "
                "object with a 'path' and optional 'symbol'"
            )
        unknown = set(item) - _EVIDENCE_ALLOWED_KEYS
        if unknown:
            raise ReviewArtifactError(
                f"finding {finding_id!r} evidence #{index + 1} has unknown "
                f"keys: {', '.join(sorted(unknown))}"
            )
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ReviewArtifactError(
                f"finding {finding_id!r} evidence #{index + 1} 'path' must "
                "be a non-empty string"
            )
        normalized_path = path.strip()
        if repository_registry is not None:
            try:
                qualified = plans_session_scope.parse_qualified_path(
                    normalized_path, repository_registry.aliases
                )
            except (plans_errors.PlanError, ValueError) as error:
                raise ReviewArtifactError(
                    f"finding {finding_id!r} evidence #{index + 1} 'path' "
                    "must be a qualified Alias:path beneath a declared "
                    f"repository: {error}"
                ) from error
            normalized_path = str(qualified)
        entry: dict[str, Any] = {"path": normalized_path}
        if "symbol" in item:
            symbol = item["symbol"]
            if not isinstance(symbol, str) or not symbol.strip():
                raise ReviewArtifactError(
                    f"finding {finding_id!r} evidence #{index + 1} 'symbol' "
                    "must be a non-empty string when present"
                )
            entry["symbol"] = symbol.strip()
        evidence.append(entry)
    return evidence


def _validate_string_list(finding_id: str, value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReviewArtifactError(
            f"finding {finding_id!r} field {label!r} must be a non-empty list"
        )
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ReviewArtifactError(
                f"finding {finding_id!r} {label} #{index + 1} must be a "
                "non-empty string"
            )
        items.append(item.strip())
    return items


def _validate_verification_checks(
    finding_id: str,
    value: object,
    *,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ReviewArtifactError(
            f"finding {finding_id!r} field 'verification_checks' must be a "
            "non-empty list of completion-contract check objects"
        )
    seen_names: set[str] = set()
    for index, item in enumerate(value):
        if repository_registry is not None and (
            not isinstance(item, dict) or "repo" not in item
        ):
            raise ReviewArtifactError(
                f"finding {finding_id!r} verification check #{index + 1} "
                "must identify its repository with 'repo'"
            )
        try:
            check = contracts._parse_check(
                item,
                index=index,
                session_id=_CHECK_VALIDATION_SESSION_ID,
                repository_registry=repository_registry,
            )
        except plans_errors.PlanError as error:
            detail = _CONTRACT_CHECK_MESSAGE_PREFIX_RE.sub("", str(error))
            raise ReviewArtifactError(
                f"finding {finding_id!r} verification check #{index + 1} "
                f"is invalid: {detail}"
            ) from error
        if check.name in seen_names:
            raise ReviewArtifactError(
                f"finding {finding_id!r} verification check name "
                f"{check.name!r} is duplicated"
            )
        seen_names.add(check.name)
    return list(value)
