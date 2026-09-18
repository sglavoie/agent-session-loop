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
* **Answers to questions, on the same outward link.** A service with something
  to ask puts a ``request`` frame in the body of the acknowledgement of an
  upload; :class:`Channel` returns it, and the connector answers it later with
  a correlated ``detail`` or a ``receipt`` saying why not. The read itself runs
  on a worker and the answer waits in a lane of its own, which is what keeps
  three failure modes apart: a slow disk cannot silence the heartbeat, a corpus
  being paged out cannot bury a read somebody is waiting on, and a service
  reading continuously cannot stop the corpus from ever being published. Every
  correlated body is dropped the moment it is sent, expires, or the link goes,
  and none of it is ever written down.

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
from .operations import (
    DETAIL_BRIDGE_UNAVAILABLE,
    DETAIL_DEADLINE_PASSED,
    DETAIL_NO_RESULT,
    OPERATION_EXPIRED,
    OPERATION_UNCONFIRMED,
)
from .remote_actions import (
    BodyUnavailable,
    MutationQueue,
    MutationRequest,
    MutationResult,
    RemoteActionError,
    operation_payload,
    parse_submission,
)
from .transport import (
    CAPABILITY_BY_REQUEST,
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_HISTORY,
    CAPABILITY_MUTATE,
    CAPABILITY_OBSERVE,
    DIRECTION_DOWN,
    ERROR_DEADLINE,
    ERROR_FORBIDDEN_METHOD,
    ERROR_UNSUPPORTED_CAPABILITY,
    KIND_DETAIL,
    KIND_OPERATION,
    KIND_RECEIPT,
    MAX_CATALOG_ENTRIES_PER_PAGE,
    MAX_RUNS_PER_SNAPSHOT,
    MAX_WARNING_CHARS,
    RECEIPT_EXPIRED,
    RECEIPT_REFUSED,
    REQUEST_DETAIL,
    REQUEST_MUTATE,
    TRANSPORT_PROTOCOL_VERSION,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
    TransportError,
    detail_envelope,
    encode_frame,
    observation_envelope,
    operation_envelope,
    receipt_envelope,
    validate_frame,
    validate_hello,
)

__all__ = [
    "ACTIVE_READ_INTERVAL_SECONDS",
    "ACTIVE_READ_WINDOW_SECONDS",
    "BACKOFF_BASE_SECONDS",
    "BACKOFF_CAP_SECONDS",
    "CATALOG_PAGE_LIMIT",
    "CONNECTOR_CAPABILITIES",
    "CONNECTOR_STATES",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_OBSERVATION_INTERVAL_SECONDS",
    "LOCK_FILE_NAME",
    "MAX_PENDING_REQUESTS",
    "MAX_PUBLISHED_RUNS",
    "MAX_QUEUED_ANSWERS",
    "MUTATION_CAPABILITIES",
    "OBSERVATION_CAPABILITIES",
    "STATE_BACKOFF",
    "STATE_READY",
    "STATE_STARTING",
    "STATE_STOPPED",
    "Backoff",
    "Channel",
    "ChannelError",
    "ConnectorError",
    "DetailResponder",
    "MutationBodySource",
    "MutationPerformer",
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

#: What a connector offers before anything correlated is wired up: the three
#: views it publishes on its own schedule, and nothing that answers a question.
OBSERVATION_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_OBSERVE,
    CAPABILITY_CATALOG,
    CAPABILITY_HISTORY,
)

#: What this connector offers a service by default: the three views plus
#: ``detail``, because a :class:`DetailResponder` can answer a correlated read.
#: ``mutate`` is absent from the *default* rather than from the build -- see
#: :data:`MUTATION_CAPABILITIES` -- so a deployment that did not ask to be acted
#: on remotely stays observation-only whatever the far end would like to send.
#:
#: Declared is not the same as offered: an instance built without a responder
#: drops ``detail`` from its own hello rather than advertising a capability it
#: would have to refuse on arrival. A service reads
#: :attr:`ObservationConnector.capabilities`, never this tuple.
CONNECTOR_CAPABILITIES: tuple[str, ...] = (
    *OBSERVATION_CAPABILITIES,
    CAPABILITY_DETAIL,
)

#: What a connector offers once somebody has wired a :class:`MutationPerformer`
#: into it. Reaching this set is an act of configuration on the Mac, never a
#: request from the far end: ``mutate`` is what makes the hello stop saying
#: ``observation_only``, and the service refuses a mutating frame from an
#: observation-only pairing by construction. A connector that advertised it
#: without a performer would be inviting exactly the traffic it must refuse.
MUTATION_CAPABILITIES: tuple[str, ...] = (
    *CONNECTOR_CAPABILITIES,
    CAPABILITY_MUTATE,
)

#: How often a heartbeat is published when nothing else is. Kept well inside
#: ``ObservationPolicy.observation_stale_seconds`` so an ordinary schedule
#: never produces a connector the receiver reads as stale.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0

#: How often the corpus is walked. Slower than the heartbeat on purpose: the
#: walk is the expensive half, and the cheap half is what liveness is made of.
DEFAULT_OBSERVATION_INTERVAL_SECONDS = 30.0

#: The cadence the connector offers frames at while a service is reading, and
#: how long after the last request it keeps offering them.
#:
#: There is one channel and it points outward, so a request travels back on the
#: acknowledgement of a frame this connector sent: no frame, no question. At the
#: ordinary heartbeat cadence that would put a quarter of a minute in front of
#: every read, which is not a cockpit. So the link goes conversational while
#: somebody is using it and idles again when they stop -- the fast cadence costs
#: a small frame a second, and only for as long as it is being answered.
ACTIVE_READ_INTERVAL_SECONDS = 1.0
ACTIVE_READ_WINDOW_SECONDS = 30.0

#: How many requests may be waiting for the answering worker, and how many
#: finished answers may be waiting for the channel. Both are small: a request
#: arrives at most one per delivered frame, and a service that has filled either
#: queue is not reading a plan, it is looping.
MAX_PENDING_REQUESTS = 8
MAX_QUEUED_ANSWERS = 8

#: The frame kinds that share the answer lane, as opposed to the four views the
#: connector publishes on its own schedule. All three are things somebody is
#: owed rather than things this Mac decided to say.
_ANSWER_KINDS = (KIND_DETAIL, KIND_OPERATION, KIND_RECEIPT)

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
    """One frame out, and whatever the acknowledgement carried back.

    Raises :class:`ChannelError` and nothing else.

    The return value is what makes a correlated read possible over an outbound
    link: a service with something to ask puts a ``request`` frame in the body
    of the response to an upload, and the connector answers it on a later step.
    Returning ``None`` -- an acknowledgement with nothing in it -- is the
    ordinary case and the only thing a channel written before this existed ever
    did, so the widening costs no implementation anything.

    A second inbound socket would have been the alternative, and it is the one
    thing the deployment cannot have: the Mac is behind a router nobody
    administers, which is why the link points outward in the first place.
    """

    def send(self, frame: bytes) -> bytes | None:
        """Deliver one encoded envelope and return any frame carried back.

        Raises :class:`ChannelError` if the frame did not land.
        """
        ...


class DetailResponder(Protocol):
    """Answers one correlated detail read, off the delivery thread.

    The only opening in this module for ASL-aware code. A responder is handed
    the request payload the far end sent -- never a path, never a handle it
    could use to reach anything else -- and returns a payload in the shape
    :data:`asl_workbench.transport.DETAIL_SHAPE` declares, including for a read
    that found nothing: a refusal carries ``reason``, and a refusal is still an
    answer.

    :meth:`answer` runs on a worker, so it may block on a disk without the
    heartbeat going quiet behind it. An exception escaping it is caught and
    turned into a receipt, because a provider bug must cost the caller one read
    rather than the whole connector.
    """

    def answer(self, request: Mapping[str, Any], *, now: float) -> Mapping[str, Any]:
        """The detail payload answering one ``detail.read`` request."""
        ...


class MutationPerformer(Protocol):
    """Carries out one submitted mutation, off the delivery thread.

    The second and last opening for ASL-aware code, and a deliberately narrower
    one than :class:`DetailResponder`: it is handed a parsed
    :class:`~asl_workbench.remote_actions.MutationRequest` and the body that was
    redeemed for it, and it answers with the status ASL reached. It decides
    nothing about the transport and the transport decides nothing about ASL.

    The implementation is responsible for journalling *before* the side effect,
    which is what makes :func:`~asl_workbench.remote_actions.restart_settlement`
    able to tell a mutation that certainly did not run from one that may have.

    An exception escaping :meth:`perform` is caught and reported ``unconfirmed``
    rather than failed, because a performer that raised part-way through is
    exactly the case nobody can say the outcome of.
    """

    def perform(
        self, request: MutationRequest, *, body: str | None, now: float
    ) -> MutationResult:
        """Carry out one mutation and report what ASL settled it as."""
        ...

    def recover(self, *, now: float) -> Sequence[Mapping[str, Any]]:
        """Close out what a previous connector left open, and report it.

        Returns ``operation`` payloads for rows this process settled on the way
        up. Nothing is re-attempted: a mutation whose outcome is unknown is
        reported unknown.
        """
        ...


class MutationBodySource(Protocol):
    """Redeems the prose a mutation needs, at the moment it is needed.

    Kept off :class:`MutationPerformer` on purpose. The body lives at the far
    end and is fetched over the link; the performer is ASL and knows nothing
    about links. Splitting them is also what keeps the body inside a single
    stack frame -- fetched here, passed to ``perform``, gone when it returns --
    so no pending command ever holds prose that a restart could carry forward.

    Raises :class:`~asl_workbench.remote_actions.BodyUnavailable` when the body
    cannot be had, which settles the mutation without attempting it.
    """

    def fetch(self, request: MutationRequest, *, now: float) -> str:
        """The prose ``request.reply_ref`` names, redeemed once."""
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
class _Request:
    """One correlated read this connector has taken in but not yet answered."""

    request_id: str
    payload: dict[str, Any]
    #: The far end's own deadline. A request found past it is answered
    #: ``expired`` *without the read being performed*: the material would be
    #: built for a caller that has already stopped waiting, and building it
    #: would spend the disk on nobody.
    deadline: float | None = None


@dataclass
class _Answer:
    """One finished answer waiting for the channel.

    ``kind`` is a detail envelope carrying material, a receipt declining to, or
    an operation reporting what became of a mutation. The first two are
    correlated by ``request_id`` and both settle a read -- a request that
    produced neither would leave the far end waiting out a deadline for an
    answer this end already decided not to send.

    An operation has no ``request_id``: a mutation is correlated by its
    ``operation_id``, which is the only identifier that still means anything
    after the connector that took the submission in has been restarted.
    """

    request_id: str | None
    kind: str
    payload: dict[str, Any]
    deadline: float | None = None
    #: Which capability a receipt is declining on behalf of. A refused read and
    #: a refused mutation are different refusals and the far end routes them
    #: differently, so the word is carried rather than assumed.
    capability: str = CAPABILITY_DETAIL

    def forget(self) -> None:
        """Drop whatever body this answer is holding.

        Called wherever an answer stops being deliverable. The payload is this
        connector's own dict, so emptying it releases the only reference to the
        text that was read -- which is the whole of "clear bodies after expiry
        or disconnect" on this end of the link.
        """
        self.payload.clear()


@dataclass
class _Queue:
    """What is waiting to go out, in the order it goes.

    Heartbeat and snapshot are single slots rather than lists: a newer one
    replaces an older one because the older one described a moment nobody needs
    any more. Pages and batches are queues because each carries material the
    next one does not. Answers are a queue of their own rather than a fifth
    observation view: they are correlated to a question somebody is waiting on,
    and burying one behind a hundred catalog pages is the starvation this lane
    exists to prevent.
    """

    heartbeat: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    answers: list[_Answer] = field(default_factory=list)

    def cycle_drained(self) -> bool:
        return not self.pages and not self.batches

    def cycle_waiting(self) -> bool:
        return self.snapshot is not None or bool(self.pages) or bool(self.batches)

    def clear_cycle(self) -> None:
        self.pages.clear()
        self.batches.clear()

    def clear_answers(self) -> None:
        for answer in self.answers:
            answer.forget()
        self.answers.clear()

    def drop_correlated(self) -> None:
        """Drop the answers a lost link made pointless, and keep the rest.

        A detail or a receipt answers a question whose asker has already been
        told the link went down, so delivering one after the reconnect would
        hand a service material it reported as offline. An operation is not an
        answer to a waiter: it is the only record this end has of what ASL did
        to somebody's plan, and the far end can still absorb it late -- an
        ``unconfirmed`` command settling once the link returns is precisely the
        reconciliation its state machine keeps a door open for.
        """
        live: list[_Answer] = []
        for answer in self.answers:
            if answer.kind == KIND_OPERATION:
                live.append(answer)
                continue
            answer.forget()
        self.answers = live


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
        responder: DetailResponder | None = None,
        performer: MutationPerformer | None = None,
        body_source: MutationBodySource | None = None,
        run_observation: (
            Callable[[Callable[[], None]], threading.Thread | None] | None
        ) = None,
        run_answer: (
            Callable[[Callable[[], None]], threading.Thread | None] | None
        ) = None,
        run_mutation: (
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
        self._responder = responder
        self._performer = performer
        self._body_source = body_source
        # Derived rather than declared: a connector with nothing to read from
        # says so in its hello instead of accepting reads it would refuse, and
        # one with nothing that performs a mutation stays observation-only.
        offered: list[str] = list(OBSERVATION_CAPABILITIES)
        if responder is not None:
            offered.append(CAPABILITY_DETAIL)
        if performer is not None:
            offered.append(CAPABILITY_MUTATE)
        self.capabilities: tuple[str, ...] = tuple(offered)
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
        # -- the correlated half ------------------------------------------
        self._run_answer = run_answer or _spawn_answer
        self._requests: list[_Request] = []
        self._answering: _Request | None = None
        self._answer_result: _Answer | None = None
        self._answer_thread: threading.Thread | None = None
        self._answer_fault: str | None = None
        self._request_fault: str | None = None
        self._last_request_at: float | None = None
        # Which lane went last, so a busy reader and a busy corpus take turns
        # instead of one of them never being reached.
        self._answered_last = False
        # -- the mutation half --------------------------------------------
        # Its own queue and its own worker, never the read ones. A mutation
        # waits on ASL taking an action and a read waits on a disk, and putting
        # them in one lane would let a slow archive hold up a cockpit's refresh
        # -- or, worse, let a flood of reads run a command's window out.
        self._run_mutation = run_mutation or _spawn_mutation
        self._mutations = MutationQueue()
        self._mutating: MutationRequest | None = None
        self._mutation_result: _Answer | None = None
        self._mutation_thread: threading.Thread | None = None
        self._mutation_fault: str | None = None
        self._recovered = False

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

    @property
    def answer_fault(self) -> str | None:
        """Why the last answer was dropped, or ``None``.

        Deliberately not ``degraded_reason``: that word describes the
        observation the heartbeat is carrying, and an unsendable answer says
        nothing about whether the corpus was read. Reporting one as the other
        would have a single malformed read make a healthy Mac look degraded, and
        the far end's own deadline is already the recovery path.
        """
        return self._answer_fault

    @property
    def request_fault(self) -> str | None:
        """Why the last carried request was not taken in, or ``None``."""
        return self._request_fault

    @property
    def answering(self) -> bool:
        """Whether a read is being performed right now."""
        with self._lock:
            return self._answering is not None

    def pending(self) -> int:
        """How many frames are waiting to be delivered."""
        queued = len(self._queue.pages) + len(self._queue.batches)
        queued += len(self._queue.answers)
        if self._queue.heartbeat is not None:
            queued += 1
        if self._queue.snapshot is not None:
            queued += 1
        return queued

    def pending_reads(self) -> int:
        """How many correlated reads are taken in and not yet answered."""
        with self._lock:
            in_flight = 1 if self._answering is not None else 0
        return len(self._requests) + in_flight + len(self._queue.answers)

    def pending_mutations(self) -> int:
        """How many submitted mutations are in hand and not yet settled."""
        return self._mutations.pending + self._mutations.attempting

    @property
    def mutation_fault(self) -> str | None:
        """Why the last mutation could not be carried out, or ``None``.

        Separate from :attr:`answer_fault` for the same reason that one is
        separate from ``degraded_reason``: a performer that raised says nothing
        about whether reads work or the corpus was walked.
        """
        return self._mutation_fault

    @property
    def observation_only(self) -> bool:
        """Whether this connector will act on anything the far end sends."""
        return CAPABILITY_MUTATE not in self.capabilities

    def hello(self) -> dict[str, Any]:
        """This connector's announcement.

        ``observation_only`` is read off the capabilities rather than passed in:
        the two are the same claim said twice, and a hello that offered
        ``mutate`` while calling itself observation-only would be asking the
        service to refuse every frame it then sent.
        """
        return connector_hello(
            connector_id=self.connector_id,
            target_id=self.target_id,
            asl_version=self.asl_version,
            observation_only=self.observation_only,
            capabilities=self.capabilities,
        )

    def recover(self, now: float) -> int:
        """Settle and report what a previous connector left open.

        Called once, before the loop starts, because that is the only moment at
        which "every open row belongs to a process that is gone" is something
        this connector can act on. Returns how many rows were closed out.

        Nothing is re-attempted. A row that was journalled and never dispatched
        is reported ``expired``; one that got further is reported
        ``unconfirmed``. Carrying a pending command forward and running it now
        -- against state that has moved, on behalf of somebody who has stopped
        waiting -- is the single failure the durable journal exists to make
        impossible.
        """
        if self._performer is None or self._recovered:
            return 0
        self._recovered = True
        try:
            settled = self._performer.recover(now=now)
        except Exception as error:  # a performer is arbitrary provider code
            self._mutation_fault = f"{type(error).__name__}: {error}"[:MAX_LABEL_CHARS]
            return 0
        for payload in settled:
            self._enqueue(
                _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=dict(payload),
                    capability=CAPABILITY_MUTATE,
                )
            )
        return len(settled)

    # -- the loop --------------------------------------------------------
    def step(self, now: float) -> str | None:
        """Service the schedules and deliver at most one frame.

        Returns the lane that was sent -- an observation view, ``detail`` or
        ``receipt`` -- or ``None`` when nothing was: an empty queue, a backoff
        that has not elapsed, or a delivery that failed. One frame per step is
        what keeps a recovered link from turning a built-up queue into an
        unbounded burst.

        Whatever the acknowledgement carried back is taken in here, which is the
        only way a question reaches this process. It is taken in *after* the
        delivery succeeded, so a request riding a response this connector never
        managed to read is simply never seen -- the far end's deadline covers
        it, and guessing would mean answering a question that may not have been
        asked.
        """
        if self._state == STATE_STOPPED:
            return None
        self._service(now)
        if self._state == STATE_BACKOFF and now < self._retry_at:
            return None
        selected = self._select()
        if selected is None:
            return None
        lane, payload, request_id, capability = selected
        try:
            frame = encode_frame(
                self._envelope(lane, payload, request_id, now, capability)
            )
        except TransportError as error:
            self._unpublishable(lane, error)
            return None
        try:
            carried = self._channel.send(frame)
        except ChannelError:
            self._fail(now)
            return None
        self._delivered(lane)
        self._receive(carried, now)
        return lane

    def _envelope(
        self,
        lane: str,
        payload: Mapping[str, Any],
        request_id: str | None,
        now: float,
        capability: str = CAPABILITY_DETAIL,
    ) -> dict[str, Any]:
        """The envelope for one selected frame, built by its own lane's rules."""
        if lane == KIND_DETAIL:
            assert request_id is not None
            return detail_envelope(
                payload=payload,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=request_id,
            )
        if lane == KIND_OPERATION:
            return operation_envelope(
                payload=payload,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=request_id,
            )
        if lane == KIND_RECEIPT:
            assert request_id is not None
            return receipt_envelope(
                payload=payload,
                capability=capability,
                connector_id=self.connector_id,
                target_id=self.target_id,
                sent_at=now,
                request_id=request_id,
            )
        return observation_envelope(
            payload=payload,
            connector_id=self.connector_id,
            target_id=self.target_id,
            sent_at=now,
        )

    def _unpublishable(self, lane: str, error: TransportError) -> None:
        """A frame this connector built is one its own receiver would refuse.

        An observation takes the cycle down with it: a refusal is a property of
        the payload, so leaving it queued would refuse it again on every step
        and the connector would never heartbeat again. Nothing is committed, so
        the next pass reads the same window.

        An answer takes only itself. The cycle is untouched and the heartbeat
        keeps claiming what is true, because one unencodable read says nothing
        about whether the corpus was walked -- and the caller's deadline is
        already waiting to report it.
        """
        self._release(lane)
        if lane in _ANSWER_KINDS:
            self._answer_fault = f"unsendable {lane}: {error}"[:MAX_LABEL_CHARS]
            return
        self._discard(f"unpublishable {lane}: {error}")

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
        # Bodies first, then the queue: a stopped connector holds no material,
        # and the process that replaces it starts with nothing to leak. Every
        # read this one had taken in is simply never answered, which is what the
        # far end's deadline is for.
        self._queue.clear_answers()
        self._forget_reads()
        # Every command still in hand goes with them, body references and
        # redemption tokens first. A stopped connector holds no authority to
        # act, so the only mutation that can still be running is the one already
        # handed to a worker -- and nothing restarts it.
        self._mutations.clear()
        self._queue = _Queue()
        self._cycle = None
        for thread in (self._thread, self._answer_thread, self._mutation_thread):
            if thread is not None and thread.is_alive():
                thread.join(WORKER_JOIN_TIMEOUT_SECONDS)
        self._thread = None
        self._answer_thread = None
        self._mutation_thread = None
        # After the join rather than in ``_forget_reads``: everywhere else that
        # abandons reads, the worker is still running and clearing this slot
        # would let a second read start beside it. Here there is no second read
        # and no next step, so the last thing a stopped connector is holding
        # goes too.
        with self._lock:
            self._answering = None
            self._answer_result = None
            self._mutating = None
            self._mutation_result = None

    # -- scheduling ------------------------------------------------------
    def _service(self, now: float) -> None:
        """Collect what the workers finished, start what is due, keep liveness."""
        self._collect(now)
        self._collect_answer()
        self._collect_mutation()
        self._expire_reads(now)
        self._expire_mutations(now)
        if now >= self._next_observation and not self.observing:
            self._start_observation(now)
        self._start_answer(now)
        self._start_mutation(now)
        if now >= self._next_heartbeat:
            self._queue.heartbeat = self._heartbeat(now)
            self._next_heartbeat = now + self._carrier_interval(now)

    def _carrier_interval(self, now: float) -> float:
        """How long until the next heartbeat, given whether anyone is reading.

        The heartbeat is also the carrier a request rides back on, so its
        cadence is the floor on read latency. It tightens while a service is
        asking and relaxes once it stops.
        """
        asked = self._last_request_at
        if asked is not None and now - asked <= ACTIVE_READ_WINDOW_SECONDS:
            return min(self._heartbeat_interval, ACTIVE_READ_INTERVAL_SECONDS)
        return self._heartbeat_interval

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

    # -- correlated reads ------------------------------------------------
    def _receive(self, carried: bytes | None, now: float) -> None:
        """Take in whatever the acknowledgement carried back.

        Everything is checked before anything is believed: the frame against the
        transport, its direction against ``down``, its target against this
        deployment, and its connector id against this connector. A service that
        is paired with somebody else does not get to queue work here.
        """
        if not carried:
            return
        try:
            envelope = validate_frame(
                carried, direction=DIRECTION_DOWN, target_id=self.target_id
            )
        except TransportError as error:
            self._request_fault = f"undecodable request: {error}"[:MAX_LABEL_CHARS]
            return
        if envelope.get("connector_id") != self.connector_id:
            self._request_fault = "request addressed to another connector"
            return
        request_id = envelope.get("request_id")
        if not isinstance(request_id, str):
            self._request_fault = "request without a correlation id"
            return
        self._request_fault = None
        self._last_request_at = now
        # Pull the next carrier in. The heartbeat that brought this request was
        # scheduled before anybody was asking, so leaving its successor where
        # the idle cadence put it would make the *first* read of a quiet period
        # wait out a whole heartbeat interval -- which is exactly the read a
        # person is sitting in front of.
        self._next_heartbeat = min(
            self._next_heartbeat, now + self._carrier_interval(now)
        )
        if self._known(request_id):
            # A resend of something already in hand. Dropped rather than
            # queued twice: the answer is correlated, so one is all the far end
            # can match, and a second would be a body built for nobody.
            return
        request = _Request(
            request_id=request_id,
            payload=dict(envelope["payload"]),
            deadline=envelope.get("deadline"),
        )
        refusal = self._decline(request, now)
        if refusal is not None:
            self._enqueue(refusal)
            return
        if request.payload.get("request") == REQUEST_MUTATE:
            self._submit(request, now)
            return
        if len(self._requests) >= MAX_PENDING_REQUESTS:
            self._enqueue(
                self._receipt(request, RECEIPT_REFUSED, DETAIL_BRIDGE_UNAVAILABLE, now)
            )
            return
        self._requests.append(request)

    def _known(self, request_id: str) -> bool:
        """Whether this correlation id is already somewhere in the pipeline.

        All four places it can be, including the finished-but-not-yet-collected
        slot: a worker hands its answer back between two steps, and a resend
        arriving in that gap would otherwise be read as a new question and the
        disk walked a second time for a caller who is about to be answered.
        """
        with self._lock:
            in_flight = self._answering
            finished = self._answer_result
        return (
            any(pending.request_id == request_id for pending in self._requests)
            or (in_flight is not None and in_flight.request_id == request_id)
            or (finished is not None and finished.request_id == request_id)
            or any(done.request_id == request_id for done in self._queue.answers)
        )

    def _decline(self, request: _Request, now: float) -> _Answer | None:
        """The receipt refusing this request outright, or ``None`` to take it.

        This is where detail traffic is kept apart from mutation delivery. The
        two are told apart here and never meet again: a ``detail.read`` goes to
        the read queue and the answering worker, an ``operation.submit`` goes to
        the mutation queue and its own worker, and neither can be starved or
        delayed by a backlog of the other.

        A capability this connector did not offer is refused on arrival with the
        word that says which one is missing, so a service that ignored the hello
        gets an answer it can route rather than silence.
        """
        method = request.payload.get("request")
        capability = CAPABILITY_BY_REQUEST.get(str(method))
        if capability is None or capability not in self.capabilities:
            return self._receipt(
                request,
                RECEIPT_REFUSED,
                ERROR_UNSUPPORTED_CAPABILITY,
                now,
                capability=capability or CAPABILITY_DETAIL,
            )
        if method == REQUEST_DETAIL:
            if self._responder is None:
                return self._receipt(
                    request, RECEIPT_REFUSED, ERROR_UNSUPPORTED_CAPABILITY, now
                )
            if request.deadline is not None and now >= request.deadline:
                # Answered expired without the read being performed. The
                # material would be built for a caller that has already given up.
                return self._receipt(request, RECEIPT_EXPIRED, ERROR_DEADLINE, now)
            return None
        if method == REQUEST_MUTATE:
            if self._performer is None:
                return self._receipt(
                    request,
                    RECEIPT_REFUSED,
                    ERROR_UNSUPPORTED_CAPABILITY,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            if request.deadline is not None and now >= request.deadline:
                # The window closed in flight. Refused without being journalled,
                # which is what makes ``expired`` here mean *it did not run*.
                return self._receipt(
                    request,
                    RECEIPT_EXPIRED,
                    ERROR_DEADLINE,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            return None
        return self._receipt(request, RECEIPT_REFUSED, ERROR_FORBIDDEN_METHOD, now)

    def _receipt(
        self,
        request: _Request,
        outcome: str,
        detail_code: str,
        now: float,
        *,
        capability: str = CAPABILITY_DETAIL,
    ) -> _Answer:
        """One correlated refusal, in the shape a receipt declares."""
        return _Answer(
            request_id=request.request_id,
            kind=KIND_RECEIPT,
            payload={
                "outcome": outcome,
                "observed_at": now,
                "detail_code": detail_code,
            },
            deadline=request.deadline,
            capability=capability,
        )

    # -- mutation delivery -----------------------------------------------
    def _submit(self, request: _Request, now: float) -> None:
        """Take one ``operation.submit`` into the mutation queue, or refuse it.

        Refusals here are the ones that can still be made honestly, because
        nothing has been journalled and nothing has been attempted: a method
        this connector will not carry, a submission missing the authority its
        method needs, a window that has already closed, a queue that is full.
        Every one of them is a receipt the service can act on immediately.

        A resubmission of a command already in hand is dropped without a
        receipt. The answer the service is waiting for is the operation's
        outcome, correlated by ``operation_id``, and it is coming; a second copy
        would only mean carrying the same mutation out twice.
        """
        try:
            submission = parse_submission(
                request.payload, now=now, deadline=request.deadline
            )
        except RemoteActionError as error:
            self._enqueue(
                self._receipt(
                    request,
                    error.outcome,
                    error.detail_code,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            )
            return
        try:
            accepted = self._mutations.accept(submission, now=now)
        except RemoteActionError as error:
            self._enqueue(
                self._receipt(
                    request,
                    error.outcome,
                    error.detail_code,
                    now,
                    capability=CAPABILITY_MUTATE,
                )
            )
            return
        if not accepted:
            return
        # Pull the carrier in: the operation frame reporting this command rides
        # the same outbound schedule every other answer does, and a service that
        # has just asked for an action is the one least willing to wait.
        self._next_heartbeat = min(
            self._next_heartbeat, now + self._carrier_interval(now)
        )

    def _cancel(self, operation_id: str) -> bool:
        """Withdraw a submitted mutation that has not been attempted yet.

        ``False`` when there was nothing to withdraw, including when the command
        is already being carried out -- which is not cancellable, because the
        attempt is running and its result is the only honest answer left.
        """
        return self._mutations.cancel(operation_id) is not None

    def _start_mutation(self, now: float) -> None:
        """Carry out the oldest submitted mutation, on a worker, one at a time.

        One at a time for a stronger reason than reads are: two mutations
        running together are two actions on plans whose state each was decided
        against, and serialising them is what keeps "the remote action behaves
        like the local one" true of a burst as well as of a single command.
        """
        if self._performer is None:
            return
        if len(self._queue.answers) >= MAX_QUEUED_ANSWERS:
            return
        with self._lock:
            if self._mutating is not None:
                return
        request = self._mutations.next_attempt(now)
        if request is None:
            return
        with self._lock:
            self._mutating = request
        performer = self._performer
        source = self._body_source

        def attempt() -> None:
            fault: str | None = None
            body: str | None = None
            try:
                if request.bears_body:
                    if source is None:
                        raise BodyUnavailable("no way to redeem this command's body")
                    body = source.fetch(request, now=now)
                result = performer.perform(request, body=body, now=now)
            except BodyUnavailable as error:
                # Nothing was attempted, so the one word that is certainly true
                # is that it did not run.
                result = MutationResult(
                    status=OPERATION_EXPIRED, detail_code=error.detail_code
                )
                fault = error.message[:MAX_LABEL_CHARS]
            except Exception as error:  # a performer is arbitrary provider code
                # It may have acted before it raised, and nobody can say which.
                # ``unconfirmed`` is the only answer that is not a guess.
                result = MutationResult(
                    status=OPERATION_UNCONFIRMED, detail_code=DETAIL_NO_RESULT
                )
                fault = f"{type(error).__name__}: {error}"[:MAX_LABEL_CHARS]
            finally:
                # The body dies with the frame that used it, delivered or not.
                body = None
            answer = _Answer(
                request_id=None,
                kind=KIND_OPERATION,
                payload=operation_payload(request, result, observed_at=now),
                capability=CAPABILITY_MUTATE,
            )
            with self._lock:
                self._mutation_result = answer
                self._mutating = None
                if fault is not None:
                    self._mutation_fault = fault

        self._mutation_thread = self._run_mutation(attempt)

    def _collect_mutation(self) -> None:
        """Take a finished mutation's report into the outbound lane."""
        with self._lock:
            answer = self._mutation_result
            self._mutation_result = None
        if answer is None:
            return
        operation_id = answer.payload.get("operation_id")
        if isinstance(operation_id, str):
            self._mutations.settle(operation_id)
        self._enqueue(answer)

    def _expire_mutations(self, now: float) -> None:
        """Report every waiting command whose delivery window has closed.

        Only waiting ones. A command being carried out is past the point where
        ``expired`` could still be true, and saying it anyway would tell an
        owner their action did not happen while it was happening.
        """
        for request in self._mutations.expire(now):
            self._enqueue(
                _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=operation_payload(
                        request,
                        MutationResult(
                            status=OPERATION_EXPIRED,
                            detail_code=DETAIL_DEADLINE_PASSED,
                        ),
                        observed_at=now,
                    ),
                    capability=CAPABILITY_MUTATE,
                )
            )

    def _enqueue(self, answer: _Answer) -> None:
        """Put one finished answer in the lane, or drop it if the lane is full."""
        if len(self._queue.answers) >= MAX_QUEUED_ANSWERS:
            answer.forget()
            self._answer_fault = "the answer queue is full"
            return
        self._queue.answers.append(answer)

    def _start_answer(self, now: float) -> None:
        """Perform the oldest taken-in read, on a worker, one at a time.

        One at a time because the worker is reading a disk and a fan-out of
        them is how a cockpit turns a slow mount into a stalled Mac. Off the
        delivery thread for the reason observation is: a read that blocks must
        not take the heartbeat down with it.
        """
        if not self._requests or self._responder is None:
            return
        if len(self._queue.answers) >= MAX_QUEUED_ANSWERS:
            return
        with self._lock:
            if self._answering is not None:
                return
            request = self._requests.pop(0)
            self._answering = request
        responder = self._responder

        def attempt() -> None:
            answer: _Answer
            try:
                payload = responder.answer(request.payload, now=now)
                answer = _Answer(
                    request_id=request.request_id,
                    kind=KIND_DETAIL,
                    payload=dict(payload),
                    deadline=request.deadline,
                )
            except Exception as error:  # a responder is arbitrary provider code
                # Still an answer. A provider bug costs the caller one read
                # rather than a deadline's worth of silence, and the code says
                # the connector could not reach what it was asked for.
                answer = self._receipt(
                    request, RECEIPT_REFUSED, DETAIL_BRIDGE_UNAVAILABLE, now
                )
                fault: str | None = f"{type(error).__name__}: {error}"[:MAX_LABEL_CHARS]
            else:
                fault = None
            with self._lock:
                self._answer_result = answer
                self._answering = None
                if fault is not None:
                    self._answer_fault = fault

        self._answer_thread = self._run_answer(attempt)

    def _collect_answer(self) -> None:
        """Take the finished answer, if there is one, into the outbound lane."""
        with self._lock:
            answer = self._answer_result
            self._answer_result = None
        if answer is not None:
            self._enqueue(answer)

    def _expire_reads(self, now: float) -> None:
        """Drop everything correlated whose deadline has passed.

        A pending request goes: its deadline passed before the disk was walked,
        so walking it now would build material for somebody who has stopped
        listening.

        A queued *detail* goes for the same reason, and its body goes with it.
        A queued *receipt* stays. It carries no body, it is the frame that
        settles the read, and the two ends do not share a clock -- a caller
        whose deadline this Mac thinks has passed may still be waiting, and one
        small frame ends that wait immediately instead of leaving it to run out.
        """
        self._requests = [
            request
            for request in self._requests
            if request.deadline is None or now < request.deadline
        ]
        live: list[_Answer] = []
        for answer in self._queue.answers:
            expired = answer.deadline is not None and now >= answer.deadline
            if expired and answer.kind == KIND_DETAIL:
                answer.forget()
                continue
            live.append(answer)
        self._queue.answers = live

    def _forget_reads(self) -> None:
        """Abandon every correlated read that has not been answered."""
        self._requests.clear()
        with self._lock:
            self._answer_result = None

    # -- delivery --------------------------------------------------------
    def _select(self) -> tuple[str, dict[str, Any], str | None, str] | None:
        """The next frame, in the order liveness outranks everything.

        The heartbeat goes first because a connector the receiver has written
        off cannot answer anything anyway. After that the two lanes take turns:
        a correlated read is not buried behind a corpus being paged out, and a
        service asking continuously cannot keep the corpus from ever going. A
        strict priority either way starves one of them, and both starvations
        are the kind nobody notices until a plan is missing from a cockpit.
        """
        if self._queue.heartbeat is not None:
            return VIEW_HEARTBEAT, self._queue.heartbeat, None, CAPABILITY_OBSERVE
        answer_ready = bool(self._queue.answers)
        if answer_ready and not (self._answered_last and self._queue.cycle_waiting()):
            answer = self._queue.answers[0]
            return answer.kind, answer.payload, answer.request_id, answer.capability
        if self._queue.snapshot is not None:
            return VIEW_SNAPSHOT, self._queue.snapshot, None, CAPABILITY_OBSERVE
        if self._queue.pages:
            return VIEW_CATALOG_PAGE, self._queue.pages[0], None, CAPABILITY_CATALOG
        if self._queue.batches:
            return VIEW_HISTORY_BATCH, self._queue.batches[0], None, CAPABILITY_HISTORY
        if answer_ready:
            answer = self._queue.answers[0]
            return answer.kind, answer.payload, answer.request_id, answer.capability
        return None

    def _release(self, view: str) -> None:
        """Take the frame :meth:`_select` returned off the queue."""
        if view in _ANSWER_KINDS:
            if self._queue.answers:
                # Forgotten on the way out, delivered or not: the frame has
                # already been encoded, so nothing here needs the body any more
                # and holding it would be holding a plan's text for no reason.
                self._queue.answers.pop(0).forget()
            return
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
        if view in _ANSWER_KINDS:
            self._answered_last = True
        elif view != VIEW_HEARTBEAT:
            self._answered_last = False
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

        The correlated half goes too, bodies and all. The far end abandons its
        waiters when the link drops, so every answer held here is an answer to a
        question nobody is waiting on any more -- and delivering one after the
        reconnect would hand a service material it had already reported as
        offline.

        Mutations are the exception, in both directions. A command that has not
        been attempted is dropped with its body reference and its tokens: its
        window is running out against a link that is down, and the far end will
        resubmit if it still wants the action. A *report* of one that was
        carried out is kept, because it is the only record this end has of what
        ASL did and the far end can still absorb it late.
        """
        self._queue.clear_cycle()
        self._queue.drop_correlated()
        self._forget_reads()
        for abandoned in self._mutations.abandon():
            self._enqueue(
                _Answer(
                    request_id=None,
                    kind=KIND_OPERATION,
                    payload=operation_payload(
                        abandoned,
                        MutationResult(
                            status=OPERATION_EXPIRED,
                            detail_code=DETAIL_BRIDGE_UNAVAILABLE,
                        ),
                        observed_at=now,
                    ),
                    capability=CAPABILITY_MUTATE,
                )
            )
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


def _spawn_answer(attempt: Callable[[], None]) -> threading.Thread:
    """Perform one correlated read off the delivery thread.

    A daemon thread for the same reason the observation pass is one: the read
    writes nothing, so an interpreter exiting mid-read loses an answer the
    caller will time out on and nothing else.
    """
    thread = threading.Thread(target=attempt, name="workbench-detail", daemon=True)
    thread.start()
    return thread


def _spawn_mutation(attempt: Callable[[], None]) -> threading.Thread:
    """Carry out one submitted mutation off the delivery thread.

    A daemon thread like the other two, and the only one where that costs
    something: an interpreter exiting mid-attempt loses the report of an action
    that may already have happened. It is still the right trade, because the
    durable row was written before the side effect -- so the next connector
    reads it, calls it ``unconfirmed`` rather than re-running it, and the far
    end learns the truth one restart late instead of never.
    """
    thread = threading.Thread(target=attempt, name="workbench-mutation", daemon=True)
    thread.start()
    return thread
