"""The single policy that decides what conversation a turn runs on."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import conversations
from .conversation_resume_capture import ConversationCapture
from .conversation_resume_models import (
    CONTINUATION_FRESH,
    CONTINUATION_OPENED,
    CONTINUATION_RECOVERY_REQUIRED,
    CONTINUATION_RESUMED,
    CONTINUATION_UNAVAILABLE,
    FreshConversationAuthorization,
)


@dataclass(frozen=True)
class TurnContinuation:
    """What one turn was decided to do about a conversation, before it started.

    ``paused`` is the single field the run loop branches on; everything else is
    what the turn or the pause needs to say why.
    """

    disposition: str
    binding: conversations.ConversationBinding | None = None
    selection: conversations.ConversationSelection | None = None
    intent: conversations.ContinuationIntent | None = None
    #: Handed to the adapter as ``resume_conversation_id``. ``None`` means the
    #: turn opens a conversation rather than continuing one; it never means
    #: "pick whichever one is latest", which no code path can express.
    resume_conversation_id: str | None = None
    #: Whether the turn should capture the conversation it runs on. ``False``
    #: only when the harness cannot report one, in which case asking would make
    #: the adapter refuse the turn outright.
    captures: bool = False
    detail: str = ""
    #: Named binding fields that no longer match, for an incompatible refusal.
    mismatched_fields: tuple[str, ...] = ()
    #: Conversations retired to make room for the authorized fresh start.
    superseded: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def paused(self) -> bool:
        """Whether the run loop must stop instead of running this turn."""
        return self.disposition == CONTINUATION_RECOVERY_REQUIRED

    @property
    def resumes(self) -> bool:
        return self.disposition == CONTINUATION_RESUMED

    def capture(self, plan_state: Path) -> ConversationCapture | None:
        """Build this turn's capture sink, or ``None`` when it needs none.

        The narrowing lives here rather than at the call site because
        ``captures`` and ``binding`` are two halves of one fact: a turn that
        captures always has a binding, and a caller reconstructing that
        invariant is a caller that can get it wrong.
        """
        if not self.captures or self.binding is None:
            return None
        return ConversationCapture(
            plan_state,
            binding=self.binding,
            expected_native_id=self.resume_conversation_id,
        )

    def describe(self) -> dict[str, Any]:
        """An observable, feedback-free projection for events and pause details.

        Never read back into a decision: a resumed run re-derives its
        continuation from the ledger, which is the only thing that can be
        trusted to still be true.
        """
        payload: dict[str, Any] = {
            "disposition": self.disposition,
            "conversation": self.resume_conversation_id,
            "detail": self.detail,
        }
        if self.binding is not None:
            payload["session_id"] = self.binding.session_id
            payload["harness"] = self.binding.harness
        if self.selection is not None:
            payload["selection_state"] = self.selection.state
        if self.mismatched_fields:
            payload["mismatched_fields"] = list(self.mismatched_fields)
        if self.superseded:
            payload["superseded"] = list(self.superseded)
        if self.intent is not None:
            payload["intent"] = self.intent.describe()
        if self.notes:
            payload["notes"] = list(self.notes)
        return payload


def recovery_message(continuation: TurnContinuation) -> str:
    """The operator-facing sentence a recovery pause carries.

    Says what was refused and what the two ways forward are, in that order. The
    repair hint registered at
    ``preflight_runtime_hints.RUNTIME_REPAIR_HINTS`` carries the long form;
    this is the one line that reaches a pause message.
    """
    detail = continuation.detail or "the saved conversation cannot be continued"
    return (
        f"{detail}. Re-run with the original workspace and harness to continue "
        "it, or with --fresh-conversation to authorize one fresh start "
        "(retained files and outstanding feedback are kept; the agent's "
        "accumulated context is not)"
    )


def _harness_capability(harness: str) -> conversations.HarnessCapability:
    return conversations.capability_report().for_harness(harness)


def plan_turn_continuation(
    plan_state: Path,
    *,
    binding: conversations.ConversationBinding,
    authorization: FreshConversationAuthorization,
    interrupted_operations: Sequence[str] = (),
) -> TurnContinuation:
    """Decide what this turn does about a conversation, and write nothing.

    The only write this function may cause is the retirement of a conversation
    an *authorized* fresh start supersedes, which is bookkeeping for a decision
    the operator already made. Everything else it returns is a plan the caller
    executes.

    ``interrupted_operations`` names operations a previous attempt cut short, by
    identifier only. It is carried into the intent so a resumed worker can be
    told what to pick up; the feedback that caused the interruption travels on
    the channel that owns it and never through here. The producer of those
    identifiers is the redirect journal, which lands with its own session; until
    then the sequence is empty and the parameter is the seam that keeps this
    function from needing to change when it arrives.
    """
    capability = _harness_capability(binding.harness)
    if not capability.available:
        # Not a refusal: continuity is a capability, and a harness that has
        # none runs its turns exactly as it did before any of this existed.
        # Asking such an adapter to capture or resume makes it raise, so the
        # turn must be launched with neither.
        return TurnContinuation(
            disposition=CONTINUATION_UNAVAILABLE,
            binding=binding,
            detail=(
                capability.detail
                or f"{binding.harness} does not carry a conversation across turns"
            ),
        )

    try:
        selection = conversations.select_conversation(plan_state, binding)
    except conversations.ConversationError as error:
        # A binding this module built cannot be invalid, so reaching here means
        # the ledger itself is unusable. Fail closed rather than opening a
        # conversation whose predecessor we could not read.
        return TurnContinuation(
            disposition=CONTINUATION_RECOVERY_REQUIRED,
            binding=binding,
            detail=f"the conversation ledger could not be read: {error}",
        )

    if selection.state == conversations.SELECTION_RESUMABLE:
        reference = selection.reference
        assert reference is not None  # SELECTION_RESUMABLE always names one
        return TurnContinuation(
            disposition=CONTINUATION_RESUMED,
            binding=binding,
            selection=selection,
            intent=conversations.resume_intent(
                reference, interrupted_operations=interrupted_operations
            ),
            resume_conversation_id=reference.native_id,
            captures=True,
            detail=f"continuing {reference.locator}",
        )

    if selection.state == conversations.SELECTION_ABSENT:
        # A session's first turn continues nothing, so it needs no
        # authorization and carries no intent: there is no conversation being
        # discarded, only one about to exist.
        return TurnContinuation(
            disposition=CONTINUATION_OPENED,
            binding=binding,
            selection=selection,
            captures=True,
            detail=selection.detail,
        )

    if not authorization.consume():
        return TurnContinuation(
            disposition=CONTINUATION_RECOVERY_REQUIRED,
            binding=binding,
            selection=selection,
            detail=selection.detail,
            mismatched_fields=selection.mismatched_fields,
        )

    superseded, notes = _supersede(plan_state, selection=selection)
    return TurnContinuation(
        disposition=CONTINUATION_FRESH,
        binding=binding,
        selection=selection,
        intent=conversations.fresh_intent(
            authorized=True, interrupted_operations=interrupted_operations
        ),
        captures=True,
        detail=selection.detail,
        mismatched_fields=selection.mismatched_fields,
        superseded=superseded,
        notes=notes,
    )


def _supersede(
    plan_state: Path, *, selection: conversations.ConversationSelection
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Retire the conversation an authorized fresh start replaces.

    Strictly bookkeeping. Opening a conversation appends a newer record under
    the same key, so selection would prefer the new one either way; the
    retirement is what makes the ledger say *why* the old one stopped being
    used, months later, to someone reading it rather than running it.

    Two states are deliberately skipped. A ``retired`` record is already
    retired, and a ``corrupt`` ledger is one whose unreadable line may name the
    very conversation being retired -- writing against a history we could not
    read is how a partial ledger becomes a wrong one.
    """
    reference = selection.reference
    if reference is None or selection.state == conversations.SELECTION_RETIRED:
        return (), ()
    try:
        conversations.retire_conversation(plan_state, reference, reason="superseded")
    except (conversations.ConversationError, OSError) as error:
        # The fresh start is already authorized and the turn is about to run.
        # Losing the retirement costs the ledger an explanation, not its
        # correctness, so it is reported rather than raised.
        return (), (f"could not retire {reference.locator}: {error}",)
    return (reference.locator,), ()
