"""VolatileBodyTests: bodies."""

from __future__ import annotations

from .workbench_uplink_support import (
    CONNECTOR,
    NOW,
    OPERATION_ACCEPTED,
    TARGET,
    UplinkRefusal,
    VolatileBodyTestsBase,
    operation_envelope,
)


class VolatileBodyTests(VolatileBodyTestsBase):
    def test_a_dispatched_body_is_redeemed_by_the_paired_connector(self) -> None:
        reply_ref, action_token = self._dispatch("reply-1", "Use approach A.")
        self.assertEqual(
            "Use approach A.",
            self.uplink.redeem_body(
                "reply-1",
                reply_ref,
                action_token,
                connector_id=CONNECTOR,
                now=NOW + 2,
            ),
        )
        with self.assertRaises(UplinkRefusal) as raised:
            self.uplink.redeem_body(
                "reply-1",
                reply_ref,
                "wrong-token",
                connector_id=CONNECTOR,
                now=NOW + 2,
            )
        self.assertEqual("invalid-action-token", raised.exception.reason)
        with self.assertRaises(UplinkRefusal) as raised:
            self.uplink.redeem_body(
                "reply-1",
                reply_ref,
                "jeton-é",
                connector_id=CONNECTOR,
                now=NOW + 2,
            )
        self.assertEqual("invalid-action-token", raised.exception.reason)

    def test_a_settled_command_forgets_its_reply(self) -> None:
        reply_ref, action_token = self._dispatch("reply-2", "Continue.")
        self.uplink.ingest(
            operation_envelope(
                payload={
                    "operation_id": "reply-2",
                    "status": OPERATION_ACCEPTED,
                    "observed_at": NOW + 3,
                    "settled_at": NOW + 3,
                },
                connector_id=CONNECTOR,
                target_id=TARGET,
                sent_at=NOW + 3,
            ),
            now=NOW + 3,
            subject=CONNECTOR,
        )
        with self.assertRaises(UplinkRefusal):
            self.uplink.redeem_body(
                "reply-2",
                reply_ref,
                action_token,
                connector_id=CONNECTOR,
                now=NOW + 4,
            )
