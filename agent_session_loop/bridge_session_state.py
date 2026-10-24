"""Session state, projection cadence, handshake gate, and event polling.

The bottom layer of the :class:`BridgeSession` stack: it owns the dataclass
fields and everything that only reads the projection.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_mux_projection import CONTRACT_SET_ID
from .bridge_errors import _optional_str, _require_str
from .bridge_protocol import (
    ERROR_CONTRACT_SET_MISMATCH,
    ERROR_INTERNAL,
    ERROR_STALE_SNAPSHOT,
    EVENT_RUNS_CHANGED,
    OPERATION_ACCEPTED,
    OPERATION_TERMINAL_STATUSES,
    POLL_INTERVAL_SECONDS,
    PROJECTION_RETRY_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    BridgeError,
    _bounded_id,
    _bounded_path,
    _bounded_text,
)
from .bridge_workers import ProjectionPending, WorkbenchLifecycle, projection_cadence


@dataclass
class Operation:
    """One accepted mutation, described by identifiers rather than by content.

    A mutation is durable the moment ASL's own record lands, which is strictly
    earlier than the moment the runner acts on it. The operation is what lets a
    client say "submitted" immediately and "consumed" later without inventing a
    second source of truth: every terminal status below is settled by observing
    canonical ASL data, never by the bridge remembering what it intended.
    """

    operation_id: str
    method: str
    plan_dir: str
    row_id: str
    accepted_at: float
    status: str = OPERATION_ACCEPTED
    run_id: str | None = None
    question_id: str | None = None
    closure_id: str | None = None
    command_id: str | None = None
    detail: str | None = None
    #: How far into the plan's event log this operation has already looked for
    #: proof its reply was read. Set to the log's size *before* the mutation, so
    #: a record already there cannot be mistaken for this one's, and advanced by
    #: every look so a growing log is read once rather than from the top. Kept
    #: in memory rather than persisted: a mark is meaningless to the process
    #: that did not take it, and the durable journal reconciles by timestamp.
    witness_offset: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in OPERATION_TERMINAL_STATUSES

    def settle(self, status: str, detail: str | None = None) -> None:
        self.status = status
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        return {
            "operation_id": _bounded_id(self.operation_id),
            "method": self.method,
            "status": self.status,
            "plan_dir": _bounded_path(self.plan_dir),
            "row_id": self.row_id,
            "run_id": _bounded_id(self.run_id),
            "question_id": _bounded_id(self.question_id),
            "closure_id": _bounded_id(self.closure_id),
            "command_id": _bounded_id(self.command_id),
            "detail": _bounded_text(self.detail),
        }


@dataclass
class SessionState:
    """Fields and read-side protocol state shared by every session layer."""

    snapshot_builder: Callable[[], dict[str, Any]]
    state_dir: str | Path | None = None
    workbench_lifecycle: WorkbenchLifecycle | None = None
    clock: Callable[[], float] = time.monotonic
    poll_interval: float = POLL_INTERVAL_SECONDS
    projection_retry_interval: float = PROJECTION_RETRY_INTERVAL_SECONDS
    projection_cadence: Callable[[], dict[str, Any]] | None = None
    seq: int = 0
    last_digest: str | None = None
    # The mutation gate. ``handshake_id`` records that a client proved it speaks
    # this contract set; ``handshake_digest`` records the snapshot that proof
    # was made against, and is cleared rather than advanced when the corpus
    # moves, so a handshake authorizes exactly the projection it was shown.
    handshake_id: str | None = None
    handshake_digest: str | None = None
    handshake_count: int = 0
    operations: OrderedDict[str, Operation] = field(
        default_factory=OrderedDict, repr=False
    )
    _last_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    _last_snapshot_at: float | None = field(default=None, repr=False)
    _last_build_seconds: float = field(default=0.0, repr=False)
    _retry_at: float | None = field(default=None, repr=False)

    def projection_interval(self) -> float:
        """How long a projection stays authoritative, and how often to poll.

        A projection is only worth repeating if it can finish appreciably
        sooner than it costs. Scaling the interval by the last build keeps the
        server under half duty on a corpus where one projection takes ten
        seconds, instead of starting the next one before the last has landed
        and never answering a request again.
        """
        return max(self.poll_interval, self._last_build_seconds)

    def next_projection_at(self) -> float:
        """When the next projection is owed, counted from the last one.

        Counting from the projection rather than from the poll is what stops a
        client request from being immediately followed by a poll that rebuilds
        the same corpus, which on an expensive one doubles the work and blocks
        the next request behind it.

        A failure owes its own deadline, because the deadline of the last
        *successful* projection is already in the past by then and would put
        the loop into a zero-timeout retry spin.
        """
        if self._retry_at is not None:
            return self._retry_at
        if self._last_snapshot_at is None:
            return self.clock() + self.poll_interval
        return self._last_snapshot_at + self.projection_interval()

    def select_timeout(self) -> float:
        """How long the protocol loop may wait for a frame before reprojecting."""
        return max(0.0, self.next_projection_at() - self.clock())

    def _project(self) -> dict[str, Any]:
        started = self.clock()
        try:
            snapshot = self.snapshot_builder()
        except BaseException:
            self._defer_projection()
            raise
        finished = self.clock()
        self._last_build_seconds = max(0.0, finished - started)
        self._last_snapshot = snapshot
        self._last_snapshot_at = finished
        self._retry_at = None
        return snapshot

    def _defer_projection(self) -> None:
        """Owe the next projection a bounded nonzero delay after a failure."""
        deadline = self.clock() + self.projection_retry_interval
        self._retry_at = (
            deadline if self._retry_at is None else max(self._retry_at, deadline)
        )

    def cadence(self) -> dict[str, Any]:
        """The cadence block served with a snapshot.

        Delegated to the projector when there is one, because only the thread
        that runs the walks knows what they actually cost; a session building
        inline answers from its own last walk instead.
        """
        if self.projection_cadence is not None:
            return self.projection_cadence()
        age = 0.0
        if self._last_snapshot_at is not None:
            age = max(0.0, self.clock() - self._last_snapshot_at)
        return projection_cadence(
            interval=self.poll_interval,
            observed=self.projection_interval(),
            build_seconds=self._last_build_seconds,
            age=age,
            failures=0,
        )

    def _current_snapshot(self) -> dict[str, Any]:
        """Return the projection this session would serve right now.

        Shared by ``runs.snapshot`` and by the mutation gate so both answer from
        one definition of "current": a handshake can never be validated against
        a projection the client was never eligible to be shown.
        """
        if self._last_snapshot is not None and self.clock() < self.next_projection_at():
            return self._last_snapshot
        try:
            snapshot = self._project()
        except ProjectionPending as error:
            # Refusing is what keeps a slow first walk from blocking the loop:
            # the client re-asks, and meanwhile every other method still answers.
            raise BridgeError(
                ERROR_INTERNAL,
                f"no projection is available yet: {error}",
                retryable=True,
            ) from error
        except Exception as error:
            raise BridgeError(
                ERROR_INTERNAL,
                f"snapshot projection failed: {error}",
                retryable=True,
            ) from error
        self.last_digest = snapshot["digest"]
        return snapshot

    def runs_snapshot(self) -> dict[str, Any]:
        # The cadence block is attached per response rather than per walk: the
        # projection is shared and cached, but how stale it is by now is not.
        return self._current_snapshot() | {"projection": self.cadence()}

    # ----------------------------------------------------------------
    # Compatibility handshake

    def session_handshake(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Prove contract-set agreement and pin the snapshot mutations may act on.

        Deliberately not a negotiation. The bridge implements exactly one
        contract set, so a client naming another is told so once, non-retryably,
        and keeps its read-only access; there is nothing to fall back to.
        """
        offered_contract_set = _require_str(params, "contract_set_id")
        if offered_contract_set != CONTRACT_SET_ID:
            # The message is a fixed string rather than an interpolation of the
            # client's value: the reason a client renders must be stable across
            # runs, and echoing an unvalidated identifier back into a UI is how
            # a compatibility notice turns into an injection surface.
            raise BridgeError(
                ERROR_CONTRACT_SET_MISMATCH,
                "client contract set is not the one this bridge implements",
                retryable=False,
                details={"contract_set_id": CONTRACT_SET_ID},
            )
        offered_digest = _optional_str(params, "snapshot_digest")
        current = str(self._current_snapshot()["digest"])
        enabled = offered_digest == current
        self.handshake_count += 1
        self.handshake_id = (
            "hs-"
            + hashlib.sha256(f"{self.handshake_count}:{current}".encode()).hexdigest()[
                :8
            ]
        )
        self.handshake_digest = current if enabled else None
        return {
            "contract_set_id": CONTRACT_SET_ID,
            "handshake_id": self.handshake_id,
            "mutations_enabled": enabled,
            "snapshot_digest": current,
        }

    def _require_mutations_enabled(self, method: str) -> None:
        """Refuse a mutation that no live handshake authorizes.

        Complementary to the action token rather than redundant with it: the
        token proves the *plan* has not moved since the client read it, and this
        proves the client read it from a projection produced by the contract set
        the bridge implements and still holds.
        """
        if self.handshake_id is None:
            raise BridgeError(
                ERROR_CONTRACT_SET_MISMATCH,
                "mutations require a completed session.handshake",
                retryable=False,
                details={"contract_set_id": CONTRACT_SET_ID, "method": method},
            )
        current = str(self._current_snapshot()["digest"])
        if self.handshake_digest != current:
            raise BridgeError(
                ERROR_STALE_SNAPSHOT,
                "the handshake snapshot is no longer current; "
                "refresh the snapshot and handshake again",
                retryable=True,
                details={"snapshot_digest": current},
            )

    def poll(self) -> dict[str, Any] | None:
        """Return a ``runs.changed`` event, or ``None`` when nothing changed."""
        try:
            snapshot = self._project()
        except Exception:  # noqa: BLE001 - _project already owes the retry delay
            return None
        if snapshot["digest"] == self.last_digest:
            return None
        self.last_digest = snapshot["digest"]
        self.seq += 1
        return {
            "type": "event",
            "protocol": PROTOCOL_VERSION,
            "seq": self.seq,
            "event": EVENT_RUNS_CHANGED,
            "data": {
                "digest": snapshot["digest"],
                "row_count": snapshot["row_count"],
            },
        }
