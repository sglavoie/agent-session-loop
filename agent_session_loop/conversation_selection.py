"""Conversation selection."""

from __future__ import annotations

from pathlib import Path

from . import (
    conversation_ledger_models as models,
)
from . import (
    conversation_references as references,
)
from . import (
    conversation_storage as storage,
)
from . import (
    conversation_validation as validation,
)
from .harnesses import (
    HARNESS_NAMES,
    HarnessName,
)


def open_conversation(
    plan_state: str | Path, reference: references.ConversationReference
) -> models.ConversationRecord:
    """Record ``reference`` before the work that will use it is submitted.

    This is the write the continuity contract requires to happen *first*. It
    deliberately does not make the conversation resumable: until
    :func:`confirm_conversation` records that the harness accepted work on it,
    selection reports it as unconfirmed, so a failure between this write and the
    first submitted turn cannot resume into a conversation that may hold nothing.
    """
    return storage._write_record(
        plan_state,
        kind=validation.KIND_CONVERSATION_OPENED,
        reference=reference,
    )


def confirm_conversation(
    plan_state: str | Path, reference: references.ConversationReference
) -> models.ConversationRecord:
    """Record that the harness accepted work on ``reference``.

    Only a confirmed conversation is resumable. Confirming one this ledger never
    saw opened is refused: the reference would rest on nothing, and a typo in a
    native identifier would mint a resumable conversation out of thin air.
    """
    if not isinstance(reference, references.ConversationReference):
        raise validation.ConversationError(
            validation.ERROR_INVALID_REFERENCE,
            "reference must be a ConversationReference value",
        )
    opened = [
        record
        for record in storage.read_conversations(plan_state).history(reference.key)
        if record.kind == validation.KIND_CONVERSATION_OPENED
        and record.reference.native_id == reference.native_id
    ]
    if not opened:
        raise validation.ConversationError(
            validation.ERROR_UNKNOWN_CONVERSATION,
            f"no opened conversation to confirm for {reference.locator}",
            {
                "session_id": reference.binding.session_id,
                "harness": reference.binding.harness,
            },
        )
    return storage._write_record(
        plan_state,
        kind=validation.KIND_CONVERSATION_CONFIRMED,
        reference=reference,
    )


def retire_conversation(
    plan_state: str | Path,
    reference: references.ConversationReference,
    *,
    reason: str,
) -> models.ConversationRecord:
    """Record that ``reference`` may never be selected again.

    Retirement is an append, not a deletion: the history of a completed session
    stays readable, and a run that has already read the ledger cannot be raced
    into resuming a conversation whose record vanished underneath it.
    """
    return storage._write_record(
        plan_state,
        kind=validation.KIND_CONVERSATION_RETIRED,
        reference=reference,
        retirement_reason=validation._narrow(
            reason, validation.RETIREMENT_REASONS, "retirement_reason"
        ),
    )


def retire_completed_session(
    plan_state: str | Path, *, session_id: str, harness: str | None = None
) -> tuple[models.ConversationRecord, ...]:
    """Retire every live conversation belonging to a completed plan session.

    A session that reached ``complete`` has no turn left to continue, so leaving
    its conversation resumable would let a later, unrelated attempt at the same
    session id continue work that already finished. ``harness=None`` retires
    every harness's conversation for the session, which is what completion means;
    naming one retires only that harness's.

    A ledger that cannot be fully read is refused rather than partially retired:
    the unreadable line may name the very conversation that would be left behind.
    """
    ledger = storage.read_conversations(plan_state)
    if not ledger.trustworthy:
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            "conversation ledger holds unreadable lines; refusing to retire "
            "against a partial history",
            {"rejected": len(ledger.rejected)},
        )
    wanted = validation._validate_session_id(session_id)
    selected: HarnessName | None = None
    if harness is not None:
        selected = validation._narrow(harness, HARNESS_NAMES, "harness")
    retired: list[models.ConversationRecord] = []
    for name in HARNESS_NAMES:
        if selected is not None and name != selected:
            continue
        history = ledger.history((wanted, name))
        if not history or history[-1].kind == validation.KIND_CONVERSATION_RETIRED:
            continue
        retired.append(
            retire_conversation(
                plan_state, history[-1].reference, reason="session-completed"
            )
        )
    return tuple(retired)


def select_conversation(
    plan_state: str | Path, binding: references.ConversationBinding
) -> models.ConversationSelection:
    """Answer whether ``binding``'s next turn may continue a saved conversation.

    The lookup is by ``(session_id, harness)`` and nothing else, and the record it
    finds is then *checked* against every other bound field. There is no fallback:
    a mismatch, an unconfirmed record, a retirement, or an unreadable ledger each
    yield a refusal naming why, and none of them yields a different conversation.
    That is what makes an invalid reference unable to select another conversation,
    and what leaves the recovery decision with the operator rather than here.
    """
    if not isinstance(binding, references.ConversationBinding):
        raise validation.ConversationError(
            validation.ERROR_INVALID_REFERENCE,
            "binding must be a ConversationBinding value",
        )
    ledger = storage.read_conversations(plan_state)
    if not ledger.trustworthy:
        return models.ConversationSelection(
            state=validation.SELECTION_CORRUPT,
            detail=(
                f"{len(ledger.rejected)} unreadable ledger line(s); an unreadable "
                "line may retire or supersede any conversation, so none may be "
                "selected"
            ),
        )
    history = ledger.history(binding.key)
    if not history:
        return models.ConversationSelection(
            state=validation.SELECTION_ABSENT,
            detail=(
                f"no conversation recorded for session {binding.session_id} under "
                f"{binding.harness}"
            ),
        )
    latest = history[-1]
    if latest.kind == validation.KIND_CONVERSATION_RETIRED:
        return models.ConversationSelection(
            state=validation.SELECTION_RETIRED,
            reference=latest.reference,
            detail=(
                f"conversation {latest.reference.locator} was retired "
                f"({latest.retirement_reason})"
            ),
        )
    mismatches = binding.mismatches(latest.reference.binding)
    if mismatches:
        return models.ConversationSelection(
            state=validation.SELECTION_INCOMPATIBLE,
            reference=latest.reference,
            mismatched_fields=mismatches,
            detail=(
                f"conversation {latest.reference.locator} is bound to a different "
                f"{', '.join(mismatches)}"
            ),
        )
    if latest.kind != validation.KIND_CONVERSATION_CONFIRMED:
        return models.ConversationSelection(
            state=validation.SELECTION_UNCONFIRMED,
            reference=latest.reference,
            detail=(
                f"conversation {latest.reference.locator} was recorded but no work "
                "was ever accepted on it"
            ),
        )
    return models.ConversationSelection(
        state=validation.SELECTION_RESUMABLE, reference=latest.reference
    )
