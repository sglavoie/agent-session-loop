"""The Workbench's own transport: five envelopes, bounded, and separately versioned.

Session 10 adds a wire of the Workbench's own between the Mac connector and the
Pi service. Everything here holds one of the three properties that wire is for.

**Valid exchanges round-trip.** The fixture corpus under
``tests/fixtures/workbench_transport/valid`` is composed through the builders,
encoded, decoded and validated again, and the result has to equal the fixture
byte for byte in content. The round trip does not stop at the envelope: an
observation that validates is handed straight to :class:`WorkbenchStore` and
ingested with no translation step, because the payload bodies *are* what the
store already reads. A transport that needed a shim between "valid" and
"storable" would have two schemas and one name for them.

**Invalid or oversized messages fail predictably.** Every frame under
``invalid/`` names the single error code it must raise, and the code is
asserted rather than the mere fact of failure -- a refusal that reports the
wrong reason is how an oversized frame gets diagnosed as a version mismatch and
a perfectly good connector gets torn out. Size is measured before the bytes are
parsed, so a frame too large to accept is never a frame this process decodes.

**Incompatible deployments cannot execute mutations.** This is the distinction
the session exists to draw, and it is drawn twice: an unusable protocol
*version* is fatal in both directions, while an unsupported individual
*capability* -- or a contract set this build has never seen -- narrows what is
permitted and leaves reads working. :func:`transport.authorize` is where that
verdict stops being a dataclass and becomes a refusal on the path a mutation
takes.

Two boundaries are held alongside them. The transport schemas are separate from
AgentMux's contract: the corpus lives outside the digested tree, and the
contract set id travels through these envelopes as *data* rather than as this
transport's own version number. And the credential roles are the Companion's,
ported rather than reinvented, with each role now bound to the envelope kinds
and capabilities it may exercise.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import sys
import unittest
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import agent_mux_contract
from asl_workbench import (
    companion_inventory,
    credentials,
    observation,
    store,
    transport,
)
from asl_workbench.operations import (
    FORBIDDEN_PERSISTED_KEYS,
    OPERATION_PERSIST_FIELDS,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = REPO_ROOT / "asl_workbench"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "workbench_transport"

NOW = 1770000000.0
CONNECTOR_ID = "mac-studio"
SERVICE_ID = "pi-companion"
PLAN_KEY = "0123456789abcdef"
CONTRACT_SET_ID = observation.SUPPORTED_CONTRACT_SET_IDS[0]

POLICY = observation.ObservationPolicy()

#: The four codes the corpus cannot carry, and where each is proved instead.
#: ``frame-too-large`` needs a frame no fixture file should contain; the other
#: three are decided by :func:`transport.evaluate_hello` and
#: :func:`credentials.authorize_upload` about a *pairing*, not about a frame.
CODES_PROVED_OUTSIDE_THE_CORPUS = frozenset(
    {
        transport.ERROR_FRAME_TOO_LARGE,
        transport.ERROR_INCOMPATIBLE_CONTRACT,
        transport.ERROR_OBSERVATION_ONLY,
        transport.ERROR_UNAUTHORIZED,
    }
)


# --------------------------------------------------------------------------
# Corpus helpers
# --------------------------------------------------------------------------


def canonical(document: Any) -> str:
    """The one rendering every fixture in this corpus is written in."""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def load_corpus(kind: str) -> list[tuple[str, dict[str, Any]]]:
    paths = sorted((CORPUS / kind).glob("*.json"))
    return [(path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in paths]


def fixture(name: str) -> dict[str, Any]:
    """One valid envelope, freshly decoded so a test may mutate it."""
    path = CORPUS / "valid" / f"{name}.json"
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    envelope: dict[str, Any] = document["envelope"]
    return envelope


def rebuild(envelope: dict[str, Any]) -> dict[str, Any]:
    """Compose the same envelope through the builder for its kind."""
    kind = envelope["kind"]
    common: dict[str, Any] = {
        "payload": envelope["payload"],
        "connector_id": envelope["connector_id"],
        "target_id": envelope["target_id"],
        "sent_at": envelope["sent_at"],
    }
    if kind == transport.KIND_REQUEST:
        return transport.request_envelope(
            **common,
            request_id=envelope["request_id"],
            deadline=envelope["deadline"],
        )
    if kind == transport.KIND_OBSERVATION:
        return transport.observation_envelope(**common)
    if kind == transport.KIND_DETAIL:
        return transport.detail_envelope(**common, request_id=envelope["request_id"])
    if kind == transport.KIND_OPERATION:
        return transport.operation_envelope(
            **common, request_id=envelope.get("request_id")
        )
    return transport.receipt_envelope(
        **common,
        capability=envelope["capability"],
        request_id=envelope["request_id"],
    )


def hello(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "protocol": transport.TRANSPORT_PROTOCOL_VERSION,
        "connector_id": CONNECTOR_ID,
        "target_id": SERVICE_ID,
        "capabilities": list(transport.TRANSPORT_CAPABILITIES),
        "contract_set_id": CONTRACT_SET_ID,
    }
    document.update(overrides)
    return document


def detail_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "detail": "final-review",
        "target": {"plan_key": PLAN_KEY},
        "observed_at": NOW,
        "complete": "partial",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


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
        self.store = store.WorkbenchStore.open(
            ":memory:", policy=POLICY, created_at=NOW
        )
        self.addCleanup(self.store.close)

    def test_the_transport_views_are_the_stores_ingest_kinds(self) -> None:
        self.assertEqual(
            {
                store.KIND_HEARTBEAT,
                store.KIND_SNAPSHOT,
                store.KIND_HISTORY_BATCH,
                store.KIND_CATALOG_PAGE,
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


# --------------------------------------------------------------------------
# The privacy boundary
# --------------------------------------------------------------------------


class ProseNeverTravelsExceptWhereItIsDeclaredTo(unittest.TestCase):
    """One envelope may carry a body. It is the one that is never written down."""

    def test_a_body_key_is_refused_on_every_kind_but_a_detail(self) -> None:
        cases = {
            "request-catalog-publish": transport.DIRECTION_DOWN,
            "observation-heartbeat": transport.DIRECTION_UP,
            "operation-dispatched": transport.DIRECTION_UP,
            "receipt-delivered": transport.DIRECTION_UP,
        }
        for name, direction in cases.items():
            with self.subTest(fixture=name):
                envelope = fixture(name)
                envelope["payload"]["question_body"] = "Which harness should I use?"
                with self.assertRaises(transport.TransportError) as caught:
                    transport.validate_envelope(envelope, direction=direction)
                self.assertEqual(transport.ERROR_BODY_LEAK, caught.exception.code)

    def test_the_body_walk_reaches_a_nested_key(self) -> None:
        # A leak that only had to survive a top-level check would be spelled
        # one level down.
        envelope = fixture("observation-snapshot")
        envelope["payload"]["runs"][0]["target"] = {"reply_text": "go ahead"}
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_UP)
        self.assertEqual(transport.ERROR_BODY_LEAK, caught.exception.code)

    def test_the_leak_is_reported_as_a_leak_and_not_as_an_unknown_field(self) -> None:
        """Ordering, and the reason it is worth pinning.

        The closed shapes would refuse a prose key either way. Only the order
        says *why*, and "you tried to send a body" is the refusal a connector
        author can act on.
        """
        envelope = fixture("operation-dispatched")
        envelope["payload"]["prompt"] = "anything"
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_UP)
        self.assertEqual(transport.ERROR_BODY_LEAK, caught.exception.code)
        self.assertIn("prompt", caught.exception.detail)

    def test_a_detail_may_carry_the_prose_it_was_asked_for(self) -> None:
        envelope = transport.detail_envelope(
            payload=detail_payload(
                content="# Final review\n\nAll clear.\n",
                content_bytes=27,
                encoding="utf-8",
            ),
            connector_id=CONNECTOR_ID,
            target_id=SERVICE_ID,
            sent_at=NOW,
            request_id="req-0002",
        )
        self.assertEqual(
            "# Final review\n\nAll clear.\n", envelope["payload"]["content"]
        )

    def test_the_exempt_field_names_are_not_storage_column_names(self) -> None:
        """The exemption is safe only because nothing named here is persisted.

        A detail body field sharing a name with a persisted column would be a
        route from "answered online and dropped" to "written to disk".
        """
        allowlists = {
            "run summary": observation.RUN_SUMMARY_PERSIST_FIELDS,
            "history": observation.HISTORY_PERSIST_FIELDS,
            "catalog": observation.CATALOG_PERSIST_FIELDS,
            "operation": OPERATION_PERSIST_FIELDS,
        }
        for label, allowlist in allowlists.items():
            with self.subTest(allowlist=label):
                self.assertEqual(frozenset(), transport.DETAIL_BODY_FIELDS & allowlist)

    def test_the_wire_walk_uses_the_list_the_store_already_keeps(self) -> None:
        # Not a second list that could drift from the first: a key forbidden on
        # disk is forbidden on the wire by construction.
        envelope = fixture("observation-heartbeat")
        for name in sorted(FORBIDDEN_PERSISTED_KEYS):
            with self.subTest(key=name):
                candidate = dict(envelope)
                candidate["payload"] = dict(envelope["payload"])
                candidate["payload"][name] = "x"
                with self.assertRaises(transport.TransportError) as caught:
                    transport.validate_envelope(
                        candidate, direction=transport.DIRECTION_UP
                    )
                self.assertEqual(transport.ERROR_BODY_LEAK, caught.exception.code)


# --------------------------------------------------------------------------
# Compatibility
# --------------------------------------------------------------------------


class VersionsAndCapabilitiesAreDifferentFailures(unittest.TestCase):
    """An unusable version is fatal. An unusable capability is a narrowing."""

    def test_a_matched_pairing_may_do_everything(self) -> None:
        verdict = transport.evaluate_hello(hello(), policy=POLICY, target_id=SERVICE_ID)
        self.assertTrue(verdict.compatible)
        self.assertTrue(verdict.reads_allowed)
        self.assertTrue(verdict.mutations_allowed)
        self.assertIsNone(verdict.mutation_refusal)
        self.assertEqual(frozenset(transport.TRANSPORT_CAPABILITIES), verdict.granted)
        self.assertEqual((), verdict.reasons)
        self.assertEqual(transport.TRANSPORT_PROTOCOL_VERSION, verdict.protocol)
        for capability in transport.TRANSPORT_CAPABILITIES:
            self.assertTrue(verdict.permits(capability))

    def test_an_unusable_version_stops_everything_in_both_directions(self) -> None:
        verdict = transport.evaluate_hello(
            hello(protocol=transport.TRANSPORT_PROTOCOL_VERSION + 1),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertIsNone(verdict.protocol)
        self.assertFalse(verdict.reads_allowed)
        self.assertFalse(verdict.compatible)
        self.assertFalse(verdict.mutations_allowed)
        self.assertIn(transport.ERROR_INCOMPATIBLE_PROTOCOL, verdict.reasons)
        self.assertEqual(
            transport.ERROR_INCOMPATIBLE_PROTOCOL, verdict.mutation_refusal
        )
        for capability in transport.TRANSPORT_CAPABILITIES:
            self.assertFalse(verdict.permits(capability))

    def test_a_version_this_build_knows_is_negotiated_out_of_a_list(self) -> None:
        # A newer connector offering both keeps working against this build,
        # which is the difference between "newer" and "incompatible".
        verdict = transport.evaluate_hello(
            hello(
                protocol=transport.TRANSPORT_PROTOCOL_VERSION + 1,
                protocols=[
                    transport.TRANSPORT_PROTOCOL_VERSION + 1,
                    transport.TRANSPORT_PROTOCOL_VERSION,
                ],
            ),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertEqual(transport.TRANSPORT_PROTOCOL_VERSION, verdict.protocol)
        self.assertTrue(verdict.compatible)
        self.assertTrue(verdict.mutations_allowed)

    def test_protocol_negotiation_takes_the_highest_shared_version(self) -> None:
        self.assertEqual(
            transport.TRANSPORT_PROTOCOL_VERSION,
            transport.negotiate_protocol([transport.TRANSPORT_PROTOCOL_VERSION, 99]),
        )
        self.assertIsNone(transport.negotiate_protocol([]))
        self.assertIsNone(transport.negotiate_protocol([99]))
        # ``True`` is an ``int`` in Python and is not a protocol version.
        self.assertIsNone(transport.negotiate_protocol([True]))
        self.assertIsNone(transport.negotiate_protocol(["1"]))

    def test_a_capability_the_peer_lacks_narrows_rather_than_severs(self) -> None:
        offered = [
            capability
            for capability in transport.TRANSPORT_CAPABILITIES
            if capability != transport.CAPABILITY_MUTATE
        ]
        verdict = transport.evaluate_hello(
            hello(capabilities=offered), policy=POLICY, target_id=SERVICE_ID
        )
        self.assertTrue(verdict.reads_allowed)
        self.assertTrue(verdict.compatible)
        self.assertFalse(verdict.mutations_allowed)
        self.assertEqual(
            transport.ERROR_UNSUPPORTED_CAPABILITY, verdict.mutation_refusal
        )
        self.assertTrue(verdict.permits(transport.CAPABILITY_OBSERVE))
        self.assertFalse(verdict.permits(transport.CAPABILITY_MUTATE))

    def test_a_capability_this_build_has_never_heard_of_is_a_note(self) -> None:
        verdict = transport.evaluate_hello(
            hello(capabilities=[*transport.TRANSPORT_CAPABILITIES, "teleport"]),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertEqual(frozenset({"teleport"}), verdict.unknown)
        self.assertTrue(verdict.compatible)
        self.assertTrue(verdict.mutations_allowed)
        self.assertTrue(any("teleport" in note for note in verdict.notes))
        self.assertNotIn("teleport", verdict.granted)

    def test_a_peer_that_cannot_observe_is_not_a_peer_this_build_can_use(
        self,
    ) -> None:
        verdict = transport.evaluate_hello(
            hello(capabilities=[transport.CAPABILITY_CATALOG]),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertEqual(transport.REQUIRED_CAPABILITIES, frozenset(verdict.missing))
        self.assertFalse(verdict.compatible)
        self.assertFalse(verdict.mutations_allowed)

    def test_an_unknown_contract_set_keeps_the_reads_and_stops_the_mutations(
        self,
    ) -> None:
        """The narrowing this program is actually going to hit.

        A connector shipping an AgentMux contract set this build has never seen
        is still a connector whose observations are worth recording. What it
        must not do is drive a mutation whose method vocabulary neither end can
        agree on.
        """
        verdict = transport.evaluate_hello(
            hello(contract_set_id="asl-amx-1.deadbeefdeadbeef"),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertTrue(verdict.reads_allowed)
        self.assertFalse(verdict.compatible)
        self.assertFalse(verdict.mutations_allowed)
        self.assertEqual(
            transport.ERROR_INCOMPATIBLE_CONTRACT, verdict.mutation_refusal
        )

    def test_an_envelope_addressed_elsewhere_is_fatal(self) -> None:
        verdict = transport.evaluate_hello(
            hello(target_id="pi-somewhere-else"),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertFalse(verdict.reads_allowed)
        self.assertIn(transport.ERROR_WRONG_TARGET, verdict.reasons)

    def test_observation_only_is_reported_as_a_setting_and_not_a_mismatch(
        self,
    ) -> None:
        by_the_peer = transport.evaluate_hello(
            hello(observation_only=True), policy=POLICY, target_id=SERVICE_ID
        )
        self.assertTrue(by_the_peer.reads_allowed)
        self.assertEqual(transport.ERROR_OBSERVATION_ONLY, by_the_peer.mutation_refusal)

        local = observation.ObservationPolicy(mutations_permitted=False)
        by_this_end = transport.evaluate_hello(
            hello(), policy=local, target_id=SERVICE_ID
        )
        self.assertTrue(by_this_end.reads_allowed)
        self.assertFalse(by_this_end.mutations_allowed)
        self.assertEqual(transport.ERROR_OBSERVATION_ONLY, by_this_end.mutation_refusal)

    def test_a_real_incompatibility_outranks_a_deliberate_setting(self) -> None:
        # Otherwise a deployment set to observe only would report "observation
        # only" for a version mismatch and hide it for as long as the setting
        # stayed on.
        verdict = transport.evaluate_hello(
            hello(
                protocol=transport.TRANSPORT_PROTOCOL_VERSION + 1,
                observation_only=True,
            ),
            policy=POLICY,
            target_id=SERVICE_ID,
        )
        self.assertIn(transport.ERROR_OBSERVATION_ONLY, verdict.reasons)
        self.assertEqual(
            transport.ERROR_INCOMPATIBLE_PROTOCOL, verdict.mutation_refusal
        )

    def test_a_hello_is_closed_like_every_other_document_here(self) -> None:
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_hello(hello(retry_after=5))
        self.assertEqual(transport.ERROR_UNKNOWN_FIELD, caught.exception.code)


class IncompatibleDeploymentsCannotMutate(unittest.TestCase):
    """Where the verdict stops being a dataclass and becomes a refusal."""

    def setUp(self) -> None:
        self.operation = fixture("operation-dispatched")
        self.observation = fixture("observation-heartbeat")

    def _verdict(self, **overrides: Any) -> transport.TransportCompatibility:
        return transport.evaluate_hello(
            hello(**overrides), policy=POLICY, target_id=SERVICE_ID
        )

    def test_a_matched_pairing_authorizes_both(self) -> None:
        verdict = self._verdict()
        self.assertIs(self.operation, transport.authorize(self.operation, verdict))
        self.assertIs(self.observation, transport.authorize(self.observation, verdict))

    def test_an_incompatible_version_refuses_even_an_observation(self) -> None:
        verdict = self._verdict(protocol=transport.TRANSPORT_PROTOCOL_VERSION + 1)
        for envelope in (self.observation, self.operation):
            with self.subTest(kind=envelope["kind"]):
                with self.assertRaises(transport.TransportError) as caught:
                    transport.authorize(envelope, verdict)
                self.assertEqual(
                    transport.ERROR_INCOMPATIBLE_PROTOCOL, caught.exception.code
                )

    def test_a_narrowed_pairing_refuses_the_mutation_and_keeps_the_observation(
        self,
    ) -> None:
        verdict = self._verdict(
            capabilities=[
                capability
                for capability in transport.TRANSPORT_CAPABILITIES
                if capability != transport.CAPABILITY_MUTATE
            ]
        )
        self.assertIs(self.observation, transport.authorize(self.observation, verdict))
        with self.assertRaises(transport.TransportError) as caught:
            transport.authorize(self.operation, verdict)
        self.assertEqual(transport.ERROR_UNSUPPORTED_CAPABILITY, caught.exception.code)

    def test_an_unknown_contract_set_refuses_only_the_mutation(self) -> None:
        verdict = self._verdict(contract_set_id="asl-amx-1.deadbeefdeadbeef")
        self.assertIs(self.observation, transport.authorize(self.observation, verdict))
        with self.assertRaises(transport.TransportError) as caught:
            transport.authorize(self.operation, verdict)
        self.assertEqual(transport.ERROR_INCOMPATIBLE_CONTRACT, caught.exception.code)

    def test_observation_only_refuses_the_mutation_by_that_name(self) -> None:
        verdict = self._verdict(observation_only=True)
        self.assertIs(self.observation, transport.authorize(self.observation, verdict))
        with self.assertRaises(transport.TransportError) as caught:
            transport.authorize(self.operation, verdict)
        self.assertEqual(transport.ERROR_OBSERVATION_ONLY, caught.exception.code)

    def test_an_envelope_from_another_connector_is_refused(self) -> None:
        # The handshake settled which connector this pairing is with; a frame
        # from a different one is not covered by it.
        verdict = self._verdict()
        stranger = dict(self.observation, connector_id="mac-mini")
        with self.assertRaises(transport.TransportError) as caught:
            transport.authorize(stranger, verdict)
        self.assertEqual(transport.ERROR_WRONG_TARGET, caught.exception.code)

    def test_an_envelope_without_a_readable_capability_is_refused(self) -> None:
        verdict = self._verdict()
        with self.assertRaises(transport.TransportError) as caught:
            transport.authorize({"capability": "teleport"}, verdict)
        self.assertEqual(transport.ERROR_INVALID_FIELD, caught.exception.code)


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


class RolesSeparateWhatEachEndMayDo(unittest.TestCase):
    """The Companion's role separation, now bound to envelope kinds."""

    def setUp(self) -> None:
        self.store = credentials.CredentialStore()
        self.connector, self.connector_token = credentials.issue(
            credentials.ROLE_CONNECTOR, created_at=NOW, subject=CONNECTOR_ID
        )
        self.summary, self.summary_token = credentials.issue(
            credentials.ROLE_SUMMARY, created_at=NOW
        )
        self.store.add(self.connector)
        self.store.add(self.summary)
        self.envelope = fixture("observation-heartbeat")

    def bearer(self, token: str) -> str:
        return f"Bearer {token}"

    def test_a_connector_credential_uploads_under_its_own_subject(self) -> None:
        granted = credentials.authorize_upload(
            self.store, self.envelope, authorization=self.bearer(self.connector_token)
        )
        self.assertEqual(credentials.ROLE_CONNECTOR, granted.role)
        self.assertEqual(CONNECTOR_ID, granted.subject)
        self.assertEqual(self.connector.credential_id, granted.credential_id)

    def test_a_connector_credential_cannot_upload_under_another_name(self) -> None:
        """The binding a token alone cannot provide.

        The envelope names its own ``connector_id`` and the store keys
        everything by it, so without the subject check a leaked connector token
        would be a licence to file observations under any Mac's name.
        """
        stranger = dict(self.envelope, connector_id="mac-mini")
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                self.store, stranger, authorization=self.bearer(self.connector_token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_a_summary_credential_cannot_upload_anything(self) -> None:
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                self.store, self.envelope, authorization=self.bearer(self.summary_token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)
        self.assertEqual(frozenset(), credentials.ROLE_CAPABILITIES["summary"])

    def test_no_credential_at_all_is_a_refusal_and_not_a_default(self) -> None:
        for header in (None, "", "Basic abc", "Bearer", "Bearer nonsense"):
            with self.subTest(header=header):
                with self.assertRaises(transport.TransportError) as caught:
                    credentials.authorize_upload(
                        self.store, self.envelope, authorization=header
                    )
                self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_only_the_bearer_scheme_is_read(self) -> None:
        # A service that also accepted Basic would accept a credential a
        # browser can be talked into sending automatically.
        self.assertEqual(
            self.connector_token,
            credentials.parse_authorization(self.bearer(self.connector_token)),
        )
        self.assertIsNone(
            credentials.parse_authorization(f"Basic {self.connector_token}")
        )
        self.assertIsNone(credentials.parse_authorization(None))
        self.assertEqual(
            self.connector_token,
            credentials.parse_authorization(f"bearer {self.connector_token}"),
        )

    def test_a_connector_cannot_send_the_envelope_the_service_sends(self) -> None:
        request = fixture("request-catalog-publish")
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                self.store, request, authorization=self.bearer(self.connector_token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_every_upload_kind_is_the_connectors_and_only_the_connectors(
        self,
    ) -> None:
        for kind in transport.KINDS_BY_DIRECTION[transport.DIRECTION_UP]:
            with self.subTest(kind=kind):
                self.assertEqual(
                    frozenset({credentials.ROLE_CONNECTOR}),
                    credentials.ROLES_BY_KIND[kind],
                )
        self.assertEqual(
            frozenset({credentials.ROLE_OWNER}),
            credentials.ROLES_BY_KIND[transport.KIND_REQUEST],
        )

    def test_a_credential_that_speaks_for_nobody_uploads_nothing(self) -> None:
        loose, token = credentials.issue(
            credentials.ROLE_CONNECTOR, created_at=NOW, label="no subject"
        )
        store_with_loose = credentials.CredentialStore([loose])
        with self.assertRaises(transport.TransportError) as caught:
            credentials.authorize_upload(
                store_with_loose, self.envelope, authorization=self.bearer(token)
            )
        self.assertEqual(transport.ERROR_UNAUTHORIZED, caught.exception.code)

    def test_a_disabled_credential_proves_nothing(self) -> None:
        disabled = credentials.Credential(
            credential_id=self.connector.credential_id,
            role=self.connector.role,
            salt=self.connector.salt,
            digest=self.connector.digest,
            created_at=self.connector.created_at,
            subject=self.connector.subject,
            disabled=True,
        )
        revoked = credentials.CredentialStore([disabled])
        self.assertIsNone(revoked.authenticate(self.connector_token))

    def test_a_wrong_secret_and_an_unknown_id_fail_the_same_way(self) -> None:
        # A caller that could tell them apart could enumerate the file's
        # contents through the front door.
        credential_id, _, secret = self.connector_token.partition(
            credentials.TOKEN_SEPARATOR
        )
        self.assertIsNone(self.store.authenticate(f"{credential_id}.wrong"))
        self.assertIsNone(self.store.authenticate(f"deadbeef.{secret}"))
        self.assertIsNone(self.store.authenticate("no-separator"))
        self.assertIsNone(self.store.authenticate(None))

    def test_the_owner_is_an_identity_rather_than_a_token(self) -> None:
        owner = credentials.resolve_role(
            self.store,
            identity="Owner@Example.COM",
            owner="owner@example.com",
        )
        self.assertEqual(credentials.ROLE_OWNER, owner.role)
        self.assertIsNone(owner.credential_id)
        self.assertTrue(owner.permits(transport.CAPABILITY_DETAIL))
        self.assertFalse(owner.permits(transport.CAPABILITY_MUTATE))

    def test_an_identity_outranks_a_token_the_same_caller_also_holds(self) -> None:
        # Otherwise a debugging header would quietly downgrade a browser
        # session to whatever token happened to be attached.
        resolved = credentials.resolve_role(
            self.store,
            authorization=self.bearer(self.summary_token),
            identity="owner@example.com",
            owner="owner@example.com",
        )
        self.assertEqual(credentials.ROLE_OWNER, resolved.role)

    def test_an_unconfigured_owner_is_never_matched(self) -> None:
        for configured, presented in ((None, "x@y"), ("", "x@y"), ("  ", "  ")):
            with self.subTest(configured=configured):
                self.assertFalse(credentials.owner_matches(configured, presented))

    def test_a_caller_proving_nothing_is_public_rather_than_refused(self) -> None:
        # The refusal belongs to what is being asked for, not to the act of
        # asking.
        resolved = credentials.resolve_role(self.store, authorization="Bearer nope")
        self.assertEqual(credentials.ROLE_PUBLIC, resolved.role)
        self.assertEqual(frozenset(), credentials.ROLE_CAPABILITIES["public"])
        for capability in transport.TRANSPORT_CAPABILITIES:
            self.assertFalse(resolved.permits(capability))

    def test_only_two_roles_may_be_issued(self) -> None:
        # An owner is an identity and the public is everybody; neither is
        # something a token can mint.
        for role in (credentials.ROLE_OWNER, credentials.ROLE_PUBLIC):
            with (
                self.subTest(role=role),
                self.assertRaises(credentials.CredentialError),
            ):
                credentials.issue(role, created_at=NOW)

    def test_a_duplicate_credential_id_is_refused(self) -> None:
        with self.assertRaises(credentials.CredentialError):
            self.store.add(self.connector)


class TheCredentialFileIsTheCompanionsFile(unittest.TestCase):
    """Ported, not reinvented: an installed file has to keep working."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "credentials.json"

    def test_a_saved_file_round_trips_and_is_owner_readable_only(self) -> None:
        credential, token = credentials.issue(
            credentials.ROLE_CONNECTOR, created_at=NOW, subject=CONNECTOR_ID
        )
        original = credentials.CredentialStore([credential])
        original.save(self.path)
        mode = self.path.stat().st_mode
        self.assertEqual(0, mode & (stat.S_IRWXG | stat.S_IRWXO))

        reloaded = credentials.CredentialStore.load(self.path)
        self.assertEqual(original.as_document(), reloaded.as_document())
        self.assertIsNotNone(reloaded.authenticate(token))

    def test_a_file_anyone_can_read_is_refused_rather_than_used(self) -> None:
        credential, _ = credentials.issue(
            credentials.ROLE_SUMMARY, created_at=NOW, label="kuma"
        )
        credentials.CredentialStore([credential]).save(self.path)
        os.chmod(self.path, 0o644)
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore.load(self.path)

    def test_a_file_written_by_the_companion_loads_unchanged(self) -> None:
        """The interop claim, written out by hand rather than by this module.

        The digest is an HMAC-SHA256 over the secret keyed by the credential's
        own salt, exactly as ``companion/auth.py`` computes it. Recomputing it
        here proves the algorithm rather than asserting the two files look
        alike.
        """
        salt = "5f0d2c1b9a8877665544332211aabbcc"
        secret = "a-secret-the-companion-handed-out-once"
        digest = hmac_new(
            bytes.fromhex(salt), secret.encode("utf-8"), sha256
        ).hexdigest()
        document = {
            "version": credentials.CREDENTIALS_SCHEMA_VERSION,
            "credentials": [
                {
                    "credential_id": "0011223344556677",
                    "role": "connector",
                    "salt": salt,
                    "digest": digest,
                    "created_at": NOW,
                    "subject": CONNECTOR_ID,
                    "label": "mac studio",
                }
            ],
        }
        loaded = credentials.CredentialStore.from_document(document)
        self.assertEqual(1, len(loaded))
        credential = loaded.authenticate(f"0011223344556677.{secret}")
        self.assertIsNotNone(credential)
        assert credential is not None
        self.assertEqual(CONNECTOR_ID, credential.subject)
        self.assertEqual(document, loaded.as_document())

    def test_an_unreadable_or_unknown_file_is_refused_by_name(self) -> None:
        for document, why in (
            ([], "not an object"),
            ({"version": 99, "credentials": []}, "unsupported schema"),
            ({"version": 1, "credentials": {}}, "credentials not an array"),
            ({"version": 1, "credentials": [], "extra": 1}, "unknown key"),
        ):
            with (
                self.subTest(case=why),
                self.assertRaises(credentials.CredentialError),
            ):
                credentials.CredentialStore.from_document(document)

    def test_a_missing_file_is_reported_rather_than_treated_as_empty(self) -> None:
        with self.assertRaises(credentials.CredentialError):
            credentials.CredentialStore.load(self.path)


# --------------------------------------------------------------------------
# Boundaries
# --------------------------------------------------------------------------


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _literal(node: ast.expr) -> Any:
    """Fold the two non-literal spellings a constant here is written in."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and node.args
    ):
        return frozenset(ast.literal_eval(node.args[0]))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        # ``1024 * 1024`` is a size written the way a reader wants to see it.
        return _literal(node.left) * _literal(node.right)
    return ast.literal_eval(node)


def _source_constant(tree: ast.Module, name: str) -> Any:
    """Read one constant out of a parsed module, without importing it.

    The Pi is a read-only dependency here and importing it would leave bytecode
    behind in somebody else's repository, so the cross-check is a parse.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if name not in names or node.value is None:
            continue
        return _literal(node.value)
    raise AssertionError(f"{name} is not a literal constant in the checkout")


class TheTransportStaysInsideThePortablePackage(unittest.TestCase):
    """Two new modules, and neither may reach for the Mac-only runtime."""

    MODULES = ("transport.py", "credentials.py")

    def test_the_new_modules_import_nothing_but_the_standard_library(self) -> None:
        for name in self.MODULES:
            with self.subTest(module=name):
                roots = _imported_roots(PACKAGE / name)
                self.assertEqual(set(), roots - set(sys.stdlib_module_names))

    def test_the_new_modules_never_name_the_worker_runtime(self) -> None:
        # Including in a deferred import inside a function body, which is how
        # this boundary would realistically be broken.
        for name in self.MODULES:
            with self.subTest(module=name):
                source = (PACKAGE / name).read_text(encoding="utf-8")
                self.assertNotIn("agent_session_loop", source)

    def test_the_package_exports_the_transport_surface(self) -> None:
        import asl_workbench

        for name in (
            "TransportError",
            "TransportCompatibility",
            "validate_envelope",
            "validate_frame",
            "encode_frame",
            "evaluate_hello",
            "authorize",
            "authorize_upload",
            "CredentialStore",
        ):
            with self.subTest(name=name):
                self.assertIn(name, asl_workbench.__all__)
                self.assertTrue(hasattr(asl_workbench, name))


class TheTransportIsSeparateFromTheAgentMuxContract(unittest.TestCase):
    """Two protocols on one wire, and two schemas that never share a version."""

    def test_the_transport_corpus_is_outside_the_digested_tree(self) -> None:
        """A fixture in the wrong directory would move the contract set id.

        The AgentMux identity is a digest over every ``.json`` under
        ``tests/fixtures/agent_mux_contract``. A transport frame placed there
        would mark every deployed connector incompatible with a build whose
        bridge had not changed at all.
        """
        digested = set(agent_mux_contract.file_digests())
        self.assertTrue(digested)
        for path in sorted(CORPUS.rglob("*.json")):
            with self.subTest(fixture=path.name):
                self.assertNotIn(path.name, {Path(n).name for n in digested})
        self.assertEqual(CONTRACT_SET_ID, agent_mux_contract.contract_set_id())

    def test_the_contract_set_id_travels_as_data_not_as_a_version(self) -> None:
        # The transport version gates the frame; the contract set id rides
        # inside it and is judged by the handshake. A frame from a build with
        # an unknown contract set is still a well-formed frame.
        envelope = fixture("observation-heartbeat")
        envelope["payload"]["contract_set_id"] = "asl-amx-1.deadbeefdeadbeef"
        checked = transport.validate_envelope(
            envelope, direction=transport.DIRECTION_UP, target_id=SERVICE_ID
        )
        self.assertEqual(
            "asl-amx-1.deadbeefdeadbeef", checked["payload"]["contract_set_id"]
        )
        self.assertIsInstance(transport.TRANSPORT_PROTOCOL_VERSION, int)
        self.assertEqual(
            (transport.TRANSPORT_PROTOCOL_VERSION,),
            transport.SUPPORTED_TRANSPORT_PROTOCOLS,
        )

    def test_a_transport_version_this_build_cannot_read_is_its_own_refusal(
        self,
    ) -> None:
        envelope = fixture("observation-heartbeat")
        envelope["protocol"] = transport.TRANSPORT_PROTOCOL_VERSION + 1
        with self.assertRaises(transport.TransportError) as caught:
            transport.validate_envelope(envelope, direction=transport.DIRECTION_UP)
        self.assertEqual(transport.ERROR_INCOMPATIBLE_PROTOCOL, caught.exception.code)
        self.assertNotIn("contract", caught.exception.detail)


class TheCompanionsVocabulariesAreReusedNotReinvented(unittest.TestCase):
    """Borrowed words, checked against the checkout they were borrowed from."""

    def setUp(self) -> None:
        self.package_root = companion_inventory.locate_companion_package(REPO_ROOT)

    def _parse(self, name: str) -> ast.Module:
        if self.package_root is None:
            self.skipTest(
                "no Pi checkout reachable; set "
                f"{companion_inventory.ENV_PI_CHECKOUT} to cross-check the "
                "borrowed Companion vocabularies"
            )
        return ast.parse((self.package_root / name).read_text(encoding="utf-8"))

    def test_the_run_phases_match_the_companions_list(self) -> None:
        tree = self._parse("protocol.py")
        self.assertEqual(tuple(_source_constant(tree, "ASL_PHASES")), transport.PHASES)

    def test_the_credential_roles_and_file_shape_match_the_companions(self) -> None:
        tree = self._parse("auth.py")
        for name, ours in (
            ("ROLE_CONNECTOR", credentials.ROLE_CONNECTOR),
            ("ROLE_SUMMARY", credentials.ROLE_SUMMARY),
            ("ROLE_OWNER", credentials.ROLE_OWNER),
            ("ROLE_PUBLIC", credentials.ROLE_PUBLIC),
            ("IDENTITY_HEADER", credentials.IDENTITY_HEADER),
            ("TOKEN_SEPARATOR", credentials.TOKEN_SEPARATOR),
            (
                "CREDENTIALS_SCHEMA_VERSION",
                credentials.CREDENTIALS_SCHEMA_VERSION,
            ),
        ):
            with self.subTest(constant=name):
                self.assertEqual(_source_constant(tree, name), ours)

    def test_the_stored_credential_keys_match_the_companions(self) -> None:
        # An installed credentials file is read by both; a key one side does
        # not know is a file the other side refuses.
        tree = self._parse("auth.py")
        self.assertEqual(
            _source_constant(tree, "_CREDENTIAL_KEYS"),
            frozenset(
                {
                    "credential_id",
                    "role",
                    "salt",
                    "digest",
                    "created_at",
                    "subject",
                    "label",
                    "disabled",
                }
            ),
        )
        self.assertEqual(
            _source_constant(tree, "_FILE_KEYS"), frozenset({"version", "credentials"})
        )

    def test_the_workbench_frame_ceiling_is_its_own_number(self) -> None:
        """Bounded frames are the Companion's idea; this ceiling is not its value.

        The Companion's limit belongs to the Companion's protocol. Sharing the
        number would make one of the two impossible to change without touching
        the other, which is the coupling the separate versioning exists to
        avoid.
        """
        tree = self._parse("protocol.py")
        theirs = _source_constant(tree, "MAX_FRAME_BYTES")
        self.assertIsInstance(theirs, int)
        self.assertNotEqual(theirs, transport.MAX_FRAME_BYTES)


if __name__ == "__main__":
    unittest.main()
