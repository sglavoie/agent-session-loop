# ruff: noqa: F403, F405
from .asl_guidance_delivery_support import *


class DeliveryVocabularyTests(unittest.TestCase):
    """The spine restates the ledger's vocabulary; it may not drift from it.

    ``guidance_delivery`` cannot import ``mailbox`` -- the mailbox reaches
    ``harnesses`` through ``state``, and ``harnesses`` imports the spine -- so
    these assertions are what keeps the two copies identical.
    """

    def test_states_and_channel_match_the_ledger(self) -> None:
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            guidance_delivery.STATE_DELIVERED_LIVE,
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            guidance_delivery.STATE_DEFERRED,
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE,
            guidance_delivery.CHANNEL_LIVE,
        )

    def test_reasons_match_the_ledger(self) -> None:
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
            guidance_delivery.REASON_DELIVERED,
        )
        self.assertEqual(
            set(mailbox_vocabulary.GUIDANCE_DEFERRAL_REASONS),
            set(guidance_delivery.DEFERRAL_REASONS),
        )

    def test_every_outcome_is_a_record_the_mailbox_accepts(self) -> None:
        """The states and reasons this module produces are appendable as-is."""
        outcomes = [
            guidance_delivery.DELIVERED,
            *(
                guidance_delivery.deferred(reason)
                for reason in guidance_delivery.DEFERRAL_REASONS
            ),
        ]
        for outcome in outcomes:
            with self.subTest(reason=outcome.reason):
                self.assertIn(
                    outcome.delivery_state,
                    mailbox_vocabulary.GUIDANCE_DELIVERY_STATES,
                )
                self.assertIn(
                    outcome.channel, mailbox_vocabulary.GUIDANCE_DELIVERY_CHANNELS
                )
                self.assertIn(
                    outcome.reason, mailbox_vocabulary.GUIDANCE_DELIVERY_REASONS
                )


class GuidanceOutcomeTests(unittest.TestCase):
    def test_a_delivered_outcome_is_a_live_delivery(self) -> None:
        outcome = guidance_delivery.DELIVERED

        self.assertTrue(outcome.delivered)
        self.assertEqual(guidance_delivery.STATE_DELIVERED_LIVE, outcome.delivery_state)
        self.assertEqual(guidance_delivery.CHANNEL_LIVE, outcome.channel)
        self.assertIsNone(outcome.detail)

    def test_a_deferral_names_a_reason_and_stays_deferred(self) -> None:
        outcome = guidance_delivery.deferred(
            guidance_delivery.REASON_STALE_TURN,
            detail="turn 2 already finished",
        )

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.STATE_DEFERRED, outcome.delivery_state)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)

    def test_success_cannot_borrow_a_deferral_reason(self) -> None:
        with self.assertRaises(ValueError):
            guidance_delivery.GuidanceOutcome(
                delivered=True,
                reason=guidance_delivery.REASON_HARNESS_UNSUPPORTED,
            )

    def test_a_deferral_cannot_invent_a_reason(self) -> None:
        with self.assertRaises(ValueError):
            guidance_delivery.deferred("because")


class LiveDeliveryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.turn = turn_identity()
        self.endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)
        self.request = request_for(self.turn)

    def test_an_unopened_endpoint_has_no_turn_to_deliver_into(self) -> None:
        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_NO_ACTIVE_TURN, outcome.reason)

    def test_an_open_endpoint_without_a_transport_is_unsupported(self) -> None:
        self.endpoint.open(None)

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_HARNESS_UNSUPPORTED, outcome.reason)

    def test_a_transport_receives_the_body_and_the_turn(self) -> None:
        seen: list[guidance_delivery.GuidanceRequest] = []
        self.endpoint.open(recording_transport(seen))

        outcome = self.endpoint.submit(self.request)

        self.assertTrue(outcome.delivered)
        self.assertEqual([self.request], seen)
        self.assertEqual("prefer the smaller change", seen[0].text)
        self.assertEqual(self.turn, seen[0].turn)

    def test_a_closed_endpoint_is_a_stale_turn(self) -> None:
        calls: list[guidance_delivery.GuidanceRequest] = []
        self.endpoint.open(recording_transport(calls))
        self.endpoint.close()

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual([], calls)

    def test_reopening_a_closed_endpoint_does_nothing(self) -> None:
        """A late open must not resurrect a channel into a finished turn."""
        self.endpoint.close()
        self.endpoint.open(lambda request: guidance_delivery.DELIVERED)

        self.assertTrue(self.endpoint.closed)
        self.assertEqual(
            guidance_delivery.REASON_STALE_TURN,
            self.endpoint.submit(self.request).reason,
        )

    def test_a_request_for_another_turn_never_reaches_the_transport(self) -> None:
        calls: list[guidance_delivery.GuidanceRequest] = []
        self.endpoint.open(recording_transport(calls))

        for label, other in (
            ("turn_index", turn_identity(turn_index=3)),
            ("session_id", turn_identity(session_id="8")),
            ("run_id", turn_identity(run_id="run-2")),
            ("harness", turn_identity(harness="claude")),
        ):
            with self.subTest(field=label):
                outcome = self.endpoint.submit(request_for(other))

                self.assertFalse(outcome.delivered)
                self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual([], calls)

    def test_a_failing_transport_defers_instead_of_raising(self) -> None:
        def explode(request: guidance_delivery.GuidanceRequest) -> Any:
            raise RuntimeError("pipe is gone")

        self.endpoint.open(explode)

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(
            guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
            outcome.reason,
        )
        self.assertEqual("RuntimeError: pipe is gone", outcome.detail)

    def test_a_transport_that_returns_junk_defers(self) -> None:
        self.endpoint.open(lambda request: "sent")  # type: ignore[arg-type,return-value]

        outcome = self.endpoint.submit(self.request)

        self.assertFalse(outcome.delivered)
        self.assertEqual(
            guidance_delivery.REASON_TRANSPORT_UNAVAILABLE,
            outcome.reason,
        )

    def test_close_waits_for_a_delivery_already_in_flight(self) -> None:
        """The endpoint's quiescence guarantee, mirroring the reader join.

        ``close`` returning has to mean no transport is running, otherwise a
        turn could be torn down underneath one.
        """
        entered = threading.Event()
        release = threading.Event()
        left = threading.Event()

        def slow(request: guidance_delivery.GuidanceRequest) -> Any:
            entered.set()
            release.wait(WAIT_SECONDS)
            left.set()
            return guidance_delivery.DELIVERED

        self.endpoint.open(slow)
        submitted: list[guidance_delivery.GuidanceOutcome] = []
        submitter = threading.Thread(
            target=lambda: submitted.append(self.endpoint.submit(self.request))
        )
        submitter.start()
        self.assertTrue(entered.wait(WAIT_SECONDS))

        closer = threading.Thread(target=self.endpoint.close)
        closer.start()
        # The transport is still running, so the close cannot have completed.
        closer.join(0.2)
        self.assertTrue(closer.is_alive())
        self.assertFalse(left.is_set())

        release.set()
        closer.join(WAIT_SECONDS)
        submitter.join(WAIT_SECONDS)

        self.assertFalse(closer.is_alive())
        self.assertTrue(left.is_set())
        self.assertTrue(submitted[0].delivered)
        # Nothing starts after the close: the next attempt never reaches the
        # transport at all.
        self.assertEqual(
            guidance_delivery.REASON_STALE_TURN,
            self.endpoint.submit(self.request).reason,
        )

    def test_activated_closes_even_when_the_body_raises(self) -> None:
        with (
            self.assertRaises(RuntimeError),
            self.endpoint.activated(lambda request: guidance_delivery.DELIVERED),
        ):
            self.assertTrue(self.endpoint.opened)
            raise RuntimeError("turn blew up")

        self.assertTrue(self.endpoint.closed)


__all__ = tuple(name for name in globals() if not name.startswith("__"))
