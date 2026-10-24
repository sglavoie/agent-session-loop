"""Shared constants, the refusal type and the state base for the uplink.

:class:`~asl_workbench.uplink.Uplink` is one object with one lock; the mixin
modules next to it split its methods by responsibility, and
:class:`UplinkState` is where the attributes they all read are declared.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import count
from typing import Any

from .remote_reads import DetailReadClient, ReadAnswer
from .store_workbench import WorkbenchStore

#: How long a carried command stays worth performing. Wide enough that a
#: connector on its idle cadence still collects it, short enough that a control
#: the operator submitted and forgot about does not act on a run that has since
#: moved on.
COMMAND_LIFETIME_SECONDS = 300.0

#: How long a request thread waits for the Mac's answer before settling the
#: read itself. The budget covers two things the caller cannot see apart: the
#: wait for the next upload to carry the request out, and the Mac's own read.
#: It is the browser's patience, so it is bounded by what a person will sit in
#: front of rather than by what the transport permits.
READ_WAIT_SECONDS = 45.0

#: How long the link may be silent before it is treated as down. A connector
#: uploads a heartbeat on its idle cadence, so silence past this is not a quiet
#: Mac, it is no Mac -- and a read composed against a dead link must be
#: refused ``offline`` rather than left to time out, because those two are
#: different answers to the operator.
LINK_SILENCE_SECONDS = 180.0

#: How many downward frames the Pi will hold for a connector that is not
#: collecting them. A bound rather than a queue, because the alternative to
#: refusing the newest is growing without limit on behalf of a Mac that is
#: gone.
MAX_CARRIED_FRAMES = 64

#: The slice a waiting request thread sleeps for between checks. Short enough
#: that a link declared down mid-wait is noticed, long enough that a hundred
#: idle readers are not a hundred spinning threads.
_WAIT_SLICE_SECONDS = 0.25


class UplinkRefusal(Exception):
    """An upload was refused. Carries the status and reason the route answers.

    Deliberately not a ``ValueError`` subclass: the reason travels with it
    because the connector's own retry decision depends on which refusal this
    was, and a message it has to parse is not a decision it can make.
    """

    def __init__(self, status: int, reason: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.details = details


@dataclass
class _HeldBody:
    """One operator-authored body kept only for this process's live command."""

    reply_ref: str
    action_token: str
    text: str
    expires_at: float


class UplinkState:
    """The attributes every uplink mixin reads, declared once."""

    store: WorkbenchStore
    clock: Callable[[], float]
    monotonic: Callable[[], float]
    connector_id: str
    target_id: str
    capabilities: frozenset[str]
    read_wait_seconds: float
    command_lifetime_seconds: float
    silence_seconds: float
    max_carried: int
    reads: DetailReadClient
    _ready: threading.Condition
    _carry: deque[Mapping[str, Any]]
    _settled: dict[str, ReadAnswer]
    _bodies: dict[str, _HeldBody]
    _last_upload_at: float | None
    _last_upload_mono: float | None
    _reads_asked: count[int]
    _epoch: str

    def _keep(self, answers: Iterable[ReadAnswer]) -> None:
        """Hold settled answers where the waiting threads will find them."""
        answers = tuple(answers)
        if not answers:
            return
        with self._ready:
            for answer in answers:
                self._settled[answer.request_id] = answer
            self._ready.notify_all()

    def _take(self, request_id: str) -> ReadAnswer | None:
        with self._ready:
            return self._settled.pop(request_id, None)
