"""Background machinery behind the bridge: the projection worker that keeps
snapshot builds off the request path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from .bridge_protocol import (
    POLL_INTERVAL_SECONDS,
    PROJECTION_RETRY_INTERVAL_SECONDS,
    WORKER_JOIN_TIMEOUT_SECONDS,
)


class ProjectionPending(Exception):
    """Raised when no projection has been published yet.

    A cold bridge has nothing truthful to serve: an empty snapshot would claim
    an empty corpus and re-enable actions against rows it has never read. The
    caller turns this into a retryable refusal instead, which a client answers
    by asking again — and which, unlike waiting for the walk, leaves the single
    protocol loop free to frame every request behind it.
    """


class ProjectionWorker:
    """Builds the corpus projection off the protocol loop, on a fixed cadence.

    A projection walks every discovered plan, so its cost scales with the
    corpus: several hundred plans take seconds. Building it on the loop that
    frames requests meant the server spent most of its life inside a build
    rather than inside ``select``, and owing the next build one whole interval
    *after* the last one finished made the real start-to-start cadence the sum
    of the build and the interval. A ten-second walk therefore observed change
    every twenty seconds while the handshake advertised one.

    Here the build runs on its own thread and the next one is owed ``interval``
    after the last one *started*, so change observation begins on the declared
    cadence and only a corpus that cannot be walked within it slips — by the
    overrun, never by a second full build.
    """

    def __init__(
        self,
        build: Callable[[], dict[str, Any]],
        *,
        interval: float = POLL_INTERVAL_SECONDS,
        retry_interval: float = PROJECTION_RETRY_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._build = build
        self._interval = interval
        self._retry_interval = retry_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._published: dict[str, Any] | None = None
        self._published_at: float | None = None
        self._started_at: float | None = None
        self._build_seconds = 0.0
        self._observed_interval = 0.0
        self._failures = 0
        self._last_failure: str | None = None

    # ----------------------------------------------------------------
    # Lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._run, name="asl-bridge-projection", daemon=True
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self._settled:
            thread, self._thread = self._thread, None
            self._settled.notify_all()
        if thread is not None:
            thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while not self._stopped.is_set():
            # A build that overruns its interval owes no wait at all, so the
            # cadence degrades to the corpus cost instead of to twice it.
            self._stopped.wait(self.cycle())

    def cycle(self) -> float:
        """Run one projection; return the seconds owed before the next start.

        Separate from :meth:`_run` so the cadence is testable without a thread
        and without real time.
        """
        started = self._clock()
        try:
            snapshot = self._build()
        except Exception as error:  # noqa: BLE001 - one bad walk must not end the loop
            self._record(started, failure=error)
            # An overrunning *successful* walk owes no wait because it published
            # something; an overrunning failed one published nothing, so
            # retrying it at once only spends the same cost again.
            return max(self._retry_interval, started + self._interval - self._clock())
        self._record(started, snapshot=snapshot)
        return max(0.0, started + self._interval - self._clock())

    def _record(
        self,
        started: float,
        *,
        snapshot: dict[str, Any] | None = None,
        failure: BaseException | None = None,
    ) -> None:
        finished = self._clock()
        with self._settled:
            if failure is not None:
                # Timing describes the projection being served, and a failed
                # walk serves nothing: the last successful one stays published,
                # so replacing its cost or its cadence here would describe one
                # walk with another walk's numbers.
                self._failures += 1
                self._last_failure = str(failure)
                self._settled.notify_all()
                return
            if self._started_at is not None:
                self._observed_interval = max(0.0, started - self._started_at)
            self._started_at = started
            self._build_seconds = max(0.0, finished - started)
            self._published = snapshot
            self._published_at = finished
            self._last_failure = None
            self._settled.notify_all()

    # ----------------------------------------------------------------
    # Observation

    def latest(self) -> dict[str, Any]:
        """Return the most recently published projection without building one.

        This never waits, which is the whole point: a request is answered from
        the last walk while the next one runs beside it. Before the first walk
        lands there is nothing truthful to serve, and waiting for it would block
        the single protocol loop — the cold snapshot and every frame behind it —
        for the whole corpus walk. The caller refuses retryably instead, so the
        client re-asks while the loop keeps framing.
        """
        with self._settled:
            if self._published is not None:
                return self._published
            raise ProjectionPending(
                self._last_failure or "the first corpus projection is still in flight"
            )

    def cadence(self) -> dict[str, Any]:
        """Describe how fresh the published projection can honestly claim to be."""
        with self._settled:
            age = 0.0
            if self._published_at is not None:
                age = max(0.0, self._clock() - self._published_at)
            return projection_cadence(
                interval=self._interval,
                observed=self._observed_interval,
                build_seconds=self._build_seconds,
                age=age,
                failures=self._failures,
            )


def projection_cadence(
    *,
    interval: float,
    observed: float,
    build_seconds: float,
    age: float,
    failures: int,
) -> dict[str, Any]:
    """The `projection` block a snapshot carries so a client can label staleness.

    ``interval_seconds`` is the declared floor, the same number the handshake
    advertises. Everything else is measured, because on a corpus the floor
    cannot buy, the declared interval is the one number a client must not
    believe: it is what made a twenty-second observation cadence look like a
    one-second one.
    """
    observed = max(interval, observed, build_seconds)
    return {
        "interval_seconds": _round_seconds(interval),
        "observed_interval_seconds": _round_seconds(observed),
        "build_seconds": _round_seconds(build_seconds),
        "age_seconds": _round_seconds(age),
        # Worst case for any row in this snapshot: read at the start of a walk
        # that took ``build_seconds``, aged ``age_seconds`` since that walk
        # landed, and not replaced for another whole observed interval.
        "staleness_bound_seconds": _round_seconds(build_seconds + age + observed),
        "failed_projections": failures,
    }


def _round_seconds(value: float) -> float:
    """Round to milliseconds so a duration cannot carry float noise on the wire."""
    return round(float(value), 3)


# Derived rather than written out, so the frame-budget probe below cannot
# describe a block the server does not actually send.
PROJECTION_KEYS: tuple[str, ...] = tuple(
    projection_cadence(
        interval=0.0, observed=0.0, build_seconds=0.0, age=0.0, failures=0
    )
)
