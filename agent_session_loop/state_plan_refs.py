"""Plan-reference resolution and run discovery under the state root."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import events, plans
from .state import (
    LOCK_FILE_NAME,
    RESULT_FILE_NAME,
    RUNS_DIR_NAME,
    TURN_DIR_PREFIX,
    AmbiguousPlanError,
    StateError,
    _nonempty_str,
    plan_state_dir,
    state_root,
)


def _is_bare_label(raw_value: str) -> bool:
    """Return True when ``raw_value`` is a bare basename with no path parts.

    Only bare labels trigger state lookup. Anything containing a directory
    component (``foo/bar``, ``./foo``, ``/abs``) or the ``.``/``..`` relative
    references retains the current path-only behavior.
    """
    if raw_value in ("", ".", ".."):
        return False
    head, tail = os.path.split(raw_value)
    if head:
        return False
    return tail == raw_value


def _read_json_object_safe(path: Path) -> dict[str, Any] | None:
    """Best-effort read of a JSON object; None on any failure or non-object.

    Tolerates malformed, unreadable, or concurrently removed files so one bad
    metadata record cannot block discovery of other candidates.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError, ValueError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _recorded_content_hash(state_obj: dict[str, Any]) -> str | None:
    """Return the plan content hash a stored state should match against.

    Prefers ``final_snapshot.content_sha256``: a successful run mutates session
    handoffs, so the on-disk plan content matches the snapshot taken when the
    run finished, not the one taken at its start. Falls back to
    ``initial_snapshot.content_sha256`` only for legacy state written before a
    final snapshot was persisted. Returns ``None`` when neither is present.
    """
    for section_key in ("final_snapshot", "initial_snapshot"):
        section = state_obj.get(section_key)
        if isinstance(section, dict):
            recorded = _nonempty_str(section.get("content_sha256"))
            if recorded is not None:
                return recorded
    return None


def _plan_dir_from_result(payload: dict[str, Any]) -> str | None:
    """Recover a plan directory from a turn result, ``after`` before ``before``."""
    for section_key in ("after", "before"):
        section = payload.get(section_key)
        if isinstance(section, dict):
            value = _nonempty_str(section.get("plan_dir"))
            if value:
                return value
    return None


def _iter_result_paths(runs_dir: Path) -> Iterator[Path]:
    """Yield ``runs/*/turn-*/result.json`` files under one state directory."""
    try:
        if not runs_dir.is_dir():
            return
        run_dirs = sorted(runs_dir.iterdir())
    except OSError:
        return
    for run_dir in run_dirs:
        try:
            if not run_dir.is_dir():
                continue
            turn_dirs = sorted(run_dir.iterdir())
        except OSError:
            continue
        for turn_dir in turn_dirs:
            try:
                if not turn_dir.name.startswith(TURN_DIR_PREFIX):
                    continue
                if not turn_dir.is_dir():
                    continue
                result_path = turn_dir / RESULT_FILE_NAME
                if result_path.is_file():
                    yield result_path
            except OSError:
                continue


def _collect_plan_dir_strings(state_dir: Path) -> list[str]:
    """Gather raw ``plan_dir`` strings from one per-plan state directory.

    Sources, in order: ``state.json``, ``lock.json``, then every
    ``runs/*/turn-*/result.json`` recovering ``after.plan_dir`` before
    ``before.plan_dir``. Missing or malformed records are silently skipped.
    """
    results: list[str] = []
    state_obj = _read_json_object_safe(state_dir / events.STATE_FILE_NAME)
    if state_obj is not None:
        value = _nonempty_str(state_obj.get("plan_dir"))
        if value:
            results.append(value)
    lock_obj = _read_json_object_safe(state_dir / LOCK_FILE_NAME)
    if lock_obj is not None:
        value = _nonempty_str(lock_obj.get("plan_dir"))
        if value:
            results.append(value)
    for result_path in _iter_result_paths(state_dir / RUNS_DIR_NAME):
        result_obj = _read_json_object_safe(result_path)
        if result_obj is None:
            continue
        value = _plan_dir_from_result(result_obj)
        if value:
            results.append(value)
    return results


def iter_plan_state_dirs(state_root: Path) -> Iterator[Path]:
    """Yield per-plan state directories under ``state_root``.

    A missing root scans as an empty corpus. A root that exists but is not a
    directory, or that cannot be enumerated, raises :class:`StateError`.
    """
    if not state_root.exists():
        return
    if not state_root.is_dir():
        raise StateError(f"state root is not a directory: {state_root}")
    try:
        children = sorted(state_root.iterdir())
    except OSError as error:
        raise StateError(f"could not scan state root {state_root}: {error}") from error
    for child in children:
        try:
            if child.is_dir():
                yield child
        except OSError:
            continue


def resolve_plan_reference(raw_value: str, state_root: Path) -> Path:
    """Resolve a plan argument to a plan directory, honoring direct paths.

    Existing directories always win and are returned canonicalized without any
    state lookup. Path-like nonexistent arguments (those with a directory
    component) also bypass lookup and return the canonicalized argument. Only a
    bare, nonexistent label scans the state corpus under ``state_root``,
    matching persisted ``plan_dir`` basenames exactly and case-sensitively
    against existing plan directories.

    Returns the sole matching plan directory, or the canonicalized argument
    when nothing matches. Raises :class:`AmbiguousPlanError` when more than one
    existing plan directory shares the requested basename. Discovery is
    read-only and independent of process liveness.
    """
    original = plans.canonicalize_path(raw_value)
    if original.is_dir():
        return original
    if not _is_bare_label(raw_value):
        return original

    matches: dict[str, Path] = {}
    for state_dir in iter_plan_state_dirs(state_root):
        for raw_plan_dir in _collect_plan_dir_strings(state_dir):
            try:
                candidate = plans.canonicalize_path(raw_plan_dir)
            except OSError, ValueError:
                continue
            key = str(candidate)
            if key in matches:
                continue
            if candidate.name != raw_value:
                continue
            try:
                is_existing_dir = candidate.is_dir()
            except OSError:
                continue
            if is_existing_dir:
                matches[key] = candidate

    if not matches:
        return original
    if len(matches) == 1:
        return next(iter(matches.values()))
    raise AmbiguousPlanError(raw_value, list(matches.values()), state_root)


@dataclass(frozen=True)
class RunLocation:
    """Where one run's artifacts live, and which plan produced them."""

    run_id: str
    state_dir: Path
    plan_dir: Path | None

    @property
    def plan_id(self) -> str | None:
        """The compact plan label, or ``None`` when no plan dir was recorded."""
        return None if self.plan_dir is None else self.plan_dir.name


def _recorded_plan_dir(state_dir: Path) -> Path | None:
    for raw_plan_dir in _collect_plan_dir_strings(state_dir):
        try:
            return plans.canonicalize_path(raw_plan_dir)
        except OSError, ValueError:
            continue
    return None


def find_run_locations(
    run_id: str,
    *,
    state_dir: str | Path | None = None,
    plan_dir: str | Path | None = None,
) -> list[RunLocation]:
    """Return every plan state directory that holds a run directory for ``run_id``.

    Run IDs are minted per plan, so the corpus is the only thing that can say
    which plan a bare run ID belongs to. Passing ``plan_dir`` restricts the scan
    to that one plan, which is how an ID that several plans happen to share is
    disambiguated. Discovery is read-only, and a run ID that could name anything
    other than a single directory entry matches nothing rather than escaping the
    state root.
    """
    if not run_id or run_id in {".", ".."} or "/" in run_id or os.sep in run_id:
        return []
    root = state_root(state_dir)
    if plan_dir is not None:
        candidates = [plan_state_dir(plan_dir, state_dir)]
    else:
        candidates = list(iter_plan_state_dirs(root))
    located: list[RunLocation] = []
    for candidate in candidates:
        try:
            if not (candidate / RUNS_DIR_NAME / run_id).is_dir():
                continue
        except OSError:
            continue
        located.append(
            RunLocation(
                run_id=run_id,
                state_dir=candidate,
                plan_dir=(
                    plans.canonicalize_path(plan_dir)
                    if plan_dir is not None
                    else _recorded_plan_dir(candidate)
                ),
            )
        )
    return located
