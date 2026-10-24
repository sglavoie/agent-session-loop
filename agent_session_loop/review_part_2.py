# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import review_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .review_shared import *
from .review_part_1 import *


def _validate_finding(
    payload: object,
    *,
    index: int,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReviewArtifactError(f"finding #{index + 1} must be a JSON object")
    required_keys = FINDING_REQUIRED_KEYS + (
        ("repo",) if repository_registry is not None else ()
    )
    unknown = set(payload) - set(required_keys)
    if unknown:
        raise ReviewArtifactError(
            f"finding #{index + 1} has unknown keys: {', '.join(sorted(unknown))}"
        )
    missing = [key for key in required_keys if key not in payload]
    if missing:
        raise ReviewArtifactError(
            f"finding #{index + 1} is incomplete; missing required keys: "
            f"{', '.join(missing)}"
        )
    raw_id = payload.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ReviewArtifactError(
            f"finding #{index + 1} 'id' must be a non-empty string"
        )
    finding_id = raw_id.strip()
    severity = payload.get("severity")
    if severity not in REVIEW_SEVERITIES:
        allowed = ", ".join(REVIEW_SEVERITIES)
        raise ReviewArtifactError(
            f"finding {finding_id!r} 'severity' must be one of: {allowed}; "
            f"got {severity!r}"
        )
    finding = {
        "id": finding_id,
        "severity": severity,
        "title": _require_finding_str(finding_id, payload.get("title"), label="title"),
        "evidence": _validate_evidence_for_registry(
            finding_id,
            payload.get("evidence"),
            repository_registry=repository_registry,
        ),
        "explanation": _require_finding_str(
            finding_id, payload.get("explanation"), label="explanation"
        ),
        "repair_group": _require_finding_str(
            finding_id, payload.get("repair_group"), label="repair_group"
        ),
        "done_criteria": _validate_string_list(
            finding_id, payload.get("done_criteria"), label="done_criteria"
        ),
        "verification_checks": _validate_verification_checks(
            finding_id,
            payload.get("verification_checks"),
            repository_registry=repository_registry,
        ),
    }
    if repository_registry is not None:
        finding["repo"] = _validate_repository_alias(
            finding_id,
            payload.get("repo"),
            label="repo",
            repository_registry=repository_registry,
        )
    return finding


def parse_review_artifact(
    text: str,
    *,
    repository_registry: workspaces.RepositoryRegistry | None = None,
) -> dict[str, Any]:
    """Parse ``final-review.json`` into a normalized structured payload.

    Fail-closed: raises :class:`ReviewArtifactError` for invalid JSON, an
    unsupported version, unknown keys, an invalid outcome, or any finding that
    is missing required fields or carries invalid values. Returns the
    normalized artifact with computed ``total_findings`` and
    ``findings_by_severity``.
    """
    if not text.strip():
        raise ReviewArtifactError(f"{FINAL_REVIEW_ARTIFACT_FILENAME} is empty")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} is not valid JSON: "
            f"{error.msg} at line {error.lineno} column {error.colno}"
        ) from error
    if not isinstance(payload, dict):
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} must be a top-level JSON object"
        )
    unknown = set(payload) - _ARTIFACT_ALLOWED_KEYS
    if unknown:
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} has unknown keys: "
            f"{', '.join(sorted(unknown))}"
        )
    version = payload.get("version")
    if version not in SUPPORTED_REVIEW_ARTIFACT_VERSIONS:
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} 'version' must be one of "
            f"{', '.join(str(item) for item in SUPPORTED_REVIEW_ARTIFACT_VERSIONS)}; "
            f"got {version!r}"
        )
    if version == REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION:
        if repository_registry is None:
            raise ReviewArtifactError(
                f"{FINAL_REVIEW_ARTIFACT_FILENAME} version {version} requires "
                "a declared repository registry"
            )
        finding_registry = repository_registry
    else:
        finding_registry = None
    outcome = payload.get("outcome")
    if outcome not in REVIEW_OUTCOMES:
        allowed = ", ".join(REVIEW_OUTCOMES)
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} 'outcome' must be one of: "
            f"{allowed}; got {outcome!r}"
        )
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} 'findings' must be a list"
        )

    findings: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    tally = {"high": 0, "med": 0, "low": 0}
    for index, raw_finding in enumerate(raw_findings):
        finding = _validate_finding(
            raw_finding,
            index=index,
            repository_registry=finding_registry,
        )
        if finding["id"] in seen_ids:
            raise ReviewArtifactError(f"finding id {finding['id']!r} is duplicated")
        seen_ids.add(finding["id"])
        tally[finding["severity"]] += 1
        findings.append(finding)

    if outcome == REVIEW_OUTCOME_ALL_CLEAR and findings:
        raise ReviewArtifactError(
            "outcome 'all-clear' contradicts a non-empty findings list"
        )
    if outcome == REVIEW_OUTCOME_FINDINGS and not findings:
        raise ReviewArtifactError(
            "outcome 'findings' requires a non-empty findings list"
        )

    return {
        "version": version,
        "outcome": outcome,
        "total_findings": len(findings),
        "findings_by_severity": tally,
        "findings": findings,
    }


def ensure_review_consistency(
    verdict: dict[str, Any], artifact: dict[str, Any]
) -> None:
    """Raise when the structured artifact contradicts the Markdown verdict.

    ``verdict`` is an ``ok: True`` payload from :func:`parse_review_verdict`;
    ``artifact`` is the normalized payload from :func:`parse_review_artifact`.
    The Markdown status line and the JSON outcome/severity totals must agree.
    """
    if artifact["outcome"] != verdict["outcome"]:
        raise ReviewConsistencyError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} outcome "
            f"{artifact['outcome']!r} contradicts the {FINAL_REVIEW_FILENAME} "
            f"status line outcome {verdict['outcome']!r}"
        )
    if artifact["total_findings"] != verdict["total_findings"]:
        raise ReviewConsistencyError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} carries "
            f"{artifact['total_findings']} findings but the "
            f"{FINAL_REVIEW_FILENAME} status line declares "
            f"{verdict['total_findings']}"
        )
    if artifact["findings_by_severity"] != verdict["findings_by_severity"]:
        raise ReviewConsistencyError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} severity totals "
            f"{artifact['findings_by_severity']} contradict the "
            f"{FINAL_REVIEW_FILENAME} status line totals "
            f"{verdict['findings_by_severity']}"
        )


def load_review_verdict(
    plan_dir: str | Path, *, require_artifact: bool = False
) -> dict[str, Any]:
    """Read and classify the review artifacts inside ``plan_dir``.

    Returns the shared verdict payload used by both the runner and the
    autopilot. ``ok: True`` payloads always carry ``outcome``, ``status_line``,
    ``total_findings``, ``findings_by_severity``, and ``review_output_path``;
    when ``final-review.json`` is present (and valid and consistent) they also
    carry ``artifact_version``, ``findings``, and ``review_artifact_path``.

    ``require_artifact=True`` (review sessions at or above
    :data:`STRUCTURED_REVIEW_MIN_VERSION`) makes a missing
    ``final-review.json`` a failure. A present-but-invalid artifact always
    fails, regardless of ``require_artifact``.
    """
    review_path = final_review_output_path(plan_dir)
    artifact_path = final_review_artifact_path(plan_dir)
    try:
        text = review_path.read_text(encoding="utf-8")
    except OSError as error:
        return {
            "ok": False,
            "reason": REVIEW_REASON_MISSING,
            "error": f"could not read {FINAL_REVIEW_FILENAME}: {error}",
            "review_output_path": str(review_path),
        }

    payload = parse_review_verdict(text)
    payload["review_output_path"] = str(review_path)
    if not payload["ok"]:
        return payload

    if not artifact_path.is_file():
        if require_artifact:
            return {
                "ok": False,
                "reason": REVIEW_REASON_MISSING_ARTIFACT,
                "error": (
                    f"{FINAL_REVIEW_ARTIFACT_FILENAME} is required for "
                    "generated review sessions at version "
                    f"{STRUCTURED_REVIEW_MIN_VERSION} or newer"
                ),
                "review_output_path": str(review_path),
                "review_artifact_path": str(artifact_path),
            }
        return payload

    try:
        artifact_text = artifact_path.read_text(encoding="utf-8")
    except OSError as error:
        return {
            "ok": False,
            "reason": REVIEW_REASON_MALFORMED_ARTIFACT,
            "error": (f"could not read {FINAL_REVIEW_ARTIFACT_FILENAME}: {error}"),
            "review_output_path": str(review_path),
            "review_artifact_path": str(artifact_path),
        }

    try:
        repository_registry = plans_registry.read_repository_registry(plan_dir)
        artifact = parse_review_artifact(
            artifact_text,
            repository_registry=repository_registry,
        )
        ensure_review_consistency(payload, artifact)
    except ReviewConsistencyError as error:
        return {
            "ok": False,
            "reason": REVIEW_REASON_CONTRADICTORY_ARTIFACT,
            "error": str(error),
            "review_output_path": str(review_path),
            "review_artifact_path": str(artifact_path),
        }
    except (ReviewArtifactError, plans_errors.PlanError) as error:
        return {
            "ok": False,
            "reason": REVIEW_REASON_MALFORMED_ARTIFACT,
            "error": str(error),
            "review_output_path": str(review_path),
            "review_artifact_path": str(artifact_path),
        }

    payload["artifact_version"] = artifact["version"]
    payload["findings"] = artifact["findings"]
    payload["review_artifact_path"] = str(artifact_path)
    return payload


# Command name for the authoritative review-verdict surface.
REVIEW_VERDICT_COMMAND = "review-verdict"


def _review_verdict_summary(verdict: dict[str, Any]) -> tuple[str, int]:
    """Return the full ``final-review.md`` body and its character count.

    ``asl review-verdict`` is the authoritative escape hatch from the bounded
    helper excerpt, so it never truncates: it reads the entire review body and
    reports the true length. The bounded helper summary (which does truncate)
    is the only surface that ever flags ``summary_truncated: true``.
    """
    output_path = verdict.get("review_output_path")
    text = ""
    if isinstance(output_path, str) and output_path:
        try:
            text = Path(output_path).read_text(encoding="utf-8")
        except OSError:
            text = ""
    stripped = text.strip()
    return stripped, len(stripped)


def _review_verdict_next_steps(
    verdict: dict[str, Any],
    *,
    plan_dir: str,
    guidance: str | None,
) -> list[str]:
    """Return exact remediation commands for the current verdict/continuation.

    A non-ok verdict names the exact artifact to correct (or, for a missing
    review, the command that generates it); an all-clear verdict needs nothing;
    a findings verdict defers to the shared continuation renderer so the runner,
    the CLI, and the autopilot describe the same continuation identically.

    Both fallback commands render their plan path through the shared shell-safe
    :func:`agent_session_loop.responses.format_command` builder, so a plan
    directory containing whitespace or shell metacharacters round-trips as a
    single argument under :func:`shlex.split` instead of splitting apart.
    """
    from . import responses

    run_command = responses.format_command(["asl", "run", plan_dir])
    if not verdict.get("ok"):
        reason = verdict.get("reason")
        error = verdict.get("error")
        if reason == REVIEW_REASON_MISSING:
            return [
                f"{run_command} --review-harness codex|claude|copilot  "
                f"# generate {FINAL_REVIEW_FILENAME}"
            ]
        artifact_reasons = {
            REVIEW_REASON_MISSING_ARTIFACT,
            REVIEW_REASON_MALFORMED_ARTIFACT,
            REVIEW_REASON_CONTRADICTORY_ARTIFACT,
        }
        target = (
            verdict.get("review_artifact_path")
            if reason in artifact_reasons
            else verdict.get("review_output_path")
        )
        detail = f": {error}" if error else ""
        return [f"correct {target}{detail}"]
    if verdict.get("outcome") == REVIEW_OUTCOME_ALL_CLEAR:
        return []
    # Findings outcome: the continuation projection is authoritative when it
    # has actionable guidance; otherwise fall back to the command that resumes
    # the runner-owned repair/rereview cycles so a findings verdict always
    # carries a concrete remediation.
    if guidance:
        return [guidance]
    return [
        f"{run_command} --review-harness codex|claude|copilot  "
        "# resume repair/rereview cycles to resolve the findings"
    ]


def _relevant_review_session_version(
    snapshot: plans_models.PlanSnapshot, review_resume: dict[str, Any]
) -> int | None:
    """Return the generated-review version that governs artifact enforcement.

    The relevant session is the source review the continuation projection
    resolved (``review_session_id``); its marker version decides whether
    ``final-review.json`` is required, mirroring the runner's own enforcement.
    Returns ``None`` when no generated review session governs the current
    verdict — a legacy v1 (base-marker-only) review or no generated review at
    all — so the permissive default applies and Markdown-only reviews stay
    supported.
    """
    source_id = review_resume.get("review_session_id")
    if not isinstance(source_id, str) or not source_id:
        return None
    for session in snapshot.sessions:
        if session.id != source_id:
            continue
        try:
            body = session.path.read_text(encoding="utf-8")
        except OSError:
            return None
        return review_session_marker_version(body)
    return None
