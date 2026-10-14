"""Connector errors, tunable constants, bounded retry and the single-instance lock.

The small, dependency-free pieces the observation runtime is built from. Nothing
here knows about frames or schedules beyond the constants that name them:
:class:`Backoff` is a pure delay sequence and :class:`SingleInstanceLock` is a pid
file. The runtime that uses them lives in
:mod:`asl_workbench.connector_runtime`.
"""

from __future__ import annotations

import errno
import os

from .transport import (
    CAPABILITY_CATALOG,
    CAPABILITY_DETAIL,
    CAPABILITY_HISTORY,
    CAPABILITY_MUTATE,
    CAPABILITY_OBSERVE,
    MAX_CATALOG_ENTRIES_PER_PAGE,
    MAX_RUNS_PER_SNAPSHOT,
)

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

#: How often an accepted remote mutation is checked for ASL's own durable
#: consumption evidence.  This runs on the mutation worker, never the delivery
#: thread, and keeps an accepted live reply from remaining open forever after
#: the waiting runner has recorded that it consumed the reply.
MUTATION_SETTLEMENT_INTERVAL_SECONDS = 1.0
ACTIVE_READ_WINDOW_SECONDS = 30.0

#: How many requests may be waiting for the answering worker, and how many
#: finished answers may be waiting for the channel. Both are small: a request
#: arrives at most one per delivered frame, and a service that has filled either
#: queue is not reading a plan, it is looping.
MAX_PENDING_REQUESTS = 8
MAX_QUEUED_ANSWERS = 8

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
