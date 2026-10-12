"""Evidence bundle guidance delivery and action-required projection."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    mailbox,
    supervisor_audit_evidence,
)
from tests import support_supervisor_audit_evidence as evidence_support
from tests.support_supervisor_audit import RUN_ID


class EvidenceBundleTests(evidence_support.EvidenceBundleFixture):
    """The audit input bundle is bounded, deterministic, and control-plane only."""

    GUIDANCE_ID = "99999999-9999-4999-8999-999999999999"

    SECOND_QUESTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    GUIDANCE_TEXT = "Prefer the narrower fixture; do not touch the shared one."

    def _append_mailbox_record(self, state_dir: Path, record: dict[str, Any]) -> None:
        with (state_dir / mailbox.MAILBOX_FILE_NAME).open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(record) + "\n")

    def _append_event(
        self, state_dir: Path, name: str, payload: dict[str, Any]
    ) -> None:
        with (state_dir / events.EVENTS_FILE_NAME).open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "name": name,
                        "version": events.EVENT_SCHEMA_VERSION,
                        "created_at": "2026-08-10T10:11:13Z",
                        "payload": payload,
                    }
                )
                + "\n"
            )

    def _write_guidance(self, state_dir: Path, *, observed: bool) -> None:
        """Append plan-addressed guidance, optionally delivered into this run."""
        self._append_mailbox_record(
            state_dir,
            {
                "created_at": "2026-08-10T10:11:11Z",
                "id": self.GUIDANCE_ID,
                "kind": mailbox.KIND_GUIDANCE,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "text": self.GUIDANCE_TEXT,
            },
        )
        if not observed:
            return
        self._append_event(
            state_dir,
            events.MAILBOX_EVENT,
            {
                "run_id": RUN_ID,
                "action": events.MAILBOX_ACTION_GUIDANCE_OBSERVED,
                "message_id": self.GUIDANCE_ID,
                "seq": 1,
                "session_id": "01",
                "turn_index": 1,
                "length": len(self.GUIDANCE_TEXT),
                "digest": hashlib.sha256(
                    self.GUIDANCE_TEXT.encode("utf-8")
                ).hexdigest(),
            },
        )

    def _set_action_required(
        self, state_dir: Path, signal: dict[str, Any] | None, **status: object
    ) -> None:
        path = state_dir / events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["action_required"] = signal
        payload.update(status)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _live_signal(
        self,
        *,
        created_at: str = "2026-08-10T10:11:13Z",
        reason: str = "runner-status-paused",
    ) -> dict[str, Any]:
        return {
            "created_at": created_at,
            "reason": reason,
            "message": "paused",
            "matched": events.RUNNER_STATUS_PREFIX,
            "run_id": RUN_ID,
            "session_id": "01",
            "turn_index": 1,
        }

    def _write_guidance_delivery(
        self,
        state_dir: Path,
        *,
        run_id: str = RUN_ID,
        target: str | None = None,
        delivery_state: str = "delivered-by-read",
    ) -> None:
        self._append_mailbox_record(
            state_dir,
            {
                "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "kind": mailbox.KIND_GUIDANCE_DELIVERY,
                "version": mailbox.MAILBOX_SCHEMA_VERSION,
                "run_id": run_id,
                "to": target or self.GUIDANCE_ID,
                "delivery_state": delivery_state,
                "delivery_reason": "delivered",
                "channel": "read",
                "session_id": "01",
                "turn_index": 1,
            },
        )

    def test_guidance_delivered_into_a_turn_reaches_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._write_guidance(state_dir, observed=True)

            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)
            section = bundle.payload["mailbox"]
            guidance = next(
                record
                for record in section["records"]
                if record["kind"] == mailbox.KIND_GUIDANCE
            )
            text = bundle.to_bytes().decode("utf-8")

            self.assertEqual(self.GUIDANCE_ID, guidance["id"])
            self.assertEqual(
                supervisor_audit_evidence.MAILBOX_OWNERSHIP_DELIVERED,
                guidance["ownership"],
            )
            self.assertEqual(1, section["summary"]["delivered_count"])
            self.assertEqual(1, section["summary"]["counts"][mailbox.KIND_GUIDANCE])
            self.assertNotIn(self.GUIDANCE_TEXT, text)
            self.assertIn(
                hashlib.sha256(self.GUIDANCE_TEXT.encode("utf-8")).hexdigest()[:16],
                text,
            )

    def test_guidance_this_run_never_read_is_counted_rather_than_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._write_guidance(state_dir, observed=False)

            summary = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "mailbox"
            ]["summary"]

            self.assertEqual(3, summary["unscoped_count"])
            self.assertEqual(0, summary["delivered_count"])
            self.assertNotIn(mailbox.KIND_GUIDANCE, summary["counts"])

    def test_explicit_read_receipts_pull_in_earlier_guidance_without_an_event(
        self,
    ) -> None:
        for delivery_state in mailbox.GUIDANCE_DELIVERED_STATES:
            with (
                self.subTest(delivery_state=delivery_state),
                tempfile.TemporaryDirectory() as tmp,
            ):
                history = self._history(Path(tmp))
                self._write_guidance(history[2], observed=False)
                self._write_guidance_delivery(history[2], delivery_state=delivery_state)
                bundle = self._collect(*history)
                section = bundle.payload["mailbox"]
                guidance = next(
                    row
                    for row in section["records"]
                    if row["kind"] == mailbox.KIND_GUIDANCE
                )
                self.assertEqual(self.GUIDANCE_ID, guidance["id"])
                self.assertEqual(
                    supervisor_audit_evidence.MAILBOX_OWNERSHIP_DELIVERED,
                    guidance["ownership"],
                )
                self.assertEqual(1, section["summary"]["delivered_count"])
                self.assertEqual(0, section["summary"]["missing_reference_count"])
                self.assertNotIn(self.GUIDANCE_TEXT, bundle.to_bytes().decode())
                receipt = next(
                    row
                    for row in section["records"]
                    if row["kind"] == mailbox.KIND_GUIDANCE_DELIVERY
                )
                self.assertLess(guidance["sequence"], receipt["sequence"])

    def test_another_runs_delivery_does_not_claim_the_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_guidance(history[2], observed=False)
            self._write_guidance_delivery(history[2], run_id=self.FOREIGN_RUN_ID)
            section = self._collect(*history).payload["mailbox"]
            self.assertNotIn(mailbox.KIND_GUIDANCE, section["summary"]["counts"])
            self.assertNotIn(
                mailbox.KIND_GUIDANCE_DELIVERY, section["summary"]["counts"]
            )

    def test_a_missing_delivery_target_is_an_explicit_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_guidance_delivery(history[2])
            bundle = self._collect(*history)
            self.assertEqual(
                1, bundle.payload["mailbox"]["summary"]["missing_reference_count"]
            )
            self.assertTrue(
                any(
                    row["section"] == "mailbox"
                    and row["reason"] == supervisor_audit_evidence.REASON_MISSING
                    for row in bundle.payload["omissions"]
                )
            )

    def test_a_deferred_delivery_retains_guidance_but_does_not_claim_delivery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._write_guidance(history[2], observed=False)
            self._write_guidance_delivery(history[2], delivery_state="deferred")
            section = self._collect(*history).payload["mailbox"]
            guidance = next(
                row
                for row in section["records"]
                if row["kind"] == mailbox.KIND_GUIDANCE
            )
            self.assertEqual("delivery-referenced", guidance["ownership"])
            self.assertEqual(0, section["summary"]["delivered_count"])

    def test_a_ruled_and_consumed_ask_retires_the_action_required_projection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            signal = self._live_signal()
            self._set_action_required(
                state_dir, signal, paused=True, pause_reason="blocked"
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]
            superseded = status["action_required_superseded"]

            self.assertIsNone(status["action_required"])
            self.assertTrue(status["paused"])
            self.assertEqual("blocked", status["pause_reason"])
            self.assertEqual(
                supervisor_audit_evidence.SUPERSEDED_BY_CONSUMED_RULING,
                superseded["superseded_by"],
            )
            self.assertEqual(signal, superseded["record"])
            self.assertEqual(
                [(self.QUESTION_ID, self.REPLY_ID, True)],
                [
                    (ask["id"], ask["closure_id"], ask["consumed"])
                    for ask in superseded["questions"]
                ],
            )

    def test_an_unanswered_ask_from_the_same_turn_keeps_the_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._append_mailbox_record(
                state_dir,
                {
                    "created_at": "2026-08-10T10:11:20Z",
                    "id": self.SECOND_QUESTION_ID,
                    "kind": mailbox.KIND_QUESTION,
                    "version": mailbox.MAILBOX_SCHEMA_VERSION,
                    "text": "And what about the second one?",
                    "delivery_mode": mailbox.DELIVERY_MODE_ASYNC,
                    "run_id": RUN_ID,
                    "session_id": "01",
                    "turn_index": 1,
                },
            )
            self._set_action_required(
                state_dir, self._live_signal(), paused=True, pause_reason="blocked"
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]

            self.assertEqual(
                "runner-status-paused", status["action_required"]["reason"]
            )
            self.assertNotIn("action_required_superseded", status)

    def test_a_ruling_older_than_the_signal_does_not_retire_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._set_action_required(
                state_dir,
                self._live_signal(created_at="2026-08-10T10:11:16Z"),
                paused=True,
                pause_reason="blocked",
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]

            self.assertEqual(
                "runner-status-paused", status["action_required"]["reason"]
            )
            self.assertNotIn("action_required_superseded", status)

    def test_a_reason_the_runner_declared_itself_is_never_retired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._set_action_required(
                state_dir,
                self._live_signal(reason="review-findings"),
                paused=True,
                pause_reason="review-findings",
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]

            self.assertEqual("review-findings", status["action_required"]["reason"])
            self.assertNotIn("action_required_superseded", status)

    def test_another_turns_answer_does_not_retire_this_turns_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            signal = self._live_signal()
            signal["turn_index"] = 2
            self._set_action_required(
                state_dir, signal, paused=True, pause_reason="blocked"
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]

            self.assertEqual(
                "runner-status-paused", status["action_required"]["reason"]
            )
            self.assertNotIn("action_required_superseded", status)


if __name__ == "__main__":
    unittest.main()
