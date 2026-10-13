"""Live mailbox guidance observed alongside external control commands."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import (
    events,
    guards,
    guidance_delivery,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_snapshot,
    run_control,
    run_coordination_constants,
    run_coordination_control_drain,
    run_coordination_seams,
)


class MailboxPoll:
    """Observe live mailbox guidance alongside external control commands."""

    def __init__(
        self,
        *,
        plan_dir: Path,
        state_dir_arg: str | None,
        control_state: run_control.RunControlState,
        seen_control_ids: set[str],
        payload: dict[str, Any],
        event_path: Path,
        control_payload: dict[str, Any],
        persistence: run_coordination_seams.CoordinationPersistence,
        run_control_lock: AbstractContextManager[Any],
        enabled: bool = True,
        wakeup: threading.Event | None = None,
        guard_controller: guards.GuardController | None = None,
    ) -> None:
        self._plan_dir = plan_dir
        self._state_dir_arg = state_dir_arg
        self._control_state = control_state
        self._seen_control_ids = seen_control_ids
        self._payload = payload
        self._event_path = event_path
        self._control_payload = control_payload
        self._persistence = persistence
        self._run_control_lock = run_control_lock
        self._enabled = enabled
        self._wakeup = wakeup
        self._guard_controller = guard_controller
        self._context: dict[str, Any] | None = None
        self._abort: harnesses.AbortSignal | None = None
        self._finalization = False
        self._evented_guidance_ids: set[str] = set()
        self._live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None
        self._settled_guidance_ids: set[str] = set()
        self._recorded_guidance_attempts: set[tuple[str, str, str]] = set()
        self._unconfirmed_guidance_ids: set[str] = set()
        self._mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            plan_dir,
            state_dir_arg,
        )
        self._last_read_token: object = run_coordination_constants.MAILBOX_TOKEN_UNSEEN

    def set_context(self, context: Mapping[str, Any] | None) -> None:
        """Set the active turn metadata attached to subsequent poll events."""
        self._context = None if context is None else dict(context)

    def set_abort(
        self,
        abort: harnesses.AbortSignal | None,
        *,
        finalization: bool = False,
    ) -> None:
        """Attach the signal an observed abort should arm, or detach it.

        Set while a turn is in flight and cleared once it has finished, so an
        abort observed between turns (during a mailbox reply wait, for example)
        cannot arm a signal no running turn is watching. ``finalization`` marks
        the signal as belonging to the run's terminal work instead of a turn.
        """
        self._abort = abort
        self._finalization = finalization

    def set_live_delivery(
        self,
        endpoint: guidance_delivery.LiveDeliveryEndpoint | None,
    ) -> None:
        """Attach the running turn's live-delivery endpoint, or detach it.

        Set before the turn starts and cleared after the poll thread has been
        joined, exactly like :meth:`set_abort`, so no tick can deliver into a
        turn that has finished. Both per-turn ledgers are reset with the
        endpoint: what a turn settled and what it recorded are facts about that
        turn, and a new turn is a new chance to deliver.

        Attaching also drops the change token so the first tick of the new turn
        parses the mailbox again. Without that, guidance deferred in the
        previous turn would sit undelivered until something else happened to
        write the mailbox: the unchanged-token short circuit would keep
        returning before the delivery pass ever ran.
        """
        self._live_delivery = endpoint
        self._settled_guidance_ids = set()
        self._recorded_guidance_attempts = set()
        self._unconfirmed_guidance_ids = set()
        if endpoint is not None:
            self._last_read_token = run_coordination_constants.MAILBOX_TOKEN_UNSEEN

    def __call__(self) -> None:
        """Drain controls, then emit once-only metadata for pending guidance."""
        run_coordination_control_drain.drain_external_control_queue(
            plan_dir=self._plan_dir,
            state_dir_arg=self._state_dir_arg,
            control_state=self._control_state,
            seen_ids=self._seen_control_ids,
            payload=self._payload,
            event_path=self._event_path,
            control_payload=self._control_payload,
            context=self._context,
            persistence=self._persistence,
            run_control_lock=self._run_control_lock,
            wakeup=self._wakeup,
            abort=self._abort,
            mailbox_enabled=self._enabled,
            finalization=self._finalization,
            guard_controller=self._guard_controller,
        )

        if not self._enabled:
            return

        current_token = mailbox.mailbox_change_token(self._mailbox_path)
        if (
            # An attempt that is written but unconfirmed has to be asked again,
            # and its evidence arrives on the harness's schedule rather than by
            # anything writing the mailbox. Short circuiting on an unchanged
            # token here would stop the retries after the one tick whose own
            # ledger append happened to move the token.
            not self._unconfirmed_guidance_ids
            and self._last_read_token
            is not run_coordination_constants.MAILBOX_TOKEN_UNSEEN
            and current_token is not mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            and current_token == self._last_read_token
        ):
            return

        try:
            view = mailbox.read_mailbox(
                self._plan_dir,
                state_dir=self._state_dir_arg,
            )
        except OSError, mailbox_models.MailboxError:
            # Mailbox status remains authoritative through its own direct read;
            # a live observation failure must not terminate the harness turn.
            return

        # Retain the token captured before the locked read. An append racing
        # that read will conservatively trigger another parse on the next tick.
        self._last_read_token = current_token
        for message in view.pending_guidance:
            if message.id in self._evented_guidance_ids:
                continue
            text = message.text or ""
            event_payload = {
                "run_id": self._payload.get("run_id"),
                "action": events.MAILBOX_ACTION_GUIDANCE_OBSERVED,
                "message_id": message.id,
                "seq": message.seq,
                "length": len(text),
                "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            if self._context is not None:
                for key in (
                    "session",
                    "session_id",
                    "turn_index",
                    "total_sessions",
                ):
                    value = self._context.get(key)
                    if value is not None:
                        event_payload[key] = value
            self._persistence.append_event(
                self._event_path,
                events.MAILBOX_EVENT,
                event_payload,
            )
            self._evented_guidance_ids.add(message.id)

        self._deliver_guidance(view)

    def _deliver_guidance(self, view: mailbox_snapshot.MailboxView) -> None:
        """Offer each undelivered guidance record to the turn now running.

        Nothing happens between turns: with no endpoint there is no live
        process to deliver into, and recording a deferral per tick would fill
        the ledger with history about a turn that does not exist.

        One attempt per guidance per turn, with one exception: an attempt that
        is still open. A transport that has written guidance out but cannot yet
        prove a model received it answers ``awaiting-confirmation``, and the
        only way that ever becomes a delivery is by asking again — the evidence
        arrives on the harness's own schedule, not on the poller's. Such an
        attempt is retried for the rest of the turn and needs no separate cap:
        each retry is a cheap local check, the ledger deduplicates below, and
        the endpoint's own close turns the last retry into ``stale-turn``.
        """
        endpoint = self._live_delivery
        if endpoint is None:
            return
        for message in view.deliverable_guidance:
            if message.id in self._settled_guidance_ids:
                continue
            # Marked before the submit, not after: an attempt that settles stays
            # settled even if recording the outcome fails below.
            self._settled_guidance_ids.add(message.id)
            outcome = endpoint.submit(
                guidance_delivery.GuidanceRequest(
                    guidance_id=message.id,
                    seq=message.seq,
                    text=message.text or "",
                    turn=endpoint.turn,
                )
            )
            if outcome.retriable_in_turn:
                self._settled_guidance_ids.discard(message.id)
                self._unconfirmed_guidance_ids.add(message.id)
            else:
                self._unconfirmed_guidance_ids.discard(message.id)
            self._record_delivery(message, outcome, endpoint.turn)

    def _record_delivery(
        self,
        message: mailbox_models.MailboxMessage,
        outcome: guidance_delivery.GuidanceOutcome,
        turn: guidance_delivery.TurnIdentity,
    ) -> None:
        """Append the attempt to the ledger and event what it did.

        The append happens only after the endpoint has returned an outcome, so
        the record always describes something that finished. If the append
        itself fails after a successful transport, nothing is written and the
        guidance stays deliverable: a later turn may then deliver it a second
        time. At-least-once is the honest guarantee, and a duplicate is a far
        smaller harm than a durable record claiming a delivery the ledger could
        not actually witness.

        At most one record per (guidance, state, reason) per turn. A retried
        attempt reports the same thing until something changes, and a ledger
        that appended every poll tick would say a hundred times over what it
        only knows once. The moment the answer does change — confirmed, or
        broken — the new pair is unrecorded and is written.
        """
        signature = (message.id, outcome.delivery_state, outcome.reason)
        if signature in self._recorded_guidance_attempts:
            return
        self._recorded_guidance_attempts.add(signature)
        try:
            record = mailbox_appenders.append_guidance_delivery(
                self._plan_dir,
                message.id,
                delivery_state=outcome.delivery_state,
                delivery_reason=outcome.reason,
                channel=outcome.channel,
                run_id=turn.run_id,
                session_id=turn.session_id,
                turn_index=turn.turn_index,
                harness=turn.harness,
                state_dir=self._state_dir_arg,
            )
        except OSError, mailbox_models.MailboxError:
            # Same fail-open discipline as the read above: a mailbox that
            # cannot record a delivery must not end the harness turn. The
            # signature is released so a retried attempt can still record what
            # this one failed to; dedup is for repetition, not for suppression.
            self._recorded_guidance_attempts.discard(signature)
            return
        event_payload = {
            "action": (
                events.MAILBOX_ACTION_GUIDANCE_DELIVERED
                if outcome.delivered
                else events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED
            ),
            **mailbox_appenders.guidance_delivery_event_payload(record),
        }
        self._persistence.append_event(
            self._event_path,
            events.MAILBOX_EVENT,
            event_payload,
        )
