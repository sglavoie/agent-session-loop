from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_transport_contract_support import *


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
