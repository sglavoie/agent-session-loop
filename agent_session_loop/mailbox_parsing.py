"""Parse raw mailbox bytes into a validated view, rejecting malformed lines."""

from __future__ import annotations

import json
from pathlib import Path

from .mailbox_constants import (
    MAILBOX_LEGACY_SCHEMA_VERSION,
    MAILBOX_PREVIOUS_SCHEMA_VERSION,
    MAILBOX_SUPPORTED_SCHEMA_VERSIONS,
    MAILBOX_WAIT_SCHEMA_VERSION,
    REJECT_CLOSURE_ALREADY_CONSUMED,
    REJECT_DUPLICATE_ID,
    REJECT_EMPTY_LINE,
    REJECT_GUIDANCE_ALREADY_DELIVERED,
    REJECT_GUIDANCE_NOT_FOUND,
    REJECT_INCOMPLETE_TURN_IDENTITY,
    REJECT_INVALID_ACKNOWLEDGEMENT,
    REJECT_INVALID_ACTION,
    REJECT_INVALID_CLOSURE_ID,
    REJECT_INVALID_CREATED_AT,
    REJECT_INVALID_DELIVERY_CHANNEL,
    REJECT_INVALID_DELIVERY_MODE,
    REJECT_INVALID_DELIVERY_REASON,
    REJECT_INVALID_DELIVERY_STATE,
    REJECT_INVALID_HARNESS,
    REJECT_INVALID_ID,
    REJECT_INVALID_JSON,
    REJECT_INVALID_NOTE,
    REJECT_INVALID_RUN_ID,
    REJECT_INVALID_SESSION_ID,
    REJECT_INVALID_SUPERVISION_ID,
    REJECT_INVALID_SUPERVISION_OUTCOME,
    REJECT_INVALID_TARGET,
    REJECT_INVALID_TURN_INDEX,
    REJECT_INVALID_WAIT_END_REASON,
    REJECT_INVALID_WAIT_ID,
    REJECT_INVALID_WAITER_ORIGIN,
    REJECT_MISSING_TEXT,
    REJECT_NOT_OBJECT,
    REJECT_QUESTION_ALREADY_ANSWERED,
    REJECT_QUESTION_ALREADY_RESOLVED,
    REJECT_QUESTION_ALREADY_WAITING,
    REJECT_QUESTION_NOT_FOUND,
    REJECT_QUESTION_NOT_OPEN,
    REJECT_QUESTION_NOT_WAITING,
    REJECT_UNEXPECTED_TEXT,
    REJECT_UNKNOWN_KIND,
    REJECT_UNSUPPORTED_VERSION,
)
from .mailbox_models import MailboxMessage, RejectedMailboxRecord
from .mailbox_snapshot import MailboxView
from .mailbox_validation import (
    _is_uuid,
    _parse_timestamp,
    _physical_lines,
    _reject,
    _valid_harness,
    _valid_run_id,
    _valid_session_id,
    _valid_turn_index,
)
from .mailbox_vocabulary import (
    CLOSURE_KINDS,
    DELIVERY_MODE_WAIT,
    DELIVERY_MODES,
    GUIDANCE_DEFERRAL_REASONS,
    GUIDANCE_DELIVERED_REASONS,
    GUIDANCE_DELIVERED_STATE_CHANNEL,
    GUIDANCE_DELIVERED_STATES,
    GUIDANCE_DELIVERY_CHANNELS,
    GUIDANCE_DELIVERY_STATES,
    GUIDANCE_STATE_DEFERRED,
    KIND_ACK,
    KIND_GUIDANCE,
    KIND_GUIDANCE_DELIVERY,
    KIND_NOTICE,
    KIND_QUESTION,
    KIND_RECEIPT,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_SUPERVISION_END,
    KIND_WAIT_END,
    KIND_WAIT_START,
    LEGACY_MESSAGE_KINDS,
    LIFECYCLE_KINDS,
    MESSAGE_KINDS,
    RESOLUTION_ACTIONS,
    SUPERVISION_OUTCOMES,
    V2_MESSAGE_KINDS,
    V3_MESSAGE_KINDS,
    WAIT_END_REASON_CLOSURE,
    WAIT_END_REASONS,
    WAITER_ORIGINS,
    DeliveryMode,
    GuidanceDeliveryChannel,
    GuidanceDeliveryReason,
    GuidanceDeliveryState,
    ResolutionAction,
    SupervisionOutcome,
    WaitEndReason,
    WaiterOrigin,
)


def _parse_mailbox_bytes(data: bytes, mailbox_path: Path) -> MailboxView:
    messages: list[MailboxMessage] = []
    rejected: list[RejectedMailboxRecord] = []
    seen_ids: set[str] = set()
    questions: dict[str, MailboxMessage] = {}
    closures: dict[str, MailboxMessage] = {}
    wait_starts: dict[str, MailboxMessage] = {}
    active_waits: dict[str, MailboxMessage] = {}
    receipts: dict[str, MailboxMessage] = {}
    guidance_records: dict[str, MailboxMessage] = {}
    terminal_deliveries: set[tuple[str, str | None, str | None, int | None]] = set()
    physical_lines = _physical_lines(data)

    for seq, raw_bytes in enumerate(physical_lines, start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if raw.strip() == "":
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_EMPTY_LINE,
                raw=raw,
            )
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_JSON,
                raw=raw,
                detail=str(error),
            )
            continue
        if not isinstance(payload, dict):
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_NOT_OBJECT,
                raw=raw,
            )
            continue

        # Versionless records predate the v2 writer and therefore retain the
        # legacy v1 interpretation even after the current write version moves.
        version = payload.get("version", MAILBOX_LEGACY_SCHEMA_VERSION)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version not in MAILBOX_SUPPORTED_SCHEMA_VERSIONS
        ):
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNSUPPORTED_VERSION,
                raw=raw,
                detail=str(version),
            )
            continue

        kind = payload.get("kind")
        allowed_kinds = (
            LEGACY_MESSAGE_KINDS
            if version == MAILBOX_LEGACY_SCHEMA_VERSION
            else V2_MESSAGE_KINDS
            if version == MAILBOX_PREVIOUS_SCHEMA_VERSION
            else V3_MESSAGE_KINDS
            if version == MAILBOX_WAIT_SCHEMA_VERSION
            else MESSAGE_KINDS
        )
        if kind not in allowed_kinds:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_UNKNOWN_KIND,
                raw=raw,
                detail=str(kind),
            )
            continue

        message_id = payload.get("id")
        if not _is_uuid(message_id):
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_ID,
                raw=raw,
            )
            continue
        assert isinstance(message_id, str)
        if message_id in seen_ids:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_DUPLICATE_ID,
                raw=raw,
                detail=message_id,
            )
            continue

        created_at = payload.get("created_at")
        if _parse_timestamp(created_at) is None:
            _reject(
                rejected,
                seq=seq,
                reason=REJECT_INVALID_CREATED_AT,
                raw=raw,
            )
            continue
        assert isinstance(created_at, str)

        text: str | None = None
        target: str | None = None
        upto: int | None = None
        run_id: str | None = None
        session_id: str | None = None
        turn_index: int | None = None
        waiter_origin: WaiterOrigin | None = None
        action: ResolutionAction | None = None
        note: str | None = None
        delivery_mode: DeliveryMode | None = None
        wait_id: str | None = None
        wait_end_reason: WaitEndReason | None = None
        closure_id: str | None = None
        supervision_outcome: SupervisionOutcome | None = None
        supervision_id: str | None = None
        delivery_state: GuidanceDeliveryState | None = None
        delivery_reason: GuidanceDeliveryReason | None = None
        channel: GuidanceDeliveryChannel | None = None
        harness: str | None = None

        if kind in (
            KIND_GUIDANCE,
            KIND_QUESTION,
            KIND_REPLY,
            KIND_NOTICE,
            KIND_SUPERVISION_END,
        ):
            text_value = payload.get("text")
            if not isinstance(text_value, str) or text_value.strip() == "":
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_MISSING_TEXT,
                    raw=raw,
                )
                continue
            text = text_value

        if kind == KIND_QUESTION:
            if version == MAILBOX_LEGACY_SCHEMA_VERSION:
                # v1 accepted ownership before it was part of the schema and
                # deliberately degraded malformed owners to unattributed.
                if _valid_run_id(payload.get("run_id")):
                    run_id = payload["run_id"]
            else:
                if "run_id" in payload:
                    if not _valid_run_id(payload.get("run_id")):
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_RUN_ID,
                            raw=raw,
                        )
                        continue
                    run_id = payload["run_id"]
                if "session_id" in payload:
                    if not _valid_session_id(payload.get("session_id")):
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_SESSION_ID,
                            raw=raw,
                        )
                        continue
                    session_id = payload["session_id"]
                if "turn_index" in payload:
                    if not _valid_turn_index(payload.get("turn_index")):
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_TURN_INDEX,
                            raw=raw,
                        )
                        continue
                    turn_index = payload["turn_index"]
                if "waiter_origin" in payload:
                    waiter_value = payload.get("waiter_origin")
                    if waiter_value not in WAITER_ORIGINS:
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_WAITER_ORIGIN,
                            raw=raw,
                            detail=str(waiter_value),
                        )
                        continue
                    waiter_origin = waiter_value
                if version >= MAILBOX_WAIT_SCHEMA_VERSION:
                    delivery_value = payload.get("delivery_mode")
                    if delivery_value not in DELIVERY_MODES:
                        _reject(
                            rejected,
                            seq=seq,
                            reason=REJECT_INVALID_DELIVERY_MODE,
                            raw=raw,
                            detail=str(delivery_value),
                        )
                        continue
                    delivery_mode = delivery_value

        if kind == KIND_NOTICE:
            run_value = payload.get("run_id")
            session_value = payload.get("session_id")
            turn_value = payload.get("turn_index")
            if not _valid_run_id(run_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_RUN_ID,
                    raw=raw,
                )
                continue
            if not _valid_session_id(session_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_SESSION_ID,
                    raw=raw,
                )
                continue
            if not _valid_turn_index(turn_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TURN_INDEX,
                    raw=raw,
                )
                continue
            run_id = run_value
            session_id = session_value
            turn_index = turn_value

        if kind == KIND_SUPERVISION_END:
            outcome_value = payload.get("outcome")
            if outcome_value not in SUPERVISION_OUTCOMES:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_SUPERVISION_OUTCOME,
                    raw=raw,
                    detail=str(outcome_value),
                )
                continue
            if "run_id" in payload:
                if not _valid_run_id(payload.get("run_id")):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_RUN_ID,
                        raw=raw,
                    )
                    continue
                run_id = payload["run_id"]
            if "supervision_id" in payload:
                if not _is_uuid(payload.get("supervision_id")):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_SUPERVISION_ID,
                        raw=raw,
                    )
                    continue
                supervision_id = payload["supervision_id"]
            supervision_outcome = outcome_value

        if kind == KIND_GUIDANCE_DELIVERY:
            if "text" in payload:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_UNEXPECTED_TEXT,
                    raw=raw,
                )
                continue
            target_value = payload.get("to")
            if not _is_uuid(target_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TARGET,
                    raw=raw,
                )
                continue
            assert isinstance(target_value, str)
            target = target_value
            if target not in guidance_records:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_GUIDANCE_NOT_FOUND,
                    raw=raw,
                    detail=target,
                )
                continue
            state_value = payload.get("delivery_state")
            if state_value not in GUIDANCE_DELIVERY_STATES:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_DELIVERY_STATE,
                    raw=raw,
                    detail=str(state_value),
                )
                continue
            channel_value = payload.get("channel")
            if channel_value not in GUIDANCE_DELIVERY_CHANNELS or (
                state_value in GUIDANCE_DELIVERED_STATES
                and channel_value != GUIDANCE_DELIVERED_STATE_CHANNEL[state_value]
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_DELIVERY_CHANNEL,
                    raw=raw,
                    detail=str(channel_value),
                )
                continue
            reason_value = payload.get("delivery_reason")
            allowed_reasons = (
                GUIDANCE_DEFERRAL_REASONS
                if state_value == GUIDANCE_STATE_DEFERRED
                else GUIDANCE_DELIVERED_REASONS
            )
            if reason_value not in allowed_reasons:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_DELIVERY_REASON,
                    raw=raw,
                    detail=str(reason_value),
                )
                continue
            identity_keys = ("run_id", "session_id", "turn_index")
            present = tuple(key for key in identity_keys if key in payload)
            if present not in ((), identity_keys):
                # A partial turn identity cannot key the one-terminal-attempt
                # rule, so it is refused rather than silently widened to "no
                # turn" — which would let a second terminal attempt through.
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INCOMPLETE_TURN_IDENTITY,
                    raw=raw,
                    detail=",".join(present),
                )
                continue
            if present:
                if not _valid_run_id(payload.get("run_id")):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_RUN_ID,
                        raw=raw,
                    )
                    continue
                if not _valid_session_id(payload.get("session_id")):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_SESSION_ID,
                        raw=raw,
                    )
                    continue
                if not _valid_turn_index(payload.get("turn_index")):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_TURN_INDEX,
                        raw=raw,
                    )
                    continue
                run_id = payload["run_id"]
                session_id = payload["session_id"]
                turn_index = payload["turn_index"]
            if "harness" in payload and not _valid_harness(payload.get("harness")):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_HARNESS,
                    raw=raw,
                )
                continue
            identity = (target, run_id, session_id, turn_index)
            if (
                state_value in GUIDANCE_DELIVERED_STATES
                and identity in terminal_deliveries
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_GUIDANCE_ALREADY_DELIVERED,
                    raw=raw,
                    detail=target,
                )
                continue
            delivery_state = state_value
            delivery_reason = reason_value
            channel = channel_value
            harness = payload.get("harness")

        if kind in CLOSURE_KINDS:
            target_value = payload.get("to")
            if not _is_uuid(target_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TARGET,
                    raw=raw,
                )
                continue
            assert isinstance(target_value, str)
            target = target_value
            if target not in questions:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_FOUND,
                    raw=raw,
                    detail=target,
                )
                continue
            prior_closure = closures.get(target)
            if prior_closure is not None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=(
                        REJECT_QUESTION_ALREADY_ANSWERED
                        if prior_closure.kind == KIND_REPLY
                        else REJECT_QUESTION_ALREADY_RESOLVED
                    ),
                    raw=raw,
                    detail=target,
                )
                continue
            question = questions[target]
            if (
                question.version >= MAILBOX_WAIT_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
                and target not in active_waits
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_WAITING,
                    raw=raw,
                    detail=target,
                )
                continue

        if kind == KIND_RESOLUTION:
            run_value = payload.get("run_id")
            if not _valid_run_id(run_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_RUN_ID,
                    raw=raw,
                )
                continue
            assert target is not None
            if questions[target].run_id != run_value:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_OPEN,
                    raw=raw,
                    detail=target,
                )
                continue
            action_value = payload.get("action")
            if action_value not in RESOLUTION_ACTIONS:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_ACTION,
                    raw=raw,
                    detail=str(action_value),
                )
                continue
            if "note" in payload and not isinstance(payload.get("note"), str):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_NOTE,
                    raw=raw,
                )
                continue
            run_id = run_value
            action = action_value
            note = payload.get("note")

        if kind == KIND_ACK:
            upto_value = payload.get("upto")
            if (
                isinstance(upto_value, bool)
                or not isinstance(upto_value, int)
                or upto_value < 0
                or upto_value >= seq
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_ACKNOWLEDGEMENT,
                    raw=raw,
                    detail=str(upto_value),
                )
                continue
            upto = upto_value

        if kind in LIFECYCLE_KINDS:
            target_value = payload.get("to")
            if not _is_uuid(target_value):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_TARGET,
                    raw=raw,
                )
                continue
            assert isinstance(target_value, str)
            target = target_value
            question = questions.get(target)
            if question is None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_FOUND,
                    raw=raw,
                    detail=target,
                )
                continue
        if kind == KIND_WAIT_START:
            assert target is not None
            if target in closures:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_NOT_OPEN,
                    raw=raw,
                    detail=target,
                )
                continue
            if target in active_waits:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_QUESTION_ALREADY_WAITING,
                    raw=raw,
                    detail=target,
                )
                continue

        if kind == KIND_WAIT_END:
            assert target is not None
            wait_value = payload.get("wait_id")
            active_wait = active_waits.get(target)
            if (
                not _is_uuid(wait_value)
                or active_wait is None
                or active_wait.id != wait_value
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_WAIT_ID,
                    raw=raw,
                    detail=str(wait_value),
                )
                continue
            wait_id = wait_value

        if kind == KIND_RECEIPT:
            assert target is not None
            wait_value = payload.get("wait_id")
            active_wait = active_waits.get(target)
            question = questions[target]
            strict = (
                question.version >= MAILBOX_WAIT_SCHEMA_VERSION
                and question.delivery_mode == DELIVERY_MODE_WAIT
            )
            if wait_value is not None:
                if (
                    not _is_uuid(wait_value)
                    or active_wait is None
                    or active_wait.id != wait_value
                ):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_WAIT_ID,
                        raw=raw,
                        detail=str(wait_value),
                    )
                    continue
                wait_id = wait_value
            elif strict or active_wait is not None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=(
                        REJECT_QUESTION_NOT_WAITING
                        if strict
                        else REJECT_INVALID_WAIT_ID
                    ),
                    raw=raw,
                    detail=target,
                )
                continue

        if kind == KIND_WAIT_END:
            assert target is not None
            reason_value = payload.get("reason")
            if reason_value not in WAIT_END_REASONS:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_WAIT_END_REASON,
                    raw=raw,
                    detail=str(reason_value),
                )
                continue
            wait_end_reason = reason_value
            closure_value = payload.get("closure_id")
            closure = closures.get(target)
            if reason_value == WAIT_END_REASON_CLOSURE:
                if (
                    not _is_uuid(closure_value)
                    or closure is None
                    or closure.id != closure_value
                    or closure_value not in receipts
                ):
                    _reject(
                        rejected,
                        seq=seq,
                        reason=REJECT_INVALID_CLOSURE_ID,
                        raw=raw,
                        detail=str(closure_value),
                    )
                    continue
                closure_id = closure_value
            elif closure_value is not None or closure is not None:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_CLOSURE_ID,
                    raw=raw,
                    detail=str(closure_value),
                )
                continue

        if kind == KIND_RECEIPT:
            assert target is not None
            closure_value = payload.get("closure_id")
            closure = closures.get(target)
            if (
                not _is_uuid(closure_value)
                or closure is None
                or closure.id != closure_value
            ):
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_INVALID_CLOSURE_ID,
                    raw=raw,
                    detail=str(closure_value),
                )
                continue
            if closure_value in receipts:
                _reject(
                    rejected,
                    seq=seq,
                    reason=REJECT_CLOSURE_ALREADY_CONSUMED,
                    raw=raw,
                    detail=closure_value,
                )
                continue
            closure_id = closure_value

        message = MailboxMessage(
            seq=seq,
            id=message_id,
            kind=kind,
            created_at=created_at,
            text=text,
            to=target,
            upto=upto,
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
            waiter_origin=waiter_origin,
            action=action,
            note=note,
            delivery_mode=delivery_mode,
            wait_id=wait_id,
            wait_end_reason=wait_end_reason,
            closure_id=closure_id,
            supervision_outcome=supervision_outcome,
            supervision_id=supervision_id,
            delivery_state=delivery_state,
            delivery_reason=delivery_reason,
            channel=channel,
            harness=harness,
            version=version,
        )
        messages.append(message)
        seen_ids.add(message_id)
        if kind == KIND_GUIDANCE:
            guidance_records[message_id] = message
        elif kind == KIND_GUIDANCE_DELIVERY and target is not None:
            if delivery_state in GUIDANCE_DELIVERED_STATES:
                terminal_deliveries.add((target, run_id, session_id, turn_index))
        elif kind == KIND_QUESTION:
            questions[message_id] = message
        elif kind in CLOSURE_KINDS and target is not None:
            closures[target] = message
        elif kind == KIND_WAIT_START and target is not None:
            wait_starts[message_id] = message
            active_waits[target] = message
        elif kind == KIND_RECEIPT and closure_id is not None:
            receipts[closure_id] = message
        elif kind == KIND_WAIT_END and target is not None:
            active_waits.pop(target, None)

    return MailboxView(
        mailbox_path=mailbox_path,
        messages=tuple(messages),
        rejected=tuple(rejected),
        last_seq=len(physical_lines),
        size_bytes=len(data),
    )
