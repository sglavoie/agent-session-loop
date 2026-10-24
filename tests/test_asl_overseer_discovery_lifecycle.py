"""Lifecycle, ownership, pause-window and published-metadata discovery rules."""

from __future__ import annotations

import json
import socket
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_session_loop import overseer_discovery, run_termination
from tests.asl_overseer_discovery_support import (
    FINISHED_AT,
    PAUSED_AT,
    _Corpus,
    _initial_payload,
    _repositories,
)

ASL_TEST_PORTABLE = True


class LifecycleAndOwnershipTests(unittest.TestCase):
    """Timestamps, pause state, and who holds the lock."""

    def test_a_finished_run_reports_its_finish(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", run_finished_at=FINISHED_AT)

        candidate = corpus.only()

        self.assertEqual("finished", candidate.lifecycle)
        self.assertEqual(FINISHED_AT, candidate.run_finished_at)

    def test_a_live_local_owner_makes_an_unfinished_run_running(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        corpus.write_lock(plan_dir, pid=1, hostname=socket.gethostname())

        candidate = corpus.only()

        self.assertEqual("running", candidate.lifecycle)
        self.assertTrue(candidate.ownership.alive)

    def test_a_pause_outranks_the_finish_its_runner_recorded(self) -> None:
        """A paused run does finish; the pause is the fact the window is about."""
        corpus = _Corpus(self)
        corpus.write_run(
            "plan-alpha",
            paused=True,
            ok=False,
            pause_reason="agent-question",
            run_finished_at=FINISHED_AT,
            **{overseer_discovery.PAUSED_AT_KEY: PAUSED_AT},
        )

        candidate = corpus.only()

        self.assertEqual("paused", candidate.lifecycle)
        self.assertTrue(candidate.paused)
        self.assertEqual("agent-question", candidate.pause_reason)
        self.assertEqual(PAUSED_AT, candidate.paused_at)

    def test_a_lock_from_another_host_leaves_liveness_unknown(self) -> None:
        """Nothing here can see another machine's process table."""
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        corpus.write_lock(
            plan_dir,
            pid=1,
            hostname=f"not-{socket.gethostname()}",
            process_start="2026-06-19T13:00:00",
        )

        candidate = corpus.only()

        self.assertIsNone(candidate.ownership.alive)
        self.assertTrue(candidate.ownership.foreign_host)
        self.assertEqual("2026-06-19T13:00:00", candidate.ownership.process_start)
        self.assertEqual("unknown", candidate.lifecycle)

    def test_a_run_with_no_lock_is_unowned(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha")

        ownership = corpus.only().ownership

        self.assertFalse(ownership.lock_present)
        self.assertIsNone(ownership.pid)
        self.assertIsNone(ownership.alive)
        self.assertFalse(ownership.foreign_host)


class PauseWindowTests(unittest.TestCase):
    """The trailing window the overseer is invoked for."""

    def _paused(self, corpus: _Corpus, name: str, paused_at: Any) -> Any:
        corpus.write_run(
            name,
            paused=True,
            ok=False,
            pause_reason="agent-question",
            **{overseer_discovery.PAUSED_AT_KEY: paused_at},
        )
        return corpus.only()

    def test_a_recent_pause_is_inside_the_default_window(self) -> None:
        corpus = _Corpus(self)
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        candidate = self._paused(
            corpus, "plan-alpha", (now - timedelta(hours=3)).isoformat()
        )

        self.assertTrue(candidate.paused_within(now=now))

    def test_a_pause_older_than_the_window_is_outside_it(self) -> None:
        corpus = _Corpus(self)
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        candidate = self._paused(
            corpus, "plan-alpha", (now - timedelta(hours=30)).isoformat()
        )

        self.assertFalse(candidate.paused_within(now=now))
        self.assertTrue(
            candidate.paused_within(
                overseer_discovery.DEFAULT_PAUSE_WINDOW_SECONDS * 2, now=now
            )
        )

    def test_an_undated_pause_is_not_recent(self) -> None:
        """A pause this boundary cannot date excludes rather than includes."""
        corpus = _Corpus(self)
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        candidate = self._paused(corpus, "plan-alpha", None)

        self.assertEqual("paused", candidate.lifecycle)
        self.assertIsNone(candidate.paused_at)
        self.assertFalse(candidate.paused_within(now=now))

    def test_a_run_that_is_not_paused_is_never_in_the_pause_window(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", run_finished_at=FINISHED_AT)

        self.assertFalse(corpus.only().paused_within())


class PublishedLifecycleMetadataTests(unittest.TestCase):
    """The run lifecycle publishes what discovery is not allowed to go find."""

    def test_the_pause_writer_stamps_when_the_pause_happened(self) -> None:
        payload: dict[str, Any] = {}

        run_termination.set_pause(
            payload, reason="agent-question", message="Which column?"
        )

        stamped = payload[overseer_discovery.PAUSED_AT_KEY]
        self.assertIsNotNone(stamped)
        parsed = datetime.fromisoformat(stamped)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertLess(
            abs((datetime.now(UTC) - parsed).total_seconds()),
            60,
            "the pause stamp is not the moment of the pause",
        )

    def test_a_later_pause_replaces_the_stamp_of_an_earlier_one(self) -> None:
        payload: dict[str, Any] = {overseer_discovery.PAUSED_AT_KEY: PAUSED_AT}

        run_termination.set_pause(payload, reason="verify-failed", message="checks")

        self.assertNotEqual(PAUSED_AT, payload[overseer_discovery.PAUSED_AT_KEY])

    def test_a_run_is_born_publishing_both_discovery_fields(self) -> None:
        seed = _initial_payload(self)

        self.assertIsNone(seed[overseer_discovery.PAUSED_AT_KEY])
        self.assertEqual(
            overseer_discovery.DISCOVERY_CONTRACT_VERSION,
            seed[overseer_discovery.DISCOVERY_VERSION_KEY],
        )

    def test_a_seeded_record_paused_by_the_runner_is_discoverable(self) -> None:
        """End to end: what the lifecycle writes is what the inventory reads."""
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        seed = _initial_payload(self)
        seed.update(
            {
                "plan_dir": str(plan_dir),
                "workspace_root": str(corpus.root),
                "registry_digest": "d" * 64,
                "repositories": _repositories(corpus.root),
            }
        )
        run_termination.set_pause(seed, reason="agent-question", message="Which?")
        corpus.write_raw_record(plan_dir, json.dumps(seed, sort_keys=True, default=str))

        candidate = corpus.only()

        self.assertTrue(candidate.eligible)
        self.assertEqual("paused", candidate.lifecycle)
        self.assertTrue(candidate.paused_within())


if __name__ == "__main__":
    unittest.main()
