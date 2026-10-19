"""Test workbench remote reads paging."""

from __future__ import annotations

import unittest
from typing import Any

from asl_workbench import remote_reads
from tests import support_workbench_remote_reads as fixtures


class PagingTests(unittest.TestCase):
    """The cursor the Mac sent is the cursor the next read uses."""

    def page(self, request_id: str, **overrides: Any) -> Any:
        from asl_workbench.transport import detail_envelope

        return detail_envelope(
            payload=fixtures.rows_body(
                [
                    {
                        "name": "run.started",
                        "created_at": fixtures.iso(-10),
                    }
                ],
                **overrides,
            ),
            connector_id=fixtures.CONNECTOR_ID,
            target_id=fixtures.TARGET_ID,
            sent_at=fixtures.NOW,
            request_id=request_id,
        )

    def test_a_cut_page_reports_more_and_the_next_read_starts_there(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        answer = client.accept(
            self.page("r1", offset=0, next_offset=25, total=90, complete="partial"),
            now=fixtures.NOW + 1.0,
        )
        assert answer is not None
        self.assertTrue(answer.has_more)
        self.assertEqual(answer.complete, "partial")
        follow = client.next_page(answer, request_id="r2", now=fixtures.NOW + 2.0)
        assert follow is not None
        self.assertEqual(follow.request.offset, 25)
        self.assertEqual(follow.request.detail, "events")
        self.assertTrue(follow.outstanding)

    def test_the_last_page_has_no_follow_up(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        answer = client.accept(
            self.page("r1", offset=25, total=26),
            now=fixtures.NOW + 1.0,
        )
        assert answer is not None
        self.assertFalse(answer.has_more)
        self.assertIsNone(
            client.next_page(answer, request_id="r2", now=fixtures.NOW + 2.0)
        )

    def test_each_page_is_held_under_its_own_window(self) -> None:
        client = fixtures.make_client()
        first = client.read(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        answer = client.accept(
            self.page("r1", offset=0, next_offset=25, complete="partial"),
            now=fixtures.NOW + 1.0,
        )
        assert answer is not None
        follow = client.next_page(answer, request_id="r2", now=fixtures.NOW + 2.0)
        assert follow is not None
        client.accept(self.page("r2", offset=25), now=fixtures.NOW + 3.0)
        self.assertEqual(len(client.bodies), 2)
        self.assertIsNotNone(client.cached(first.request, now=fixtures.NOW + 4.0))
        self.assertIsNotNone(client.cached(follow.request, now=fixtures.NOW + 4.0))

    def test_a_page_read_while_offline_is_offline_rather_than_the_end(self) -> None:
        client = fixtures.make_client()
        client.read(
            request_id="r1",
            detail="events",
            plan_key=fixtures.PLAN_KEY,
            now=fixtures.NOW,
        )
        answer = client.accept(
            self.page("r1", offset=0, next_offset=25, complete="partial"),
            now=fixtures.NOW + 1.0,
        )
        assert answer is not None
        client.link_down(now=fixtures.NOW + 2.0)
        follow = client.next_page(answer, request_id="r2", now=fixtures.NOW + 3.0)
        assert follow is not None and follow.answer is not None
        self.assertEqual(follow.answer.outcome, remote_reads.OUTCOME_OFFLINE)
