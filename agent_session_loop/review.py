"""Shared final-review artifact contract for the runner and the autopilot.

This module is the single source of truth for the review verdict grammar and
the versioned structured review artifact:

- ``final-review.md`` — the human review document. Its final non-empty line
  must be exactly one machine-readable ``Status:`` verdict line.
- ``final-review.json`` — the machine-readable structured review artifact
  (schema :data:`REVIEW_ARTIFACT_VERSION`). Required for review sessions
  generated at :data:`GENERATED_REVIEW_SESSION_VERSION` >= 2; v1 review
  sessions remain Markdown-only compatible.

Both the runner (``agent_session_loop.runner``) and the autopilot helper
(``skills/asl/scripts/asl_autopilot.py``) build their verdict payloads through
:func:`load_review_verdict`, so they report the same payload by construction.
Parsing is fail-closed: a malformed, contradictory, or incomplete structured
artifact never yields an ``ok`` verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from . import contracts
from . import plans


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
GENERATED_REVIEW_SESSION_VERSION = 2

# Schema version stamped on ``final-review.json``. The parser fail-closes on
# any other value so future schema changes cannot be half-read.
REVIEW_ARTIFACT_VERSION = 2

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
_CONTRACT_CHECK_MESSAGE_PREFIX_RE = re.compile(
    r"^session \S+ contract check #[0-9]+ "
)
_CHECK_VALIDATION_SESSION_ID = plans.parse_session_id("1")


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

    The file is streamed through :func:`plans.file_sha256`, whose fixed-size
    reads keep memory use bounded even when a malformed or manually edited
    review artifact is unexpectedly large.
    """
    return plans.file_sha256(final_review_output_path(plan_dir))


def final_review_artifact_sha256(plan_dir: str | Path) -> str:
    """Return the full SHA-256 digest of ``final-review.json``.

    This hashes the complete serialized artifact, intentionally covering
    repair grouping, done criteria, and verification checks that are excluded
    from :func:`finding_fingerprint` identity.
    """
    return plans.file_sha256(final_review_artifact_path(plan_dir))


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


def _validate_evidence(finding_id: str, value: object) -> list[dict[str, Any]]:
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
        entry: dict[str, Any] = {"path": path.strip()}
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


def _validate_string_list(
    finding_id: str, value: object, *, label: str
) -> list[str]:
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
    finding_id: str, value: object
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ReviewArtifactError(
            f"finding {finding_id!r} field 'verification_checks' must be a "
            "non-empty list of completion-contract check objects"
        )
    seen_names: set[str] = set()
    for index, item in enumerate(value):
        try:
            check = contracts._parse_check(
                item, index=index, session_id=_CHECK_VALIDATION_SESSION_ID
            )
        except plans.PlanError as error:
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


def _validate_finding(payload: object, *, index: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReviewArtifactError(f"finding #{index + 1} must be a JSON object")
    unknown = set(payload) - set(FINDING_REQUIRED_KEYS)
    if unknown:
        raise ReviewArtifactError(
            f"finding #{index + 1} has unknown keys: "
            f"{', '.join(sorted(unknown))}"
        )
    missing = [key for key in FINDING_REQUIRED_KEYS if key not in payload]
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
    return {
        "id": finding_id,
        "severity": severity,
        "title": _require_finding_str(
            finding_id, payload.get("title"), label="title"
        ),
        "evidence": _validate_evidence(finding_id, payload.get("evidence")),
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
            finding_id, payload.get("verification_checks")
        ),
    }


def parse_review_artifact(text: str) -> dict[str, Any]:
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
    if version != REVIEW_ARTIFACT_VERSION:
        raise ReviewArtifactError(
            f"{FINAL_REVIEW_ARTIFACT_FILENAME} 'version' must be "
            f"{REVIEW_ARTIFACT_VERSION}; got {version!r}"
        )
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
        finding = _validate_finding(raw_finding, index=index)
        if finding["id"] in seen_ids:
            raise ReviewArtifactError(
                f"finding id {finding['id']!r} is duplicated"
            )
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

    ``require_artifact=True`` (generated v2 review sessions) makes a missing
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
                    "generated v2 review sessions"
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
            "error": (
                f"could not read {FINAL_REVIEW_ARTIFACT_FILENAME}: {error}"
            ),
            "review_output_path": str(review_path),
            "review_artifact_path": str(artifact_path),
        }

    try:
        artifact = parse_review_artifact(artifact_text)
        ensure_review_consistency(payload, artifact)
    except ReviewConsistencyError as error:
        return {
            "ok": False,
            "reason": REVIEW_REASON_CONTRADICTORY_ARTIFACT,
            "error": str(error),
            "review_output_path": str(review_path),
            "review_artifact_path": str(artifact_path),
        }
    except ReviewArtifactError as error:
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
    snapshot: plans.PlanSnapshot, review_resume: dict[str, Any]
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


def review_verdict_payload(
    plan_dir: str | Path, state_dir: str | Path | None = None
) -> dict[str, Any]:
    """Build the authoritative ``asl review-verdict`` payload.

    Merges :func:`load_review_verdict` (the shared verdict, carrying full
    untruncated ``findings`` when the structured artifact is present) with the
    read-only continuation projection under ``review_resume``, then attaches the
    full untruncated ``summary`` (with ``summary_truncated``/
    ``summary_total_chars``) and exact ``next_steps`` remediation commands. The
    verdict's own ``ok``/``reason`` surface at the top level so a malformed or
    missing review reports a stable ``reason`` from the shared vocabulary.

    When the governing generated review session is version
    :data:`GENERATED_REVIEW_SESSION_VERSION` or newer, a missing
    ``final-review.json`` is enforced as ``missing-review-artifact`` — matching
    runner and continuation enforcement — while legacy v1 Markdown-only reviews
    remain accepted.

    Local imports keep ``review`` free of an import cycle: ``review_continuation``
    imports this module, and ``state`` reaches its continuation evaluator lazily.
    """
    from . import events, responses, review_continuation, state

    snapshot = plans.inspect_plan(plan_dir)
    plan_state = state.plan_state_dir(snapshot.plan_dir, state_dir)
    event_path = plan_state / events.EVENTS_FILE_NAME
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, event_path
    ).to_payload()
    review_session_version = _relevant_review_session_version(
        snapshot, review_resume
    )
    verdict = load_review_verdict(
        snapshot.plan_dir,
        require_artifact=(
            review_session_version is not None
            and review_session_version >= GENERATED_REVIEW_SESSION_VERSION
        ),
    )
    summary, summary_total = _review_verdict_summary(verdict)
    guidance = review_continuation.review_resume_guidance(
        review_resume, review_harness_active=False
    )
    next_steps = _review_verdict_next_steps(
        verdict, plan_dir=str(snapshot.plan_dir), guidance=guidance
    )

    payload = responses.success_payload(REVIEW_VERDICT_COMMAND)
    # Merge the verdict; its ``ok``/``reason``/``error`` surface at the top
    # level, and ``findings`` (when present) is the full untruncated list.
    payload.update(verdict)
    payload["command"] = REVIEW_VERDICT_COMMAND
    payload["plan_dir"] = str(snapshot.plan_dir)
    payload["state_dir"] = str(plan_state)
    payload["events_path"] = str(event_path)
    payload["review_resume"] = review_resume
    payload["summary"] = summary
    payload["summary_truncated"] = False
    payload["summary_total_chars"] = summary_total
    payload["findings"] = verdict.get("findings")
    payload["next_steps"] = next_steps
    return payload


def review_verdict_from_args(args: argparse.Namespace) -> int:
    """Run the ``review-verdict`` subcommand from CLI arguments.

    Returns ``0`` for a valid all-clear or findings verdict and ``1`` for a
    malformed plan or any stable non-ok verdict (missing, malformed, or
    contradictory review), so shell automation can gate on the exit status.
    """
    from . import responses

    plan_dir_arg = args.plan_dir
    want_json = bool(args.json)
    try:
        payload = review_verdict_payload(plan_dir_arg, args.state_dir)
    except plans.PlanError as error:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    REVIEW_VERDICT_COMMAND,
                    reason="malformed-plan",
                    error=str(error),
                    paused=False,
                    plan_dir=str(plan_dir_arg),
                )
            )
        else:
            print(
                f"agent-session-loop review-verdict: {error}",
                file=sys.stderr,
            )
        return 1

    if want_json:
        responses.print_json(payload)
    else:
        _print_text_review_verdict(payload)
    # A stable missing, malformed, or contradictory verdict is a non-ok
    # payload; exit nonzero so shell automation can trust the status. Valid
    # all-clear and findings verdicts stay ``ok`` and exit zero.
    return 0 if payload.get("ok") else 1


def _print_text_review_verdict(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    if payload.get("ok"):
        print(f"Verdict: {payload.get('status_line')}")
        print(f"Outcome: {payload.get('outcome')}")
        print(f"Total findings: {payload.get('total_findings')}")
    else:
        print(f"Verdict: not ok ({payload.get('reason')})")
        error = payload.get("error")
        if error:
            print(f"Error: {error}")
    if payload.get("summary_total_chars"):
        print(f"Summary ({payload['summary_total_chars']} chars):")
        print(payload["summary"])
    next_steps = payload.get("next_steps") or []
    if next_steps:
        print("Next steps:")
        for step in next_steps:
            print(f"  - {step}")


# Cross-cycle finding identity. Rereview cycles need to recognize a finding
# that survived its repair session even when the regenerated review shifts
# line numbers or rewords nothing else, so the fingerprint hashes only the
# normalized severity, title, evidence paths, and stable symbols — never ids
# (regenerated per review) or line numbers (drift between reviews).
FINDING_FINGERPRINT_PREFIX = "finding"

# ``path/to/file.py:12`` / ``file.py:12-34`` / ``file.py:12:8`` trailing
# location suffixes, and prose ``line 12`` / ``lines 12-34`` references.
_EVIDENCE_LINE_SUFFIX_RE = re.compile(r"(:[0-9]+(?:[:-][0-9]+)*)+$")
_TITLE_LINE_REF_RE = re.compile(
    r"(?::[0-9]+(?:[:-][0-9]+)*)|(?:\blines?\s+[0-9]+(?:\s*[-–]\s*[0-9]+)?\b)",
    re.IGNORECASE,
)


def _normalized_title(title: str) -> str:
    stripped = _TITLE_LINE_REF_RE.sub(" ", title)
    return " ".join(stripped.split()).lower()


def _normalized_evidence_path(path: str) -> str:
    return _EVIDENCE_LINE_SUFFIX_RE.sub("", path.strip())


def finding_fingerprint(finding: dict[str, Any]) -> str:
    """Return the stable ``finding:<16hex>`` fingerprint for a finding.

    Hashes the normalized severity, title (line references removed,
    whitespace collapsed, lowercased), sorted unique evidence paths (trailing
    ``:line`` suffixes removed), and sorted unique evidence symbols. Two
    findings that differ only in id, explanation, repair grouping, or
    line-number drift therefore share one fingerprint.
    """
    severity = str(finding.get("severity", "")).strip().lower()
    title = _normalized_title(str(finding.get("title", "")))
    paths: set[str] = set()
    symbols: set[str] = set()
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        for entry in evidence:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if isinstance(path, str) and path.strip():
                paths.add(_normalized_evidence_path(path))
            symbol = entry.get("symbol")
            if isinstance(symbol, str) and symbol.strip():
                symbols.add(symbol.strip())
    identity = json.dumps(
        {
            "severity": severity,
            "title": title,
            "evidence_paths": sorted(paths),
            "symbols": sorted(symbols),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{FINDING_FINGERPRINT_PREFIX}:{digest}"


def finding_fingerprints(
    findings: Any,
) -> tuple[str, ...] | None:
    """Fingerprint a structured findings list, or ``None`` when absent.

    Returns one fingerprint per finding in order (duplicates preserved) so
    callers can align fingerprints with findings; ``None`` when ``findings``
    is not a list (v1 Markdown-only verdicts carry no structured findings).
    """
    if not isinstance(findings, list):
        return None
    return tuple(
        finding_fingerprint(finding)
        for finding in findings
        if isinstance(finding, dict)
    )
