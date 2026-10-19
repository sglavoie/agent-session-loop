from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_transport_contract_support import *


class ValidExchangesRoundTrip(unittest.TestCase):
    """Compose, encode, decode, validate -- and arrive back where you started."""

    def test_every_valid_fixture_validates_in_its_direction(self) -> None:
        loaded = load_corpus("valid")
        self.assertGreaterEqual(len(loaded), len(transport.ENVELOPE_KINDS))
        for name, document in loaded:
            with self.subTest(fixture=name):
                checked = transport.validate_envelope(
                    document["envelope"],
                    direction=document["direction"],
                    target_id=SERVICE_ID,
                )
                self.assertEqual(document["envelope"], checked)

    def test_the_corpus_covers_every_kind_and_every_observation_view(self) -> None:
        kinds = set()
        views = set()
        for _, document in load_corpus("valid"):
            envelope = document["envelope"]
            kinds.add(envelope["kind"])
            if envelope["kind"] == transport.KIND_OBSERVATION:
                views.add(envelope["payload"]["view"])
        self.assertEqual(set(transport.ENVELOPE_KINDS), kinds)
        self.assertEqual(set(transport.OBSERVATION_VIEWS), views)

    def test_the_builders_compose_the_corpus_exactly(self) -> None:
        # The builders take the capability from the payload rather than from
        # the caller, so a producer cannot label a catalog page as an
        # observation of runs. Rebuilding every fixture is the check that the
        # derivation and the hand-written corpus agree.
        for name, document in load_corpus("valid"):
            with self.subTest(fixture=name):
                self.assertEqual(document["envelope"], rebuild(document["envelope"]))

    def test_encoding_and_decoding_returns_the_same_envelope(self) -> None:
        for name, document in load_corpus("valid"):
            with self.subTest(fixture=name):
                raw = transport.encode_frame(document["envelope"])
                self.assertIsInstance(raw, bytes)
                decoded = transport.validate_frame(
                    raw,
                    direction=document["direction"],
                    target_id=SERVICE_ID,
                )
                self.assertEqual(document["envelope"], decoded)

    def test_a_kind_travelling_the_wrong_way_is_refused(self) -> None:
        # Only a request travels down. An "observation" arriving from the
        # service would be the service telling the store what the Mac saw.
        for kind in transport.ENVELOPE_KINDS:
            with self.subTest(kind=kind):
                expected = transport.DIRECTION_BY_KIND[kind]
                other = (
                    transport.DIRECTION_UP
                    if expected == transport.DIRECTION_DOWN
                    else transport.DIRECTION_DOWN
                )
                self.assertIn(kind, transport.KINDS_BY_DIRECTION[expected])
                self.assertNotIn(kind, transport.KINDS_BY_DIRECTION[other])

        envelope = fixture("observation-heartbeat")
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_DOWN)
        self.assertEqual(transport.ERROR_UNKNOWN_KIND, caught.exception.code)

    def test_the_fixture_files_are_written_in_the_canonical_rendering(self) -> None:
        # A hand-edited fixture and a regenerated one have to look the same in
        # a diff, or the corpus stops being reviewable.
        for kind in ("valid", "invalid"):
            for path in sorted((CORPUS / kind).glob("*.json")):
                with self.subTest(fixture=path.name):
                    text = path.read_text(encoding="utf-8")
                    self.assertEqual(canonical(json.loads(text)), text)

    def test_every_fixture_says_which_direction_it_travels(self) -> None:
        for kind in ("valid", "invalid"):
            for name, document in load_corpus(kind):
                with self.subTest(fixture=name):
                    self.assertIn(document["direction"], transport.KINDS_BY_DIRECTION)


class ObservationsReachTheStoreUntranslated(unittest.TestCase):
    """A validated observation is an ingestible one. No shim in between."""

    def setUp(self) -> None:
        self.store = store_workbench.WorkbenchStore.open(
            ":memory:", policy=POLICY, created_at=NOW
        )
        self.addCleanup(self.store.close)

    def test_the_transport_views_are_the_stores_ingest_kinds(self) -> None:
        self.assertEqual(
            {
                store_types.KIND_HEARTBEAT,
                store_types.KIND_SNAPSHOT,
                store_types.KIND_HISTORY_BATCH,
                store_types.KIND_CATALOG_PAGE,
            },
            set(transport.OBSERVATION_VIEWS),
        )

    def test_each_observation_fixture_ingests_as_it_arrived(self) -> None:
        ingest = {
            transport.VIEW_HEARTBEAT: self.store.ingest_heartbeat,
            transport.VIEW_SNAPSHOT: self.store.ingest_snapshot,
            transport.VIEW_HISTORY_BATCH: self.store.ingest_history,
            transport.VIEW_CATALOG_PAGE: self.store.ingest_catalog,
        }
        seen = set()
        for name, document in load_corpus("valid"):
            envelope = document["envelope"]
            if envelope["kind"] != transport.KIND_OBSERVATION:
                continue
            with self.subTest(fixture=name):
                checked = transport.validate_envelope(
                    envelope, direction=transport.DIRECTION_UP, target_id=SERVICE_ID
                )
                view = checked["payload"]["view"]
                result = ingest[view](checked, received_at=NOW + 1.0)
                self.assertEqual(view, result.kind)
                self.assertEqual(CONNECTOR_ID, result.connector_id)
                self.assertFalse(result.duplicate)
                seen.add(view)
        self.assertEqual(set(transport.OBSERVATION_VIEWS), seen)

        # And the rows are readable afterwards, which is the part that proves
        # the payload bodies were the store's own and not merely accepted.
        self.assertEqual(1, len(self.store.runs(now=NOW + 1.0)))
        self.assertEqual(1, len(self.store.history(now=NOW + 1.0)))
        self.assertEqual(1, len(self.store.catalog(now=NOW + 1.0)))


# --------------------------------------------------------------------------
# Predictable failure
# --------------------------------------------------------------------------


class InvalidMessagesFailPredictably(unittest.TestCase):
    """Each refused frame names one code, and gets the code it names."""

    def test_every_invalid_fixture_raises_the_code_it_declares(self) -> None:
        loaded = load_corpus("invalid")
        self.assertGreaterEqual(len(loaded), 20)
        for name, document in loaded:
            with self.subTest(fixture=name):
                self.assertIn(document["error"], transport.TRANSPORT_ERROR_CODES)
                self.assertTrue(document["why"].strip())
                with self.assertRaises(transport.TransportError) as caught:
                    if "frame" in document:
                        transport.validate_frame(
                            document["frame"].encode("utf-8"),
                            direction=document["direction"],
                        )
                    else:
                        transport.validate_envelope(
                            document["envelope"],
                            direction=document["direction"],
                            target_id=SERVICE_ID,
                        )
                self.assertEqual(document["error"], caught.exception.code)
                self.assertTrue(caught.exception.detail)

    def test_the_corpus_covers_every_frame_level_refusal(self) -> None:
        """A new error code cannot arrive without a frame that produces it.

        The four exceptions are listed rather than filtered by a rule, so
        adding a fifth is a decision somebody makes here and not a silence.
        """
        covered = {document["error"] for _, document in load_corpus("invalid")}
        expected = (
            set(transport.TRANSPORT_ERROR_CODES) - CODES_PROVED_OUTSIDE_THE_CORPUS
        )
        self.assertEqual(expected, covered)

    def test_an_oversized_frame_is_refused_before_it_is_parsed(self) -> None:
        # Junk, and too much of it. If the size check ran after the parse this
        # would come back "malformed" -- which is to say the process would have
        # parsed an unbounded frame to find out it was too big.
        raw = b"{" + b"x" * 4096
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_frame(raw, limit=1024)
        self.assertEqual(transport.ERROR_FRAME_TOO_LARGE, caught.exception.code)

        oversized = b"[" + b" " * transport.MAX_FRAME_BYTES
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_frame(oversized)
        self.assertEqual(transport.ERROR_FRAME_TOO_LARGE, caught.exception.code)

    def test_a_frame_within_the_ceiling_is_still_parsed_normally(self) -> None:
        envelope = fixture("observation-heartbeat")
        raw = transport.encode_frame(envelope)
        self.assertLess(len(raw), transport.MAX_FRAME_BYTES)
        self.assertEqual(envelope, transport.validate_frame(raw))

    def test_encoding_refuses_a_frame_the_far_end_would_reject(self) -> None:
        # Measured on the way out too, so an oversized answer is a local error
        # the connector can page around instead of a refusal discovered after
        # the bytes have crossed the network.
        envelope = fixture("observation-heartbeat")
        with self.assertRaises(transport.TransportError) as caught:
            transport.encode_frame(envelope, limit=64)
        self.assertEqual(transport.ERROR_FRAME_TOO_LARGE, caught.exception.code)

    def test_encoding_refuses_a_non_finite_number(self) -> None:
        envelope = fixture("observation-heartbeat")
        envelope["payload"]["projection_age_seconds"] = float("inf")
        with self.assertRaises(transport.TransportError) as caught:
            transport.encode_frame(envelope)
        self.assertEqual(transport.ERROR_MALFORMED, caught.exception.code)

    def test_detail_content_is_bounded_well_under_the_frame_ceiling(self) -> None:
        payload = detail_payload(
            content="x" * (transport.MAX_DETAIL_BYTES + 1), encoding="utf-8"
        )
        with self.assertRaises(transport.TransportError) as caught:
            transport.detail_envelope(
                payload=payload,
                connector_id=CONNECTOR_ID,
                target_id=SERVICE_ID,
                sent_at=NOW,
                request_id="req-0002",
            )
        self.assertEqual(transport.ERROR_FRAME_TOO_LARGE, caught.exception.code)

    def test_worst_case_escaping_of_a_full_detail_still_fits_one_frame(self) -> None:
        """The headroom the detail ceiling exists to buy.

        JSON's worst case is one source byte becoming a six-character escape,
        so a detail window at its limit has to leave a factor of six of room
        under the frame ceiling. Proved with the character that actually costs
        six, rather than asserted about the constants.
        """
        self.assertLessEqual(
            transport.MAX_DETAIL_BYTES * transport.DETAIL_ESCAPE_FACTOR,
            transport.MAX_FRAME_BYTES,
        )
        content = "\x00" * transport.MAX_DETAIL_BYTES
        envelope = transport.detail_envelope(
            payload=detail_payload(
                content=content,
                content_bytes=transport.MAX_DETAIL_BYTES,
                encoding="utf-8",
                truncated=True,
            ),
            connector_id=CONNECTOR_ID,
            target_id=SERVICE_ID,
            sent_at=NOW,
            request_id="req-0002",
        )
        raw = transport.encode_frame(envelope)
        self.assertGreater(
            len(raw), transport.MAX_DETAIL_BYTES * (transport.DETAIL_ESCAPE_FACTOR - 1)
        )
        self.assertEqual(envelope, transport.validate_frame(raw))

    def test_a_list_longer_than_its_ceiling_is_refused_as_an_oversized_frame(
        self,
    ) -> None:
        envelope = fixture("observation-snapshot")
        row = envelope["payload"]["runs"][0]
        count = transport.MAX_RUNS_PER_SNAPSHOT + 1
        envelope["payload"]["runs"] = [dict(row) for _ in range(count)]
        envelope["payload"]["row_count"] = count
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_UP)
        self.assertEqual(transport.ERROR_FRAME_TOO_LARGE, caught.exception.code)

    def test_a_deadline_is_only_carried_by_the_kind_that_owns_one(self) -> None:
        envelope = fixture("observation-heartbeat")
        envelope["deadline"] = NOW + 30.0
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_UP)
        self.assertEqual(transport.ERROR_UNKNOWN_FIELD, caught.exception.code)

    def test_a_request_deadline_is_bounded_on_both_sides(self) -> None:
        envelope = fixture("request-catalog-publish")
        for deadline, why in (
            (envelope["sent_at"] - 1.0, "already passed"),
            (
                envelope["sent_at"] + transport.MAX_REQUEST_LIFETIME_SECONDS + 1.0,
                "too far out",
            ),
        ):
            with self.subTest(deadline=why):
                candidate = dict(envelope, deadline=deadline)
                with self.assertRaises(transport.TransportError) as caught:
                    transport.validate_envelope(
                        candidate, direction=transport.DIRECTION_DOWN
                    )
                self.assertEqual(transport.ERROR_DEADLINE, caught.exception.code)

    def test_a_connector_cannot_report_a_status_it_does_not_own(self) -> None:
        # ``pending`` is the service's and ``cancelled`` is the owner's. Both
        # are real operation states; neither is one the far end may assert.
        from asl_workbench import operations

        self.assertEqual(
            set(operations.OPERATION_STATUSES)
            - {operations.OPERATION_PENDING, operations.OPERATION_CANCELLED},
            set(transport.CONNECTOR_REPORTED_STATUSES),
        )
        envelope = fixture("operation-dispatched")
        for status in (operations.OPERATION_PENDING, operations.OPERATION_CANCELLED):
            with self.subTest(status=status):
                candidate = dict(envelope)
                candidate["payload"] = dict(envelope["payload"], status=status)
                with self.assertRaises(transport.TransportError) as caught:
                    transport.validate_envelope(
                        candidate, direction=transport.DIRECTION_UP
                    )
                self.assertEqual(transport.ERROR_INVALID_FIELD, caught.exception.code)

    def test_an_unconfirmed_operation_is_not_an_expired_one(self) -> None:
        """The two words the replay decision turns on are both carried.

        ``expired`` says the mutation certainly did not run; ``unconfirmed``
        says it may have. Collapsing them would make a replay look safe.
        """
        self.assertIn(
            transport.OPERATION_EXPIRED, transport.CONNECTOR_REPORTED_STATUSES
        )
        self.assertIn(
            transport.OPERATION_UNCONFIRMED, transport.CONNECTOR_REPORTED_STATUSES
        )
        envelope = fixture("operation-unconfirmed")
        checked = transport.validate_envelope(
            envelope, direction=transport.DIRECTION_UP, target_id=SERVICE_ID
        )
        self.assertEqual(transport.OPERATION_UNCONFIRMED, checked["payload"]["status"])
