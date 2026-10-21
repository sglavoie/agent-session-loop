"""Volatile mutation bodies: operator prose held in memory for a live command."""

from __future__ import annotations

import hmac
import secrets

from .operations import OPERATION_DISPATCHED
from .remote_actions import MAX_BODY_CHARS
from .uplink_support import UplinkRefusal, UplinkState, _HeldBody


class UplinkBodiesMixin(UplinkState):
    """Stage, redeem, discard and expire the reply text a command carries."""

    def stage_body(
        self, operation_id: str, text: str, *, now: float
    ) -> tuple[str, str, bool]:
        """Hold one reply in memory until its dispatched command redeems it."""
        if not isinstance(text, str) or not text.strip():
            raise UplinkRefusal(400, "invalid-reply", "reply text must not be empty")
        if len(text) > MAX_BODY_CHARS:
            raise UplinkRefusal(
                400,
                "invalid-reply",
                f"reply text exceeds {MAX_BODY_CHARS} characters",
            )
        with self._ready:
            existing = self._bodies.get(operation_id)
            if existing is not None:
                return existing.reply_ref, existing.action_token, False
            held = _HeldBody(
                reply_ref=f"ref-{operation_id}",
                action_token=secrets.token_hex(16),
                text=text,
                expires_at=now + self.command_lifetime_seconds,
            )
            self._bodies[operation_id] = held
            return held.reply_ref, held.action_token, True

    def discard_body(self, operation_id: str) -> None:
        """Overwrite and remove any prose held for a command."""
        with self._ready:
            held = self._bodies.pop(operation_id, None)
            if held is not None:
                held.text = ""
                held.action_token = ""

    def redeem_body(
        self,
        operation_id: str,
        reply_ref: str,
        action_token: str,
        *,
        connector_id: str,
        now: float,
    ) -> str:
        """Return a dispatched command's body to its paired connector only."""
        self._expire_bodies(now)
        record = self.store.operation(operation_id)
        if record is None or record.get("connector_id") != connector_id:
            raise UplinkRefusal(
                404, "unknown-operation", "no dispatched command has that identity"
            )
        if record.get("status") != OPERATION_DISPATCHED:
            raise UplinkRefusal(
                409,
                "not-dispatched",
                f"operation {operation_id} is {record.get('status')}",
            )
        with self._ready:
            held = self._bodies.get(operation_id)
            if held is None or not held.text:
                raise UplinkRefusal(
                    409, "not-dispatched", "the command body is no longer available"
                )
            if held.reply_ref != reply_ref or not hmac.compare_digest(
                held.action_token, action_token
            ):
                raise UplinkRefusal(
                    403, "invalid-action-token", "the command capability does not match"
                )
            return held.text

    def _expire_bodies(self, now: float) -> None:
        with self._ready:
            expired = [
                operation_id
                for operation_id, held in self._bodies.items()
                if now >= held.expires_at
            ]
        for operation_id in expired:
            self.discard_body(operation_id)
