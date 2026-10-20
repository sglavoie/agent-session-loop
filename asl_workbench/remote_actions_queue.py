"""The bounded, ordered queue of commands a connector is holding.

Tracks which commands are waiting, which have been handed out for an attempt,
and which may still be cancelled or expired. Holds no body and performs
nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .operations import DETAIL_BRIDGE_UNAVAILABLE, DETAIL_DEADLINE_PASSED
from .remote_actions_models import (
    MAX_PENDING_MUTATIONS,
    MutationRequest,
    RemoteActionError,
)
from .transport import RECEIPT_EXPIRED


@dataclass
class _Held:
    """One command the queue is holding, and whether it has been handed out."""

    request: MutationRequest
    accepted_at: float
    attempted_at: float | None = None

    @property
    def attempted(self) -> bool:
        return self.attempted_at is not None


@dataclass
class MutationQueue:
    """The commands this connector is holding, in the order they arrived.

    Driven by the delivery thread alone and therefore unlocked, exactly like the
    observation cycle it shares a step with. Everything that would need a lock
    -- performing the mutation, redeeming a body -- happens off this object, on
    a request the queue has already handed out.
    """

    limit: int = MAX_PENDING_MUTATIONS
    _held: list[_Held] = field(default_factory=list)

    @property
    def pending(self) -> int:
        """How many commands are waiting to be attempted."""
        return sum(1 for held in self._held if not held.attempted)

    @property
    def attempting(self) -> int:
        """How many commands have been handed out and not yet settled."""
        return sum(1 for held in self._held if held.attempted)

    def holds(self, operation_id: str) -> bool:
        """Whether this operation is already in hand.

        A resubmission of one that is answers itself: the operation frame this
        lane owes will name the status it actually reached, and a second copy
        would only mean carrying the same mutation out twice.
        """
        return any(held.request.operation_id == operation_id for held in self._held)

    def accept(self, request: MutationRequest, *, now: float) -> bool:
        """Take a parsed command, or refuse it while the service can react.

        ``False`` means the command was already in hand and this copy was
        dropped -- deliberately without a receipt, because the answer the
        service is waiting for is the operation's outcome and it is coming.
        """
        if self.holds(request.operation_id):
            request.forget()
            return False
        if len(self._held) >= self.limit:
            request.forget()
            raise RemoteActionError(
                DETAIL_BRIDGE_UNAVAILABLE,
                f"this connector is holding {self.limit} commands already",
            )
        if request.deliver_by <= now:
            request.forget()
            raise RemoteActionError(
                DETAIL_DEADLINE_PASSED,
                "the delivery window closed before the command was queued",
                outcome=RECEIPT_EXPIRED,
            )
        self._held.append(_Held(request=request, accepted_at=now))
        return True

    def next_attempt(self, now: float) -> MutationRequest | None:
        """Hand out the oldest command that has not been attempted.

        Marking it on the way out is what makes an attempt happen at most once:
        nothing this method has returned is ever returned again, cancellable, or
        expirable, whatever happens to the process that is carrying it out.
        """
        for held in self._held:
            if not held.attempted:
                held.attempted_at = now
                return held.request
        return None

    def cancel(self, operation_id: str) -> MutationRequest | None:
        """Withdraw a command that is still waiting.

        Returns the command when it was dropped and ``None`` when there was
        nothing to drop -- including when it is already being carried out, which
        is not cancellable and must not be reported as though it were.
        """
        for index, held in enumerate(self._held):
            if held.request.operation_id != operation_id:
                continue
            if held.attempted:
                return None
            del self._held[index]
            held.request.forget()
            return held.request
        return None

    def expire(self, now: float) -> list[MutationRequest]:
        """Drop every waiting command whose window has closed.

        Only waiting ones: a command being carried out is past the point where
        ``expired`` -- which means *it did not run* -- could still be true.
        """
        return self._drop(
            lambda held: not held.attempted and now >= held.request.deliver_by
        )

    def abandon(self) -> list[MutationRequest]:
        """Drop every waiting command, whatever its window still says.

        What a lost link owes: nothing waiting here can be carried out and its
        window is running out against a service that cannot be told, so the
        command is dropped -- tokens and body reference with it -- and the far
        end resubmits if it still wants the action. A command already being
        carried out is untouched, because it is happening.
        """
        return self._drop(lambda held: not held.attempted)

    def settle(self, operation_id: str) -> MutationRequest | None:
        """Drop a command whose outcome is now known."""
        for index, held in enumerate(self._held):
            if held.request.operation_id == operation_id:
                del self._held[index]
                held.request.forget()
                return held.request
        return None

    def clear(self) -> list[MutationRequest]:
        """Drop everything, forgetting every body reference on the way out."""
        held, self._held = self._held, []
        for entry in held:
            entry.request.forget()
        return [entry.request for entry in held]

    def waiting(self) -> Sequence[MutationRequest]:
        """The commands that have not been attempted, oldest first."""
        return [held.request for held in self._held if not held.attempted]

    def _drop(self, matches: Callable[[_Held], bool]) -> list[MutationRequest]:
        """Remove the commands a rule selects, forgetting each on the way out."""
        removed = [held for held in self._held if matches(held)]
        for held in removed:
            self._held.remove(held)
            held.request.forget()
        return [held.request for held in removed]
