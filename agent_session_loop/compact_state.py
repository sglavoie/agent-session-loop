"""Reclaim duplicated operational JSON by compacting v1 state to schema v2.

Historical plan state directories accumulate large ``state.json`` records that
embed every turn's full plan snapshot and raw verification output, oversized
``run_finished`` / ``run_paused`` events, and per-turn ``result.json`` files that
carry complete command stdout/stderr. Session 3 made *new* runs write the
compact v2 projection at the ``run_persistence._write_state_payload`` chokepoint; this
module retroactively compacts the records already on disk.

Compaction is lossless for failed-check output — the full stream is extracted to
a gzip artifact (recoverable via :func:`agent_session_loop.artifacts.read_gzip`)
before the raw copy is dropped from the JSON — and intentionally lossy for
successful-check output, which is replaced by a bounded preview. Every rewrite
uses temp-file + :func:`os.replace`, so an interrupted compaction can never leave
a partially rewritten JSON file. ``--apply`` performs the writes; the default is
a dry-run that reports the projected reclaim without touching disk. Reclaim
figures are net: the gzip bytes added by retained failure artifacts are
subtracted from the JSON shrinkage, so the report never overstates what is
actually reclaimed on disk.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    artifacts,
    cli_common,
    events,
    plans_errors,
    plans_files,
    plans_snapshot,
    run_persistence,
    state,
    state_locks,
    state_status,
    turn_execution_constants,
)

# Subdirectory (relative to a turn artifact directory) where failed-command
# gzip artifacts live, matching the runner-managed verification convention.
_VERIFICATION_ARTIFACT_SUBDIR = "verification"


@dataclass
class _Stats:
    """Mutable accumulator threaded through the per-record compactors."""

    files_scanned: int = 0
    files_rewritten: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    artifact_bytes_added: int = 0
    pass_output_removed: int = 0
    failed_output_extracted: int = 0
    rewritten_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompactionReport:
    """Outcome of compacting one plan state directory."""

    plan_dir: str
    state_dir: str
    applied: bool
    files_scanned: int
    files_rewritten: int
    bytes_before: int
    bytes_after: int
    artifact_bytes_added: int
    projected_bytes_reclaimed: int
    actual_bytes_reclaimed: int
    pass_output_removed: int
    failed_output_extracted: int
    rewritten_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "compact-state",
            "plan_dir": self.plan_dir,
            "state_dir": self.state_dir,
            "applied": self.applied,
            "files_scanned": self.files_scanned,
            "files_rewritten": self.files_rewritten,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "artifact_bytes_added": self.artifact_bytes_added,
            "projected_bytes_reclaimed": self.projected_bytes_reclaimed,
            "actual_bytes_reclaimed": self.actual_bytes_reclaimed,
            "pass_output_removed": self.pass_output_removed,
            "failed_output_extracted": self.failed_output_extracted,
            "rewritten_paths": list(self.rewritten_paths),
        }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_state_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize a state/result payload the way the runner persists it."""
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _dump_events_bytes(records: list[dict[str, Any]]) -> bytes:
    """Serialize events one JSON object per line, matching ``append_event``."""
    lines = [json.dumps(record, sort_keys=True) + "\n" for record in records]
    return "".join(lines).encode("utf-8")


def _stream_has_metadata(check: dict[str, Any], stream: str) -> bool:
    """Return True when ``stream`` already carries v2 bounded metadata.

    A v2 check stores a preview in ``stdout``/``stderr`` plus a populated
    ``*_sha256`` digest; a v1 check embeds the full stream with no digest. The
    digest is the reliable discriminator, so an already-compact record is left
    untouched (idempotent re-runs and no double-preview).
    """
    return bool(check.get(f"{stream}_sha256"))


def _compact_check_streams(
    check: dict[str, Any],
    *,
    artifact_dir: Path,
    apply: bool,
    stats: _Stats,
) -> None:
    """Bound raw stdout/stderr on one verification check in place.

    Failed-check output is extracted to a gzip artifact (so it stays byte-for-
    byte recoverable) before the raw copy is replaced with a bounded preview;
    successful-check output is replaced with a preview and its full copy is
    discarded. Already-compact v2 streams are skipped.
    """
    failed = check.get("status") != "pass" or bool(check.get("failure_reason"))
    check_name = check.get("name") or "check"
    for stream in ("stdout", "stderr"):
        raw = check.get(stream)
        if not isinstance(raw, str) or raw == "":
            continue
        if _stream_has_metadata(check, stream):
            continue
        raw_bytes = raw.encode("utf-8")
        preview, truncated = artifacts.bounded_preview(raw_bytes)
        check[f"{stream}_byte_count"] = len(raw_bytes)
        check[f"{stream}_sha256"] = artifacts.sha256_hex(raw_bytes)
        check[f"{stream}_truncated"] = truncated
        if failed:
            stats.failed_output_extracted += 1
            if not check.get(f"{stream}_artifact_path"):
                stem = artifacts.derive_artifact_stem(f"{check_name}-{stream}")
                target = artifact_dir / f"{stem}.gz"
                # Deterministic compression lets a dry-run project the exact
                # artifact size without writing it; an artifact already on
                # disk (interrupted prior apply) only counts its size delta.
                compressed = artifacts.gzip_compress(raw_bytes)
                try:
                    existing_size = target.stat().st_size
                except OSError:
                    existing_size = 0
                stats.artifact_bytes_added += len(compressed) - existing_size
                if apply:
                    artifact_dir.mkdir(parents=True, exist_ok=True)
                    artifacts.atomic_write_bytes(target, compressed, durable=True)
                check[f"{stream}_artifact_path"] = str(target)
        else:
            check.setdefault(f"{stream}_artifact_path", None)
            stats.pass_output_removed += 1
        check[stream] = preview


def _compact_result_payload(
    payload: dict[str, Any],
    *,
    artifact_dir: Path,
    apply: bool,
    stats: _Stats,
) -> dict[str, Any]:
    """Return a compact copy of a per-turn ``result.json`` payload.

    Result records are allowed to keep full per-turn detail (plan snapshots),
    but raw verification command output is extracted/bounded so the dominant
    size driver is removed. The record is stamped as schema v2.
    """
    compact = json.loads(json.dumps(payload))  # deep copy without shared refs
    compact["version"] = turn_execution_constants.RESULT_SCHEMA_VERSION
    verification = compact.get("verification")
    if isinstance(verification, dict):
        checks = verification.get("checks")
        if isinstance(checks, list):
            for check in checks:
                if isinstance(check, dict):
                    _compact_check_streams(
                        check,
                        artifact_dir=artifact_dir,
                        apply=apply,
                        stats=stats,
                    )
    return compact


def _compact_event_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a compact copy of one ``events.jsonl`` record.

    Order, ``created_at`` timestamps, names, and run IDs are preserved. The
    heavyweight ``run_finished`` payload (which embeds the entire state) is
    projected to its compact form; every other payload has raw verification
    output stripped from any embedded checks/failures while pause evidence
    (reason, message, details) is kept.
    """
    compact = dict(record)
    compact["version"] = events.EVENT_SCHEMA_VERSION
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return compact
    name = record.get("name")
    if name == "run_finished" and "turns" in payload:
        compact["payload"] = run_persistence._run_finished_event_payload(payload)
        return compact
    new_payload = run_persistence._bounded_verification_details(payload)
    if isinstance(new_payload, dict):
        for nested_key in ("details", "pause_details"):
            nested = new_payload.get(nested_key)
            if isinstance(nested, dict):
                new_payload = dict(new_payload)
                new_payload[nested_key] = run_persistence._bounded_verification_details(
                    nested
                )
    compact["payload"] = new_payload
    return compact


def _rewrite(
    path: Path,
    original: bytes,
    compact: bytes,
    *,
    apply: bool,
    stats: _Stats,
) -> None:
    """Account for (and, when applying, atomically write) one compacted file."""
    stats.files_scanned += 1
    stats.bytes_before += len(original)
    if compact == original:
        stats.bytes_after += len(original)
        return
    stats.bytes_after += len(compact)
    stats.files_rewritten += 1
    stats.rewritten_paths.append(str(path))
    if apply:
        artifacts.atomic_write_bytes(path, compact, durable=True)


def _compact_state_file(path: Path, *, apply: bool, stats: _Stats) -> None:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        stats.files_scanned += 1
        original = path.read_bytes()
        stats.bytes_before += len(original)
        stats.bytes_after += len(original)
        return
    original = path.read_bytes()
    compact = _dump_state_bytes(run_persistence._compact_state_payload(payload))
    _rewrite(path, original, compact, apply=apply, stats=stats)


def _compact_events_file(path: Path, *, apply: bool, stats: _Stats) -> None:
    original = path.read_bytes()
    records: list[dict[str, Any]] = []
    for line in original.decode("utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            records.append(_compact_event_record(parsed))
        else:
            records.append(parsed)
    compact = _dump_events_bytes(records)
    _rewrite(path, original, compact, apply=apply, stats=stats)


def _compact_result_file(path: Path, *, apply: bool, stats: _Stats) -> None:
    payload = _read_json(path)
    original = path.read_bytes()
    if not isinstance(payload, dict):
        stats.files_scanned += 1
        stats.bytes_before += len(original)
        stats.bytes_after += len(original)
        return
    artifact_dir = path.parent / _VERIFICATION_ARTIFACT_SUBDIR
    compact_payload = _compact_result_payload(
        payload, artifact_dir=artifact_dir, apply=apply, stats=stats
    )
    compact = _dump_state_bytes(compact_payload)
    _rewrite(path, original, compact, apply=apply, stats=stats)


def compact_plan_state(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    apply: bool = False,
) -> CompactionReport:
    """Compact the state, events, and result records for one plan directory.

    ``apply`` refuses while a live run holds the plan lock: that run appends
    to ``events.jsonl`` and rewrites ``state.json`` and the replace here would
    drop whatever it wrote since this read. A dry run writes nothing and so
    reports on a live plan freely.
    """
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    plan_state = state.plan_state_dir(canonical_plan_dir, state_dir)
    if apply:
        lock = state_locks.inspect_lock_info(canonical_plan_dir, state_dir)
        if lock is not None and lock.active:
            raise state.StateError(
                f"plan is locked by pid {lock.pid} at {lock.path}; "
                "compact its state once that run has stopped"
            )
    stats = _Stats()

    state_path = plan_state / events.STATE_FILE_NAME
    if state_path.is_file():
        _compact_state_file(state_path, apply=apply, stats=stats)

    events_path = plan_state / events.EVENTS_FILE_NAME
    if events_path.is_file():
        _compact_events_file(events_path, apply=apply, stats=stats)

    runs_dir = plan_state / "runs"
    if runs_dir.is_dir():
        for result_path in sorted(runs_dir.glob("*/*/result.json")):
            if result_path.is_file():
                _compact_result_file(result_path, apply=apply, stats=stats)

    # Net reclaim: JSON shrinkage minus the gzip bytes retained failure
    # artifacts add, so the report matches what is actually freed on disk.
    reclaimed = stats.bytes_before - stats.bytes_after - stats.artifact_bytes_added
    return CompactionReport(
        plan_dir=str(canonical_plan_dir),
        state_dir=str(plan_state),
        applied=apply,
        files_scanned=stats.files_scanned,
        files_rewritten=stats.files_rewritten,
        bytes_before=stats.bytes_before,
        bytes_after=stats.bytes_after,
        artifact_bytes_added=stats.artifact_bytes_added,
        projected_bytes_reclaimed=reclaimed,
        actual_bytes_reclaimed=reclaimed if apply else 0,
        pass_output_removed=stats.pass_output_removed,
        failed_output_extracted=stats.failed_output_extracted,
        rewritten_paths=tuple(stats.rewritten_paths),
    )


def _print_report(report: CompactionReport) -> None:
    mode = "applied" if report.applied else "dry-run"
    print(f"Compact state ({mode}) for {report.plan_dir}")
    print(f"State dir: {report.state_dir}")
    print(
        f"Files: {report.files_rewritten} rewritten of {report.files_scanned} scanned"
    )
    print(f"Projected bytes reclaimed: {report.projected_bytes_reclaimed}")
    if report.artifact_bytes_added:
        print(
            "Failure artifact bytes added (already subtracted): "
            f"{report.artifact_bytes_added}"
        )
    if report.applied:
        print(f"Actual bytes reclaimed: {report.actual_bytes_reclaimed}")
    print(
        f"Failed-check outputs extracted: {report.failed_output_extracted}; "
        f"pass outputs removed (lossy): {report.pass_output_removed}"
    )
    if not report.applied and report.files_rewritten:
        print("Re-run with --apply to write the compact records.")


def _compact_state_error_reason(error: Exception) -> str:
    if isinstance(error, plans_errors.PlanError):
        return state_status.plans_errors_reason(error)
    if isinstance(error, artifacts.ArtifactError):
        return "artifact-error"
    if isinstance(error, OSError):
        return "os-error"
    return "state-error"


def compact_state_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl compact-state``."""
    try:
        # The same validation `asl status` runs, so a directory that is not a
        # plan fails here instead of reporting on a state dir nothing owns.
        plans_snapshot.inspect_plan(args.plan_dir)
        report = compact_plan_state(
            args.plan_dir,
            state_dir=getattr(args, "state_dir", None),
            apply=args.apply,
        )
    except (
        OSError,
        ValueError,
        artifacts.ArtifactError,
        plans_errors.PlanError,
        state.StateError,
    ) as error:
        cli_common.print_error(
            args,
            command_name="compact-state",
            text_label="compact-state",
            reason=_compact_state_error_reason(error),
            error=str(error),
        )
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        _print_report(report)
    return 0
