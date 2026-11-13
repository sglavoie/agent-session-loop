"""Retained evidence for validator batches that preparation rejected.

A rejected batch never reaches the cache or the aggregate, so without this
record the only trace of what a validator said is one stderr line. The file is
diagnostic evidence only: nothing reads it back as validation, it is outside
every plan content and readiness hash, and each validation wave starts by
clearing the previous wave's record.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from . import durability
from .plans_errors import PlanError
from .plans_files import canonicalize_path

PREPARE_VALIDATION_REJECTED_FILENAME = "prepare-validation-rejected.json"
PREPARE_VALIDATION_REJECTED_VERSION = 1


class ValidatorOutputRejected(PlanError):
    """A validator answer that was rejected, carrying the answer itself."""

    def __init__(self, message: str, *, output: object) -> None:
        super().__init__(message)
        self.output = output


def prepare_validation_rejected_path(plan_dir: str | Path) -> Path:
    return canonicalize_path(plan_dir) / PREPARE_VALIDATION_REJECTED_FILENAME


def clear_rejections(plan_dir: str | Path) -> None:
    """Remove the previous wave's record; a missing file is already clear."""
    try:
        prepare_validation_rejected_path(plan_dir).unlink(missing_ok=True)
    except OSError as error:
        raise PlanError(f"cannot remove rejected validator output: {error}") from error


def record_rejection(
    plan_dir: str | Path,
    *,
    batch: int,
    session_ids: tuple[str, ...],
    error: Exception,
    output: object,
) -> Path:
    """Append one rejected batch to this wave's record and return its path.

    ``output`` is the decoded payload when the answer was JSON, otherwise the
    raw final message text.
    """
    path = prepare_validation_rejected_path(plan_dir)
    by_batch: dict[int, object] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        existing = None
    if (
        isinstance(existing, dict)
        and existing.get("version") == PREPARE_VALIDATION_REJECTED_VERSION
        and isinstance(existing.get("rejections"), list)
    ):
        for entry in existing["rejections"]:
            if isinstance(entry, dict) and isinstance(entry.get("batch"), int):
                by_batch[entry["batch"]] = entry
    by_batch[batch] = {
        "batch": batch,
        "session_ids": list(session_ids),
        "error": str(error),
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(
            timespec="seconds"
        ),
        "output": output,
    }
    rejections = [by_batch[key] for key in sorted(by_batch)]
    document = {
        "version": PREPARE_VALIDATION_REJECTED_VERSION,
        "rejections": rejections,
    }
    try:
        durability.replace_text(
            path, json.dumps(document, indent=2, sort_keys=True, default=str) + "\n"
        )
    except OSError as error_:
        raise PlanError(f"cannot write {path}: {error_}") from error_
    return path
