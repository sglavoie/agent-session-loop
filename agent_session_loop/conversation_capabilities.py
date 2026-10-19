"""Conversation capabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import conversation_references as references
from . import conversation_validation as validation
from .harnesses import (
    HARNESS_NAMES,
    HarnessName,
    continues_conversations,
)


@dataclass(frozen=True)
class ContinuationIntent:
    """What the next turn is being asked to do about a conversation.

    Deliberately has no serialization. The recovery policy says an operator's
    fresh-conversation authorization is explicit and one-use and must never be
    persisted or automatically rehydrated -- so the value that carries it is one
    this module cannot write down. There is no ``to_payload`` here, and the
    ledger's writers accept references and retirements only.

    ``interrupted_operations`` holds operation *identifiers*, never the feedback
    that caused an interruption: the resumed worker is told which operations were
    cut short and reads their content from the channel that owns it.
    """

    mode: validation.ContinuationMode
    reference: references.ConversationReference | None = None
    interrupted_operations: tuple[str, ...] = ()
    fresh_authorized: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mode",
            validation._narrow(self.mode, validation.CONTINUATION_MODES, "mode"),
        )
        operations = tuple(self.interrupted_operations)
        if len(operations) > validation.MAX_INTERRUPTED_OPERATIONS:
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "interrupted_operations must hold at most "
                f"{validation.MAX_INTERRUPTED_OPERATIONS} entries; "
                f"got {len(operations)}",
            )
        object.__setattr__(
            self,
            "interrupted_operations",
            tuple(validation.validate_operation_id(entry) for entry in operations),
        )
        if self.mode == "resume":
            if not isinstance(self.reference, references.ConversationReference):
                raise validation.ConversationError(
                    validation.ERROR_UNKNOWN_CONVERSATION,
                    "a resume intent requires the conversation reference it continues",
                )
            if self.fresh_authorized:
                raise validation.ConversationError(
                    validation.ERROR_INVALID_REFERENCE,
                    "a resume intent must not carry a fresh-conversation authorization",
                )
            return
        if self.reference is not None:
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "a fresh intent must not name a conversation to continue",
            )
        if not self.fresh_authorized:
            raise validation.ConversationError(
                validation.ERROR_FRESH_NOT_AUTHORIZED,
                "starting a fresh conversation requires an explicit, one-use "
                "operator authorization",
            )

    @property
    def resumes(self) -> bool:
        return self.mode == "resume"

    def describe(self) -> dict[str, Any]:
        """An observable, feedback-free projection for status and events.

        Not a serialization: it names the conversation and the interrupted
        operations for an operator, and it is never read back into an intent. The
        authorization flag is *reported* rather than stored, because a run that
        started fresh should say so even though nothing may rehydrate it.
        """
        return {
            "mode": self.mode,
            "conversation": None if self.reference is None else self.reference.locator,
            "interrupted_operations": list(self.interrupted_operations),
            "fresh_authorized": self.fresh_authorized,
        }


def resume_intent(
    reference: references.ConversationReference,
    *,
    interrupted_operations: Sequence[str] = (),
) -> ContinuationIntent:
    """Build the intent that continues ``reference``."""
    return ContinuationIntent(
        mode="resume",
        reference=reference,
        interrupted_operations=tuple(interrupted_operations),
    )


def fresh_intent(
    *,
    authorized: bool,
    interrupted_operations: Sequence[str] = (),
) -> ContinuationIntent:
    """Build the intent that starts a fresh conversation.

    ``authorized`` is the operator's explicit, one-use permission. Passing
    ``False`` raises rather than downgrading to a fresh start, which is the whole
    point: no code path may turn a missing or incompatible conversation into a
    silent new one.
    """
    return ContinuationIntent(
        mode="fresh",
        interrupted_operations=tuple(interrupted_operations),
        fresh_authorized=authorized,
    )


@dataclass(frozen=True)
class HarnessCapability:
    """What one harness can do about conversation continuity in this build."""

    harness: HarnessName
    state: validation.CapabilityState
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "harness",
            validation._narrow(self.harness, HARNESS_NAMES, "harness"),
        )
        object.__setattr__(
            self,
            "state",
            validation._narrow(self.state, validation.CAPABILITY_STATES, "state"),
        )
        if not isinstance(self.detail, str):
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                f"detail must be a string; got {self.detail!r}",
            )
        object.__setattr__(
            self,
            "detail",
            self.detail.strip()[: validation.MAX_DETAIL_CHARS],
        )

    @property
    def available(self) -> bool:
        return self.state == "available"

    def to_payload(self) -> dict[str, Any]:
        return {"harness": self.harness, "state": self.state, "detail": self.detail}


@dataclass(frozen=True)
class CapabilityReport:
    """Conversation-continuity capability for every harness, as one value.

    The report is what an operator surface asks before offering continuation. It
    names *every* harness, including the ones that cannot do it, so a caller
    iterating the report cannot silently omit a harness it forgot about.
    """

    capabilities: tuple[HarnessCapability, ...]
    version: int = validation.CONVERSATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        capabilities = tuple(self.capabilities)
        if any(
            not isinstance(capability, HarnessCapability) for capability in capabilities
        ):
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                "capabilities entries must be HarnessCapability values",
            )
        named = sorted(capability.harness for capability in capabilities)
        if named != sorted(HARNESS_NAMES):
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                "a capability report must name every harness exactly once; "
                f"got {named}",
            )
        order = {name: index for index, name in enumerate(HARNESS_NAMES)}
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted(capabilities, key=lambda entry: order[entry.harness])),
        )
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version not in validation.SUPPORTED_CONVERSATION_SCHEMA_VERSIONS
        ):
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                "version must be one of "
                f"{sorted(validation.SUPPORTED_CONVERSATION_SCHEMA_VERSIONS)}; "
                f"got {self.version!r}",
            )

    def for_harness(self, name: str) -> HarnessCapability:
        harness = validation._narrow(name, HARNESS_NAMES, "harness")
        for capability in self.capabilities:
            if capability.harness == harness:
                return capability
        raise AssertionError("capability report is missing a validated harness")

    @property
    def available_harnesses(self) -> tuple[HarnessName, ...]:
        return tuple(
            capability.harness
            for capability in self.capabilities
            if capability.available
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "capabilities": [
                capability.to_payload() for capability in self.capabilities
            ],
        }


#: Why a harness is staged rather than available. The contracts, the durable
#: store, and the turn-lifecycle seam exist; *this* adapter does not report or
#: resume a native conversation through that seam yet, and an operator action
#: backed by incomplete recovery must not be offered for it.
STAGED_CAPABILITY_DETAIL = (
    "conversation contracts, durable storage, and the turn lifecycle seam are "
    "in place; this harness does not capture or resume a native conversation yet"
)


#: Why a harness whose adapter does capture and resume is available.
AVAILABLE_CAPABILITY_DETAIL = (
    "this harness reports the conversation each turn runs on and can start a "
    "turn on a conversation it is handed"
)


def capability_report() -> CapabilityReport:
    """Return this build's conversation-continuity capability for each harness.

    Derived from the adapters rather than declared here: a harness is
    ``available`` exactly when its adapter both reports the conversation a turn
    ran on and accepts one to resume, which is the same pair of flags
    ``HarnessAdapter.run`` enforces when it refuses a capture sink or a resume
    intent. One producer, so this report cannot promise recovery the turn
    boundary would refuse to perform. Every shipped harness is ``available``
    today; ``staged`` remains the answer for one that is wired later.
    """
    return CapabilityReport(
        capabilities=tuple(
            HarnessCapability(
                harness=name,
                state="available" if continues_conversations(name) else "staged",
                detail=(
                    AVAILABLE_CAPABILITY_DETAIL
                    if continues_conversations(name)
                    else STAGED_CAPABILITY_DETAIL
                ),
            )
            for name in HARNESS_NAMES
        )
    )


def parse_capability_report(payload: object) -> CapabilityReport:
    """Read a capability report back from a stored or transported payload."""
    if not isinstance(payload, Mapping):
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            f"capability report must be an object; got {payload!r}",
        )
    entries = payload.get("capabilities", ())
    if isinstance(entries, str) or not isinstance(entries, Sequence):
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            f"capabilities must be a sequence; got {entries!r}",
        )
    capabilities: list[HarnessCapability] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                f"capability must be an object; got {entry!r}",
            )
        capabilities.append(
            HarnessCapability(
                harness=validation._narrow(
                    entry.get("harness"),
                    HARNESS_NAMES,
                    "harness",
                    reason=validation.ERROR_INVALID_RECORD,
                ),
                state=validation._narrow(
                    entry.get("state"),
                    validation.CAPABILITY_STATES,
                    "state",
                    reason=validation.ERROR_INVALID_RECORD,
                ),
                detail=entry.get("detail", ""),
            )
        )
    return CapabilityReport(
        capabilities=tuple(capabilities),
        version=payload.get("version", validation.CONVERSATION_SCHEMA_VERSION),
    )
