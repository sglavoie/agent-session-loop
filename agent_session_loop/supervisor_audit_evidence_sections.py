"""Plan, registry, scope, repository and version evidence sections."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    __version__,
    assurance_models_vocab,
    events,
    plans_discovery,
    plans_errors,
    plans_metadata,
    plans_progress,
    plans_session_ids,
    plans_session_scope,
)
from .supervisor_audit_evidence_collector import _Collector
from .supervisor_audit_evidence_constants import (
    _SNAPSHOT_IDENTITY_KEYS,
    REASON_MALFORMED,
    REASON_NOT_RECORDED,
    REASON_UNREADABLE,
)
from .supervisor_audit_evidence_helpers import (
    _read_json_file,
    _sha256_text,
    _tree_digest,
)


def _plan_section(
    collector: _Collector,
    *,
    plan_dir: Path,
    state_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Identity of the source plan directory and of the split it produced."""
    section: dict[str, Any] = {
        "reference": collector.reference(plan_dir, kind="plan"),
        "context": collector.file_digest(plan_dir / "context.md", kind="plan"),
        "manifest": collector.file_digest(plan_dir / "manifest.md", kind="plan"),
        # Every turn claims ``progress_appended``; without this the claim cites
        # nothing, and a journal that was never written goes unreported.
        "progress": collector.file_digest(
            plans_progress.progress_path(plan_dir), kind="plan"
        ),
        "metadata": _plan_metadata(collector, plan_dir),
    }
    for label, key in (("initial", "initial_snapshot"), ("final", "final_snapshot")):
        snapshot = state_payload.get(key)
        if isinstance(snapshot, Mapping):
            section[label] = {
                name: collector.redact(snapshot.get(name), key=name)
                for name in _SNAPSHOT_IDENTITY_KEYS
                if name in snapshot
            }
        else:
            section[label] = {"available": False, "reason": REASON_NOT_RECORDED}
    sessions = []
    for path in sorted(plan_dir.glob("session-*.md")):
        entry = collector.file_digest(path, kind="plan")
        entry["name"] = path.name
        sessions.append(entry)
    section["sessions"] = sessions
    return section


def _plan_metadata(collector: _Collector, plan_dir: Path) -> dict[str, Any]:
    """Read the durable workspace binding without recovering a transaction.

    ``plans_metadata.load_plan_metadata`` repairs a pending plan transaction as a side
    effect. An evidence collector must observe the run rather than change it,
    so the file is parsed directly.
    """
    path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
    payload, reason = _read_json_file(path)
    if payload is None:
        # A file holding the literal `null` parses to `None` with no reason, so
        # it is reported the way every other reader in this module reports an
        # unusable payload rather than as an omission with a null reason.
        reason = reason or REASON_MALFORMED
        collector.omissions.record("plan.metadata", reason, str(path))
        return {"available": False, "reason": reason}
    return {
        "available": True,
        "reference": collector.reference(
            path, kind="plan", digest=_sha256_text(json.dumps(payload, sort_keys=True))
        ),
        "content": collector.redact(payload),
    }


def _registry_section(
    collector: _Collector, state_payload: Mapping[str, Any]
) -> dict[str, Any]:
    """The workspace binding and repository registry the run executed under."""
    repositories = state_payload.get("repositories")
    if not isinstance(repositories, (list, tuple)):
        run_config = state_payload.get("run_config")
        if isinstance(run_config, Mapping):
            repositories = run_config.get("repositories")
    if not isinstance(repositories, (list, tuple)):
        return {"registered": False, "reason": REASON_NOT_RECORDED}
    return {
        "registered": True,
        "workspace_root": collector.locator(
            state_payload.get("workspace_root") or state_payload.get("cwd") or "."
        ),
        "digest": state_payload.get("registry_digest"),
        "repositories": collector.redact(list(repositories)),
    }


def _scopes_section(
    collector: _Collector, *, plan_dir: Path, aliases: Sequence[str]
) -> dict[str, Any]:
    """Per-session read and write scopes as the split plan declares them."""
    if not aliases:
        return {"available": False, "reason": REASON_NOT_RECORDED}
    sessions: list[dict[str, Any]] = []
    try:
        session_files = plans_discovery.discover_session_files(plan_dir)
    except plans_errors.PlanError:
        collector.omissions.record("scopes", REASON_UNREADABLE, str(plan_dir))
        return {"available": False, "reason": REASON_UNREADABLE}
    paths = {session_file.path for session_file in session_files}
    # Historical audit evidence still uses zero-padded session IDs. Preserve
    # those exact numeric filenames without treating session-N.notes.md (or
    # other supporting documents) as sessions.
    paths.update(
        path
        for path in plan_dir.glob("session-*.md")
        if plans_session_ids.LOOSE_SESSION_FILE_RE.fullmatch(path.name)
    )
    for path in sorted(paths):
        entry: dict[str, Any] = {"session": path.stem}
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            entry["available"] = False
            entry["reason"] = REASON_UNREADABLE
            sessions.append(entry)
            continue
        try:
            scope = plans_session_scope.parse_session_scope(text, tuple(aliases))
        except plans_errors.PlanError, ValueError:
            entry["available"] = False
            entry["reason"] = REASON_MALFORMED
        else:
            entry.update(
                available=True, reads=list(scope.reads), writes=list(scope.writes)
            )
        sessions.append(entry)
    return {"available": True, "sessions": sessions}


def _repository_section(
    collector: _Collector,
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Per-repository mutation fingerprints: paths and hashes, never content."""
    if not isinstance(snapshot, Mapping):
        return {"available": False, "reason": REASON_NOT_RECORDED}
    return {
        "available": True,
        "repositories": {
            str(name): collector.redact(fingerprint)
            for name, fingerprint in snapshot.items()
        },
    }


def _versions_section() -> dict[str, Any]:
    """What ASL and its skills were, by version and by content digest."""
    package_root = Path(__file__).resolve().parent
    return {
        "asl": __version__,
        "package": _tree_digest(package_root, patterns=("*.py",)),
        "skills": _tree_digest(package_root.parent / "skills", patterns=("*",)),
        "assurance_schema_version": assurance_models_vocab.ASSURANCE_SCHEMA_VERSION,
        "event_schema_version": events.EVENT_SCHEMA_VERSION,
    }
