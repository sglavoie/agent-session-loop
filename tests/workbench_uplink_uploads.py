"""UploadTests: uploads."""

from __future__ import annotations

from .workbench_uplink_support import (
    CONNECTOR,
    NOW,
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_REJECTED,
    OPERATION_UNCONFIRMED,
    PLAN_KEY,
    REQUEST_DETAIL,
    TARGET,
    Any,
    UploadTestsBase,
    heartbeat,
    heartbeat_payload,
    mock,
    observation_envelope,
    operation_envelope,
    operation_record,
    receipt_envelope,
)


class UploadTests(UploadTestsBase):
    def test_a_heartbeat_is_retained_and_brings_the_link_up(self) -> None:
        self.assertFalse(self.uplink.linked)
        carried = self.uplink.ingest(heartbeat(), now=NOW, subject=CONNECTOR)
        self.assertIsNone(carried, "nothing was being held, so nothing rides back")
        self.assertTrue(self.uplink.linked)
        self.assertEqual(self.uplink.last_upload_at, NOW)
        statuses = self.store.connector_statuses(now=NOW)
        self.assertEqual([row.connector_id for row in statuses], [CONNECTOR])

    def test_an_upload_for_another_connector_is_refused(self) -> None:
        """Two ways to be the wrong connector, and both are the same refusal.

        The credential's subject and the envelope's sender are checked
        separately because they fail separately: a credential issued to one Mac
        may not upload as another, and a frame from a Mac this service was
        never paired with is not attributable at all. Everything the store
        holds is keyed by connector id, so a frame accepted under the wrong one
        is a row nobody can account for.
        """
        by_subject = self.refusal(heartbeat(), subject="someone-else")
        self.assertEqual(by_subject.status, 403)
        self.assertEqual(by_subject.reason, "wrong-connector")

        unpaired = self.refusal(heartbeat(connector="other-mac"))
        self.assertEqual(unpaired.status, 403)
        self.assertEqual(unpaired.reason, "wrong-connector")
        self.assertEqual(unpaired.details["connector_id"], CONNECTOR)

    def test_a_frame_addressed_elsewhere_is_refused(self) -> None:
        elsewhere = observation_envelope(
            payload=heartbeat_payload(observed_at=NOW, bridge_alive=True),
            connector_id=CONNECTOR,
            target_id="someone-elses-pi.example.ts.net",
            sent_at=NOW,
        )
        refused = self.refusal(elsewhere)
        self.assertEqual(refused.status, 400)

    def test_a_payload_nested_too_deeply_is_a_malformed_refusal(self) -> None:
        # Shallow enough for the request body to parse, too deep for the body
        # walk: a 400 the connector can read, not a 500 from the safety net.
        frame = heartbeat()
        nested: list[Any] = []
        for _ in range(3000):
            nested = [nested]
        frame["payload"]["x"] = nested
        refused = self.refusal(frame, subject=CONNECTOR)
        self.assertEqual(refused.status, 400)
        self.assertEqual(refused.reason, "malformed")

    def test_a_downward_kind_may_not_be_uploaded(self) -> None:
        """A request travels down, so a request arriving up is refused.

        Not a pedantic check: the response body of an upload is where the
        service puts requests, and an end that could also *send* them is an end
        that could ask this service to perform a read on its behalf.
        """
        refused = self.refusal(
            {
                "kind": "request",
                "capability": "detail",
                "connector_id": CONNECTOR,
                "target_id": TARGET,
                "sent_at": NOW,
                "payload": {"request": REQUEST_DETAIL, "detail": "state"},
            }
        )
        self.assertEqual(refused.status, 400)

    def test_a_report_for_an_unknown_operation_is_dropped_not_refused(self) -> None:
        """The journal records what this service handed down, and only that.

        Dropped rather than refused because the connector has nothing to fix
        and a retry would not help it: inserting a row from an incoming frame
        would be a command nobody here can account for.
        """
        report = operation_envelope(
            payload={
                "operation_id": "op-never-submitted",
                "status": OPERATION_ACCEPTED,
                "observed_at": NOW,
            },
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW,
        )
        self.assertIsNone(self.uplink.ingest(report, now=NOW, subject=CONNECTOR))
        self.assertIsNone(self.store.operation("op-never-submitted"))

    def test_a_report_or_receipt_cannot_settle_another_connectors_operation(
        self,
    ) -> None:
        for operation_id in ("foreign-report", "foreign-receipt"):
            self.store.record_operation(
                operation_record(operation_id, status=OPERATION_DISPATCHED),
                connector_id="other-mac",
                received_at=NOW,
            )
        report = operation_envelope(
            payload={
                "operation_id": "foreign-report",
                "status": OPERATION_ACCEPTED,
                "observed_at": NOW + 1,
            },
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW + 1,
        )
        receipt = receipt_envelope(
            payload={
                "outcome": "refused",
                "observed_at": NOW + 1,
                "detail_code": "forbidden-method",
            },
            capability="mutate",
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW + 1,
            request_id="submit-foreign-receipt",
        )
        self.uplink.ingest(report, now=NOW + 1, subject=CONNECTOR)
        self.uplink.ingest(receipt, now=NOW + 1, subject=CONNECTOR)
        for operation_id in ("foreign-report", "foreign-receipt"):
            with self.subTest(operation_id=operation_id):
                row = self.store.operation(operation_id)
                assert row is not None
                self.assertEqual(row["status"], OPERATION_DISPATCHED)

    def test_a_mutation_receipt_must_match_its_request_id(self) -> None:
        for operation_id, request_id in (
            ("mismatched-receipt", "submit-another-operation"),
            ("uncorrelated-receipt", "unrelated-request"),
        ):
            with self.subTest(request_id=request_id):
                self.store.record_operation(
                    operation_record(operation_id, status=OPERATION_DISPATCHED),
                    connector_id=CONNECTOR,
                    received_at=NOW,
                )
                receipt = receipt_envelope(
                    payload={
                        "outcome": "refused",
                        "observed_at": NOW + 1,
                        "detail_code": "forbidden-method",
                        "operation_id": operation_id,
                    },
                    capability="mutate",
                    connector_id=CONNECTOR,
                    target_id=TARGET,
                    sent_at=NOW + 1,
                    request_id=request_id,
                )
                self.uplink.ingest(receipt, now=NOW + 1, subject=CONNECTOR)
                self.assertEqual(
                    self.store.operation(operation_id)["status"],
                    OPERATION_DISPATCHED,
                )

    def test_a_report_settles_the_operation_this_service_submitted(self) -> None:
        self.store.record_operation(
            operation_record("op-1"), connector_id=CONNECTOR, received_at=NOW
        )
        self.store.advance_operation(
            "op-1", OPERATION_DISPATCHED, now=NOW, dispatched_at=NOW
        )
        report = operation_envelope(
            payload={
                "operation_id": "op-1",
                "status": OPERATION_ACCEPTED,
                "observed_at": NOW + 5,
                "settled_at": NOW + 5,
            },
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW + 5,
        )
        self.uplink.ingest(report, now=NOW + 5, subject=CONNECTOR)
        row = self.store.operation("op-1")
        assert row is not None
        self.assertEqual(row["status"], OPERATION_ACCEPTED)
        self.assertEqual(row["settled_at"], NOW + 5)

    def test_a_report_racing_a_sweep_rechecks_the_current_status(self) -> None:
        original = self.store.operation
        for reported, expected in (
            (OPERATION_ACCEPTED, OPERATION_UNCONFIRMED),
            (OPERATION_CONSUMED, OPERATION_CONSUMED),
        ):
            with self.subTest(reported=reported):
                operation_id = f"race-{reported}"
                self.store.record_operation(
                    operation_record(operation_id, status=OPERATION_DISPATCHED),
                    connector_id=CONNECTOR,
                    received_at=NOW,
                )

                def read_then_sweep(identifier: str) -> Any:
                    row = original(identifier)
                    self.store.advance_operation(
                        identifier, OPERATION_UNCONFIRMED, now=NOW + 1
                    )
                    return row

                report = operation_envelope(
                    payload={
                        "operation_id": operation_id,
                        "status": reported,
                        "observed_at": NOW + 2,
                    },
                    connector_id=CONNECTOR,
                    target_id=TARGET,
                    sent_at=NOW + 2,
                )
                with mock.patch.object(
                    self.store, "operation", side_effect=read_then_sweep
                ):
                    self.assertIsNone(self.uplink.ingest(report, now=NOW + 2))
                self.assertEqual(self.store.operation(operation_id)["status"], expected)

    def test_a_refusal_receipt_cannot_reject_a_concurrently_accepted_operation(
        self,
    ) -> None:
        self.store.record_operation(
            operation_record("race-refusal", status=OPERATION_DISPATCHED),
            connector_id=CONNECTOR,
            received_at=NOW,
        )
        original = self.store.operation

        def read_then_accept(identifier: str) -> Any:
            row = original(identifier)
            self.store.advance_operation(identifier, OPERATION_ACCEPTED, now=NOW + 1)
            return row

        receipt = receipt_envelope(
            payload={
                "outcome": "refused",
                "observed_at": NOW + 2,
                "detail_code": "forbidden-method",
            },
            capability="mutate",
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW + 2,
            request_id="submit-race-refusal",
        )
        with mock.patch.object(self.store, "operation", side_effect=read_then_accept):
            self.uplink.ingest(receipt, now=NOW + 2)
        self.assertEqual(
            self.store.operation("race-refusal")["status"], OPERATION_ACCEPTED
        )

    def test_a_receipt_refusing_a_submission_settles_its_row(self) -> None:
        """The connector refuses a submission it will not queue with a receipt.

        That receipt is the only report the row will ever get; left
        `dispatched`, the plan would refuse every later mutation.
        """
        for operation_id, outcome, detail_code, status in (
            ("op-refused", "refused", "forbidden-method", OPERATION_REJECTED),
            ("op-late", "expired", "deadline-passed", OPERATION_EXPIRED),
        ):
            with self.subTest(outcome=outcome):
                self.store.record_operation(
                    operation_record(operation_id),
                    connector_id=CONNECTOR,
                    received_at=NOW,
                )
                self.store.advance_operation(
                    operation_id, OPERATION_DISPATCHED, now=NOW, dispatched_at=NOW
                )
                receipt = receipt_envelope(
                    payload={
                        "outcome": outcome,
                        "observed_at": NOW + 1,
                        "detail_code": detail_code,
                    },
                    capability="mutate",
                    connector_id=CONNECTOR,
                    target_id=TARGET,
                    sent_at=NOW + 1,
                    request_id=f"submit-{operation_id}",
                )
                self.uplink.ingest(receipt, now=NOW + 1, subject=CONNECTOR)
                row = self.store.operation(operation_id)
                assert row is not None
                self.assertEqual(row["status"], status)
                self.assertEqual(row["settled_at"], NOW + 1)
                self.assertEqual(
                    row.get("detail_code"),
                    "deadline-passed" if outcome == "expired" else None,
                )
                self.assertIsNone(self.store.outstanding_operation(PLAN_KEY))

    def test_a_late_refusal_resolves_a_swept_unconfirmed_submission(self) -> None:
        for operation_id, outcome, settled_status in (
            ("late-refused", "refused", OPERATION_REJECTED),
            ("late-expired", "expired", OPERATION_EXPIRED),
        ):
            with self.subTest(outcome=outcome):
                self.store.record_operation(
                    operation_record(operation_id, status=OPERATION_DISPATCHED),
                    connector_id=CONNECTOR,
                    received_at=NOW,
                )
                self.store.advance_operation(
                    operation_id, OPERATION_UNCONFIRMED, now=NOW + 1
                )
                receipt = receipt_envelope(
                    payload={
                        "outcome": outcome,
                        "observed_at": NOW + 2,
                        "detail_code": "deadline-passed",
                    },
                    capability="mutate",
                    connector_id=CONNECTOR,
                    target_id=TARGET,
                    sent_at=NOW + 2,
                    request_id=f"submit-{operation_id}",
                )

                self.uplink.ingest(receipt, now=NOW + 2, subject=CONNECTOR)

                row = self.store.operation(operation_id)
                assert row is not None
                self.assertEqual(row["status"], settled_status)
                self.assertEqual(row["settled_at"], NOW + 2)

    def test_a_matching_operation_id_in_a_refusal_receipt_is_accepted(self) -> None:
        operation_id = "op-explicit-receipt"
        self.store.record_operation(
            operation_record(operation_id, status=OPERATION_DISPATCHED),
            connector_id=CONNECTOR,
            received_at=NOW,
        )
        receipt = receipt_envelope(
            payload={
                "outcome": "refused",
                "observed_at": NOW + 1,
                "detail_code": "forbidden-method",
                "operation_id": operation_id,
            },
            capability="mutate",
            connector_id=CONNECTOR,
            target_id=TARGET,
            sent_at=NOW + 1,
            request_id=f"submit-{operation_id}",
        )
        self.uplink.ingest(receipt, now=NOW + 1, subject=CONNECTOR)
        self.assertEqual(
            self.store.operation(operation_id)["status"], OPERATION_REJECTED
        )
