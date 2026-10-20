"""Recording a turn's conversation, its outcome, and refused restores."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import conversations, harnesses
from .conversation_resume_models import (
    _UNSUBMITTED_CANCEL_STAGES,
    CONTINUATION_RESTORE_REFUSED,
    RESTORE_REFUSED_RETIREMENT_REASON,
)


class ConversationCapture:
    """The turn-scoped sink that records a conversation before work is submitted.

    Handed to the adapter as ``on_conversation`` and called on the harness's own
    driver thread, once, ahead of the first submission. It is a class rather
    than a closure because the caller needs what it captured afterwards: the
    reference to confirm, or the fact that nothing was ever reported.

    Raising here is a supported outcome, not an accident. The transport treats a
    sink that raises as a turn it must not submit, which is exactly right for
    both failures this can have: a ledger that cannot be written, and a harness
    that reported a conversation other than the one it was told to continue.
    """

    __slots__ = ("_plan_state", "_binding", "_expected", "_reference", "_error")

    def __init__(
        self,
        plan_state: Path,
        *,
        binding: conversations.ConversationBinding,
        expected_native_id: str | None = None,
    ) -> None:
        self._plan_state = Path(plan_state)
        self._binding = binding
        self._expected = expected_native_id
        self._reference: conversations.ConversationReference | None = None
        self._error: str | None = None

    @property
    def reference(self) -> conversations.ConversationReference | None:
        """The conversation this turn ran on, or ``None`` if none was reported."""
        return self._reference

    @property
    def error(self) -> str | None:
        """Why the capture refused the turn, if it did."""
        return self._error

    def __call__(self, native_id: str) -> None:
        if self._expected is not None and native_id != self._expected:
            # The adapters promise exact-or-fail, so this is defence in depth
            # against a transport that grows a fork or a "latest" fallback. The
            # turn is refused before it submits, because a turn that ran on a
            # substituted conversation cannot be undone afterwards.
            self._error = (
                f"harness reported conversation {native_id} for a turn told to "
                f"continue {self._expected}"
            )
            raise harnesses.ConversationCaptureError(self._error)
        if self._reference is not None and self._reference.native_id == native_id:
            return
        reference = conversations.ConversationReference(
            binding=self._binding, native_id=native_id
        )
        if self._expected is None:
            # A conversation the run is meeting for the first time has to be
            # durable before the work that will use it is submitted; that
            # ordering is the whole contract of `open_conversation`. A resumed
            # one is already recorded, so re-opening it would add a record
            # saying nothing.
            conversations.open_conversation(self._plan_state, reference)
        self._reference = reference


@dataclass(frozen=True)
class TurnOutcomeRecord:
    """What :func:`record_turn_outcome` managed to write, and what it could not."""

    confirmed: bool = False
    attempt: conversations.AttemptReference | None = None
    conversation: str | None = None
    error: str | None = None


def turn_submitted_work(result: harnesses.HarnessResult) -> bool:
    """Whether the turn got as far as submitting work to its conversation.

    Only a conversation that was actually worked on may be confirmed, because
    confirmation is what makes it resumable. A turn abandoned at the capture
    seam, or cancelled before the handshake finished, leaves a conversation that
    may hold nothing -- and resuming into nothing is indistinguishable from
    resuming into a conversation whose work was lost.
    """
    if result.conversation_error is not None:
        return False
    return result.cancel_stage not in _UNSUBMITTED_CANCEL_STAGES


def attempt_outcome(
    result: harnesses.HarnessResult,
) -> conversations.AttemptOutcome | None:
    """Classify a turn as an unfinished attempt, or ``None`` if it finished.

    ``interrupted`` covers every way a turn was *stopped* -- an operator abort,
    a timeout, a cancellation after work began -- because from the resumed
    worker's side they are the same fact: something was underway and was cut
    short. ``failed`` is the turn that ran to its own unhappy end.
    """
    if result.interrupted or result.aborted or result.timed_out:
        return "interrupted"
    if result.cancel_stage is not None:
        return "interrupted"
    if result.returncode != 0:
        return "failed"
    return None


def record_turn_outcome(
    plan_state: Path,
    *,
    capture: ConversationCapture,
    result: harnesses.HarnessResult,
    run_id: str,
    turn: int,
) -> TurnOutcomeRecord:
    """Confirm the conversation the turn ran on, and note an unfinished attempt.

    Reports failures instead of raising. By the time this runs the turn has
    already executed and its artifacts are already written; a bookkeeping error
    must not become the reason a completed turn is lost. The cost of a lost
    confirmation is bounded and visible: the next attempt reads the conversation
    as unconfirmed and asks the operator, which is the fail-closed direction.
    """
    reference = capture.reference
    if reference is None:
        return TurnOutcomeRecord(error=capture.error)
    if not turn_submitted_work(result):
        return TurnOutcomeRecord(
            conversation=reference.locator,
            error=("turn submitted no work, so its conversation stays unconfirmed"),
        )
    outcome = attempt_outcome(result)
    attempt: conversations.AttemptReference | None = None
    if outcome is not None:
        attempt = conversations.AttemptReference(
            run_id=run_id, turn=turn, outcome=outcome
        )
        reference = reference.with_attempt(attempt)
    try:
        conversations.confirm_conversation(plan_state, reference)
    except (conversations.ConversationError, OSError) as error:
        return TurnOutcomeRecord(
            attempt=attempt,
            conversation=reference.locator,
            error=f"could not confirm {reference.locator}: {error}",
        )
    return TurnOutcomeRecord(
        confirmed=True, attempt=attempt, conversation=reference.locator
    )


@dataclass(frozen=True)
class RestoreRefusal:
    """What was done about a conversation the provider would not restore."""

    #: The conversation the turn asked to continue, when one was named.
    conversation: str | None = None
    #: The provider's own words, carried into the pause so an operator reading
    #: it sees why the history is gone rather than only that it is.
    detail: str = ""
    #: Whether the ledger now says this conversation may never be selected
    #: again. ``False`` means the block did not become durable and the next
    #: launch would try the same dead conversation.
    retired: bool = False
    error: str | None = None

    def describe(self) -> dict[str, Any]:
        """An observable projection for the pause detail and the run event."""
        payload: dict[str, Any] = {
            "disposition": CONTINUATION_RESTORE_REFUSED,
            "conversation": self.conversation,
            "detail": self.detail,
            "retired": self.retired,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


def refuse_unrestorable_conversation(
    plan_state: Path,
    *,
    binding: conversations.ConversationBinding,
    conversation: str | None,
    detail: str,
) -> RestoreRefusal:
    """Block a conversation whose native history the provider no longer holds.

    Retirement rather than a flag, because retirement is what the ledger already
    means by "this record may never be selected again", and because it makes the
    two behaviours the operator needs fall out of machinery that already exists:
    an ordinary launch selects ``retired`` and stops for recovery, and an
    authorized fresh start supersedes nothing (there is nothing live left to
    supersede) and opens a new conversation with no resume id.

    The reason is ``unusable`` and not ``superseded``: nothing replaced this
    conversation. It stopped being one.

    Non-raising, like every other ledger write on a turn's way out. The turn has
    already failed and its pause is already owed to the operator; a ledger error
    here costs the block its durability, which is reported, not the pause.
    """
    if conversation is None:
        # Nothing to block. A refusal names the conversation it refused, so this
        # is a caller that reached here without one rather than a live ledger
        # state -- say so instead of writing an invented record.
        return RestoreRefusal(
            detail=detail,
            error="restore was refused but the turn named no conversation to block",
        )
    reference = conversations.ConversationReference(
        binding=binding, native_id=conversation
    )
    try:
        conversations.retire_conversation(
            plan_state, reference, reason=RESTORE_REFUSED_RETIREMENT_REASON
        )
    except (conversations.ConversationError, OSError) as error:
        return RestoreRefusal(
            conversation=reference.locator,
            detail=detail,
            error=f"could not retire {reference.locator}: {error}",
        )
    return RestoreRefusal(conversation=reference.locator, detail=detail, retired=True)


def restore_refusal_message(refusal: RestoreRefusal) -> str:
    """The operator-facing sentence a refused-restore pause carries.

    Deliberately shaped like :func:`recovery_message`, because the operator is
    at the same fork -- continue or start over -- but the left branch is
    different: there is no re-running this one into the same conversation, so
    the only ways forward are a different workspace's history or an explicit
    fresh start.
    """
    detail = refusal.detail or "the saved conversation could not be restored"
    suffix = "" if refusal.retired else " (the block could not be made durable)"
    return (
        f"{detail}. The provider no longer holds this history, so no later "
        "launch can continue it; re-run with --fresh-conversation to authorize "
        "one fresh start (retained files and outstanding feedback are kept; the "
        f"agent's accumulated context is not){suffix}"
    )


def retire_session_conversations(
    plan_state: Path, *, session_id: str
) -> tuple[tuple[str, ...], str | None]:
    """Retire every conversation belonging to a session that reached complete.

    Returns the retired locators and, separately, why the attempt failed if it
    did. Non-raising for the same reason as :func:`record_turn_outcome`: the
    session is already complete on disk, and a ledger that could not be updated
    must not undo that. A conversation left live is caught on the next attempt
    at the same session id, which stops for recovery rather than continuing work
    that already finished.
    """
    try:
        retired = conversations.retire_completed_session(
            plan_state, session_id=session_id
        )
    except (conversations.ConversationError, OSError) as error:
        return (), f"could not retire conversations for session {session_id}: {error}"
    return tuple(record.reference.locator for record in retired), None
