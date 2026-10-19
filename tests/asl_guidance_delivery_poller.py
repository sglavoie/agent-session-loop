# ruff: noqa: F403, F405
from .asl_guidance_delivery_support import *


class MailboxPollDeliveryTests(unittest.TestCase):
    """What the live poller records, and what it refuses to record."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")
        self.event_path = self.tmp / "events.jsonl"
        self.payload: dict[str, Any] = {
            "run_id": "run-1",
            "control": run_coordination_control.initial_control_payload(),
        }
        self.persistence = run_persistence._RunPersistence()
        self.poll = run_coordination_mailbox_poll.MailboxPoll(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=run_control.RunControlState(current_harness="codex"),
            seen_control_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=dict(self.payload["control"]),
            persistence=self.persistence,
            run_control_lock=threading.Lock(),
        )
        self.turn = turn_identity()
        self.poll.set_context(
            {"session": 7, "session_id": "7", "turn_index": 2, "total_sessions": 13}
        )
        self.received: list[str] = []

    def endpoint(
        self,
        *,
        turn: guidance_delivery.TurnIdentity | None = None,
        live: bool = True,
    ) -> guidance_delivery.LiveDeliveryEndpoint:
        endpoint = guidance_delivery.LiveDeliveryEndpoint(turn or self.turn)
        endpoint.open(self.transport if live else None)
        return endpoint

    def transport(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        self.received.append(request.text)
        return guidance_delivery.DELIVERED

    def add_guidance(
        self, text: str = "use the smaller diff"
    ) -> mailbox_models.MailboxMessage:
        return mailbox_appenders.append_guidance(
            self.plan_dir, text, state_dir=self.state_dir
        )

    def view(self) -> mailbox_snapshot.MailboxView:
        return mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)

    def deliveries(self) -> list[mailbox_models.MailboxMessage]:
        return [
            message
            for message in self.view().messages
            if message.kind == mailbox_vocabulary.KIND_GUIDANCE_DELIVERY
        ]

    def mailbox_events(self, action: str) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        return [
            record["payload"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.MAILBOX_EVENT
            and record["payload"].get("action") == action
        ]

    def test_live_delivery_records_one_attempt_and_events_it(self) -> None:
        guidance = self.add_guidance("rebase before you push")
        self.poll.set_live_delivery(self.endpoint())

        self.poll()

        self.assertEqual(["rebase before you push"], self.received)
        [record] = self.deliveries()
        self.assertEqual(guidance.id, record.to)
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE, record.delivery_state
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_DELIVERED, record.delivery_reason
        )
        self.assertEqual(mailbox_vocabulary.GUIDANCE_CHANNEL_LIVE, record.channel)
        self.assertEqual("run-1", record.run_id)
        self.assertEqual("7", record.session_id)
        self.assertEqual(2, record.turn_index)
        self.assertEqual("codex", record.harness)
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            self.view().guidance_delivery_state(guidance.id),
        )
        [event_payload] = self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERED)
        self.assertEqual(guidance.id, event_payload["guidance_id"])
        self.assertEqual(record.id, event_payload["delivery_id"])
        self.assertEqual(2, event_payload["turn_index"])

    def test_delivery_events_and_records_carry_no_body(self) -> None:
        secret = "rotate the token s3cr3t-hunter2 tonight"
        self.add_guidance(secret)
        self.poll.set_live_delivery(self.endpoint())

        self.poll()

        [event_payload] = self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERED)
        self.assertNotIn("text", event_payload)
        self.assertNotIn("digest", event_payload)
        self.assertNotIn("length", event_payload)
        [record] = self.deliveries()
        self.assertIsNone(record.text)
        mailbox_text = mailbox_cursors.mailbox_path_for_plan(
            self.plan_dir, str(self.state_dir)
        ).read_text(encoding="utf-8")
        # The guidance record itself holds the body once; the delivery record
        # that reports on it holds no copy.
        self.assertEqual(1, mailbox_text.count(secret))

    def test_repeated_polling_submits_once_per_turn(self) -> None:
        self.add_guidance()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)
        submissions: list[guidance_delivery.GuidanceRequest] = []

        def counting(
            request: guidance_delivery.GuidanceRequest,
        ) -> guidance_delivery.GuidanceOutcome:
            submissions.append(request)
            return guidance_delivery.deferred(
                guidance_delivery.REASON_TRANSPORT_UNAVAILABLE
            )

        endpoint.open(counting)
        self.poll.set_live_delivery(endpoint)

        self.poll()
        self.poll()
        self.poll()

        self.assertEqual(1, len(submissions))
        self.assertEqual(1, len(self.deliveries()))

    def unconfirmed_until(
        self, confirmations: int
    ) -> tuple[guidance_delivery.LiveDeliveryEndpoint, list[str]]:
        """An endpoint whose transport confirms only on the Nth attempt.

        This is what a real stream transport looks like from the poller's side:
        it wrote the guidance out immediately and learns the model took it up
        some ticks later.
        """
        attempts: list[str] = []

        def transport(
            request: guidance_delivery.GuidanceRequest,
        ) -> guidance_delivery.GuidanceOutcome:
            attempts.append(request.text)
            if len(attempts) >= confirmations:
                return guidance_delivery.DELIVERED
            return guidance_delivery.awaiting_confirmation()

        endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)
        endpoint.open(transport)
        return endpoint, attempts

    def test_an_unconfirmed_attempt_is_asked_again_until_it_confirms(self) -> None:
        guidance = self.add_guidance("rebase before you push")
        endpoint, attempts = self.unconfirmed_until(3)
        self.poll.set_live_delivery(endpoint)

        self.poll()
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
            self.view().guidance_delivery_state(guidance.id),
        )
        self.poll()
        self.poll()

        self.assertEqual(3, len(attempts))
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            self.view().guidance_delivery_state(guidance.id),
        )

    def test_retrying_survives_an_unchanged_mailbox(self) -> None:
        """The evidence arrives without anything writing the mailbox.

        The first tick's own ledger append moves the change token; nothing
        after it does. If the unchanged-token short circuit applied to an open
        attempt, the retries would stop right there and the confirmation would
        never be read.
        """
        self.add_guidance()
        endpoint, attempts = self.unconfirmed_until(5)
        self.poll.set_live_delivery(endpoint)

        for _ in range(5):
            self.poll()

        self.assertEqual(5, len(attempts))

    def test_a_confirmed_delivery_ends_the_retries(self) -> None:
        self.add_guidance()
        endpoint, attempts = self.unconfirmed_until(2)
        self.poll.set_live_delivery(endpoint)

        for _ in range(6):
            self.poll()

        self.assertEqual(2, len(attempts))

    def test_retrying_records_one_attempt_per_distinct_verdict(self) -> None:
        """A retried attempt says the same thing; the ledger says it once."""
        guidance = self.add_guidance()
        endpoint, _ = self.unconfirmed_until(4)
        self.poll.set_live_delivery(endpoint)

        for _ in range(4):
            self.poll()

        records = self.deliveries()
        self.assertEqual(
            [
                (
                    mailbox_vocabulary.GUIDANCE_STATE_DEFERRED,
                    mailbox_vocabulary.GUIDANCE_REASON_AWAITING_CONFIRMATION,
                ),
                (
                    mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
                    mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
                ),
            ],
            [(record.delivery_state, record.delivery_reason) for record in records],
        )
        self.assertEqual([guidance.id] * 2, [record.to for record in records])

    def test_an_attempt_that_never_confirms_stays_deliverable(self) -> None:
        guidance = self.add_guidance("read the handoff first")
        endpoint, attempts = self.unconfirmed_until(1000)
        self.poll.set_live_delivery(endpoint)

        self.poll()
        self.poll()
        # The turn ends without the transport ever proving anything.
        endpoint.close()
        self.poll()

        self.assertEqual(
            [
                mailbox_vocabulary.GUIDANCE_REASON_AWAITING_CONFIRMATION,
                mailbox_vocabulary.GUIDANCE_REASON_STALE_TURN,
            ],
            [record.delivery_reason for record in self.deliveries()],
        )
        self.assertEqual(
            [guidance.id],
            [message.id for message in self.view().deliverable_guidance],
        )
        self.assertGreaterEqual(len(attempts), 2)

    def test_a_deferral_leaves_the_guidance_deliverable_for_the_next_turn(
        self,
    ) -> None:
        guidance = self.add_guidance("read the handoff first")
        self.poll.set_live_delivery(self.endpoint(live=False))

        self.poll()

        [deferral] = self.deliveries()
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED, deferral.delivery_state
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_HARNESS_UNSUPPORTED,
            deferral.delivery_reason,
        )
        self.assertEqual(
            [guidance.id],
            [message.id for message in self.view().deliverable_guidance],
        )
        self.assertEqual([], self.received)
        self.assertEqual(
            1,
            len(self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED)),
        )

        # The next turn is a new endpoint and a new attempt.
        self.poll.set_live_delivery(self.endpoint(turn=turn_identity(turn_index=3)))
        self.poll()

        self.assertEqual(["read the handoff first"], self.received)
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_LIVE,
            self.view().guidance_delivery_state(guidance.id),
        )

    def test_a_new_turn_rereads_an_unchanged_mailbox(self) -> None:
        """The unchanged-mailbox short circuit must not swallow a retry.

        Nothing writes the mailbox between the deferral and the next turn, so
        the poll's change token is identical -- yet the new turn still has to
        look, or the guidance would never be delivered at all.
        """
        self.add_guidance("try again next turn")
        self.poll.set_live_delivery(self.endpoint(live=False))
        self.poll()
        # Settle the token so the next tick would otherwise short circuit.
        self.poll()

        self.poll.set_live_delivery(self.endpoint(turn=turn_identity(turn_index=3)))
        self.poll()

        self.assertEqual(["try again next turn"], self.received)

    def test_a_delivered_record_stops_later_turns_from_redelivering(self) -> None:
        self.add_guidance()
        self.poll.set_live_delivery(self.endpoint())
        self.poll()

        self.poll.set_live_delivery(self.endpoint(turn=turn_identity(turn_index=3)))
        self.poll()

        self.assertEqual(1, len(self.received))
        self.assertEqual(1, len(self.deliveries()))

    def test_a_tick_after_the_turn_ended_defers_as_stale(self) -> None:
        self.add_guidance()
        endpoint = self.endpoint()
        endpoint.close()
        self.poll.set_live_delivery(endpoint)

        self.poll()

        [record] = self.deliveries()
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED, record.delivery_state
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_STALE_TURN, record.delivery_reason
        )
        self.assertEqual([], self.received)

    def test_no_endpoint_means_no_delivery_history_at_all(self) -> None:
        """Between turns there is nothing to deliver into, so nothing is said."""
        self.add_guidance()

        self.poll()
        self.poll()

        self.assertEqual([], self.deliveries())
        self.assertEqual(
            [],
            self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED),
        )

    def test_detaching_the_endpoint_stops_delivery(self) -> None:
        self.add_guidance()
        self.poll.set_live_delivery(self.endpoint())
        self.poll.set_live_delivery(None)

        self.poll()

        self.assertEqual([], self.deliveries())
        self.assertEqual([], self.received)

    def test_a_failed_receipt_keeps_the_guidance_pending(self) -> None:
        """Transport succeeded, the ledger could not say so: prefer a duplicate."""
        guidance = self.add_guidance("prefer at-least-once")
        self.poll.set_live_delivery(self.endpoint())

        for error in (
            OSError("read-only mailbox"),
            mailbox_models.MailboxError(
                mailbox_constants.ERROR_MAILBOX_FULL, "no room"
            ),
        ):
            with self.subTest(error=type(error).__name__):
                self.received.clear()
                self.poll.set_live_delivery(self.endpoint())
                with mock.patch.object(
                    mailbox_appenders, "append_guidance_delivery", side_effect=error
                ):
                    self.poll()

                self.assertEqual(["prefer at-least-once"], self.received)
                self.assertEqual([], self.deliveries())
                self.assertEqual(
                    mailbox_vocabulary.GUIDANCE_STATE_QUEUED,
                    self.view().guidance_delivery_state(guidance.id),
                )
                self.assertEqual(
                    [guidance.id],
                    [message.id for message in self.view().deliverable_guidance],
                )
                self.assertEqual(
                    [], self.mailbox_events(events.MAILBOX_ACTION_GUIDANCE_DELIVERED)
                )

    def test_a_mailbox_read_failure_skips_delivery_without_raising(self) -> None:
        self.add_guidance()
        self.poll.set_live_delivery(self.endpoint())

        with mock.patch.object(
            mailbox,
            "read_mailbox",
            side_effect=mailbox_models.MailboxError(
                mailbox_constants.ERROR_MAILBOX_READ, "unreadable"
            ),
        ):
            self.poll()

        self.assertEqual([], self.received)
        self.assertEqual([], self.deliveries())

    def test_a_failing_transport_is_recorded_not_raised(self) -> None:
        self.add_guidance()
        endpoint = guidance_delivery.LiveDeliveryEndpoint(self.turn)

        def explode(request: guidance_delivery.GuidanceRequest) -> Any:
            raise OSError("broken pipe")

        endpoint.open(explode)
        self.poll.set_live_delivery(endpoint)

        self.poll()

        [record] = self.deliveries()
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_STATE_DEFERRED, record.delivery_state
        )
        self.assertEqual(
            mailbox_vocabulary.GUIDANCE_REASON_TRANSPORT_UNAVAILABLE,
            record.delivery_reason,
        )

    def test_guidance_read_by_the_worker_is_never_delivered_again(self) -> None:
        guidance = self.add_guidance()
        mailbox_appenders.append_acknowledgement(
            self.plan_dir, guidance.seq, state_dir=self.state_dir
        )
        self.poll.set_live_delivery(self.endpoint())

        self.poll()

        self.assertEqual([], self.received)
        self.assertEqual([], self.deliveries())


if __name__ == "__main__":
    unittest.main()


__all__ = tuple(name for name in globals() if not name.startswith("__"))
