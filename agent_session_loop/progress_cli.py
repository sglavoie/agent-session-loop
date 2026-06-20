"""CLI handlers for ``asl backfill-progress`` and ``asl progress``.

Both commands operate on the runner-managed ``progress.md`` journal in a
plan directory. ``backfill-progress`` reconstructs a journal for plans
that completed sessions before the live runner integration shipped, by
walking each session handoff and synthesising a ``ProgressEntry`` per
completed session. ``progress`` prints the journal, dumps it as JSON, or
follows the file as new entries are appended by an active runner.

The follow loop polls the journal file at a configurable interval and
honours a :class:`threading.Event` for cooperative shutdown so tests can
exercise it without real sleeps.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from typing import Any, TextIO

from . import plans, responses, tail


DEFAULT_POLL_INTERVAL_SECONDS = tail.POLL_INTERVAL_SECONDS


def _handoff_to_progress_entry(
    handoff: plans.SessionHandoff,
    completed_at: str,
) -> plans.ProgressEntry:
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


def _entry_to_json(entry: plans.ProgressEntry) -> dict[str, Any]:
    return {
        "session_id": entry.session_id.value,
        "status": entry.status,
        "completed_at": entry.completed_at,
        "files_modified": list(entry.files_modified),
        "key_decisions": entry.key_decisions,
        "discovered_facts": list(entry.discovered_facts),
        "open_questions": list(entry.open_questions),
        "blockers": entry.blockers,
    }


def _utc_iso_from_mtime(path: Path) -> str:
    moment = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return moment.isoformat()


def _build_backfill_entries(
    snapshot: plans.PlanSnapshot,
) -> list[plans.ProgressEntry]:
    entries: list[plans.ProgressEntry] = []
    for session_status in snapshot.sessions:
        if session_status.status != "complete":
            continue
        handoff = plans.read_session_handoff(session_status.session)
        completed_at = _utc_iso_from_mtime(session_status.session.path)
        entries.append(_handoff_to_progress_entry(handoff, completed_at))
    return entries


def backfill_progress_from_args(args: argparse.Namespace) -> int:
    """Reconstruct ``progress.md`` from completed session handoffs."""
    plan_dir_arg = args.plan_dir
    force: bool = args.force
    dry_run: bool = args.dry_run

    try:
        snapshot = plans.inspect_plan(plan_dir_arg)
        entries = _build_backfill_entries(snapshot)
    except plans.PlanError as error:
        print(
            f"agent-session-loop backfill-progress: {error}",
            file=sys.stderr,
        )
        return 1

    progress_file = plans.progress_path(snapshot.plan_dir)
    rendered = plans.render_progress_journal(tuple(entries))

    if dry_run:
        sys.stdout.write(rendered)
        return 0

    if progress_file.exists() and not force:
        print(
            "agent-session-loop backfill-progress: "
            f"{progress_file} already exists; pass --force to overwrite",
            file=sys.stderr,
        )
        return 1

    try:
        plans.write_progress_journal(snapshot.plan_dir, tuple(entries))
    except (plans.PlanError, OSError) as error:
        print(
            f"agent-session-loop backfill-progress: {error}",
            file=sys.stderr,
        )
        return 1

    print(
        f"agent-session-loop backfill-progress: wrote {len(entries)} entries "
        f"to {progress_file}"
    )
    return 0


def follow_progress(
    plan_dir: str | Path,
    *,
    out: TextIO | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
) -> None:
    """Poll ``progress.md`` and stream newly appended bytes to ``out``.

    Returns when ``stop_event`` is set; tests pass a short interval and a
    pre-built event so the loop can be exercised without real waits.
    """
    out_stream = sys.stdout if out is None else out
    stop = stop_event if stop_event is not None else threading.Event()
    progress_file = plans.progress_path(plan_dir)
    try:
        offset = progress_file.stat().st_size if progress_file.exists() else 0
    except OSError:
        offset = 0

    while not stop.is_set():
        if progress_file.exists():
            try:
                data = progress_file.read_bytes()
            except OSError:
                data = None
            if data is not None and len(data) > offset:
                new_text = data[offset:].decode("utf-8", errors="replace")
                out_stream.write(new_text)
                out_stream.flush()
                offset = len(data)
        if stop.is_set():
            break
        stop.wait(poll_interval)


def progress_from_args(args: argparse.Namespace) -> int:
    """Print, JSON-dump, or follow ``progress.md`` for a plan directory."""
    plan_dir_arg = args.plan_dir
    want_json: bool = args.json
    follow: bool = args.tail

    plan_dir = plans.canonicalize_path(plan_dir_arg)
    progress_file = plans.progress_path(plan_dir)

    if want_json:
        try:
            entries = plans.read_progress_entries(plan_dir)
        except plans.PlanError as error:
            responses.print_json(
                responses.error_payload(
                    "progress",
                    reason="malformed-progress-journal",
                    error=str(error),
                    paused=False,
                    plan_dir=str(plan_dir),
                    progress_path=str(progress_file),
                )
            )
            return 1
        responses.print_json(
            responses.success_payload(
                "progress",
                plan_dir=str(plan_dir),
                progress_path=str(progress_file),
                entries=[_entry_to_json(entry) for entry in entries],
                entry_count=len(entries),
            )
        )
        return 0

    if progress_file.exists():
        try:
            sys.stdout.write(progress_file.read_text(encoding="utf-8"))
        except OSError as error:
            print(
                f"agent-session-loop progress: cannot read {progress_file}: {error}",
                file=sys.stderr,
            )
            return 1
    elif not follow:
        print(
            f"agent-session-loop progress: {progress_file} does not exist "
            "(run `asl backfill-progress` to reconstruct from session handoffs)",
            file=sys.stderr,
        )
        return 1

    if follow:
        try:
            follow_progress(plan_dir)
        except KeyboardInterrupt:
            return 0
    return 0
