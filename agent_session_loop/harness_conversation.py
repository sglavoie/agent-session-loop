"""Capture native conversations and prompt submission with turn-scoped locks."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable

from . import (
    harness_types,
)


class TurnConversation:
    """One turn's conversation-capture seam.

    A protocol harness learns its native conversation identifier during the
    handshake, before the turn's work is submitted, and that ordering is the
    whole point: a conversation recorded afterwards would be one a crash in
    between could lose while the work went ahead. A transport therefore reports
    the identifier here and only submits if this call returns.

    Turn-scoped by construction, exactly like
    :class:`~agent_session_loop.guidance_delivery.LiveDeliveryEndpoint`: the
    lock is held across the sink call, so :meth:`close` cannot return while a
    capture is still running and no capture can start after it returns. A late
    callback from an abandoned reader or a straggling protocol thread therefore
    cannot persist a conversation for a turn that is already over.
    """

    def __init__(self, sink: harness_types.ConversationCallback | None = None) -> None:
        self._sink = sink
        self._requested = sink is not None
        self._lock = threading.Lock()
        self._native_id: str | None = None
        self._closed = False

    @property
    def requested(self) -> bool:
        """Whether this turn's caller asked to be told the conversation at all.

        Fixed at construction rather than read off the sink, which :meth:`close`
        drops: a turn that ended still has to be able to say whether continuity
        was ever wanted, because "none was captured" only means something was
        lost when someone was waiting to hear it.
        """
        return self._requested

    @property
    def native_id(self) -> str | None:
        """The conversation this turn runs on, or ``None`` if none was captured."""
        with self._lock:
            return self._native_id

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def capture(self, native_id: str) -> None:
        """Record ``native_id`` durably, or raise so nothing is submitted.

        Idempotent for the identifier already captured, because a transport may
        see the same conversation named more than once in its protocol. A
        *different* identifier is refused: one turn runs on one conversation,
        and continuing the wrong one is the failure this whole seam exists to
        prevent.
        """
        if not isinstance(native_id, str) or not native_id.strip():
            raise harness_types.ConversationCaptureError(
                f"a native conversation id must be non-empty text; got {native_id!r}"
            )
        native_id = native_id.strip()
        with self._lock:
            if self._closed:
                raise harness_types.ConversationCaptureError(
                    f"the turn is over; refusing to record conversation {native_id}"
                )
            recorded = self._native_id
            if recorded is not None:
                if recorded == native_id:
                    return
                raise harness_types.ConversationCaptureError(
                    f"this turn already runs on conversation {recorded}; "
                    f"refusing to also record {native_id}"
                )
            sink = self._sink
            if sink is not None:
                try:
                    sink(native_id)
                except Exception as error:
                    raise harness_types.ConversationCaptureError(
                        f"conversation {native_id} was not persisted: "
                        f"{type(error).__name__}: {error}"
                    ) from error
            self._native_id = native_id

    def close(self) -> None:
        """Close the seam, waiting out a capture already in flight."""
        with self._lock:
            self._closed = True
            self._sink = None


#: What a turn calls once its prompt has actually reached the worker. Unlike
#: :data:`ConversationCallback`, an exception from this one *is* suppressed:
#: by the time it fires the words are already in flight, so there is nothing
#: left for a failure here to stop.
SubmissionCallback = Callable[[], None]


class TurnSubmission:
    """One turn's prompt-submission seam.

    Spawning a process is not submitting a prompt. A one-shot turn carries its
    prompt on argv, so for it the two coincide; a protocol turn spawns, then
    handshakes, and only then sends the prompt -- and everything in between can
    refuse, be cancelled, or fail. Whoever is holding the operator's words needs
    to know which of the two happened, because words consumed by a worker that
    never received them are words the operator silently lost.

    Turn-scoped and locked exactly like :class:`TurnConversation`: the lock is
    held across the sink call, so :meth:`close` cannot return while a
    notification is still running and none can start after it returns. A
    straggling protocol thread therefore cannot report a submission for a turn
    that is already over.

    The seam has three states rather than two, because a prompt on its way out
    is neither. A transport that has begun handing the words over cannot take
    them back and cannot yet say they arrived, and that window is long enough to
    lose a process in: see :attr:`verdict`.
    """

    def __init__(self, sink: SubmissionCallback | None = None) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._submitted = False
        self._in_flight = False
        self._closed = False

    @property
    def submitted(self) -> bool:
        """Whether this turn's prompt reached the worker."""
        with self._lock:
            return self._submitted

    @property
    def verdict(self) -> bool | None:
        """What this seam is willing to say about the prompt: sent, not, unknown.

        ``True`` and ``False`` are both statements about the turn. ``None`` is
        the refusal to make one, and it is reached the moment a transport says
        the words are on the wire: from there a silence has two readings -- the
        server never got them, or it got them and whatever would have said so
        did not survive -- and the seam has no way to choose between them.

        Closing does not resolve it. That is the point: a turn that ends in this
        window ends unanswered, so nothing downstream can read the end of the
        turn as proof the prompt never went out.
        """
        with self._lock:
            if self._submitted:
                return True
            return None if self._in_flight else False

    def note_submission_in_flight(self) -> None:
        """Record that the prompt is going out and can no longer be recalled.

        No sink fires here, because nothing has been received: a caller told
        "delivered" on this would be claiming a worker holds words that may
        still be sitting in a pipe. What changes is only what this seam will
        claim afterwards, which is nothing at all.
        """
        with self._lock:
            if self._closed or self._submitted:
                return
            self._in_flight = True

    def note_submitted(self) -> None:
        """Report that the prompt is the worker's now, at most once.

        The sink's failure is swallowed rather than raised: the prompt has
        already gone out, and a caller told "that did not happen" would withdraw
        words a worker is reading. Idempotent because the fact is about the
        prompt, not about the attempt that carried it.
        """
        with self._lock:
            if self._closed or self._submitted:
                return
            self._submitted = True
            sink = self._sink
            if sink is not None:
                with contextlib.suppress(Exception):
                    sink()

    def close(self) -> None:
        """Close the seam, waiting out a notification already in flight."""
        with self._lock:
            self._closed = True
            self._sink = None


class _SubmissionOnce:
    """A caller's submission callback, reached from both sides of a turn.

    An adapter that cannot state the submission from inside the turn has to be
    able to state it after -- past the point where :class:`TurnSubmission` has
    closed itself against exactly that. Two reporters means the same callback
    can be reached twice, and a caller's callback is a durable write, so the
    deduplication belongs here rather than in each caller.

    :attr:`reported` is what the late reporter reads: not "did this turn
    submit", which the result answers, but "has the caller already been told",
    which is the only question a second report has to ask.
    """

    def __init__(self, sink: SubmissionCallback | None) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._reported = False

    @property
    def reported(self) -> bool:
        """Whether the caller has been told this turn's prompt went out."""
        with self._lock:
            return self._reported

    def __call__(self) -> None:
        with self._lock:
            if self._reported:
                return
            self._reported = True
            sink = self._sink
        if sink is None:
            return
        # Swallowed for the reason :meth:`TurnSubmission.note_submitted` gives:
        # the prompt is out, and a caller told otherwise withdraws words a
        # worker is already reading.
        with contextlib.suppress(Exception):
            sink()
