# ruff: noqa: F401, I001
"""Compatibility facade for the review implementation."""

from __future__ import annotations

from .review_part_1 import (
    FINAL_REVIEW_FILENAME,
    FINAL_REVIEW_ARTIFACT_FILENAME,
    REVIEW_STATUS_ALL_CLEAR,
    _REVIEW_STATUS_FINDINGS_RE,
    REVIEW_SESSION_MARKER,
    REVIEW_SESSION_VERSION_MARKER_TEMPLATE,
    _REVIEW_SESSION_VERSION_MARKER_RE,
    GENERATED_REVIEW_SESSION_VERSION,
    REPOSITORY_AWARE_GENERATED_REVIEW_SESSION_VERSION,
    STRUCTURED_REVIEW_MIN_VERSION,
    REVIEW_ARTIFACT_VERSION,
    REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
    SUPPORTED_REVIEW_ARTIFACT_VERSIONS,
    REVIEW_OUTCOME_ALL_CLEAR,
    REVIEW_OUTCOME_FINDINGS,
    REVIEW_OUTCOMES,
    REVIEW_SEVERITIES,
    FINDING_REQUIRED_KEYS,
    _EVIDENCE_ALLOWED_KEYS,
    _ARTIFACT_ALLOWED_KEYS,
    REVIEW_REASON_MALFORMED,
    REVIEW_REASON_MISSING,
    REVIEW_REASON_MISSING_ARTIFACT,
    REVIEW_REASON_MALFORMED_ARTIFACT,
    REVIEW_REASON_CONTRADICTORY_ARTIFACT,
    _CONTRACT_CHECK_MESSAGE_PREFIX_RE,
    _CHECK_VALIDATION_SESSION_ID,
    ReviewArtifactError,
    ReviewConsistencyError,
    final_review_output_path,
    final_review_artifact_path,
    final_review_output_sha256,
    final_review_artifact_sha256,
    review_session_version_marker,
    contains_review_session_marker,
    review_session_marker_version,
    _verdict_error,
    parse_review_verdict,
    _require_finding_str,
    _validate_repository_alias,
    _validate_evidence_for_registry,
    _validate_string_list,
    _validate_verification_checks,
)
from .review_part_2 import (
    _validate_finding,
    parse_review_artifact,
    ensure_review_consistency,
    load_review_verdict,
    REVIEW_VERDICT_COMMAND,
    _review_verdict_summary,
    _review_verdict_next_steps,
    _relevant_review_session_version,
)
from .review_part_3 import (
    review_verdict_payload,
    review_verdict_from_args,
    _print_text_review_verdict,
    FINDING_FINGERPRINT_PREFIX,
    FINDING_REPEAT_KEY_PREFIX,
    _EVIDENCE_LINE_SUFFIX_RE,
    _TITLE_LINE_REF_RE,
    _normalized_title,
    _normalized_evidence_path,
    finding_fingerprint,
    finding_fingerprints,
    finding_repeat_key,
    finding_repeat_keys,
)

from . import review_shared as _shared
from . import review_part_1 as _review_part_1
from . import review_part_2 as _review_part_2
from . import review_part_3 as _review_part_3

_PARTS = (
    _review_part_1,
    _review_part_2,
    _review_part_3,
)
for _part in _PARTS:
    globals().update(
        {
            name: value
            for name, value in vars(_part).items()
            if not name.startswith("__")
        }
    )
for _part in _PARTS:
    _part.__dict__.update(
        {name: value for name, value in globals().items() if not name.startswith("__")}
    )
del _part
