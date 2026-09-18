"""The connector observation runtime: what the Mac publishes, and when.

This is the half of the Mac connector that has no opinion about where its
material comes from. It owns four things and nothing else:

* **One instance.** :class:`SingleInstanceLock` is a pid file created with
  ``O_EXCL``, so two connectors racing for it resolve without either reading a
  half-written file, and a lock left behind by a dead owner is stolen rather
  than waited on forever.
* **Bounded, deterministic retry.** :class:`Backoff` doubles to a ceiling and
  carries no jitter, because a connector whose reconnect schedule cannot be
  asserted is a connector whose reconnect behaviour cannot be tested.
* **Publication order and checkpoint timing.** A cycle's catalog pages and
  history batches are delivered before the source is told to commit, and a
  cycle that fails part way through is discarded uncommitted. The next pass
  re-reads from the same checkpoint and the receiver's duplicate suppression
  absorbs the overlap -- the only ordering under which "reconnect without
  duplication" and "no ending is ever lost" are both true.
* **Heartbeats that do not wait for observation.** The pass that walks the
  corpus runs somewhere else (a thread by default, injectable for tests), and
  :meth:`ObservationConnector.step` schedules the heartbeat from the clock
  rather than from the pass. A Mac whose disk has gone slow reports that it is
  alive and degraded, instead of going silent and being read as lost.

What it does *not* own is equally deliberate. It never reads a plan, never
spawns a subprocess, and never names a path: the corpus reaches it through
:class:`ObservationSource`, whose only ASL-aware implementation lives in
``agent_session_loop.workbench_connector``. That is what lets the publication
rules be tested against a stub source, and what removed the Companion's
bridge-subprocess dependency -- the new connector calls the shared catalog,
observation and history services in process.

Every frame leaves through :mod:`asl_workbench.transport`'s builders, so a
payload assembled wrong here is refused here rather than at the far end. The
payload mappers go further and *probe* what they build: a catalog entry or a
history entry the transport would reject is dropped, the frame that carried it
says so, and the generation stops claiming to be complete. Shipping a page that
silently lost a plan while still claiming completeness is how a receiver
concludes a plan was deleted.
"""

from __future__ import annotations

import errno
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .catalog import CatalogEntry, CatalogPage, digest_of
from .history import PAUSE_REASON_OTHER, PAUSE_REASONS, HistoryBatch, HistoryEntry
from .observation import MAX_LABEL_CHARS, MAX_WARNINGS, SUPPORTED_CONTRACT_SET_IDS
from .transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_HISTORY,
    CAPABILITY_OBSERVE,
    MAX_CATALOG_ENTRIES_PER_PAGE,
    MAX_RUNS_PER_SNAPSHOT,
    MAX_WARNING_CHARS,
    TRANSPORT_PROTOCOL_VERSION,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
    TransportError,
    encode_frame,
    observation_envelope,
    validate_hello,
)

__all__ = [
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "CATALOG_PAGE_LIMIT",
    "CONNECTOR_CAPABILITIES",
    "CONNECTOR_STATES",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_OBSERVATION_INTERVAL_SECONDS",
    "LOCK_FILE_NAME",
    "MAX_PUBLISHED_RUNS",
    "STATE_BACKOFF",
    "STATE_READY",
    "STATE_STARTING",
    "STATE_STOPPED",
    "Backoff",
    "Channel",
    "ChannelError",
    "ConnectorError",
    "ObservationConnector",
    "ObservationCycle",
    "ObservationSource",
    "SingleInstanceLock",
    "catalog_entry_payload",
    "catalog_page_payload",
    "connector_hello",
    "heartbeat_payload",
    "history_batch_payload",
    "history_entry_payload",
    "pause_reason_word",
    "session_id_word",
    "snapshot_payload",
]


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ConnectorError(Exception):
    """The connector refused to start, or could not keep its own invariants."""


class ChannelError(ConnectorError):
    """One delivery attempt failed.

    Channels raise this and nothing else. A transport that let an ``OSError``
    through would make the runtime catch exception types it cannot classify,
    and "the network blinked" and "this code has a bug" would share a branch.
    """


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

#: The pid file one connector holds for one state directory.
LOCK_FILE_NAME = "workbench-connector.pid"

#: Nothing has been delivered yet.
STATE_STARTING = "starting"
#: The last delivery succeeded.
STATE_READY = "ready"
#: The last delivery failed; nothing is sent until the delay elapses.
STATE_BACKOFF = "backoff"
#: :meth:`ObservationConnector.stop` has run. Terminal.
STATE_STOPPED = "stopped"

CONNECTOR_STATES: tuple[str, ...] = (
    STATE_STARTING,
    STATE_READY,
    STATE_BACKOFF,
    STATE_STOPPED,
)

#: What this connector offers a service. ``detail`` and ``mutate`` are absent
#: because nothing here answers a correlated read or performs a mutation yet,
#: and a hello claiming them would have the service route traffic to a
#: capability this build does not implement.
CONNECTOR_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_OBSERVE,
    CAPABILITY_CATALOG,
    CAPABILITY_HISTORY,
)

#: How often a heartbeat is published when nothing else is. Kept well inside
#: ``ObservationPolicy.observation_stale_seconds`` so an ordinary schedule
#: never produces a connector the receiver reads as stale.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0

#: How often the corpus is walked. Slower than the heartbeat on purpose: the
#: walk is the expensive half, and the cheap half is what liveness is made of.
DEFAULT_OBSERVATION_INTERVAL_SECONDS = 30.0

#: The first retry delay, and the ceiling it doubles towards.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 60.0

#: How long the observation thread is given during shutdown before the
#: connector stops waiting for it. The pass holds no lock the next process
#: needs and writes nothing, so abandoning it costs a wasted walk and nothing
#: else.
WORKER_JOIN_TIMEOUT_SECONDS = 5.0

#: Re-exported so a caller sizing its own work does not have to reach into the
#: transport for the two limits that decide how a corpus is cut into frames.
CATALOG_PAGE_LIMIT = MAX_CATALOG_ENTRIES_PER_PAGE
MAX_PUBLISHED_RUNS = MAX_RUNS_PER_SNAPSHOT

#: The exponent is clamped so a connector left running for a very long time
#: cannot turn the doubling into an overflow.
_MAX_BACKOFF_EXPONENT = 32

#: Stand-in identity for the validation probes below. Never sent.
_PROBE_ID = "probe"

#: A dotted session number, as :mod:`asl_workbench.catalog` and
#: :mod:`asl_workbench.history` each spell it for their own fields. Copied
#: rather than shared for the same reason they are: it is the Companion's
#: ``SESSION_ID_RE``, and a module that validates against it has to be able to
#: state the rule it is enforcing rather than inherit one that might move.
_SESSION_ID_RE = re.compile(r"^[1-9][0-9]*(?:\.[1-9][0-9]*)*$")


# --------------------------------------------------------------------------
# Bounded retry
# --------------------------------------------------------------------------


class Backoff:
    """Deterministic exponential delay with a ceiling.

    No jitter. Jitter earns its keep when many clients retry against one
    service at once, and there is exactly one connector per Mac; what it would
    cost here is the ability to assert a reconnect schedule, which is a
    property this program has to be able to prove.
    """

    def __init__(
        self,
        *,
        base: float = BACKOFF_BASE_SECONDS,
        cap: float = BACKOFF_CAP_SECONDS,
    ) -> None:
        if base <= 0 or cap <= 0:
            raise ConnectorError("a backoff needs a positive base and ceiling")
        self.base = float(base)
        self.cap = float(cap)
        self.attempt = 0

    def delay(self) -> float:
        """The next delay, counting this attempt."""
        self.attempt += 1
        step = self.base * (2 ** min(self.attempt - 1, _MAX_BACKOFF_EXPONENT))
        return min(self.cap, step)

    def reset(self) -> None:
        """Forget the failures. Called when a delivery succeeds."""
        self.attempt = 0


# --------------------------------------------------------------------------
# Single-instance protection
# --------------------------------------------------------------------------


class SingleInstanceLock:
    """One connector per state directory, enforced by an exclusive pid file.

    Two connectors publishing under one connector id would interleave
    generations and checkpoints, and the receiver -- which keys everything by
    connector id -- would read the interleaving as a corpus that kept changing
    its mind. The file is created with ``O_CREAT | O_EXCL`` so the race is
    resolved by the filesystem, and a file whose recorded owner is gone is
    stolen rather than honoured, because otherwise a crash would need a human
    before the Mac could be observed again.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)

    def _owner(self) -> int | None:
        """The pid recorded in the file, or ``None`` when there is no usable one."""
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(32)
        except OSError:
            return None
        try:
            pid = int(raw.decode("ascii", "replace").strip())
        except ValueError:
            return None
        return pid if pid > 0 else None

    @staticmethod
    def _alive(pid: int) -> bool:
        """Whether ``pid`` is a live process.

        A signal this user may not send is proof the process exists, so
        ``PermissionError`` means alive. Anything else unexpected is treated as
        alive too: refusing to start is recoverable, and stealing a lock from a
        running connector is not.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as error:
            return error.errno != errno.ESRCH
        return True

    def _create(self) -> bool:
        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError as error:
            raise ConnectorError(
                f"could not create the connector lock: {error}"
            ) from error
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
        finally:
            os.close(handle)
        return True

    def acquire(self) -> None:
        """Take the lock, or raise :class:`ConnectorError`.

        Two passes, not a loop: the first tries to create the file, and the
        second tries again once a dead owner's file has been removed. A third
        pass would mean another process won the re-creation, which is the case
        this refuses rather than races against.
        """
        for _ in range(2):
            if self._create():
                return
            owner = self._owner()
            if owner is not None and self._alive(owner):
                raise ConnectorError(
                    "another workbench connector already holds the lock"
                )
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ConnectorError(
                    f"could not clear a stale connector lock: {error}"
                ) from error
        raise ConnectorError("another workbench connector already holds the lock")

    def release(self) -> None:
        """Drop the lock, but only if this process still owns it."""
        if self._owner() != os.getpid():
            return
        try:
            os.unlink(self.path)
        except OSError:
            return

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# What a source hands the runtime
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationCycle:
    """One complete pass over the corpus, ready to publish.

    The three views travel together because they were read together: a catalog
    generation, the active runs inside it, and the endings imported in the same
    window. Splitting them into three independently scheduled reads is how a
    dashboard ends up showing a run the catalog says does not exist.

    ``history`` is the pass the source will be asked to commit, carried as an
    opaque value: this module's rule is *when* to commit, and the source owns
    what committing means.
    """

    observed_at: float
    runs: tuple[Mapping[str, Any], ...] = ()
    catalog_pages: tuple[CatalogPage, ...] = ()
    history_batches: tuple[HistoryBatch, ...] = ()
    history: Any = None
    source_digest: str | None = None
    truncated: bool = False
    failed_projections: int = 0
    warnings: tuple[str, ...] = ()
    #: Set when the pass completed but could not see everything. A pass that
    #: failed outright is reported by raising instead.
    degraded_reason: str | None = None


class ObservationSource(Protocol):
    """Where a cycle comes from, and what committing one means.

    Two methods, and the gap between them is the whole checkpoint contract:
    :meth:`observe` may not persist anything, and :meth:`commit` is called only
    once every frame the cycle produced has been delivered.
    """

    def observe(self, now: float) -> ObservationCycle:
        """Read the corpus once, changing nothing on disk."""
        ...

    def commit(self, cycle: ObservationCycle) -> None:
        """Persist the checkpoints a fully delivered cycle earned."""
        ...


class Channel(Protocol):
    """One frame out. Raises :class:`ChannelError` and nothing else."""

    def send(self, frame: bytes) -> None:
        """Deliver one encoded envelope, or raise :class:`ChannelError`."""
        ...


# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------


def _bounded_warnings(warnings: Iterable[str]) -> list[str]:
    """The warnings a frame may carry, bounded in both directions.

    Bounded at all because the transport refuses a longer list, and bounded
    *here* rather than at the far end because a refused frame loses the
    observation as well as the warning.
    """
    bounded: list[str] = []
    for warning in warnings:
        if not warning:
            continue
        bounded.append(warning[:MAX_WARNING_CHARS])
        if len(bounded) == MAX_WARNINGS:
            break
    return bounded


def _refusal(payload: Mapping[str, Any]) -> str | None:
    """Why the transport would refuse this observation, or ``None``.

    The probe composes a throwaway envelope rather than reaching for a private
    validator, so what it checks is exactly what a real send checks and there
    is no second, drifting copy of the rules. The identities are stand-ins:
    the payload is the only part under examination.
    """
    try:
        observation_envelope(
            payload=payload,
            connector_id=_PROBE_ID,
            target_id=_PROBE_ID,
            sent_at=0.0,
        )
    except TransportError as error:
        return str(error)
    return None


def pause_reason_word(value: object) -> str | None:
    """A pause reason the transport will accept, or ``None`` when there is none.

    :mod:`asl_workbench.catalog` keeps a pause reason as any bounded slug,
    because ASL may add one at any time and a catalog that dropped the plan
    would be worse than one carrying a word the reader does not recognise. The
    transport's vocabulary is closed. Mapping the unknown onto ``other`` -- the
    same rule the Companion's own row projection uses -- keeps the plan and
    loses only the shade of meaning.
    """
    if not isinstance(value, str) or not value:
        return None
    return value if value in PAUSE_REASONS else PAUSE_REASON_OTHER


def session_id_word(value: object) -> str | None:
    """A session number the transport will accept, or ``None``.

    A run whose session identifier is not a dotted number -- a resplit label, a
    placeholder, anything a provider might carry -- publishes no session rather
    than publishing something the receiver would refuse the whole row over. The
    row's identity is its run key; the session number is what an operator reads
    off it.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _SESSION_ID_RE.match(text) else None


def heartbeat_payload(
    *,
    observed_at: float,
    bridge_alive: bool,
    contract_set_id: str | None = None,
    asl_version: str | None = None,
    degraded_reason: str | None = None,
    source_generation: int | None = None,
    projection_age_seconds: float | None = None,
) -> dict[str, Any]:
    """The cheap frame that says the Mac is still here.

    ``bridge_alive`` keeps its transport name and loses its subprocess: there
    is no bridge to be alive any more, so what it now reports is whether the
    in-process observation pass is producing cycles. The receiver's question
    was never "is there a child process", it was "is anything looking at this
    corpus", and that is the question this still answers.

    ``projection_age_seconds`` is sent only when there is a projection to age.
    A connector that has not completed a pass yet reports no age rather than an
    age of zero, which would claim a freshness it has not earned.
    """
    payload: dict[str, Any] = {
        "view": VIEW_HEARTBEAT,
        "observed_at": float(observed_at),
        "bridge_alive": bool(bridge_alive),
        "contract_set_id": contract_set_id or SUPPORTED_CONTRACT_SET_IDS[0],
    }
    if asl_version:
        payload["asl_version"] = asl_version
    if degraded_reason:
        payload["degraded_reason"] = degraded_reason[:MAX_LABEL_CHARS]
    if source_generation is not None:
        payload["source_generation"] = max(0, int(source_generation))
    if projection_age_seconds is not None:
        payload["projection_age_seconds"] = max(0.0, float(projection_age_seconds))
    return payload


def snapshot_payload(
    runs: Sequence[Mapping[str, Any]],
    *,
    observed_at: float,
    truncated: bool = False,
    source_digest: str | None = None,
    failed_projections: int = 0,
    source_generation: int | None = None,
    projection_age_seconds: float | None = None,
    warnings: Iterable[str] = (),
    contract_set_id: str | None = None,
) -> dict[str, Any]:
    """The active runs, digested by what a receiver can observe changing.

    A row the transport would refuse is dropped and counted in
    ``failed_projections`` rather than taking the frame down with it: one
    unprojectable plan must not cost the operator every other run on the
    machine.

    The digest covers the rows and the truncation flag and nothing else, so an
    idle corpus produces one digest for as long as it stays idle and the
    receiver's duplicate suppression does the rest. Folding in the observation
    moment would make every poll look like a change.
    """
    rows = [dict(run) for run in runs[:MAX_PUBLISHED_RUNS]]
    over = max(0, len(runs) - len(rows))
    collected = list(warnings)
    if over:
        truncated = True
        collected.append(f"snapshot: {over} rows above the frame's row limit")
    payload = _snapshot_frame(
        rows,
        observed_at=observed_at,
        truncated=truncated,
        source_digest=source_digest,
        failed_projections=failed_projections,
        source_generation=source_generation,
        projection_age_seconds=projection_age_seconds,
        warnings=collected,
        contract_set_id=contract_set_id,
    )
    if _refusal(payload) is None:
        return payload
    kept = [row for row in rows if _refusal(_probe_snapshot(row)) is None]
    refused = len(rows) - len(kept)
    collected.append(f"snapshot: {refused} rows could not be published")
    return _snapshot_frame(
        kept,
        observed_at=observed_at,
        truncated=truncated,
        source_digest=source_digest,
        failed_projections=failed_projections + refused,
        source_generation=source_generation,
        projection_age_seconds=projection_age_seconds,
        warnings=collected,
        contract_set_id=contract_set_id,
    )


def _snapshot_frame(
    rows: Sequence[Mapping[str, Any]],
    *,
    observed_at: float,
    truncated: bool,
    source_digest: str | None,
    failed_projections: int,
    source_generation: int | None,
    projection_age_seconds: float | None,
    warnings: Iterable[str],
    contract_set_id: str | None,
) -> dict[str, Any]:
    material = [dict(row) for row in rows]
    payload: dict[str, Any] = {
        "view": VIEW_SNAPSHOT,
        "observed_at": float(observed_at),
        "digest": digest_of([source_digest, bool(truncated), material]),
        "row_count": len(material),
        "truncated": bool(truncated),
        "runs": material,
        "contract_set_id": contract_set_id or SUPPORTED_CONTRACT_SET_IDS[0],
    }
    if source_digest is not None:
        payload["source_digest"] = source_digest
    if failed_projections:
        payload["failed_projections"] = max(0, int(failed_projections))
    if source_generation is not None:
        payload["source_generation"] = max(0, int(source_generation))
    if projection_age_seconds is not None:
        payload["projection_age_seconds"] = max(0.0, float(projection_age_seconds))
    bounded = _bounded_warnings(warnings)
    if bounded:
        payload["warnings"] = bounded
    return payload


def _probe_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    """A one-row snapshot, built only to find out whether the row is legal."""
    return {
        "view": VIEW_SNAPSHOT,
        "observed_at": 0.0,
        "digest": _PROBE_ID,
        "row_count": 1,
        "truncated": False,
        "runs": [dict(row)],
    }


def catalog_entry_payload(entry: CatalogEntry) -> dict[str, Any]:
    """One catalog entry in the shape the transport declares.

    Three coercions, each forced by a real disagreement between what the
    catalog may hold and what the wire may carry:

    * ``title`` is ``None`` for a plan whose manifest has no ``# H1``, and the
      transport requires a string. The empty string is what the store already
      reads as "no title", so that is what absence becomes.
    * ``pause_reason`` is an open slug locally and a closed word on the wire;
      see :func:`pause_reason_word`.
    * ``projects`` is bounded at ``catalog.MAX_PROJECTS_PER_PLAN`` locally and
      at the transport's warning bound on the wire, and the two numbers differ.
      Truncating the list keeps the plan; dropping the entry over a display
      field would lose it.
    """
    payload = dict(entry.to_payload())
    payload["title"] = entry.title or ""
    payload["pause_reason"] = pause_reason_word(entry.pause_reason)
    if len(entry.projects) > MAX_WARNINGS:
        payload["projects"] = list(entry.projects[:MAX_WARNINGS])
    return payload


def catalog_page_payload(
    page: CatalogPage,
    *,
    observed_at: float,
    source_generation: int | None = None,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    """One catalog page, in the shape the store ingests.

    Not :meth:`CatalogPage.to_payload`: that is the local shape, and it names
    the entry count ``count`` and carries the page limit, neither of which the
    transport declares. Translating here rather than widening the transport
    keeps the wire closed against a field nobody agreed to.

    An entry the transport refuses clears both ``complete`` and ``final``.
    Clearing ``complete`` alone would be rejected on arrival -- the transport
    enforces that a page closing a generation claims the generation was whole
    -- and that rule is right: retirement is decided from the two together, and
    a page that lost a plan must not retire the plan it lost.
    """
    entries = [catalog_entry_payload(entry) for entry in page.entries]
    collected = list(warnings)
    payload = _catalog_frame(
        entries,
        page=page,
        observed_at=observed_at,
        complete=page.complete,
        final=page.final,
        source_generation=source_generation,
        warnings=collected,
    )
    if _refusal(payload) is None:
        return payload
    kept = [entry for entry in entries if _refusal(_probe_catalog(entry)) is None]
    refused = len(entries) - len(kept)
    collected.append(f"catalog: {refused} entries could not be published")
    return _catalog_frame(
        kept,
        page=page,
        observed_at=observed_at,
        complete=False,
        final=False,
        source_generation=source_generation,
        warnings=collected,
    )


def _catalog_frame(
    entries: Sequence[Mapping[str, Any]],
    *,
    page: CatalogPage,
    observed_at: float,
    complete: bool,
    final: bool,
    source_generation: int | None,
    warnings: Iterable[str],
) -> dict[str, Any]:
    material = [dict(entry) for entry in entries]
    payload: dict[str, Any] = {
        "view": VIEW_CATALOG_PAGE,
        "observed_at": float(observed_at),
        "generation": page.generation,
        "complete": bool(complete),
        "final": bool(final),
        "offset": max(0, int(page.offset)),
        "entry_count": len(material),
        "entries": material,
        "total": max(0, int(page.total)),
    }
    if source_generation is not None:
        payload["source_generation"] = max(0, int(source_generation))
    bounded = _bounded_warnings(warnings)
    if bounded:
        payload["warnings"] = bounded
    return payload


def _probe_catalog(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A one-entry page, built only to find out whether the entry is legal."""
    return {
        "view": VIEW_CATALOG_PAGE,
        "observed_at": 0.0,
        "generation": _PROBE_ID,
        "complete": False,
        "final": False,
        "offset": 0,
        "entry_count": 1,
        "entries": [dict(entry)],
    }


def history_entry_payload(entry: HistoryEntry) -> dict[str, Any] | None:
    """One ending, or ``None`` when the transport requires more than it has.

    ``run_id`` is the case this exists for. An entry assembled from a terminal
    record that named no run still has a run *key* -- the key is a digest, and
    a digest of ``None`` is a perfectly good digest -- but it has no run id,
    and the transport requires one. Returning ``None`` lets the batch account
    for the loss instead of shipping a frame the receiver would refuse whole.
    """
    payload = dict(entry.to_payload())
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    return payload


def history_batch_payload(batch: HistoryBatch) -> dict[str, Any]:
    """One history batch, without the question bodies it was built beside.

    :meth:`HistoryBatch.to_payload` carries ``questions`` and ``question_count``
    for the local view. The transport has no name for either and would refuse
    the frame -- rightly: a question is prose, and prose reaches the Pi through
    a correlated detail read or not at all.

    A dropped entry clears ``complete``. A batch that quietly shipped nine of
    ten endings and still claimed to be complete is how a receiver concludes a
    run never finished.
    """
    entries: list[dict[str, Any]] = []
    dropped = 0
    for entry in batch.entries:
        published = history_entry_payload(entry)
        if published is None:
            dropped += 1
            continue
        entries.append(published)
    payload = _history_frame(entries, batch=batch, dropped=dropped)
    if _refusal(payload) is None:
        return payload
    kept = [entry for entry in entries if _refusal(_probe_history(entry)) is None]
    return _history_frame(
        kept, batch=batch, dropped=dropped + (len(entries) - len(kept))
    )


def _history_frame(
    entries: Sequence[Mapping[str, Any]],
    *,
    batch: HistoryBatch,
    dropped: int,
) -> dict[str, Any]:
    material = [dict(entry) for entry in entries]
    collected = list(batch.warnings)
    if dropped:
        collected.append(f"history: {dropped} entries could not be published")
    payload: dict[str, Any] = {
        "view": VIEW_HISTORY_BATCH,
        "batch_id": batch.batch_id,
        "generated_at": float(batch.generated_at),
        "window_start": float(batch.window_start),
        "window_end": float(batch.window_end),
        "entry_count": len(material),
        "complete": bool(batch.complete) and not dropped,
        "entries": material,
    }
    if batch.more:
        payload["more"] = True
    bounded = _bounded_warnings(collected)
    if bounded:
        payload["warnings"] = bounded
    return payload


def _probe_history(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A one-entry batch, built only to find out whether the entry is legal."""
    return {
        "view": VIEW_HISTORY_BATCH,
        "batch_id": _PROBE_ID,
        "generated_at": 0.0,
        "entry_count": 1,
        "complete": False,
        "entries": [dict(entry)],
    }


def connector_hello(
    *,
    connector_id: str,
    target_id: str,
    contract_set_id: str | None = None,
    asl_version: str | None = None,
    observation_only: bool = True,
    capabilities: Sequence[str] = CONNECTOR_CAPABILITIES,
) -> dict[str, Any]:
    """The announcement this connector opens a pairing with.

    Validated here, so a connector that would have been refused on arrival is
    refused before it has sent anything.
    """
    hello: dict[str, Any] = {
        "protocol": TRANSPORT_PROTOCOL_VERSION,
        "connector_id": connector_id,
        "target_id": target_id,
        "capabilities": list(capabilities),
        "contract_set_id": contract_set_id or SUPPORTED_CONTRACT_SET_IDS[0],
        "observation_only": bool(observation_only),
    }
    if asl_version:
        hello["asl_version"] = asl_version
    return validate_hello(hello)


# --------------------------------------------------------------------------
# The runtime
# --------------------------------------------------------------------------


@dataclass
class _Pass:
    """One observation attempt as the runtime sees it, finished or failed."""

    cycle: ObservationCycle | None = None
    error: str | None = None


@dataclass
class _Queue:
    """What is waiting to go out, in the order it goes.

    Heartbeat and snapshot are single slots rather than lists: a newer one
    replaces an older one because the older one described a moment nobody needs
    any more. Pages and batches are queues because each carries material the
    next one does not.
    """

    heartbeat: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)

    def cycle_drained(self) -> bool:
        return not self.pages and not self.batches

    def clear_cycle(self) -> None:
        self.pages.clear()
        self.batches.clear()


class ObservationConnector:
    """Publishes one Mac's observations, on a schedule it can be held to.

    Driven by :meth:`step`, which does at most one thing: it sends a single
    frame, or it discovers it is in backoff, or it finds nothing to send. A
    caller loops it against a real clock; a test calls it with the moments it
    wants to assert about, and the behaviour is identical because the moment is
    always an argument and never a reading.
    """

    def __init__(
        self,
        *,
        connector_id: str,
        target_id: str,
        source: ObservationSource,
        channel: Channel,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        observation_interval: float = DEFAULT_OBSERVATION_INTERVAL_SECONDS,
        backoff: Backoff | None = None,
        asl_version: str | None = None,
        run_observation: (
            Callable[[Callable[[], None]], threading.Thread | None] | None
        ) = None,
    ) -> None:
        if heartbeat_interval <= 0 or observation_interval <= 0:
            raise ConnectorError("the connector schedules need positive intervals")
        self.connector_id = connector_id
        self.target_id = target_id
        self.asl_version = asl_version
        self._source = source
        self._channel = channel
        self._heartbeat_interval = float(heartbeat_interval)
        self._observation_interval = float(observation_interval)
        self._backoff = backoff if backoff is not None else Backoff()
        self._run_observation = run_observation or _spawn_observation
        self._state = STATE_STARTING
        self._queue = _Queue()
        # The cycle whose frames are still in the queue. Held until the last of
        # them lands, because that is the moment it may be committed.
        self._cycle: ObservationCycle | None = None
        self._last_cycle: ObservationCycle | None = None
        self._degraded_reason: str | None = None
        # A session counter rather than a clock reading. The store's ordering
        # rule is "time decides, and the generation refuses only what time
        # cannot", so this has to move when a link is re-established and must
        # never pretend to be a timestamp.
        self._generation = 1
        self._next_heartbeat = 0.0
        self._next_observation = 0.0
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self._result: _Pass | None = None
        self._observing = False
        self._thread: threading.Thread | None = None

    # -- introspection ---------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def observing(self) -> bool:
        """Whether an observation pass is in flight."""
        with self._lock:
            return self._observing

    @property
    def retry_at(self) -> float:
        """When the next delivery may be attempted. Meaningful in backoff."""
        return self._retry_at

    def pending(self) -> int:
        """How many frames are waiting to be delivered."""
        queued = len(self._queue.pages) + len(self._queue.batches)
        if self._queue.heartbeat is not None:
            queued += 1
        if self._queue.snapshot is not None:
            queued += 1
        return queued

    def hello(self) -> dict[str, Any]:
        """This connector's announcement."""
        return connector_hello(
            connector_id=self.connector_id,
            target_id=self.target_id,
            asl_version=self.asl_version,
        )

    # -- the loop --------------------------------------------------------
    def step(self, now: float) -> str | None:
        """Service the schedules and deliver at most one frame.

        Returns the view that was sent, or ``None`` when nothing was: an empty
        queue, a backoff that has not elapsed, or a delivery that failed. One
        frame per step is what keeps a recovered link from turning a built-up
        queue into an unbounded burst.
        """
        if self._state == STATE_STOPPED:
            return None
        self._service(now)
        if self._state == STATE_BACKOFF and now < self._retry_at:
            return None
        selected = self._select()
        if selected is None:
            return None
        view, payload = selected
        try:
            frame = encode_frame(
                observation_envelope(
                    payload=payload,
                    connector_id=self.connector_id,
                    target_id=self.target_id,
                    sent_at=now,
                )
            )
        except TransportError as error:
            # A frame this connector built is one its own receiver would
            # refuse. The frame is dropped as well as the cycle: a refusal is a
            # property of the payload, so leaving it queued would refuse it
            # again on every step and the connector would never heartbeat
            # again. Nothing is committed, so the next pass reads the same
            # window.
            self._release(view)
            self._discard(f"unpublishable {view}: {error}")
            return None
        try:
            self._channel.send(frame)
        except ChannelError:
            self._fail(now)
            return None
        self._delivered(view)
        return view

    def stop(self) -> None:
        """Stop publishing and let an in-flight observation pass go.

        Terminal. Nothing queued is delivered afterwards and nothing is
        committed, which leaves exactly the state a crash would have left: an
        uncommitted window is re-read next time and the receiver absorbs the
        overlap. The alternative -- committing on the way out -- would turn a
        shutdown during delivery into permanently lost history.
        """
        if self._state == STATE_STOPPED:
            return
        self._state = STATE_STOPPED
        self._queue = _Queue()
        self._cycle = None
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(WORKER_JOIN_TIMEOUT_SECONDS)
        self._thread = None

    # -- scheduling ------------------------------------------------------
    def _service(self, now: float) -> None:
        """Collect a finished pass, start one if due, and schedule a heartbeat."""
        self._collect(now)
        if now >= self._next_observation and not self.observing:
            self._start_observation(now)
        if now >= self._next_heartbeat:
            self._queue.heartbeat = self._heartbeat(now)
            self._next_heartbeat = now + self._heartbeat_interval

    def _start_observation(self, now: float) -> None:
        with self._lock:
            if self._observing:
                return
            self._observing = True
        # Scheduled before the pass runs rather than after it lands: a walk
        # that takes longer than the interval must not be re-entered the
        # instant it finishes.
        self._next_observation = now + self._observation_interval

        def attempt() -> None:
            outcome = _Pass()
            try:
                outcome.cycle = self._source.observe(now)
            except Exception as error:  # a source is arbitrary provider code
                outcome.error = f"{type(error).__name__}: {error}"
            with self._lock:
                self._result = outcome
                self._observing = False

        self._thread = self._run_observation(attempt)

    def _collect(self, now: float) -> None:
        """Take a finished pass, if one is waiting, and queue what it produced."""
        with self._lock:
            outcome = self._result
            self._result = None
        if outcome is None:
            return
        if outcome.cycle is None:
            self._degraded_reason = outcome.error or "observation failed"
            return
        if not self._queue.cycle_drained():
            # The previous cycle is still going out. Dropping the new one keeps
            # one generation's pages together; the next pass will read a corpus
            # at least as current as this one.
            return
        self._adopt(outcome.cycle, now)

    def _adopt(self, cycle: ObservationCycle, now: float) -> None:
        self._degraded_reason = cycle.degraded_reason
        self._last_cycle = cycle
        age = max(0.0, now - cycle.observed_at)
        self._queue.snapshot = snapshot_payload(
            cycle.runs,
            observed_at=cycle.observed_at,
            truncated=cycle.truncated,
            source_digest=cycle.source_digest,
            failed_projections=cycle.failed_projections,
            source_generation=self._generation,
            projection_age_seconds=age,
            warnings=cycle.warnings,
        )
        self._queue.pages = [
            catalog_page_payload(
                page,
                observed_at=cycle.observed_at,
                source_generation=self._generation,
                # The scan's warnings ride the page that closes the generation:
                # they describe the enumeration rather than any one slice of
                # it, and repeating them on every page would crowd out a page's
                # own.
                warnings=cycle.warnings if page.final else (),
            )
            for page in cycle.catalog_pages
        ]
        self._queue.batches = [
            history_batch_payload(batch) for batch in cycle.history_batches
        ]
        # Held only while it still has frames to deliver. A cycle with neither
        # pages nor batches has nothing to commit and nothing to wait for.
        self._cycle = None if self._queue.cycle_drained() else cycle

    def _heartbeat(self, now: float) -> dict[str, Any]:
        last = self._last_cycle
        return heartbeat_payload(
            observed_at=now,
            bridge_alive=last is not None and self._degraded_reason is None,
            asl_version=self.asl_version,
            degraded_reason=self._degraded_reason,
            source_generation=self._generation,
            projection_age_seconds=(
                max(0.0, now - last.observed_at) if last is not None else None
            ),
        )

    # -- delivery --------------------------------------------------------
    def _select(self) -> tuple[str, dict[str, Any]] | None:
        """The next frame, in the order liveness outranks detail."""
        if self._queue.heartbeat is not None:
            return VIEW_HEARTBEAT, self._queue.heartbeat
        if self._queue.snapshot is not None:
            return VIEW_SNAPSHOT, self._queue.snapshot
        if self._queue.pages:
            return VIEW_CATALOG_PAGE, self._queue.pages[0]
        if self._queue.batches:
            return VIEW_HISTORY_BATCH, self._queue.batches[0]
        return None

    def _release(self, view: str) -> None:
        """Take the frame :meth:`_select` returned off the queue."""
        if view == VIEW_HEARTBEAT:
            self._queue.heartbeat = None
        elif view == VIEW_SNAPSHOT:
            self._queue.snapshot = None
        elif view == VIEW_CATALOG_PAGE:
            if self._queue.pages:
                self._queue.pages.pop(0)
        elif self._queue.batches:
            self._queue.batches.pop(0)

    def _delivered(self, view: str) -> None:
        self._release(view)
        if self._state == STATE_BACKOFF:
            # A link that just came back is a new session as far as the
            # receiver is concerned, and it has to be able to tell one from a
            # connection that never dropped without being told.
            self._generation += 1
        self._state = STATE_READY
        self._backoff.reset()
        if self._cycle is not None and self._queue.cycle_drained():
            # Every frame the cycle produced has landed. This is the only place
            # a checkpoint moves, and it is after delivery rather than after
            # reading -- which is what makes an interrupted pass replay instead
            # of disappear.
            committed = self._cycle
            self._cycle = None
            self._source.commit(committed)

    def _fail(self, now: float) -> None:
        """A delivery failed. Drop the cycle and wait out the backoff.

        The reason is deliberately not recorded in ``degraded_reason``: that
        field travels on a heartbeat, and a heartbeat describing a link failure
        can only be sent once the link works again, by which point it would be
        describing something that is no longer true.
        """
        self._queue.clear_cycle()
        self._cycle = None
        self._state = STATE_BACKOFF
        self._retry_at = now + self._backoff.delay()

    def _discard(self, reason: str) -> None:
        """Drop the cycle over a payload this connector should not have built."""
        self._queue.clear_cycle()
        self._cycle = None
        self._degraded_reason = reason[:MAX_LABEL_CHARS]


def _spawn_observation(attempt: Callable[[], None]) -> threading.Thread:
    """Run one observation pass off the delivery thread.

    A daemon thread: the pass reads and never writes, so an interpreter exiting
    while one is in flight loses a walk and nothing else.
    """
    thread = threading.Thread(target=attempt, name="workbench-observation", daemon=True)
    thread.start()
    return thread
