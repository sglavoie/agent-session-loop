"""What a source hands the connector runtime, and the seams it calls out through.

:class:`ObservationCycle` is one finished pass over the corpus; the protocols are
the only openings in the connector for ASL-aware code. None of them names a
path, a plan or a subprocess: that is what lets the publication rules be tested
against stubs. The runtime lives in :mod:`asl_workbench.connector_runtime`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .catalog import CatalogPage
from .history import HistoryBatch
from .remote_actions import MutationRequest, MutationResult

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
