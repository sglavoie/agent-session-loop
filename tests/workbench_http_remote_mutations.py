"""RemoteProviderTests: mutations."""

from __future__ import annotations

from .workbench_http_remote_support import (
    CAPABILITY_OPERATION,
    COMMAND_REDEEM_PATH,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    NOW,
    OPERATION_PENDING,
    OPERATION_SUPERSEDED,
    OPERATION_UNCONFIRMED,
    OPERATIONS,
    REASON_OPERATION_CONFLICT,
    REASON_OPERATION_UNCERTAIN,
    REMOTE_POLICY,
    ROLE_CONNECTOR,
    Any,
    Application,
    FakeBodyLane,
    FixedAuthenticator,
    RemoteProviderTestsBase,
    Response,
    build,
    document,
    envelope,
    json,
    mock,
    run_row,
    snapshot_payload,
    threading,
    time,
    workbench_service,
)


class RemoteProviderTests(RemoteProviderTestsBase):
    def test_a_mutation_with_no_run_to_name_is_refused_before_the_journal(self) -> None:
        """The journal records mutations against a run, so one is required.

        Letting it through would admit the operation, hand it down the link,
        and only then fail at the insert -- a command on its way to the Mac
        with no durable record that it left.
        """
        response = self.remote_app().handle(
            build(
                "POST",
                "/api/control/stop-after-current",
                headers=self.signed(),
                body=b'{"operation_id": "op-0", "plan_key": "plan-abc"}',
            )
        )
        self.assertEqual(response.status, 400)
        self.assertIn("run_key", document(response)["error"])
        self.assertEqual(self.mutations.handed, [])

    def test_a_non_finite_number_is_not_json(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                response = self.remote_app().handle(
                    build(
                        "POST",
                        "/api/control/stop-after-current",
                        headers=self.signed(),
                        body=(
                            b'{"operation_id": "op-0", "plan_key": "plan-abc",'
                            b' "run_key": "run-1", "wait_seconds": ' + constant + b"}"
                        ),
                    )
                )
                self.assertEqual(response.status, 400)
                payload = document(response)
                self.assertEqual(payload["reason"], "invalid-request")
                self.assertIn("valid JSON", payload["error"])
        self.assertEqual(self.mutations.handed, [])

    def test_a_mutation_is_journalled_admitted_and_handed_down(self) -> None:
        response = self.submit("op-1")
        self.assertEqual(response.status, 202)
        payload = document(response)
        self.assertEqual(payload["reason"], "accepted")
        self.assertEqual(payload["operation"]["status"], OPERATION_PENDING)
        self.assertIs(payload["replay"], False)
        recorded = self.store.operation("op-1")
        assert recorded is not None
        self.assertEqual(recorded["method"], METHOD_STOP_AFTER_CURRENT)
        self.assertEqual(len(self.mutations.handed), 1)
        self.assertEqual(self.mutations.handed[0]["operation_id"], "op-1")

    def test_a_resubmitted_id_is_one_mutation_not_two(self) -> None:
        self.submit("op-2")
        second = self.submit("op-2")
        self.assertEqual(second.status, 202)
        self.assertIs(document(second)["replay"], True)
        self.assertEqual(
            len(self.mutations.handed), 1, "a replay is never handed down twice"
        )

    def test_two_tabs_submitting_together_admit_only_one(self) -> None:
        """Lookup, admission and insert share one transaction.

        The first submission's admission decision is held open long enough for
        a second, different operation on the same plan to arrive. Taken under
        separate locks, both would read "nothing outstanding" and both would be
        journalled as pending.
        """
        from asl_workbench import service_mutations

        real_admit = service_mutations.admit
        deciding = threading.Event()

        def slow_admit(*args: Any, **kwargs: Any) -> Any:
            admission = real_admit(*args, **kwargs)
            if kwargs.get("operation_id") == "op-a":
                deciding.set()
                time.sleep(0.2)
            return admission

        responses: dict[str, Response] = {}

        def submit(operation_id: str) -> None:
            responses[operation_id] = self.submit(operation_id)

        with mock.patch.object(service_mutations, "admit", slow_admit):
            first = threading.Thread(target=submit, args=("op-a",))
            first.start()
            self.assertTrue(deciding.wait(5))
            submit("op-b")
            first.join(5)
        self.assertEqual(responses["op-a"].status, 202)
        self.assertNotEqual(responses["op-b"].status, 202)
        self.assertIsNone(self.store.operation("op-b"))
        self.assertEqual(len(self.mutations.handed), 1)

    def test_reusing_an_id_for_a_different_target_is_a_conflict(self) -> None:
        self.submit("op-3", plan_key="plan-abc")
        response = self.submit("op-3", plan_key="plan-other")
        self.assertEqual(document(response)["reason"], REASON_OPERATION_CONFLICT)
        self.assertEqual(len(self.mutations.handed), 1)

    def test_an_uncertain_mutation_on_an_idle_plan_is_superseded(self) -> None:
        """Otherwise one lost acknowledgement would lock the plan for good."""
        self.observe_plan(primary_state="paused-for-reply")
        self.leave_unconfirmed("op-lost")
        response = self.submit("op-next")
        self.assertEqual(response.status, 202)
        lost = self.store.operation("op-lost")
        assert lost is not None
        self.assertEqual(lost["status"], OPERATION_SUPERSEDED)
        self.assertEqual(len(self.mutations.handed), 2)

    def test_an_uncertain_mutation_is_not_superseded_while_a_run_is_live(
        self,
    ) -> None:
        self.observe_plan(primary_state="running")
        self.leave_unconfirmed("op-lost")
        response = self.submit("op-next")
        self.assertEqual(document(response)["reason"], REASON_OPERATION_UNCERTAIN)
        lost = self.store.operation("op-lost")
        assert lost is not None
        self.assertEqual(lost["status"], OPERATION_UNCONFIRMED)

    def test_a_new_active_snapshot_before_admission_keeps_uncertain_hold(self) -> None:
        self.observe_plan(primary_state="paused-for-reply")
        self.leave_unconfirmed("op-lost")
        admit_operation = self.store.admit_operation

        def observe_before_admission(*args: Any, **kwargs: Any) -> Any:
            self.store.ingest_snapshot(
                envelope(
                    "snapshot",
                    snapshot_payload(
                        observed_at=NOW + 1,
                        runs=[
                            run_row(
                                "run-1", plan_key="plan-abc", primary_state="running"
                            )
                        ],
                    ),
                    connector_id="mac",
                ),
                received_at=NOW + 1,
            )
            return admit_operation(*args, **kwargs)

        with mock.patch.object(
            self.store, "admit_operation", side_effect=observe_before_admission
        ):
            response = self.submit("op-next")

        self.assertEqual(document(response)["reason"], REASON_OPERATION_UNCERTAIN)
        lost = self.store.operation("op-lost")
        assert lost is not None
        self.assertEqual(lost["status"], OPERATION_UNCONFIRMED)
        self.assertIsNone(self.store.operation("op-next"))

    def test_an_uncertain_mutation_is_not_superseded_without_an_observation(
        self,
    ) -> None:
        self.leave_unconfirmed("op-lost")
        response = self.submit("op-next")
        self.assertEqual(document(response)["reason"], REASON_OPERATION_UNCERTAIN)

    def test_a_live_run_control_needs_the_freshness_proof(self) -> None:
        """The far end's own parser decides, before anything is journalled."""
        response = self.remote_app().handle(
            build(
                "POST",
                "/api/control/abort",
                headers=self.signed(),
                body=(
                    b'{"operation_id": "op-4", "plan_key": "plan-abc",'
                    b' "run_key": "run-1"}'
                ),
            )
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(document(response)["reason"], "invalid-submission")
        self.assertIsNone(self.store.operation("op-4"))
        self.assertEqual(self.mutations.handed, [])

    def test_prose_never_reaches_this_service(self) -> None:
        """A reply arrives as a reference and the token that redeems it.

        The words are fetched on the Mac at the moment of use, so the machine
        on the private network never holds what an operator typed.
        """
        response = self.remote_app().handle(
            build(
                "POST",
                "/api/reply",
                headers=self.signed(),
                body=json.dumps(
                    {
                        "operation_id": "op-5",
                        "plan_key": "plan-abc",
                        "run_key": "run-1",
                        "question_id": "q1",
                        "reply_ref": "ref-1",
                        "action_token": "token-1",
                        "bridge_token": "fresh",
                    }
                ).encode("utf-8"),
            )
        )
        self.assertEqual(response.status, 202)
        handed = self.mutations.handed[0]
        self.assertEqual(handed["reply_ref"], "ref-1")
        self.assertNotIn("reply_text", handed)
        recorded = self.store.operation("op-5")
        assert recorded is not None
        self.assertNotIn("reply_text", recorded)
        self.assertNotIn("answer", recorded)

    def test_browser_live_reply_is_staged_and_sent_as_question_reply(self) -> None:
        bodies = FakeBodyLane()
        response = self.remote_app(bodies=bodies).handle(
            build(
                "POST",
                "/api/question-reply",
                headers=self.signed(),
                body=json.dumps(
                    {
                        "operation_id": "op-live",
                        "plan_key": "plan-abc",
                        "run_key": "run-1",
                        "run_id": "run-local-1",
                        "question_id": "q-live",
                        "action_token": "fresh-view",
                        "reply_text": "Use approach A.",
                    }
                ).encode("utf-8"),
            )
        )
        self.assertEqual(202, response.status)
        self.assertEqual(bodies.staged["op-live"], "Use approach A.")
        handed = self.mutations.handed[-1]
        self.assertEqual(handed["method"], METHOD_QUESTION_REPLY)
        self.assertEqual(handed["bridge_token"], "fresh-view")
        self.assertEqual(handed["reply_ref"], "ref-op-live")
        self.assertEqual(handed["action_token"], "token-op-live")
        self.assertNotIn("reply_text", handed)

    def test_connector_redeems_the_staged_browser_body(self) -> None:
        bodies = FakeBodyLane()
        bodies.stage_body("op-redeem", "Continue.", now=NOW)
        app = Application(
            self.provider(bodies=bodies),
            FixedAuthenticator(ROLE_CONNECTOR, subject="mac"),
            policy=REMOTE_POLICY,
        )
        response = app.handle(
            build(
                "POST",
                COMMAND_REDEEM_PATH,
                headers=self.signed(),
                body=json.dumps(
                    {
                        "operation_id": "op-redeem",
                        "reply_ref": "ref-op-redeem",
                        "action_token": "token-op-redeem",
                    }
                ).encode("utf-8"),
            )
        )
        self.assertEqual(200, response.status)
        self.assertEqual("Continue.", document(response)["reply_text"])

    def test_a_reply_without_a_way_to_fetch_its_prose_is_refused(self) -> None:
        response = self.remote_app().handle(
            build(
                "POST",
                "/api/reply",
                headers=self.signed(),
                body=json.dumps(
                    {
                        "operation_id": "op-6",
                        "plan_key": "plan-abc",
                        "run_key": "run-1",
                        "bridge_token": "fresh",
                    }
                ).encode("utf-8"),
            )
        )
        self.assertEqual(response.status, 400)
        self.assertIsNone(self.store.operation("op-6"))

    def test_every_route_is_either_carried_out_or_explained(self) -> None:
        """No route may be silently missing from the remote provider.

        A route with no handler and no reason would fail at the click with
        whatever exception escaped, which is the one outcome the capability
        report exists to prevent.
        """
        provider = self.provider()
        explained = set(workbench_service.UNAVAILABLE_REASONS)
        # Every table the provider dispatches from, read from the provider's own
        # module rather than transcribed. The store-answered set was a literal
        # here until `/api/history` was added to it: the provider served the new
        # route, the transcription did not know about it, and the invariant read
        # the gap as a route with no handler and no reason. Naming the table is
        # what keeps this test about the provider instead of about the copy.
        served = (
            set(workbench_service.DETAIL_OPERATIONS)
            | set(workbench_service.MUTATION_METHODS)
            | set(workbench_service.UPLOAD_OPERATIONS)
            | set(workbench_service._STORE_OPERATIONS)
            | {"archive-list"}
        )
        # `capabilities` is the one operation no provider answers: the
        # application reports *on* the provider rather than asking it.
        for operation in sorted(set(OPERATIONS) - {CAPABILITY_OPERATION}):
            with self.subTest(operation=operation):
                if operation in served:
                    self.assertIsNone(provider.unavailable(operation))
                else:
                    self.assertIn(operation, explained)
                    self.assertTrue(provider.unavailable(operation))
