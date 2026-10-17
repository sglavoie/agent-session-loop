"""Coordinate cancellation, conversation capture, and prompt submission per turn."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import (
    harness_conversation,
    harness_process,
    harness_types,
)


@dataclass(frozen=True)
class TurnLifecycle:
    """What a transport may say and ask about the turn it is opening.

    Handed to :meth:`HarnessAdapter.open_transport` so a protocol conversation
    has somewhere to report its native identifier, somewhere to look before it
    submits, and somewhere to say that it did. All three halves are turn-scoped
    and all three are closed by the boundary that owns the process, which is
    what keeps a late protocol callback from reviving a turn that has already
    ended.
    """

    conversation: harness_conversation.TurnConversation
    abort: harness_process.AbortSignal | None = None
    submission: harness_conversation.TurnSubmission = field(
        default_factory=harness_conversation.TurnSubmission
    )

    @property
    def cancel_due(self) -> bool:
        """Whether a cancellation is due for this turn.

        Due, not merely requested: a request still inside its grace period is
        one the wait loop would let the turn finish under, and a checkpoint
        that stopped on it would abort turns that used to complete.
        """
        return self.abort is not None and self.abort.is_due()

    @property
    def cancel_stage(self) -> harness_types.CancelStage | None:
        """Which window this turn's cancellation was acted on in, if any."""
        return None if self.abort is None else self.abort.cancel_stage

    def check_not_cancelled(self) -> None:
        """Stop the handshake if a cancellation is already due.

        A transport calls this at every point where it is about to submit
        something. Raising here means the turn is abandoned without work having
        been sent, so there is nothing to interrupt and nothing to continue.
        """
        abort = self.abort
        if abort is not None and abort.is_due():
            raise harness_types.TurnCancelled(abort.reason or "cancellation requested")

    @property
    def wants_conversation(self) -> bool:
        """Whether this turn's caller asked which conversation it runs on."""
        return self.conversation.requested

    def capture_conversation(self, native_id: str) -> None:
        """Record the turn's native conversation; raises if it was not stored."""
        self.conversation.capture(native_id)

    @property
    def prompt_submitted(self) -> bool | None:
        """What this turn can prove about its prompt: sent, not sent, unknown.

        Three-valued because :class:`TurnSubmission` is: a turn stopped while
        its prompt was crossing the wire answers ``None``, which is the only
        honest answer and the one a caller holding an operator's words has to be
        given rather than a ``False`` it would act on.
        """
        return self.submission.verdict

    def note_submission_in_flight(self) -> None:
        """Report that the prompt is going out, before it can be observed to.

        Called by a transport at the last instant it can still say nothing has
        been sent -- immediately before the write, not after it, because a write
        that fails partway through has already put some of the prompt on the
        wire. From here the turn proves nothing about its prompt either way.
        """
        self.submission.note_submission_in_flight()

    def note_prompt_submitted(self) -> None:
        """Report that the prompt has been submitted, and is now the worker's.

        Called at each transport's own submission point -- the spawn for a turn
        whose prompt is argv, the accepted ``turn/start`` or the written
        ``session/prompt`` for one that speaks a protocol. Never before: every
        checkpoint above it exists so a turn can still be declined, and a
        decline after this one would be withdrawing work already in flight.
        """
        self.submission.note_submitted()

    def refuse_submission(self) -> None:
        """Stop a handshake here, naming this window rather than a later one.

        The two calls are one act: a transport whose handshake runs on its own
        thread would otherwise have its cancellation named by the wait loop,
        which can only see ``execution`` -- the window that means work was
        interrupted, and precisely what stopping here avoided.
        """
        if self.cancel_due:
            self.note_cancelled(harness_types.CANCEL_STAGE_HANDSHAKE)
        self.check_not_cancelled()

    def begin(self) -> None:
        """Bind the lifecycle to a turn that is about to start."""
        if self.abort is not None:
            self.abort.begin_turn()

    def note_cancelled(self, stage: harness_types.CancelStage) -> None:
        """Name the window a pending cancellation was acted on in."""
        if self.abort is not None:
            self.abort.observed(stage)

    def close(self) -> None:
        """End the turn: no further capture, and any request is complete.

        Ordered so completion is the last thing that happens. The conversation
        and submission seams close first, so nothing can still be recorded for
        this turn, and the cancellation is reported complete only once there is
        nothing left of the turn at all.
        """
        self.conversation.close()
        self.submission.close()
        if self.abort is not None:
            self.abort.release()
