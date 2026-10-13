"""Message kinds, delivery vocabularies, and guidance-delivery phrasing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

MessageKind = Literal[
    "guidance",
    "question",
    "reply",
    "resolution",
    "notice",
    "ack",
    "wait-start",
    "wait-end",
    "receipt",
    "supervision-end",
    "guidance-delivery",
]


ResolutionAction = Literal["continue", "pause"]


WaiterOrigin = Literal["worker", "runner"]


GuidanceDeliveryState = Literal[
    "queued",
    "delivered-live",
    "delivered-next-turn",
    "delivered-by-read",
    "deferred",
]


GuidanceDeliveryChannel = Literal["live", "next-turn", "read"]


GuidanceDeliveryReason = Literal[
    "delivered",
    "harness-unsupported",
    "no-active-turn",
    "stale-turn",
    "transport-unavailable",
    "unsupported-active-steer",
    "awaiting-confirmation",
]


DeliveryMode = Literal["async", "wait"]


WaitEndReason = Literal["timeout", "closure", "error"]


SupervisionOutcome = Literal[
    "complete",
    "paused",
    "stopped",
    "blocked",
    "error",
]


KIND_GUIDANCE: MessageKind = "guidance"


KIND_QUESTION: MessageKind = "question"


KIND_REPLY: MessageKind = "reply"


KIND_RESOLUTION: MessageKind = "resolution"


KIND_NOTICE: MessageKind = "notice"


KIND_ACK: MessageKind = "ack"


KIND_WAIT_START: MessageKind = "wait-start"


KIND_WAIT_END: MessageKind = "wait-end"


KIND_RECEIPT: MessageKind = "receipt"


KIND_SUPERVISION_END: MessageKind = "supervision-end"


KIND_GUIDANCE_DELIVERY: MessageKind = "guidance-delivery"


MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_NOTICE,
    KIND_ACK,
    KIND_WAIT_START,
    KIND_WAIT_END,
    KIND_RECEIPT,
    KIND_SUPERVISION_END,
    KIND_GUIDANCE_DELIVERY,
)


V3_MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_NOTICE,
    KIND_ACK,
    KIND_WAIT_START,
    KIND_WAIT_END,
    KIND_RECEIPT,
    KIND_SUPERVISION_END,
)


V2_MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_NOTICE,
    KIND_ACK,
)


LEGACY_MESSAGE_KINDS: tuple[MessageKind, ...] = (
    KIND_GUIDANCE,
    KIND_QUESTION,
    KIND_REPLY,
    KIND_ACK,
)


CLOSURE_KINDS: tuple[MessageKind, ...] = (KIND_REPLY, KIND_RESOLUTION)


LIFECYCLE_KINDS: tuple[MessageKind, ...] = (
    KIND_WAIT_START,
    KIND_WAIT_END,
    KIND_RECEIPT,
)


DELIVERY_MODE_ASYNC: DeliveryMode = "async"


DELIVERY_MODE_WAIT: DeliveryMode = "wait"


DELIVERY_MODES: tuple[DeliveryMode, ...] = (
    DELIVERY_MODE_ASYNC,
    DELIVERY_MODE_WAIT,
)


WAIT_END_REASON_TIMEOUT: WaitEndReason = "timeout"


WAIT_END_REASON_CLOSURE: WaitEndReason = "closure"


WAIT_END_REASON_ERROR: WaitEndReason = "error"


WAIT_END_REASONS: tuple[WaitEndReason, ...] = (
    WAIT_END_REASON_TIMEOUT,
    WAIT_END_REASON_CLOSURE,
    WAIT_END_REASON_ERROR,
)


SUPERVISION_OUTCOME_COMPLETE: SupervisionOutcome = "complete"


SUPERVISION_OUTCOME_PAUSED: SupervisionOutcome = "paused"


SUPERVISION_OUTCOME_STOPPED: SupervisionOutcome = "stopped"


SUPERVISION_OUTCOME_BLOCKED: SupervisionOutcome = "blocked"


SUPERVISION_OUTCOME_ERROR: SupervisionOutcome = "error"


SUPERVISION_OUTCOMES: tuple[SupervisionOutcome, ...] = (
    SUPERVISION_OUTCOME_COMPLETE,
    SUPERVISION_OUTCOME_PAUSED,
    SUPERVISION_OUTCOME_STOPPED,
    SUPERVISION_OUTCOME_BLOCKED,
    SUPERVISION_OUTCOME_ERROR,
)


RESOLUTION_ACTION_CONTINUE: ResolutionAction = "continue"


RESOLUTION_ACTION_PAUSE: ResolutionAction = "pause"


RESOLUTION_ACTIONS: tuple[ResolutionAction, ...] = (
    RESOLUTION_ACTION_CONTINUE,
    RESOLUTION_ACTION_PAUSE,
)


WAITER_ORIGIN_WORKER: WaiterOrigin = "worker"


WAITER_ORIGIN_RUNNER: WaiterOrigin = "runner"


WAITER_ORIGINS: tuple[WaiterOrigin, ...] = (
    WAITER_ORIGIN_WORKER,
    WAITER_ORIGIN_RUNNER,
)


# ``queued`` is the state of guidance no delivery record speaks about yet, so
# it is projected rather than recorded: an append that lands with no receipt is
# already queued, and writing a record to say so would be redundant history.
GUIDANCE_STATE_QUEUED: GuidanceDeliveryState = "queued"


GUIDANCE_STATE_DELIVERED_LIVE: GuidanceDeliveryState = "delivered-live"


GUIDANCE_STATE_DELIVERED_NEXT_TURN: GuidanceDeliveryState = "delivered-next-turn"


GUIDANCE_STATE_DELIVERED_BY_READ: GuidanceDeliveryState = "delivered-by-read"


GUIDANCE_STATE_DEFERRED: GuidanceDeliveryState = "deferred"


# The states a delivery attempt may record. Every one of them is terminal for
# its own attempt; only the ``delivered-*`` subset is terminal for the guidance.
GUIDANCE_DELIVERY_STATES: tuple[GuidanceDeliveryState, ...] = (
    GUIDANCE_STATE_DELIVERED_LIVE,
    GUIDANCE_STATE_DELIVERED_NEXT_TURN,
    GUIDANCE_STATE_DELIVERED_BY_READ,
    GUIDANCE_STATE_DEFERRED,
)


GUIDANCE_DELIVERED_STATES: tuple[GuidanceDeliveryState, ...] = (
    GUIDANCE_STATE_DELIVERED_LIVE,
    GUIDANCE_STATE_DELIVERED_NEXT_TURN,
    GUIDANCE_STATE_DELIVERED_BY_READ,
)


# The full projected vocabulary, including the state no record carries.
GUIDANCE_STATES: tuple[GuidanceDeliveryState, ...] = (
    GUIDANCE_STATE_QUEUED,
    *GUIDANCE_DELIVERY_STATES,
)


GUIDANCE_CHANNEL_LIVE: GuidanceDeliveryChannel = "live"


GUIDANCE_CHANNEL_NEXT_TURN: GuidanceDeliveryChannel = "next-turn"


GUIDANCE_CHANNEL_READ: GuidanceDeliveryChannel = "read"


GUIDANCE_DELIVERY_CHANNELS: tuple[GuidanceDeliveryChannel, ...] = (
    GUIDANCE_CHANNEL_LIVE,
    GUIDANCE_CHANNEL_NEXT_TURN,
    GUIDANCE_CHANNEL_READ,
)


# Each successful state names the one channel that can produce it, so a record
# cannot claim a live delivery over the next-turn prompt. A deferred attempt
# records the channel it tried, and any channel may defer.
GUIDANCE_DELIVERED_STATE_CHANNEL: dict[
    GuidanceDeliveryState, GuidanceDeliveryChannel
] = {
    GUIDANCE_STATE_DELIVERED_LIVE: GUIDANCE_CHANNEL_LIVE,
    GUIDANCE_STATE_DELIVERED_NEXT_TURN: GUIDANCE_CHANNEL_NEXT_TURN,
    GUIDANCE_STATE_DELIVERED_BY_READ: GUIDANCE_CHANNEL_READ,
}


GUIDANCE_REASON_DELIVERED: GuidanceDeliveryReason = "delivered"


GUIDANCE_REASON_HARNESS_UNSUPPORTED: GuidanceDeliveryReason = "harness-unsupported"


GUIDANCE_REASON_NO_ACTIVE_TURN: GuidanceDeliveryReason = "no-active-turn"


GUIDANCE_REASON_STALE_TURN: GuidanceDeliveryReason = "stale-turn"


GUIDANCE_REASON_TRANSPORT_UNAVAILABLE: GuidanceDeliveryReason = "transport-unavailable"


# A harness that has a live channel and no same-turn steer in its protocol.
# Kept apart from ``harness-unsupported`` so a supervisor can tell "nothing was
# listening" from "the harness was asked and cannot do this at all".
GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER: GuidanceDeliveryReason = (
    "unsupported-active-steer"
)


# The guidance reached the transport but no confirmable boundary has reported
# the model receiving it. The one deferral that is not a verdict.
GUIDANCE_REASON_AWAITING_CONFIRMATION: GuidanceDeliveryReason = "awaiting-confirmation"


GUIDANCE_DELIVERED_REASONS: tuple[GuidanceDeliveryReason, ...] = (
    GUIDANCE_REASON_DELIVERED,
)


GUIDANCE_DEFERRAL_REASONS: tuple[GuidanceDeliveryReason, ...] = (
    GUIDANCE_REASON_HARNESS_UNSUPPORTED,
    GUIDANCE_REASON_NO_ACTIVE_TURN,
    GUIDANCE_REASON_STALE_TURN,
    GUIDANCE_REASON_TRANSPORT_UNAVAILABLE,
    GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER,
    GUIDANCE_REASON_AWAITING_CONFIRMATION,
)


GUIDANCE_DELIVERY_REASONS: tuple[GuidanceDeliveryReason, ...] = (
    *GUIDANCE_DELIVERED_REASONS,
    *GUIDANCE_DEFERRAL_REASONS,
)


#: How each state reads to an operator. One phrase per state, defined once
#: here so every surface -- ``inbox guidance``, ``status``, the supervisor
#: guide -- says the same thing about the same fact. "Sent" appears nowhere:
#: the whole point of the ledger is that appending guidance and delivering it
#: are different events, and one word for both is what hid the difference.
GUIDANCE_STATE_PHRASES: dict[GuidanceDeliveryState, str] = {
    GUIDANCE_STATE_QUEUED: "queued but not delivered",
    GUIDANCE_STATE_DELIVERED_LIVE: "delivered live",
    GUIDANCE_STATE_DELIVERED_NEXT_TURN: "delivered with the next turn's prompt",
    GUIDANCE_STATE_DELIVERED_BY_READ: "delivered by an explicit inbox read",
    GUIDANCE_STATE_DEFERRED: "queued but not delivered",
}


#: Why a deferral happened, in the operator's terms rather than the ledger's.
#: Absent reasons fall back to the bare constant, which is still honest.
GUIDANCE_REASON_PHRASES: dict[GuidanceDeliveryReason, str] = {
    GUIDANCE_REASON_HARNESS_UNSUPPORTED: ("this harness has no live guidance channel"),
    GUIDANCE_REASON_NO_ACTIVE_TURN: "no turn was running",
    GUIDANCE_REASON_STALE_TURN: "the turn ended first",
    GUIDANCE_REASON_TRANSPORT_UNAVAILABLE: "the live channel failed",
    GUIDANCE_REASON_UNSUPPORTED_ACTIVE_STEER: (
        "this harness cannot be steered mid-turn"
    ),
    GUIDANCE_REASON_AWAITING_CONFIRMATION: (
        "the harness has not confirmed receipt yet"
    ),
}


def _phrase_for[KeyT: str](phrases: Mapping[KeyT, str], value: str) -> str:
    """Render ``value`` through ``phrases``, falling back to the bare value.

    A plain ``.get`` cannot express this: the tables are keyed by their
    ``Literal`` vocabulary -- which is what makes a typo in the table itself a
    type error -- while the callers deliberately accept any string so an
    unrecognized record still renders as something. Matching against the keys
    keeps both properties without widening either.
    """
    for permitted, phrase in phrases.items():
        if permitted == value:
            return phrase
    return value


def describe_guidance_delivery(
    delivery_state: GuidanceDeliveryState | str,
    *,
    delivery_reason: GuidanceDeliveryReason | str | None = None,
    harness: str | None = None,
) -> str:
    """Render one guidance record's standing as an operator-facing phrase.

    Deliberately a phrase and not a sentence, so callers can prefix it with
    whatever names the record ("Guidance g-1 …", "Latest guidance: …") without
    duplicating the vocabulary. A delivered state names the harness that
    received it when one is known, because "delivered" is a claim about a
    specific worker; a deferral names why, because "not delivered" is only
    actionable with the reason attached.
    """
    phrase = _phrase_for(GUIDANCE_STATE_PHRASES, str(delivery_state))
    if delivery_state in GUIDANCE_DELIVERED_STATES:
        return f"{phrase} to {harness}" if harness else phrase
    if delivery_reason and delivery_reason != GUIDANCE_REASON_DELIVERED:
        detail = _phrase_for(GUIDANCE_REASON_PHRASES, str(delivery_reason))
        return f"{phrase} ({detail})"
    return phrase
