"""Versioned per-session validation cache (``prepare-validation-cache.json``)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import durability, plans
from .plans import PlanError, canonicalize_path, parse_session_id
from .prepare_models import (
    LEGACY_PREPARE_VALIDATION_VERSION,
    PREPARE_VALIDATION_CACHE_FILENAME,
    PREPARE_VALIDATION_CACHE_VERSION,
    PREPARE_VALIDATION_VERSION,
    PrepareValidationCache,
    PrepareValidationCacheEntry,
)
from .prepare_parsing import (
    _fail,
    _require_object,
    _require_text,
    parse_validator_output,
)


def prepare_validation_cache_path(plan_dir: str | Path) -> Path:
    """Return the per-session validator-result cache path for ``plan_dir``."""
    return canonicalize_path(plan_dir) / PREPARE_VALIDATION_CACHE_FILENAME


def _canonical_json_digest(payload: object) -> str:
    """Return a stable SHA-256 digest for a JSON-compatible payload."""
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _parse_prepare_validation_cache(payload: object) -> PrepareValidationCache:
    if not isinstance(payload, dict):
        raise _fail("prepare validation cache must be a JSON object")
    recorded_version = payload.get("version")
    if (
        not isinstance(recorded_version, bool)
        and isinstance(recorded_version, int)
        and recorded_version != PREPARE_VALIDATION_CACHE_VERSION
    ):
        # A cache has no authority of its own. An older/newer schema is a
        # deliberate cache miss even when its payload shape also changed.
        return PrepareValidationCache()
    allowed = {"version", "entries"}
    data = _require_object(
        payload,
        label="prepare validation cache",
        allowed_keys=tuple(sorted(allowed)),
    )
    unknown = sorted(set(data) - allowed)
    missing = sorted(allowed - set(data))
    if unknown or missing:
        problems: list[str] = []
        if unknown:
            problems.append("unknown keys " + ", ".join(repr(key) for key in unknown))
        if missing:
            problems.append("missing keys " + ", ".join(repr(key) for key in missing))
        raise _fail("prepare validation cache has " + "; ".join(problems))
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _fail("prepare validation cache 'version' must be an integer")
    if version != PREPARE_VALIDATION_CACHE_VERSION:
        raise _fail(
            "prepare validation cache 'version' must be "
            f"{PREPARE_VALIDATION_CACHE_VERSION}"
        )

    raw_entries = data["entries"]
    if not isinstance(raw_entries, list):
        raise _fail("prepare validation cache 'entries' must be a list")
    entries: list[PrepareValidationCacheEntry] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(raw_entries, start=1):
        entry_allowed = {"session_id", "key", "identity", "validation"}
        entry = _require_object(
            raw_entry,
            label=f"prepare validation cache entry #{index}",
            allowed_keys=tuple(sorted(entry_allowed)),
        )
        entry_unknown = sorted(set(entry) - entry_allowed)
        entry_missing = sorted(entry_allowed - set(entry))
        if entry_unknown or entry_missing:
            problems = []
            if entry_unknown:
                problems.append(
                    "unknown keys " + ", ".join(repr(key) for key in entry_unknown)
                )
            if entry_missing:
                problems.append(
                    "missing keys " + ", ".join(repr(key) for key in entry_missing)
                )
            raise _fail(
                f"prepare validation cache entry #{index} has " + "; ".join(problems)
            )
        session_id = parse_session_id(
            _require_text(entry["session_id"], label=f"cache entry #{index} session_id")
        )
        if session_id.value in seen:
            raise _fail(
                f"duplicate prepare validation cache session {session_id.value!r}"
            )
        seen.add(session_id.value)
        key = _require_text(entry["key"], label=f"cache entry #{index} key")
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise _fail(f"cache entry #{index} key must be a lowercase SHA-256 digest")
        identity = entry["identity"]
        if not isinstance(identity, dict):
            raise _fail(f"cache entry #{index} identity must be a JSON object")
        if _canonical_json_digest(identity) != key:
            raise _fail(f"cache entry #{index} key does not match its identity")
        validation_payload = entry["validation"]
        validation_version = (
            validation_payload.get("version")
            if isinstance(validation_payload, dict)
            else None
        )
        if (
            not isinstance(validation_version, bool)
            and isinstance(validation_version, int)
            and validation_version
            not in (
                LEGACY_PREPARE_VALIDATION_VERSION,
                PREPARE_VALIDATION_VERSION,
            )
        ):
            # The cache record can outlive a validator-output schema upgrade.
            # It has no authority, so refresh rather than asking the current
            # strict parser to understand an obsolete result format.
            return PrepareValidationCache()
        validation = parse_validator_output(validation_payload)
        if len(validation.sessions) != 1:
            raise _fail(
                f"cache entry #{index} validation must contain exactly one session"
            )
        if validation.sessions[0].session_id != session_id:
            raise _fail(
                f"cache entry #{index} session_id does not match validation output"
            )
        entries.append(
            PrepareValidationCacheEntry(
                session_id=session_id,
                key=key,
                identity=identity,
                validation=validation,
            )
        )
    return PrepareValidationCache(entries=tuple(entries), version=version)


def load_prepare_validation_cache(
    plan_dir: str | Path,
) -> PrepareValidationCache:
    """Load reusable per-session validator output, or an empty cache.

    Unlike ``prepare-validation.json``, this artifact is strictly an
    optimization. A cache schema version mismatch intentionally becomes an
    empty cache so an upgraded preparer refreshes every result; an obsolete
    validator-output schema does the same. Malformed data in the current
    schema remains a fail-closed error.
    """
    plans.recover_pending_plan_transaction(plan_dir)
    path = prepare_validation_cache_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PrepareValidationCache()
    except OSError as error:
        raise PlanError(f"cannot read {path}: {error}") from error
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(
            f"{path} is not valid JSON: {error.msg} at line {error.lineno}"
        ) from error
    return _parse_prepare_validation_cache(payload)


def serialize_prepare_validation_cache(cache: PrepareValidationCache) -> str:
    """Serialize the cache using one stable, inspectable representation."""
    return json.dumps(cache.to_dict(), indent=2, sort_keys=True) + "\n"


def save_prepare_validation_cache(
    plan_dir: str | Path, cache: PrepareValidationCache
) -> Path:
    """Atomically persist a cache update without coupling it to the aggregate."""
    path = prepare_validation_cache_path(plan_dir)
    text = serialize_prepare_validation_cache(cache)
    try:
        durability.replace_text(path, text)
    except OSError as error:
        raise PlanError(f"cannot write {path}: {error}") from error
    return path
