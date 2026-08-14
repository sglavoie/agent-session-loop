"""Shared progress-journal repair primitives used by both CLI and runner.

`asl backfill-progress` (manual) and the upcoming runner-managed
loop-ready auto-repair both need to reconstruct `<plan-dir>/progress.md`
from completed session handoffs. This module centralises that synthesis
logic so the CLI and runner cannot drift on how completion timestamps
are derived, how the `Files modified` field is split, or how the file
is written.

The CLI's user-facing flags (`--dry-run` printing, refusing to overwrite
without `--force`) remain in `progress_cli`. This module is force-style
by design: callers that need refusal semantics check the destination
themselves before calling `repair_progress_journal`. Runner-side
callers always treat the journal as derived state that can be
re-synthesised from authoritative handoffs at any time, so a stale or
malformed file is simply replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import plans


def handoff_to_progress_entry(
    handoff: plans.SessionHandoff,
    completed_at: str,
) -> plans.ProgressEntry:
    """Map a parsed `SessionHandoff` into a `plans.ProgressEntry`."""
    files_modified: tuple[str, ...] = ()
    if handoff.files_modified:
        inline = handoff.files_modified[0]
        files_modified = tuple(
            part.strip() for part in inline.split(",") if part.strip()
        )
    return plans.ProgressEntry(
        session_id=handoff.session_id,
        status=handoff.status,
        completed_at=completed_at,
        files_modified=files_modified,
        key_decisions=handoff.key_decisions,
        discovered_facts=handoff.discovered_facts,
        open_questions=handoff.open_questions,
        blockers=handoff.blockers,
    )


def utc_iso_from_mtime(path: Path) -> str:
    """Return `path`'s mtime as a UTC ISO-8601 timestamp."""
    moment = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    return moment.isoformat()


def build_backfill_entries(
    snapshot: plans.PlanSnapshot,
) -> list[plans.ProgressEntry]:
    """Synthesise progress entries for every completed session in `snapshot`."""
    entries: list[plans.ProgressEntry] = []
    for session_status in snapshot.sessions:
        if session_status.status != "complete":
            continue
        handoff = plans.read_session_handoff(session_status.session)
        completed_at = utc_iso_from_mtime(session_status.session.path)
        entries.append(handoff_to_progress_entry(handoff, completed_at))
    return entries


@dataclass(frozen=True)
class ProgressRepairResult:
    """Structured outcome of a force-style progress journal repair."""

    action: str
    path: Path
    entries_written: int
    completed_session_ids: tuple[str, ...]
    journal_replaced: bool


def repair_progress_journal(
    plan_dir: str | Path,
    *,
    snapshot: plans.PlanSnapshot | None = None,
) -> ProgressRepairResult:
    """Rebuild and atomically replace `<plan_dir>/progress.md` from handoffs.

    The journal is always rewritten (force semantics). Callers that need
    "refuse to overwrite" behaviour must check the destination file's
    existence themselves before invoking this helper. The write goes
    through `plans.write_progress_journal`, which uses an atomic
    temp-file + `os.replace` so the destination is never half-written.

    Raises `plans.PlanError` if the plan snapshot cannot be loaded or
    `OSError` if the atomic write fails; both originate from
    `plans.write_progress_journal` and are surfaced unchanged so callers
    can map them to their own error vocabularies.
    """
    if snapshot is None:
        snapshot = plans.inspect_plan(plan_dir)

    entries = build_backfill_entries(snapshot)
    path = plans.progress_path(snapshot.plan_dir)
    journal_replaced = path.exists()
    plans.write_progress_journal(snapshot.plan_dir, tuple(entries))

    return ProgressRepairResult(
        action="rewrite",
        path=path,
        entries_written=len(entries),
        completed_session_ids=tuple(entry.session_id.value for entry in entries),
        journal_replaced=journal_replaced,
    )
