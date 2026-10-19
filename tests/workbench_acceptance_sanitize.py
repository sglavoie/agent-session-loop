"""Focused portion of tests/workbench_acceptance.py."""

from .workbench_acceptance_contract import *

# Sanitization
#
# The allowlist is applied before anything is interpreted, because a reader
# that validated the fields it knows and ignored the rest would publish
# whatever was put beside them.


def _fold(where: str, key: str) -> str:
    return key if not where else f"{where}.{key}"


def check_sanitized(document: Mapping[str, Any], where: str = "") -> None:
    """Refuse anything a sanitized report has no reason to carry.

    Structural rather than textual: strings are bounded and may hold no path
    separator, no ``@`` and no newline, numbers must be finite and not
    negative, and nothing may nest deeper or wider than the schema needs.
    """
    for key, value in document.items():
        _check_sanitized_value(_fold(where, str(key)), key, value)


def _check_sanitized_value(where: str, key: Any, value: Any) -> None:
    if not isinstance(key, str) or not key or len(key) > MAX_STRING_LENGTH:
        raise EvidenceError(f"{where}: unusable key")
    if isinstance(value, bool):
        return
    if isinstance(value, str):
        _check_sanitized_string(where, value)
        return
    if isinstance(value, int | float):
        if not math.isfinite(value) or value < 0:
            raise EvidenceError(f"{where}: {value!r} is not a usable number")
        return
    if isinstance(value, dict):
        check_sanitized(value, where)
        return
    if isinstance(value, list):
        if len(value) > MAX_SEQUENCE_LENGTH:
            raise EvidenceError(f"{where}: more than {MAX_SEQUENCE_LENGTH} entries")
        for index, item in enumerate(value):
            _check_sanitized_value(f"{where}[{index}]", f"{index}", item)
        return
    raise EvidenceError(f"{where}: {type(value).__name__} is not allowed in a report")


def _check_sanitized_string(where: str, value: str) -> None:
    if len(value) > MAX_STRING_LENGTH:
        raise EvidenceError(f"{where}: longer than {MAX_STRING_LENGTH} characters")
    for forbidden in ("/", "\\", "@", "\n", "\r", "\t"):
        if forbidden in value:
            raise EvidenceError(
                f"{where}: a sanitized report carries no {forbidden!r}; "
                "paths, identities and pasted output do not belong in it"
            )
    if not value.isprintable():
        raise EvidenceError(f"{where}: unprintable characters")


def require_keys(
    document: Mapping[str, Any], expected: Sequence[str], where: str
) -> None:
    """Exactly these keys: a missing one is a gap and an extra one is a leak."""
    present = set(document)
    wanted = set(expected)
    missing = sorted(wanted - present)
    if missing:
        raise EvidenceError(f"{where}: missing {', '.join(missing)}")
    extra = sorted(present - wanted)
    if extra:
        raise EvidenceError(f"{where}: unrecognized {', '.join(extra)}")


# ---------------------------------------------------------------------------
# Typed readers, each refusing rather than coercing


def _string(document: Mapping[str, Any], key: str, where: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{_fold(where, key)}: a non-empty string is required")
    return value


def _matching(
    document: Mapping[str, Any], key: str, pattern: re.Pattern[str], where: str
) -> str:
    value = _string(document, key, where)
    if not pattern.fullmatch(value):
        raise EvidenceError(
            f"{_fold(where, key)}: {value!r} is not in the expected form"
        )
    return value


def _timestamp(document: Mapping[str, Any], key: str, where: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvidenceError(f"{_fold(where, key)}: a unix timestamp is required")
    if not math.isfinite(value) or value <= 0:
        raise EvidenceError(f"{_fold(where, key)}: {value!r} is not a unix timestamp")
    return float(value)


def _mapping(document: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise EvidenceError(f"{_fold(where, key)}: an object is required")
    return value


__all__ = ["argparse", "json", "math", "os", "re", "subprocess", "sys", "tempfile", "time", "unittest", "Mapping", "Sequence", "Path", "Any", "lifecycle", "REPO_ROOT", "SCHEMA_VERSION", "STAGE_STAGING", "STAGE_PRODUCTION", "STAGE_RETIREMENT", "STAGES", "EVIDENCE_RELATIVE_PATH", "ENV_EVIDENCE_DIR", "ENV_RELEASE_REVISION", "ENV_PI_CHECKOUT", "PI_REPOSITORY_RELATIVE_PATH", "MAX_DOCUMENT_BYTES", "MAX_STRING_LENGTH", "MAX_SEQUENCE_LENGTH", "MAX_CLOCK_SKEW_SECONDS", "SOAK_MINIMUM_SECONDS", "SOAK_PHASES", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "FORBIDDEN_HOST_MARKERS", "HOST_ROLES", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "PROVENANCE_ACTUAL_HOST", "STAGE_CHECKS", "PHONE_CHECKS", "STAGE_EXTRA_KEY", "EvidenceError", "locate_pi_repository", "evidence_directory", "evidence_path", "read_document", "_fold", "check_sanitized", "_check_sanitized_value", "_check_sanitized_string", "require_keys", "_string", "_matching", "_timestamp", "_mapping"]  # fmt: skip
