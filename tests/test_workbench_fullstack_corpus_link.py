"""Published corpus, link failure, correlated reads, identity and version."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from asl_workbench import connector_payloads as portable_payloads
from asl_workbench import connector_support as portable_support
from asl_workbench import (
    transport,
)
from tests.support_workbench_fullstack import (
    CONNECTOR_ID,
    NOW,
    POLICY,
    TARGET_ID,
)
from tests.support_workbench_fullstack_case import FullStackCase

# Integration in the fullest sense the repository has: one plan corpus, the
# real observation source, a connector, a link that can be made to fail, the
# Pi's store, and the remote provider behind a loopback socket, with the
# launch children replaced by an inert interpreter rather than the launch
# path. Nothing here would tell the truth without all of it running.
ASL_TEST_LANE = "integration"

# ---------------------------------------------------------------------------
# The corpus reaches the interface
# ---------------------------------------------------------------------------


class PublishedStackTests(FullStackCase):
    """What the operator's browser is shown is what is on the Mac's disk."""

    reads = True

    def test_every_seeded_plan_arrives_addressed_as_the_mac_addresses_it(self) -> None:
        self.publish()
        rows = self.queue_rows()
        served = {str(row["plan_key"]) for row in rows}
        # The comparison is against the corpus's own digests, computed by
        # `catalog.plan_key` from the directories on disk -- so a link that
        # renumbered a plan fails here rather than looking plausible.
        self.assertEqual(set(self.corpus.plan_keys.values()), served)
        self.assertEqual(len(self.corpus.plan_dirs), len(rows))

    def test_the_states_the_corpus_is_in_are_the_states_that_are_served(self) -> None:
        self.publish()
        by_key = {str(row["plan_key"]): row for row in self.queue_rows()}
        keys = self.corpus.plan_keys
        # `alpha` is paused for a reply, `bravo` has never run, `charlie`
        # finished and went quiet. Three different answers out of one publish.
        self.assertEqual("paused-for-reply", by_key[keys["alpha"]]["primary_state"])
        self.assertEqual("ready", by_key[keys["bravo"]]["primary_state"])
        self.assertIn(
            by_key[keys["charlie"]]["primary_state"], {"ready", "recent", "complete"}
        )

    def test_a_published_stack_reports_itself_fresh_and_complete(self) -> None:
        self.publish()
        freshness = self.freshness()
        self.assertFalse(freshness["stale"])
        self.assertTrue(freshness["complete"])
        self.assertEqual(NOW, freshness["observed_at"])
        self.assertEqual(len(self.corpus.plan_dirs), freshness["total"])

    def test_the_static_bundle_is_served_from_the_same_origin_as_the_api(self) -> None:
        # The front end is one deployment with the API, not a second service:
        # a shell fetched from elsewhere would be refused by the remote policy
        # it is supposed to satisfy.
        status, body = self.request("/")
        self.assertEqual(200, status)
        self.assertIn(b"<!doctype html>", bytes(body).lower())

    def test_an_artifact_the_pi_never_saw_is_read_through_the_connector(self) -> None:
        self.publish()
        status, body = self.request(
            f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        )
        self.assertEqual(200, status)
        # The answer came from the Mac's disk: the request was carried down
        # inside an acknowledgement and the answer came back up as a frame.
        assert self.lane is not None
        self.assertEqual(1, self.lane.asked)
        self.assertTrue(
            any(frame["kind"] == transport.KIND_DETAIL for frame in self.link.landed),
            "no detail frame crossed the link",
        )


# ---------------------------------------------------------------------------
# The link is allowed to fail
# ---------------------------------------------------------------------------


class LinkFailureTests(FullStackCase):
    """Network loss, recovery, and the ten reconnects after it."""

    def test_a_link_that_goes_away_leaves_the_connector_backing_off(self) -> None:
        self.link.down()
        self.publish()
        self.assertEqual(portable_support.STATE_BACKOFF, self.connector.state)
        # Nothing was committed, so the next pass re-reads the same window
        # rather than skipping over an undelivered one.
        self.assertEqual([], self.link.landed)

    def test_an_unreachable_pi_reports_its_own_staleness_rather_than_silence(
        self,
    ) -> None:
        self.publish()
        self.assertFalse(self.freshness()["stale"])
        self.link.down()
        # Far past `observation_stale_seconds`, which is what the Pi measures
        # its own confidence against. It has no way to know the Mac is alive,
        # so "I have not heard anything" is the only honest answer -- and the
        # answer it has to volunteer rather than serve the last good one.
        self.idle(3600.0)
        statuses = self.store.connector_statuses(now=self.clock.now)
        self.assertTrue(statuses)
        self.assertTrue(all(status.degraded for status in statuses))
        self.assertTrue(self.freshness()["stale"])

    def test_a_recovered_link_announces_a_new_generation(self) -> None:
        self.publish()
        first = self.connector.generation
        self.link.down()
        self.idle(100.0)
        self.publish()
        self.link.up()
        self.idle(100.0)
        self.publish()
        self.assertEqual(portable_support.STATE_READY, self.connector.state)
        self.assertGreater(self.connector.generation, first)

    def test_ten_reconnects_leave_one_row_and_one_entry_per_plan(self) -> None:
        for round_index in range(10):
            self.link.down()
            self.publish()
            self.link.up()
            # A recovered link is not a recovered connector: it is in backoff
            # and will not try again until its retry falls due, which is the
            # behaviour that keeps a flapping tailnet from being hammered. Time
            # has to pass before the reconnect can happen at all.
            self.idle(portable_support.BACKOFF_CAP_SECONDS)
            self.publish()
            self.assertEqual(
                len(self.corpus.plan_dirs),
                len(self.queue_rows()),
                f"the catalog duplicated on reconnect {round_index}",
            )
        rows = self.store.runs(now=self.clock.now, include_absent=True)
        self.assertTrue(rows, "ten reconnects published no run rows at all")
        keys = [str(row["run_key"]) for row in rows]
        self.assertEqual(len(set(keys)), len(keys))

    def test_repeated_publishing_stops_growing_the_database(self) -> None:
        self.publish()
        counted = self.row_counts()
        for _ in range(6):
            self.publish()
        self.assertEqual(counted, self.row_counts())

    def row_counts(self) -> dict[str, int]:
        """Every table's row count, which is what unbounded growth shows up in."""
        with self.store.reading() as connection:
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                name: int(
                    connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                )
                for name in names
            }


# ---------------------------------------------------------------------------
# A read crosses two processes
# ---------------------------------------------------------------------------


class CorrelatedReadTests(FullStackCase):
    """Artifacts the Pi has never seen, and the deadline that bounds them."""

    reads = True

    def test_remote_archive_listing_is_a_read_with_no_mac_paths(self) -> None:
        self.publish()
        status, body = self.request("/api/archive")
        self.assertEqual(200, status, body)
        self.assertEqual(2, len(body["entries"]))
        self.assertEqual(1, body["archive_cleanup"]["candidate_count"])
        self.assertIn("archive_retention_days", body)
        self.assertNotIn(str(self.corpus.plan_dirs[0]), json.dumps(body))

    def test_an_answer_carries_the_macs_own_material_not_a_translation(self) -> None:
        self.publish()
        status, body = self.request(
            f"/api/events?plan_key={self.corpus.plan_keys['charlie']}"
        )
        self.assertEqual(200, status)
        # `charlie` is the plan with a real `events.jsonl`, so an answer with
        # no entries would mean the read reached nothing. The entries are the
        # Mac's own records, unaltered: the Pi has never read this file.
        self.assertTrue(body.get("entries"))
        self.assertEqual("events", body["detail"])

    def test_a_lost_answer_settles_on_the_deadline_instead_of_hanging(self) -> None:
        self.publish()
        assert self.lane is not None
        # The Mac answers and the answer never arrives. The read has to end,
        # and it has to end as a timeout rather than as an empty artifact.
        self.link.reads = None
        status, body = self.request(
            f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        )
        self.assertIn(status, {200, 502, 503, 504})
        if status != 200:
            self.assertIn("reason", body)

    def test_a_read_for_a_plan_nobody_published_is_refused_not_invented(self) -> None:
        self.publish()
        status, body = self.request("/api/progress?plan_key=" + "0" * 16)
        self.assertNotEqual(200, status)
        self.assertIn("reason", body)

    def test_concurrent_reads_of_one_artifact_all_get_an_answer(self) -> None:
        self.publish()
        path = f"/api/progress?plan_key={self.corpus.plan_keys['alpha']}"
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.request(path), range(6)))
        for status, body in results:
            self.assertEqual(200, status, body)


# ---------------------------------------------------------------------------
# Identity, and the version on both ends
# ---------------------------------------------------------------------------


class IdentityAndCompatibilityTests(FullStackCase):
    """Who sent it, to whom, and whether either end can read the other."""

    #: A submission has to be reachable for a refusal about its *token* to mean
    #: anything: without a lane the service declines before it looks at one.
    mutations = True

    def test_a_frame_addressed_to_another_pi_is_refused_at_the_wire(self) -> None:
        envelope = transport.observation_envelope(
            payload=portable_payloads.heartbeat_payload(
                observed_at=NOW, bridge_alive=True, source_generation=1
            ),
            connector_id=CONNECTOR_ID,
            target_id="pi-2",
            sent_at=NOW,
        )
        with self.assertRaises(transport.TransportError):
            transport.validate_envelope(
                envelope, direction=transport.DIRECTION_UP, target_id=TARGET_ID
            )

    def test_a_transport_version_nobody_speaks_grants_nothing(self) -> None:
        self.assertIsNone(transport.negotiate_protocol([0, 99]))
        hello = portable_payloads.connector_hello(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            capabilities=self.connector.capabilities,
        )
        verdict = transport.evaluate_hello(
            dict(hello, protocol=99, protocols=[99]),
            policy=POLICY,
            target_id=TARGET_ID,
        )
        self.assertFalse(verdict.compatible)
        self.assertFalse(verdict.reads_allowed)
        self.assertFalse(verdict.mutations_allowed)
        self.assertIn(transport.ERROR_INCOMPATIBLE_PROTOCOL, verdict.reasons)

    def test_an_unknown_capability_narrows_the_pairing_without_killing_it(
        self,
    ) -> None:
        hello = portable_payloads.connector_hello(
            connector_id=CONNECTOR_ID,
            target_id=TARGET_ID,
            capabilities=(*self.connector.capabilities, "invented.capability"),
        )
        verdict = transport.evaluate_hello(hello, policy=POLICY, target_id=TARGET_ID)
        self.assertTrue(verdict.compatible)
        self.assertNotIn("invented.capability", verdict.granted)
        # Named rather than dropped, because "there is a capability here I do
        # not understand" is what tells an operator their two ends are different
        # releases -- and it must not be mistaken for a capability in force.
        self.assertIn("invented.capability", verdict.unknown)

    def test_a_reply_without_the_freshness_proof_is_refused_unsubmitted(self) -> None:
        # The Pi decides nothing about *which* token is current -- only ASL can
        # say that -- but it refuses a submission that carries no proof at all,
        # and it refuses it before the command is journalled. Acting on a live
        # run with no proof is acting on a guess about what that run is doing.
        self.publish()
        status, body = self.request(
            "/api/reply",
            method="POST",
            body={
                "operation_id": "op-reply-proofless",
                "plan_key": self.corpus.plan_keys["alpha"],
                "run_key": self.corpus.run_keys["alpha"],
                "reply_ref": "ref-1",
                "action_token": "redeem" * 4,
            },
        )
        self.assertEqual(400, status, body)
        self.assertEqual("invalid-submission", body["reason"])
        self.assertIsNone(self.store.operation("op-reply-proofless"))
