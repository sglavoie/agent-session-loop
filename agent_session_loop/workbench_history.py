"""The Mac provider behind the portable history service.

``asl_workbench.history`` defines what a run's ending means and may not import
this package; this module is the half that touches the filesystem. It walks the
per-plan state directories ASL already writes, reads each ``events.jsonl``
forward by one bounded pass, and hands the bytes to the portable package, which
decides what they say.

The division is the same one ``workbench_catalog`` makes for the catalog, and
for the same reason: the interpretation has to be loadable on a host with no ASL
corpus, and the reading has to happen where the corpus is. What is here is
therefore only the four things that need a filesystem -- enumerating sources,
resuming a file safely, reading ``state.json`` for a plan's identity, and
persisting the checkpoints -- and every decision about what a record *means*
lives in ``asl_workbench.history``.

Two properties are contracts rather than incidental behaviour:

* **Reading and committing are separate calls.** :func:`import_window` returns
  entries and the checkpoints that *would* consume them; :func:`commit`
  advances the store, and the caller runs it only once the entries have been
  delivered. A crash in that gap re-reads the same bytes on the next pass and
  re-derives byte-identical entries with the same ids, which an upsert absorbs.
  The other ordering loses a run's ending permanently on one badly timed crash.

* **One projection serves both consumers.** :func:`project_history` builds the
  local view and the publication batches from a single
  :class:`~asl_workbench.history.ImportResult`, over the same
  :class:`~asl_workbench.history.HistoryEntry` objects. The cockpit on the Mac
  and the cockpit on the Pi therefore cannot disagree about what a run did.

Nothing here writes into a plan directory or a state directory other than the
one checkpoint file, and nothing here reclaims a lock or runs retention: an
import observes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from asl_workbench import history as portable_history
from asl_workbench.history import (
    NO_PLAN_FACTS,
    HistoryBatch,
    HistoryError,
    HistoryView,
    ImportResult,
    PlanFacts,
    SourceCheckpoint,
    SourceRead,
)

from . import state

#: Seven days, the window the Companion retains history for. A caller that
#: wants a different window passes one; the default is here so the two
#: applications agree on what "history" means without coordinating.
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60

#: The checkpoint lives beside the per-plan state directories rather than inside
#: one: it describes the whole corpus, and a plan's own directory is the wrong
#: place for a record that outlives the plan. Discovery only ever yields
#: directories, so a file here cannot be mistaken for a source.
CHECKPOINT_FILE_NAME = "workbench-history.json"


@dataclass(frozen=True)
class HistorySource:
    """One plan's ASL state directory, and the event file inside it."""

    key: str
    state_dir: Path

    @property
    def events_path(self) -> Path:
        return self.state_dir / portable_history.EVENTS_FILE_NAME

    @property
    def state_path(self) -> Path:
        return self.state_dir / portable_history.STATE_FILE_NAME


def source_key(state_dir: Path) -> str:
    """An opaque, stable key for one state directory.

    Digested rather than used directly even though ASL's directory name is
    already a hash of the plan path: history publishes its own identifiers, and
    a name that is a digest of a Mac path is still a digest of a Mac path.
    """
    return portable_history.digest_of(["asl-source", str(state_dir)])


def discover_sources(state_dir: str | Path | None = None) -> tuple[HistorySource, ...]:
    """Every plan state directory holding an event file, in a stable order.

    A missing state root is an empty corpus rather than a failure: a Mac with
    ASL installed and never run has nothing to import. A root that exists and
    cannot be enumerated raises, because reporting "no history" for a corpus
    that is merely unreadable is the one answer that is wrong.

    The order is the sorted directory order ``state.iter_plan_state_dirs``
    yields, and it matters: a pass that spends its read budget drains the
    earlier sources first and reaches the later ones next pass.
    """
    root = state.state_root(state_dir)
    sources: list[HistorySource] = []
    for child in state.iter_plan_state_dirs(root):
        try:
            if not (child / portable_history.EVENTS_FILE_NAME).is_file():
                continue
        except OSError:
            continue
        sources.append(HistorySource(key=source_key(child), state_dir=child))
    return tuple(sources)


def read_plan_facts(source: HistorySource) -> PlanFacts:
    """Read one plan's identity from ``state.json``, tolerating every failure.

    The last fallback for a plan directory, used when neither the terminal
    record nor a remembered ``run_started`` supplied one. Unreadable, oversized,
    malformed, and concurrently-removed files all produce empty facts rather
    than an exception, because one damaged plan must not stop the import of the
    other several hundred.
    """
    path = source.state_path
    try:
        if path.stat().st_size > portable_history.MAX_STATE_BYTES:
            return NO_PLAN_FACTS
        document = json.loads(
            path.read_bytes(), parse_constant=portable_history.refuse_non_finite
        )
    except OSError, ValueError:
        return NO_PLAN_FACTS
    return portable_history.plan_facts_from_state(document)


class CheckpointStore:
    """The durable record of how far each event file has been consumed.

    One JSON document, written by rename onto the live path, so an interrupted
    write leaves the previous checkpoint intact rather than a half-file. A
    document that cannot be parsed is discarded rather than repaired: starting
    the window again costs a re-read that duplicate suppression absorbs, and
    guessing at a corrupted offset costs a run's ending.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, SourceCheckpoint]:
        try:
            document = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise HistoryError(
                f"could not read the history checkpoint: {error}"
            ) from error
        except ValueError:
            return {}
        return portable_history.checkpoints_from_document(document)

    def save(self, checkpoints: tuple[SourceCheckpoint, ...]) -> None:
        document = portable_history.checkpoint_document(checkpoints)
        payload = json.dumps(document, sort_keys=True).encode("utf-8")
        temporary = self.path.with_name(f"{self.path.name}.partial")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise HistoryError(
                f"could not write the history checkpoint: {error}"
            ) from error


def default_store(state_dir: str | Path | None = None) -> CheckpointStore:
    """The checkpoint store for a state root, at its conventional path."""
    return CheckpointStore(state.state_root(state_dir) / CHECKPOINT_FILE_NAME)


def read_source(
    source: HistorySource,
    checkpoint: SourceCheckpoint,
    *,
    window_start: float,
    budget: int,
    max_read_bytes: int = portable_history.MAX_READ_BYTES_PER_PASS,
) -> SourceRead:
    """Walk one event file forward by one bounded pass.

    Everything this function decides is about bytes: whether the file is still
    the file the checkpoint described, where the last whole record ends, and how
    far past the window a single oversized record runs.
    :func:`asl_workbench.history.consume_records` decides what the bytes say.
    """
    path = source.events_path
    try:
        status = path.stat()
    except OSError:
        return SourceRead(checkpoint=checkpoint, unreadable=True)

    if budget <= 0 and status.st_size > checkpoint.offset:
        # The pass has spent its whole read budget on earlier plans. A source
        # left for the next pass costs one `stat` and is reported as deferred,
        # so the batch it is missing from is marked incomplete rather than
        # reading as a full window.
        return SourceRead(
            checkpoint=checkpoint,
            deferred_bytes=status.st_size - checkpoint.offset,
        )

    try:
        with path.open("rb") as handle:
            head = (
                handle.read(checkpoint.head_length) if checkpoint.head_length else b""
            )
            restarted = portable_history.restart_required(
                checkpoint,
                device=status.st_dev,
                inode=status.st_ino,
                size=status.st_size,
                head=head,
            )
            offset = 0 if restarted else checkpoint.offset
            handle.seek(offset)
            chunk = handle.read(max_read_bytes)

            complete = portable_history.complete_records(chunk)
            consumed = offset + len(complete)
            scanned = len(chunk)
            oversized = 0

            if not complete and len(chunk) >= max_read_bytes:
                # A single record longer than the whole pass window. It has to
                # be stepped over here, in one bounded scan, or every later pass
                # re-reads this same window forever and the plans after this one
                # are never reached.
                end = _end_of_record(handle, offset + len(chunk))
                if end is not None:
                    oversized = 1
                    consumed = end
                    scanned = end - offset

            # The prefix digested is bounded by what has been consumed, so it
            # never covers a byte a later append could still change.
            head_length = min(portable_history.HEAD_SAMPLE_BYTES, consumed)
            handle.seek(0)
            digest = (
                portable_history.head_digest(handle.read(head_length))
                if head_length
                else ""
            )
    except OSError:
        return SourceRead(checkpoint=checkpoint, unreadable=True)

    advanced = replace(
        checkpoint,
        device=status.st_dev,
        inode=status.st_ino,
        offset=consumed,
        head_digest=digest,
        head_length=head_length,
        records=0 if restarted else checkpoint.records,
    )
    read = portable_history.consume_records(
        complete,
        checkpoint=advanced,
        source_key=source.key,
        window_start=window_start,
        facts=read_plan_facts(source) if complete else NO_PLAN_FACTS,
    )
    return replace(
        read,
        restarted=restarted,
        deferred_bytes=max(0, status.st_size - consumed),
        read_bytes=scanned,
        oversized=read.oversized + oversized,
    )


def _end_of_record(handle: BinaryIO, start: int) -> int | None:
    """The offset just past the newline ending an oversized record.

    Called only when a whole pass window held no line ending at all, which means
    one record is longer than the window. The scan is bounded twice over: it
    reads in blocks, and it gives up past ``MAX_OVERSIZED_SCAN_BYTES``.

    ``None`` means the record has no end yet -- either it is still being
    written, or it is longer than the ceiling. Either way the offset stays where
    it is, which leaves the bytes deferred rather than consuming a record whose
    end the reader never saw.
    """
    handle.seek(start)
    scanned = 0
    ceiling = portable_history.MAX_OVERSIZED_SCAN_BYTES
    while scanned < ceiling:
        block = handle.read(min(1024 * 1024, ceiling - scanned))
        if not block:
            return None
        boundary = block.find(b"\n")
        if boundary >= 0:
            return start + scanned + boundary + 1
        scanned += len(block)
    return None


def import_window(
    *,
    now: float,
    store: CheckpointStore,
    state_dir: str | Path | None = None,
    retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    max_read_bytes: int = portable_history.MAX_READ_BYTES_PER_PASS,
    max_pass_bytes: int = portable_history.MAX_READ_BYTES_PER_PASS,
) -> ImportResult:
    """Read every source forward and project the window's records.

    Returns without persisting anything. :func:`commit` is the other half, and
    the gap between them is where the caller delivers.
    """
    window_start = max(0.0, now - max(0.0, retention_seconds))
    base = store.load()
    sources = discover_sources(state_dir)
    budget = max(1, max_pass_bytes)
    reads: list[SourceRead] = []
    for source in sources:
        checkpoint = base.get(source.key) or SourceCheckpoint(source_key=source.key)
        read = read_source(
            source,
            checkpoint,
            window_start=window_start,
            budget=budget,
            max_read_bytes=max(1, max_read_bytes),
        )
        budget -= read.read_bytes
        reads.append(read)
    return portable_history.combine(
        reads,
        base=base,
        window_start=window_start,
        window_end=now,
        sources=len(sources),
    )


def commit(store: CheckpointStore, result: ImportResult) -> None:
    """Persist the checkpoints a delivered import pass earned.

    Called only after the entries have been handed on. Until it is called the
    import has not happened, and calling it twice for one result is the same as
    calling it once.
    """
    store.save(result.checkpoints)


@dataclass(frozen=True)
class HistoryProjection:
    """One import, rendered both ways, from one set of entries."""

    result: ImportResult
    view: HistoryView
    batches: tuple[HistoryBatch, ...]


def project_history(
    *,
    now: float,
    store: CheckpointStore | None = None,
    state_dir: str | Path | None = None,
    retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    max_read_bytes: int = portable_history.MAX_READ_BYTES_PER_PASS,
    max_pass_bytes: int = portable_history.MAX_READ_BYTES_PER_PASS,
) -> HistoryProjection:
    """Import one window and render it for local display and for publication.

    The single entry point both consumers use. Neither is given the corpus, and
    neither re-derives anything: the view and the batches hold the same entry
    objects, so a change to how an ending is interpreted reaches both or
    neither.
    """
    checkpoints = store if store is not None else default_store(state_dir)
    result = import_window(
        now=now,
        store=checkpoints,
        state_dir=state_dir,
        retention_seconds=retention_seconds,
        max_read_bytes=max_read_bytes,
        max_pass_bytes=max_pass_bytes,
    )
    return HistoryProjection(
        result=result,
        view=portable_history.build_view(result),
        batches=portable_history.batches(result, now=now),
    )
