"""RemoteProviderTests: reads."""

from __future__ import annotations

from .workbench_http_remote_support import (
    NOW,
    READ_OUTCOMES,
    REMOTE_POLICY,
    ROLE_OWNER,
    Any,
    Application,
    FakeDetailLane,
    FixedAuthenticator,
    ReadAnswer,
    RemoteProviderTestsBase,
    RemoteReadError,
    build,
    document,
    workbench_service,
)


class RemoteProviderTests(RemoteProviderTestsBase):
    def test_a_detail_read_becomes_a_bounded_ask(self) -> None:
        response = self.remote_app().handle(
            build("GET", "/api/state?plan_key=plan-abc&run_key=run-1")
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(document(response)["content"], "{}")
        ask = self.reads.asks[0]
        self.assertEqual(ask["detail"], "state")
        self.assertEqual(ask["plan_key"], "plan-abc")
        self.assertEqual(ask["run_key"], "run-1")
        self.assertEqual(ask["now"], NOW)

    def test_a_detail_window_out_of_bounds_is_the_callers_mistake(self) -> None:
        for query in ("offset=-1", "limit=0", "max_bytes=0", "offset=x"):
            with self.subTest(query=query):
                response = self.remote_app().handle(
                    build("GET", f"/api/logs?plan_key=plan-abc&stream=stdout&{query}")
                )
                self.assertEqual(response.status, 400)
                self.assertEqual(document(response)["reason"], "invalid-request")
        self.assertEqual(self.reads.asks, [], "nothing the far end refuses is asked")

    def test_a_read_that_cannot_be_composed_is_not_a_server_error(self) -> None:
        class RefusingLane:
            def fetch(self, **kwargs: Any) -> ReadAnswer:
                raise RemoteReadError("field 'run_key' is malformed")

        app = self.remote_app(reads=RefusingLane())
        response = app.handle(build("GET", "/api/state?plan_key=plan-abc&run_key=x"))
        self.assertEqual(response.status, 400)
        payload = document(response)
        self.assertEqual(payload["reason"], "invalid-request")
        self.assertIn("run_key", payload["error"])

    def test_a_question_detail_keeps_the_local_response_shape(self) -> None:
        answer = ReadAnswer(
            request_id="question-read",
            outcome="answered",
            detail="questions",
            plan_key="plan-abc",
            entries=(
                {
                    "context_kind": "waiting",
                    "question_id": "q-live",
                    "action_token": "fresh-view",
                    "rendered_prompt": "Choose A or B?",
                    "last_message": "Choose A or B?",
                },
            ),
            received_at=NOW,
        )
        reads = FakeDetailLane(answer)
        payload = document(
            Application(
                self.provider(reads=reads),
                FixedAuthenticator(ROLE_OWNER),
                policy=REMOTE_POLICY,
            ).handle(
                build(
                    "GET",
                    "/api/questions?plan_key=plan-abc&question_id=q-live",
                )
            )
        )
        self.assertEqual(payload["context_kind"], "waiting")
        self.assertEqual(payload["question_id"], "q-live")
        self.assertEqual(payload["rendered_prompt"], "Choose A or B?")
        self.assertNotIn("entries", payload)
        self.assertEqual(reads.asks[0]["question_id"], "q-live")

    def test_every_read_outcome_has_its_own_status(self) -> None:
        """Four failures, four codes. Folding them into one would hide which."""
        self.assertEqual(sorted(workbench_service.READ_STATUSES), sorted(READ_OUTCOMES))
        seen: set[int] = set()
        for outcome in READ_OUTCOMES:
            answer = ReadAnswer(
                request_id="r",
                outcome=outcome,
                detail="state",
                plan_key="plan-abc",
                note="the link dropped",
                received_at=NOW,
            )
            app = Application(
                self.provider(reads=FakeDetailLane(answer)),
                FixedAuthenticator(ROLE_OWNER),
                policy=REMOTE_POLICY,
            )
            response = app.handle(build("GET", "/api/state?plan_key=plan-abc"))
            payload = document(response)
            with self.subTest(outcome=outcome):
                if outcome == "answered":
                    self.assertEqual(response.status, 200)
                    self.assertIs(payload["ok"], True)
                else:
                    self.assertGreaterEqual(response.status, 400)
                    self.assertIs(payload["ok"], False)
                    self.assertEqual(payload["details"]["outcome"], outcome)
                    seen.add(response.status)
        self.assertEqual(len(seen), 4, "each unanswered outcome keeps its own code")

    def test_an_unanswered_read_is_not_an_empty_artifact(self) -> None:
        answer = ReadAnswer(
            request_id="r",
            outcome="offline",
            detail="logs",
            plan_key="plan-abc",
            note="the link dropped",
            received_at=NOW,
        )
        app = Application(
            self.provider(reads=FakeDetailLane(answer)),
            FixedAuthenticator(ROLE_OWNER),
            policy=REMOTE_POLICY,
        )
        payload = document(app.handle(build("GET", "/api/logs?plan_key=plan-abc")))
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "the link dropped")
        self.assertNotIn("content", payload, "a refusal carries no body")
