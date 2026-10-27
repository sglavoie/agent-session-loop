"""Tolerant coercion of hand-edited Workbench registry values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from asl_workbench.archive import MAX_RETENTION_DAYS

from . import (
    harnesses,
    plans_files,
)
from .workbench_registry_models import (
    COMMIT_MODES,
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    REGISTRY_SCHEMA_VERSION,
    CommitMode,
    LaunchProfile,
    RegistryPin,
)


def _canonical_plan_dir(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return str(plans_files.canonicalize_path(text))
    except OSError, ValueError:
        return None


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_max_review_fix_cycles(
    value: Any, *, warnings: list[str], context: str
) -> int | None:
    """Return a persisted review-fix cap only when it is non-negative."""
    cap = _coerce_optional_int(value)
    if cap is not None and cap < 0:
        warnings.append(f"{context}: ignoring negative max_review_fix_cycles {cap!r}")
        return None
    return cap


def _coerce_harness(value: Any, *, warnings: list[str], context: str) -> str | None:
    text = _coerce_optional_str(value)
    if text is None:
        return None
    if text not in harnesses.HARNESS_NAMES:
        warnings.append(f"{context}: ignoring unsupported harness {text!r}")
        return None
    return text


def _coerce_commit_mode(
    value: Any, *, warnings: list[str], context: str
) -> CommitMode | None:
    text = _coerce_optional_str(value)
    if text is None:
        return None
    if text not in COMMIT_MODES:
        warnings.append(f"{context}: ignoring unsupported commit_mode {text!r}")
        return None
    return text  # type: ignore[return-value]


def _coerce_profile(
    raw: Any,
    *,
    schema_version: int,
    warnings: list[str],
    context: str,
) -> LaunchProfile:
    if raw is None:
        return LaunchProfile()
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: profile must be an object; ignoring")
        return LaunchProfile()
    if "model" in raw and "harness_model" not in raw:
        warnings.append(
            f"{context}: ignoring legacy profile.model from registry schema "
            f"v{schema_version}; "
            "use harness_model and review_harness_model"
        )
    return LaunchProfile(
        cwd=_coerce_optional_str(raw.get("cwd")),
        harness=_coerce_harness(raw.get("harness"), warnings=warnings, context=context),
        harness_model=_coerce_optional_str(raw.get("harness_model")),
        effort=_coerce_optional_str(raw.get("effort")),
        timeout=_coerce_optional_int(raw.get("timeout")),
        commit_mode=_coerce_commit_mode(
            raw.get("commit_mode"), warnings=warnings, context=context
        ),
        review_harness=_coerce_harness(
            raw.get("review_harness"), warnings=warnings, context=context
        ),
        review_harness_model=_coerce_optional_str(raw.get("review_harness_model")),
        max_review_fix_cycles=_coerce_max_review_fix_cycles(
            raw.get("max_review_fix_cycles"), warnings=warnings, context=context
        ),
        unsafe_bypass=_coerce_optional_bool(raw.get("unsafe_bypass")),
        allow_not_loop_ready=_coerce_optional_bool(raw.get("allow_not_loop_ready")),
        note=_coerce_optional_str(raw.get("note")),
    )


def _coerce_pin(
    raw: Any, *, schema_version: int, warnings: list[str], index: int
) -> RegistryPin | None:
    context = f"pin[{index}]"
    if not isinstance(raw, Mapping):
        warnings.append(f"{context}: entry must be an object; skipping")
        return None
    plan_dir = _canonical_plan_dir(raw.get("plan_dir"))
    if plan_dir is None:
        warnings.append(f"{context}: missing or invalid plan_dir; skipping")
        return None
    label = _coerce_optional_str(raw.get("label"))
    profile = _coerce_profile(
        raw.get("profile"),
        schema_version=schema_version,
        warnings=warnings,
        context=context,
    )
    return RegistryPin(plan_dir=plan_dir, label=label, profile=profile)


def _coerce_plan_roots(raw: Any, *, warnings: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        warnings.append("plan_roots: must be a list; ignoring")
        return ()
    roots: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        canonical = _canonical_plan_dir(entry)
        if canonical is None:
            warnings.append(f"plan_roots[{index}]: invalid path {entry!r}; skipping")
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        roots.append(canonical)
    return tuple(roots)


def _coerce_schema_version(raw: Any, *, warnings: list[str]) -> int:
    if raw is None:
        warnings.append("schema_version: missing; assuming current version")
        return REGISTRY_SCHEMA_VERSION
    if isinstance(raw, bool) or not isinstance(raw, int):
        warnings.append(
            f"schema_version: expected integer, got {type(raw).__name__}; "
            "assuming current version"
        )
        return REGISTRY_SCHEMA_VERSION
    if raw < 1:
        warnings.append(
            f"schema_version: invalid value {raw!r}; assuming current version"
        )
        return REGISTRY_SCHEMA_VERSION
    if raw > REGISTRY_SCHEMA_VERSION:
        warnings.append(
            f"schema_version {raw} is newer than supported "
            f"v{REGISTRY_SCHEMA_VERSION}; reading best-effort"
        )
    return raw


def _coerce_retention_days(raw: Any, *, warnings: list[str]) -> int:
    if raw is None:
        # Old schema-v1 registries predate archive support; default silently.
        return DEFAULT_ARCHIVE_RETENTION_DAYS
    if isinstance(raw, bool) or not isinstance(raw, int):
        warnings.append(
            "archive_retention_days: expected positive integer, got "
            f"{type(raw).__name__}; using default "
            f"{DEFAULT_ARCHIVE_RETENTION_DAYS}"
        )
        return DEFAULT_ARCHIVE_RETENTION_DAYS
    if raw < 1 or raw > MAX_RETENTION_DAYS:
        warnings.append(
            f"archive_retention_days: invalid value {raw!r}; using default "
            f"{DEFAULT_ARCHIVE_RETENTION_DAYS}"
        )
        return DEFAULT_ARCHIVE_RETENTION_DAYS
    return raw
