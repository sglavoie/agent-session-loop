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
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from . import (
    cli_common,
    plans_errors,
    plans_files,
    plans_models,
    plans_progress,
    plans_snapshot,
    progress_repair,
    responses,
    tail,
)
from .cli_defaults import program_name

DEFAULT_POLL_INTERVAL_SECONDS = tail.POLL_INTERVAL_SECONDS


BACKFILL_COMMAND_NAME = "backfill-progress"


def _entry_to_json(entry: plans_models.ProgressEntry) -> dict[str, Any]:
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


def backfill_progress_from_args(args: argparse.Namespace) -> int:
    """Reconstruct ``progress.md`` from completed session handoffs."""
    plan_dir_arg = args.plan_dir
    force: bool = args.force
    dry_run: bool = args.dry_run
    as_json = bool(getattr(args, "json", False))

    def refuse(reason: str, error: object) -> int:
        if as_json:
            responses.print_json(
                responses.error_payload(
                    BACKFILL_COMMAND_NAME, reason=reason, error=str(error)
                )
            )
        else:
            print(f"{program_name()} backfill-progress: {error}", file=sys.stderr)
        return 1

    try:
        snapshot = plans_snapshot.inspect_plan(plan_dir_arg)
        entries = progress_repair.build_backfill_entries(snapshot)
    except plans_errors.PlanNotFoundError as error:
        return refuse("plan-not-found", error)
    except plans_errors.PlanError as error:
        return refuse("malformed-plan", error)

    progress_file = plans_progress.progress_path(snapshot.plan_dir)
    rendered = plans_progress.render_progress_journal(tuple(entries))

    if dry_run:
        if as_json:
            responses.print_json(
                responses.success_payload(
                    BACKFILL_COMMAND_NAME,
                    reason="dry-run",
                    plan_dir=str(snapshot.plan_dir),
                    progress_path=str(progress_file),
                    entries=[_entry_to_json(entry) for entry in entries],
                    journal=rendered,
                )
            )
        else:
            sys.stdout.write(rendered)
        return 0

    if progress_file.exists() and not force:
        return refuse(
            "progress-exists",
            f"{progress_file} already exists; pass --force to overwrite",
        )

    try:
        result = progress_repair.repair_progress_journal(
            snapshot.plan_dir, snapshot=snapshot
        )
    except plans_errors.PlanError as error:
        return refuse("malformed-plan", error)
    except OSError as error:
        return refuse("os-error", error)

    if as_json:
        responses.print_json(
            responses.success_payload(
                BACKFILL_COMMAND_NAME,
                reason="written",
                plan_dir=str(snapshot.plan_dir),
                progress_path=str(result.path),
                entries_written=result.entries_written,
                completed_session_ids=list(result.completed_session_ids),
                journal_replaced=result.journal_replaced,
            )
        )
        return 0
    print(
        f"{program_name()} backfill-progress: wrote {result.entries_written} "
        f"entries to {result.path}"
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

    Every append replaces the journal atomically, so a new inode is the norm
    and says nothing; what was already printed is what decides. A journal no
    longer starting with it was rewritten (``backfill-progress --force``
    replaces it whole, often shorter): a marker line says so and the journal
    is printed again from its start, so the follower neither goes silent nor
    misses an entry that lands past an offset counted against the old file.

    Returns when ``stop_event`` is set; tests pass a short interval and a
    pre-built event so the loop can be exercised without real waits.
    """
    out_stream = sys.stdout if out is None else out
    stop = stop_event if stop_event is not None else threading.Event()
    progress_file = plans_progress.progress_path(plan_dir)
    try:
        seen = progress_file.read_bytes() if progress_file.exists() else b""
    except OSError:
        seen = b""

    while not stop.is_set():
        try:
            data: bytes | None = progress_file.read_bytes()
        except OSError:
            data = None
        if data is not None and data != seen:
            if data.startswith(seen):
                new_data = data[len(seen) :]
            else:
                out_stream.write(f"--- {progress_file.name} rewritten ---\n")
                new_data = data
            out_stream.write(new_data.decode("utf-8", errors="replace"))
            out_stream.flush()
            seen = data
        if stop.is_set():
            break
        stop.wait(poll_interval)


def progress_from_args(args: argparse.Namespace) -> int:
    """Print, JSON-dump, or follow ``progress.md`` for a plan directory."""
    if not cli_common.resolve_plan_dir_argument(
        args, command_name="progress", text_label="progress"
    ):
        return 1
    plan_dir_arg = args.plan_dir
    want_json: bool = args.json
    follow: bool = args.tail
    if want_json and follow:
        # The journal is Markdown; following it streams text, so there is no
        # JSON document `--tail` could keep printing.
        responses.print_json(
            responses.error_payload(
                "progress",
                reason="input-error",
                error="--tail cannot be combined with --json",
                paused=False,
            )
        )
        return 1

    plan_dir = plans_files.canonicalize_path(plan_dir_arg)
    progress_file = plans_progress.progress_path(plan_dir)

    # A missing plan is an error in both modes: JSON must not report an empty
    # journal for a directory that is not there, and the text-mode hint to
    # `asl backfill-progress` is only useful once the directory exists.
    try:
        plans_snapshot.require_plan_directory(plan_dir)
    except plans_errors.PlanError as error:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "progress",
                    reason=(
                        "plan-not-found"
                        if isinstance(error, plans_errors.PlanNotFoundError)
                        else "malformed-plan"
                    ),
                    error=str(error),
                    paused=False,
                    plan_dir=str(plan_dir),
                    progress_path=str(progress_file),
                )
            )
        else:
            print(f"{program_name()} progress: {error}", file=sys.stderr)
        return 1

    if want_json:
        try:
            entries = plans_progress.read_progress_entries(plan_dir)
        except plans_errors.PlanError as error:
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
            # Shown as written, a stray non-UTF-8 byte replaced, the way
            # `--tail` already streams it: the text view is for reading.
            sys.stdout.write(
                progress_file.read_bytes().decode("utf-8", errors="replace")
            )
        except OSError as error:
            print(
                f"{program_name()} progress: cannot read {progress_file}: {error}",
                file=sys.stderr,
            )
            return 1
    elif not follow:
        print(
            f"{program_name()} progress: {progress_file} does not exist "
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
