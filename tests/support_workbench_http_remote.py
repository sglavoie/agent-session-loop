"""Workbench HTTP remote provider."""

from __future__ import annotations

import threading
import time
from unittest import mock

from asl_workbench.remote_reads import RemoteReadError
from asl_workbench.store_types import MAX_READ_LIMIT

from .support_workbench_http_base import (
    CAPABILITY_OPERATION,
    COMMAND_REDEEM_PATH,
    METHOD_QUESTION_REPLY,
    METHOD_STOP_AFTER_CURRENT,
    MODE_REMOTE,
    NOW,
    OPERATION_PENDING,
    OPERATIONS,
    READ_OUTCOMES,
    REASON_OPERATION_CONFLICT,
    REMOTE_POLICY,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    Any,
    Application,
    FakeBodyLane,
    FakeDetailLane,
    FakeSubmissionLane,
    FakeUploadLane,
    FixedAuthenticator,
    ObservationPolicy,
    Path,
    ReadAnswer,
    Response,
    TemporaryDirectory,
    WorkbenchStore,
    build,
    document,
    json,
    unittest,
    workbench_service,
)
from .support_workbench_store import catalog_entry, catalog_payload, envelope


class RemoteProviderTests(unittest.TestCase):
    """What a Pi can answer out of what it kept, and what it admits it cannot."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.store = WorkbenchStore.open(
            Path(self._temporary.name) / "workbench.db",
            policy=ObservationPolicy(),
            created_at=NOW,
        )
        self.addCleanup(self.store.close)
        self.reads = FakeDetailLane(
            ReadAnswer(
                request_id="r1",
                outcome="answered",
                detail="state",
                plan_key="plan-abc",
                content="{}",
                received_at=NOW,
            )
        )
        self.mutations = FakeSubmissionLane()
        self.uploads = FakeUploadLane()

    def provider(self, **kwargs: Any) -> workbench_service.RemoteProvider:
        defaults: dict[str, Any] = {
            "reads": self.reads,
            "mutations": self.mutations,
            "uploads": self.uploads,
        }
        defaults.update(kwargs)
        return workbench_service.RemoteProvider(
            self.store, clock=lambda: NOW, connector_id="mac", **defaults
        )

    def remote_app(self, **kwargs: Any) -> Application:
        return Application(
            self.provider(**kwargs),
            FixedAuthenticator(ROLE_OWNER),
            policy=REMOTE_POLICY,
        )

    def signed(self) -> dict[str, str]:
        return {
            "Origin": "http://127.0.0.1:4321",
            "X-Workbench-Request": "1",
            "Sec-Fetch-Site": "same-origin",
        }

    def test_the_mode_is_declared_not_inferred(self) -> None:
        self.assertEqual(self.provider().mode, MODE_REMOTE)

    def test_health_reports_the_service_and_nothing_observed(self) -> None:
        response = self.remote_app().handle(build("GET", "/api/health"))
        self.assertEqual(response.status, 200)
        payload = document(response)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            set(payload), {"ok", "command", "reason", "status"}, "health says no more"
        )

    def test_the_summary_answers_in_the_shape_the_local_mode_answers_in(self) -> None:
        payload = document(self.remote_app().handle(build("GET", "/api/summary")))
        self.assertEqual(sorted(payload["counts"]), ["by_state", "plans"])
        self.assertEqual(
            sorted(payload["freshness"]),
            ["complete", "generation", "observed_at", "stale", "total"],
        )
        # Nothing has ever been observed, so the view is stale rather than
        # empty-and-current: "no connector has reported" is not "no plans".
        self.assertTrue(payload["freshness"]["stale"])

    def test_the_queue_is_the_retained_catalog(self) -> None:
        payload = document(self.remote_app().handle(build("GET", "/api/queue")))
        self.assertEqual(payload["plans"], [])
        self.assertEqual(payload["connectors"], [])
        self.assertIsNone(payload["state_root"], "the Pi has no state root")

    def test_a_plan_nobody_published_is_not_found(self) -> None:
        response = self.remote_app().handle(build("GET", "/api/plan?plan_key=absent"))
        self.assertEqual(response.status, 404)
        self.assertEqual(document(response)["reason"], "not-found")

    def seed_catalog_past_the_read_limit(self) -> int:
        """More plans than one read can hold, the quietest sorting last."""
        entries = [
            catalog_entry(
                f"busy{index:06d}",
                primary_state="running",
                urgency_rank=10,
            )
            for index in range(MAX_READ_LIMIT)
        ]
        entries.append(
            catalog_entry(
                "zzz-quiet",
                primary_state="complete",
                recommended_action="view",
                urgency_rank=90,
                urgency_group="complete",
            )
        )
        self.store.ingest_catalog(
            envelope(
                "catalog_page",
                catalog_payload(
                    observed_at=NOW - 10,
                    generation="gen-large",
                    entries=entries,
                ),
            ),
            received_at=NOW - 10,
        )
        return len(entries)

    def test_a_plan_past_the_read_limit_is_still_found(self) -> None:
        self.seed_catalog_past_the_read_limit()
        response = self.remote_app().handle(
            build("GET", "/api/plan?plan_key=zzz-quiet")
        )
        self.assertEqual(response.status, 200, document(response))
        self.assertEqual(document(response)["plan"]["plan_key"], "zzz-quiet")

    def test_the_summary_histogram_counts_every_plan(self) -> None:
        total = self.seed_catalog_past_the_read_limit()
        payload = document(self.remote_app().handle(build("GET", "/api/summary")))
        self.assertEqual(payload["counts"]["plans"], total)
        self.assertEqual(
            payload["counts"]["by_state"],
            {"running": MAX_READ_LIMIT, "complete": 1},
        )
        self.assertEqual(sum(payload["counts"]["by_state"].values()), total)

    def test_the_sort_is_applied_before_the_page_is_cut(self) -> None:
        # Urgency and activity disagree on purpose: the most urgent row is the
        # oldest and sorts last by label, so a page cut in urgency order and
        # then labelled with the requested sort would put it first every time.
        self.store.ingest_catalog(
            envelope(
                "catalog_page",
                catalog_payload(
                    observed_at=NOW - 10,
                    generation="gen-sort",
                    entries=[
                        catalog_entry(
                            "urgent-old",
                            urgency_rank=10,
                            display_label="Zulu",
                            last_activity_at="2020-01-01T00:00:00+00:00",
                        ),
                        catalog_entry(
                            "quiet-new",
                            urgency_rank=50,
                            display_label="alpha",
                            last_activity_at="2030-01-01T00:00:00Z",
                        ),
                    ],
                ),
            ),
            received_at=NOW - 10,
        )
        expected = {
            "urgency": "urgent-old",
            "recent": "quiet-new",
            "label": "quiet-new",
            "oldest": "urgent-old",
        }
        for sort, first in expected.items():
            with self.subTest(sort=sort):
                payload = document(
                    self.remote_app().handle(
                        build("GET", f"/api/queue?sort={sort}&limit=1")
                    )
                )
                self.assertEqual(payload["filters"]["sort"], sort)
                self.assertEqual([row["plan_key"] for row in payload["plans"]], [first])
                self.assertTrue(payload["truncated"])

    def test_an_empty_page_size_is_refused_rather_than_rounded_up(self) -> None:
        for path in ("/api/queue?limit=0", "/api/history?limit=0"):
            with self.subTest(path=path):
                response = self.remote_app().handle(build("GET", path))
                self.assertEqual(response.status, 400)
                payload = document(response)
                self.assertEqual(payload["reason"], "invalid-request")
                self.assertIn("limit", payload["error"])

    def test_a_path_is_refused_rather_than_guessed_at(self) -> None:
        response = self.remote_app().handle(
            build("GET", "/api/plan?plan_dir=/Users/x/plan")
        )
        self.assertEqual(response.status, 400)
        self.assertIn("plan_key", document(response)["error"])

    def test_what_stays_on_the_mac_says_so_in_advance(self) -> None:
        provider = self.provider()
        for operation, reason in workbench_service.UNAVAILABLE_REASONS.items():
            with self.subTest(operation=operation):
                self.assertEqual(provider.unavailable(operation), reason)
        payload = document(self.remote_app().handle(build("GET", "/api/capabilities")))
        advertised = set(payload["unavailable_operations"])
        self.assertTrue(set(workbench_service.UNAVAILABLE_REASONS) <= advertised)
        self.assertNotIn("state", advertised, "a paired link can read details")
        self.assertNotIn(CAPABILITY_OPERATION, advertised)

    def test_an_unpaired_service_says_so_rather_than_failing_later(self) -> None:
        provider = self.provider(reads=None, mutations=None)
        self.assertIn("no connector is paired", provider.unavailable("state") or "")
        self.assertIn("no connector is paired", provider.unavailable("reply") or "")
        self.assertIsNone(provider.unavailable("summary"))

    def test_a_reason_of_its_own_outranks_the_unpaired_one(self) -> None:
        """An unpaired link is true of every operation and explains none of them."""
        provider = self.provider(reads=None, mutations=None)
        self.assertEqual(
            provider.unavailable("registry"),
            workbench_service.UNAVAILABLE_REASONS["registry"],
        )

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

    def submit(self, operation_id: str, *, plan_key: str = "plan-abc") -> Response:
        body = json.dumps(
            {
                "operation_id": operation_id,
                "plan_key": plan_key,
                "run_key": "run-1",
                "bridge_token": "fresh",
            }
        ).encode("utf-8")
        return self.remote_app().handle(
            build(
                "POST",
                "/api/control/stop-after-current",
                headers=self.signed(),
                body=body,
            )
        )

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
