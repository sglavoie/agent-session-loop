"""Test workbench remote reads responder."""

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_session_loop import workbench_remote_reads_payload
from asl_workbench import catalog, details, remote_reads
from asl_workbench.transport import (
    KIND_DETAIL,
    KIND_RECEIPT,
    MAX_DETAIL_BYTES,
    encode_frame,
)
from tests import support_workbench_remote_reads as fixtures

ASL_TEST_PORTABLE = True


class ResponderTests(fixtures.ResponderTestCase):
    """The reading half: a key becomes a directory, a failure becomes a word."""

    def test_a_key_resolves_to_the_plan_discovery_already_publishes(self) -> None:
        payload = self.ask("plan")
        self.assertEqual(payload["detail"], "plan")
        self.assertEqual(payload["target"]["plan_key"], self.plan_key)
        self.assertNotIn("reason", payload)
        (entry,) = payload["entries"]
        self.assertEqual(entry["plan"]["primary_state"], "paused-for-reply")
        self.assertEqual(entry["plan"]["counts"]["pending"], 1)

    def test_an_unknown_key_is_not_found_rather_than_an_empty_plan(self) -> None:
        payload = self.responder.answer(
            {
                "request": "detail.read",
                "detail": "plan",
                "target": {"plan_key": fixtures.OTHER_KEY},
            },
            now=fixtures.NOW,
        )
        self.assertEqual(payload["reason"], details.REASON_NOT_FOUND)
        self.assertEqual(payload["complete"], "unknown")
        self.assertNotIn("entries", payload)

    def test_a_log_read_that_names_nothing_is_an_invalid_request(self) -> None:
        payload = self.ask("logs", stream=None)
        self.assertEqual(payload["reason"], details.REASON_INVALID_REQUEST)

    def test_a_reason_from_another_vocabulary_becomes_a_declared_word(self) -> None:
        """``read_questions`` answers in the Q&A vocabulary, not this one."""
        payload = self.ask("questions")
        self.assertEqual(payload["reason"], details.REASON_NOT_FOUND)
        self.assertIn(details.REASON_NOT_FOUND, remote_reads.DETAIL_REFUSAL_REASONS)
        # The word the reader actually used is not lost, only demoted.
        self.assertTrue(
            any("no-paused-run" in warning for warning in payload["warnings"])
        )

    def test_an_undeclared_kind_is_an_invalid_request(self) -> None:
        payload = self.responder.answer(
            {
                "request": "detail.read",
                "detail": "everything",
                "target": {"plan_key": self.plan_key},
            },
            now=fixtures.NOW,
        )
        self.assertEqual(payload["reason"], details.REASON_INVALID_REQUEST)

    def test_every_declared_kind_is_answered_rather_than_unhandled(self) -> None:
        self.write_events(self.plan, 3)
        for kind in details.DETAIL_KINDS:
            with self.subTest(detail=kind):
                payload = self.ask(kind)
                self.assertEqual(payload["detail"], kind)
                self.assertNotEqual(
                    payload.get("reason"), details.REASON_INVALID_REQUEST
                )

    def test_an_empty_artifact_is_complete_and_empty_not_refused(self) -> None:
        payload = self.ask("events")
        self.assertNotIn("reason", payload)
        self.assertEqual(payload["complete"], "complete")
        self.assertEqual(payload["entries"], [])
        self.assertEqual(payload["entry_count"], 0)

    def test_a_paged_read_carries_the_cursor_it_stopped_at(self) -> None:
        self.write_events(self.plan, 6)
        first = self.ask("events", limit=2)
        self.assertEqual(first["entry_count"], 2)
        self.assertEqual(first["complete"], "partial")
        self.assertEqual(first["next_offset"], 2)
        second = self.ask("events", offset=first["next_offset"], limit=2)
        self.assertEqual(second["offset"], 2)
        self.assertNotEqual(first["entries"], second["entries"])

    def test_a_read_is_bounded_by_bytes_and_not_only_by_rows(self) -> None:
        self.write_events(self.plan, 4, payload_size=150_000)
        payload = self.ask("events", limit=4)
        self.assertLess(payload["entry_count"], 4)
        self.assertEqual(payload["complete"], "partial")
        # The cursor moved back to exactly where the sending stopped.
        self.assertEqual(payload["next_offset"], payload["entry_count"])

    def test_text_is_cut_to_what_one_frame_may_carry(self) -> None:
        payload = self.ask("last-message")
        if "content" in payload:
            self.assertLessEqual(payload["content_bytes"], MAX_DETAIL_BYTES)
            self.assertEqual(payload["encoding"], "utf-8")

    def test_the_selected_run_travels_back_in_the_target(self) -> None:
        self.write_events(self.plan, 2)
        payload = self.ask("events")
        run_id = payload["target"].get("run_id")
        if run_id is not None:
            self.assertEqual(
                payload["target"]["run_key"],
                catalog.run_key(self.plan_key, run_id),
            )

    def test_every_answer_is_a_frame_the_transport_accepts(self) -> None:
        from asl_workbench.transport import detail_envelope

        self.write_events(self.plan, 3)
        for kind in details.DETAIL_KINDS:
            with self.subTest(detail=kind):
                payload = self.ask(kind)
                encode_frame(
                    detail_envelope(
                        payload=payload,
                        connector_id=fixtures.CONNECTOR_ID,
                        target_id=fixtures.TARGET_ID,
                        sent_at=fixtures.NOW,
                        request_id="r1",
                    )
                )

    def test_reading_changes_nothing(self) -> None:
        self.write_events(self.plan, 3)
        before = self.tree_digest()
        for kind in details.DETAIL_KINDS:
            self.ask(kind)
        self.assertEqual(self.tree_digest(), before)

    def tree_digest(self) -> str:
        import hashlib

        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(self.root)).encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()


class PrivacyTests(fixtures.ResponderTestCase):
    """What crosses the wire never names this disk."""

    def test_no_answer_carries_a_local_path(self) -> None:
        self.write_events(self.plan, 3)
        needles = (str(self.root), str(self.plans_root), str(self.state_root))
        for kind in details.DETAIL_KINDS:
            with self.subTest(detail=kind):
                rendered = json.dumps(self.ask(kind), default=str)
                for needle in needles:
                    self.assertNotIn(needle, rendered)

    def test_the_scrubber_drops_a_path_column_nobody_declared(self) -> None:
        scrubbed = workbench_remote_reads_payload._scrub(
            {
                "plan_dir": "/Users/somebody/plans/x",
                "events_path": "/Users/somebody/state/events.jsonl",
                "nested": [{"state_root": "/var/asl", "title": "keep me"}],
                "title": "keep me too",
            }
        )
        self.assertEqual(
            scrubbed, {"nested": [{"title": "keep me"}], "title": "keep me too"}
        )

    def test_a_refusal_does_not_leak_the_directory_it_failed_to_find(self) -> None:
        payload = self.responder.answer(
            {
                "request": "detail.read",
                "detail": "logs",
                "target": {"plan_key": fixtures.OTHER_KEY},
            },
            now=fixtures.NOW,
        )
        self.assertNotIn(str(self.root), json.dumps(payload, default=str))


class RoundTripTests(fixtures.ResponderTestCase):
    """One real read, asked from the far end and answered off this disk."""

    def test_a_service_reads_a_real_plan_without_anything_touching_disk(self) -> None:
        self.write_events(self.plan, 5)
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, responder=self.responder)
        client = fixtures.make_client()

        ticket = client.read(
            request_id="r1",
            detail="events",
            plan_key=self.plan_key,
            limit=2,
            now=fixtures.NOW,
        )
        assert ticket.envelope is not None
        channel.carry(ticket.envelope)
        client.sent("r1", now=fixtures.NOW)

        answer = self.pump(connector, channel, client)
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_ANSWERED)
        self.assertEqual(len(answer.entries), 2)
        self.assertTrue(answer.has_more)
        self.assertEqual(answer.complete, "partial")
        self.assertEqual(len(client.bodies), 1)

        follow = client.next_page(answer, request_id="r2", now=fixtures.NOW + 1.0)
        assert follow is not None and follow.envelope is not None
        channel.carry(follow.envelope)
        second = self.pump(connector, channel, client, start=1.0)
        assert second is not None
        self.assertEqual(second.offset, 2)
        self.assertNotEqual(second.entries, answer.entries)

        # And a disconnect takes every body with it.
        client.link_down(now=fixtures.NOW + 5.0)
        self.assertEqual(len(client.bodies), 0)

    def test_a_read_for_a_plan_this_mac_does_not_have_is_refused_not_empty(
        self,
    ) -> None:
        channel = fixtures.LinkChannel()
        connector = fixtures.make_connector(channel=channel, responder=self.responder)
        client = fixtures.make_client()
        ticket = client.read(
            request_id="r1",
            detail="events",
            plan_key=fixtures.OTHER_KEY,
            now=fixtures.NOW,
        )
        assert ticket.envelope is not None
        channel.carry(ticket.envelope)
        answer = self.pump(connector, channel, client)
        assert answer is not None
        self.assertEqual(answer.outcome, remote_reads.OUTCOME_REFUSED)
        self.assertEqual(answer.reason, details.REASON_NOT_FOUND)
        self.assertFalse(answer.empty)
        self.assertEqual(len(client.bodies), 0)

    def pump(
        self,
        connector: Any,
        channel: fixtures.LinkChannel,
        client: Any,
        *,
        start: float = 0.0,
        steps: int = 10,
    ) -> Any:
        """Step the connector, feeding every answer it sends to the client."""
        seen = len(channel.frames)
        for index in range(steps):
            connector.step(fixtures.NOW + start + 0.1 * (index + 1))
            for frame in channel.frames[seen:]:
                seen += 1
                if frame["kind"] in (KIND_DETAIL, KIND_RECEIPT):
                    answer = client.accept(
                        frame,
                        now=fixtures.NOW + start + 0.1 * (index + 1),
                    )
                    if answer is not None:
                        return answer
        return None


if __name__ == "__main__":
    unittest.main()
