"""Conversation ledger models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import conversation_references as references
from . import conversation_validation as validation


@dataclass(frozen=True)
class ConversationRecord:
    """One readable line of the ledger, with the position it occupies.

    ``sequence`` is positional and one-based. A record returned by a writer
    carries the sequence its line actually took, so a caller can quote the exact
    line it appended.
    """

    sequence: int
    kind: validation.ConversationKind
    reference: references.ConversationReference
    recorded_at: str
    retirement_reason: validation.RetirementReason | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.reference.key

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": validation.CONVERSATION_SCHEMA_VERSION,
            "kind": self.kind,
            "recorded_at": self.recorded_at,
            "reference": self.reference.to_payload(),
        }
        if self.retirement_reason is not None:
            payload["retirement_reason"] = self.retirement_reason
        return payload


@dataclass(frozen=True)
class RejectedLine:
    """One physical line that could not be read, and why.

    Keeps its positional sequence: the line stays part of the history rather than
    being dropped to make the file parse, and its presence is what makes
    corruption visible to selection.
    """

    sequence: int
    reason: str
    raw: str


@dataclass(frozen=True)
class ConversationLedger:
    """Everything one plan's ledger holds, readable lines and unreadable ones."""

    records: tuple[ConversationRecord, ...]
    rejected: tuple[RejectedLine, ...]

    @property
    def trustworthy(self) -> bool:
        """Whether every physical line was accounted for.

        An unreadable line cannot be attributed to a key, so it may be the
        retirement or the supersession of any conversation in the file. Nothing
        may be selected while one exists.
        """
        return not self.rejected

    def history(self, key: tuple[str, str]) -> tuple[ConversationRecord, ...]:
        """Every readable record for one ``(session_id, harness)`` key, in order."""
        return tuple(record for record in self.records if record.key == key)

    def to_payload(self) -> dict[str, Any]:
        return {
            "records": [record.to_payload() for record in self.records],
            "rejected": [
                {"sequence": line.sequence, "reason": line.reason, "raw": line.raw}
                for line in self.rejected
            ],
        }


@dataclass(frozen=True)
class ConversationSelection:
    """The answer to "may this turn continue a saved conversation?".

    Exactly one state permits continuation. Every other state is a refusal the
    recovery policy sends to the operator, so callers test :attr:`resumable`
    rather than testing for absence.
    """

    state: validation.SelectionState
    reference: references.ConversationReference | None = None
    mismatched_fields: tuple[str, ...] = ()
    detail: str = ""

    @property
    def resumable(self) -> bool:
        return self.state == validation.SELECTION_RESUMABLE

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "conversation": None if self.reference is None else self.reference.locator,
            "mismatched_fields": list(self.mismatched_fields),
            "detail": self.detail,
        }
