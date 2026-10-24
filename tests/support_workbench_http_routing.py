"""Workbench HTTP envelopes, route table, pipeline, and policy."""

from __future__ import annotations

from .support_workbench_http_base import (
    CAPABILITY_OPERATION,
    COMMAND_REDEEM_PATH,
    LOCAL_POLICY,
    MODE_LOCAL,
    OP_HEALTH,
    OPERATION_ROLES,
    OPERATIONS,
    REMOTE_POLICY,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    ROLE_PUBLIC,
    ROLE_SUMMARY,
    ROUTES,
    ROUTES_BY_KEY,
    SECURITY_HEADERS,
    UPLINK_PATH,
    Any,
    Application,
    LocalTokenAuthenticator,
    RecordingProvider,
    RequestPolicy,
    application,
    build,
    document,
    error_payload,
    methods_for_path,
    operation_role,
    responses,
    role_satisfies,
    success_payload,
    unittest,
)


class EnvelopeTests(unittest.TestCase):
    """The portable envelope is the CLI's envelope, asserted rather than assumed."""

    def test_success_payload_matches_the_cli_builder(self) -> None:
        for fields in ({}, {"reason": "ok"}, {"reason": "ok", "plans": [1, 2]}):
            with self.subTest(fields=sorted(fields)):
                self.assertEqual(
                    success_payload("workbench", **fields),
                    responses.success_payload("workbench", **fields),
                )

    def test_error_payload_matches_the_cli_builder(self) -> None:
        mine = error_payload(
            "workbench",
            reason="invalid-request",
            error="no",
            paused=True,
            details={"a": 1},
            extra="kept",
        )
        theirs = responses.error_payload(
            "workbench",
            reason="invalid-request",
            error="no",
            paused=True,
            details={"a": 1},
            extra="kept",
        )
        self.assertEqual(mine, theirs)

    def test_an_error_envelope_always_carries_the_scriptable_keys(self) -> None:
        payload = error_payload("workbench", reason="not-found", error="gone")
        for key in ("ok", "command", "reason", "error", "paused", "details"):
            self.assertIn(key, payload)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["details"], {})


class RouteTableTests(unittest.TestCase):
    """What the one route table has to be true of to be the only one."""

    def test_every_route_declares_a_role_the_application_knows(self) -> None:
        known = {ROLE_PUBLIC, ROLE_SUMMARY, ROLE_OWNER, ROLE_CONNECTOR}
        for route in ROUTES:
            with self.subTest(route=route.key):
                self.assertIn(route.role, known)
                self.assertTrue(route.summary.strip(), "a route explains itself")
                self.assertTrue(route.path.startswith("/"))

    def test_the_lookup_table_covers_the_whole_declaration(self) -> None:
        self.assertEqual(len(ROUTES_BY_KEY), len(ROUTES))
        for route in ROUTES:
            self.assertIs(ROUTES_BY_KEY[route.key], route)

    def test_health_is_the_only_route_a_stranger_may_reach(self) -> None:
        """A stranger may reach liveness and nothing else.

        Stated as the operation rather than the path, because liveness is
        served at two paths: `/api/health` and the `/v1/health` alias the
        installed monitor and the Homepage widget are configured against. Two
        paths are still one thing a stranger learns, which is what this
        invariant is about; pinning the list to one path would have made
        aliasing an operation look like widening access to a new one.
        """
        public = [route for route in ROUTES if route.role == ROLE_PUBLIC]
        self.assertEqual({route.operation for route in public}, {OP_HEALTH})
        self.assertEqual(
            sorted(route.key for route in public),
            [("GET", "/api/health"), ("GET", "/v1/health")],
        )

    def test_every_mutation_requires_the_owner_or_is_the_uplink(self) -> None:
        """No writing route is reachable by a stranger or a summary reader.

        The connector's two routes are the writes that are not the operator's:
        one uploads observations and the other redeems a body for a command the
        service already dispatched. Both require the credential issued to that
        Mac, and the exact pair is pinned so `connector` cannot quietly become
        a second owner-like role.
        """
        connector_routes = []
        for route in ROUTES:
            if not route.mutating:
                continue
            with self.subTest(route=route.key):
                self.assertIn(route.role, (ROLE_OWNER, ROLE_CONNECTOR))
            if route.role == ROLE_CONNECTOR:
                connector_routes.append(route.key)
        self.assertEqual(
            connector_routes,
            [("POST", COMMAND_REDEEM_PATH), ("POST", UPLINK_PATH)],
        )

    def test_the_delete_alias_shares_the_archive_operation_and_its_role(self) -> None:
        alias = ROUTES_BY_KEY[("POST", "/api/plan/delete")]
        archive = ROUTES_BY_KEY[("POST", "/api/plan/archive")]
        self.assertEqual(alias.operation, archive.operation)
        self.assertEqual(alias.role, archive.role)

    def test_an_operation_has_exactly_one_role(self) -> None:
        for route in ROUTES:
            self.assertEqual(operation_role(route.operation), route.role)
        self.assertEqual(sorted(OPERATION_ROLES), sorted(OPERATIONS))

    def test_an_unknown_operation_is_a_key_error_not_a_default(self) -> None:
        with self.assertRaises(KeyError):
            operation_role("not-an-operation")

    def test_methods_for_path_is_what_a_405_is_built_from(self) -> None:
        self.assertEqual(methods_for_path("/api/queue"), ("GET",))
        self.assertEqual(methods_for_path("/api/launch"), ("POST",))
        self.assertEqual(methods_for_path("/api/nothing"), ())

    def test_role_ranking_keeps_the_connector_out_of_the_ladder(self) -> None:
        self.assertTrue(role_satisfies(ROLE_OWNER, ROLE_SUMMARY))
        self.assertTrue(role_satisfies(ROLE_SUMMARY, ROLE_PUBLIC))
        self.assertFalse(role_satisfies(ROLE_SUMMARY, ROLE_OWNER))
        # An upload credential is not a weaker owner, and must never become one
        # by being ranked against the others.
        self.assertFalse(role_satisfies(ROLE_CONNECTOR, ROLE_SUMMARY))
        self.assertFalse(role_satisfies(ROLE_OWNER, ROLE_CONNECTOR))
        self.assertTrue(role_satisfies(ROLE_CONNECTOR, ROLE_PUBLIC))


class PipelineTests(unittest.TestCase):
    """The order the application decides things in, not only what it decides."""

    def test_a_known_route_reaches_the_provider_with_its_body(self) -> None:
        provider = RecordingProvider()
        app = application(provider=provider)
        response = app.handle(
            build("POST", "/api/unlock", body=b'{"plan_dir": "/tmp/plan"}')
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(document(response)["operation"], "unlock")
        self.assertEqual(provider.calls[0].body, {"plan_dir": "/tmp/plan"})

    def test_routing_precedes_authentication(self) -> None:
        """A wrong method is a wrong method whoever is asking.

        The local server has always answered 405 here, and it answered it before
        looking at the token. Reordering these two would turn every mistyped
        method from an authenticated client into a 401, which is a different
        bug report from the one the operator actually has.
        """
        app = Application(
            RecordingProvider(),
            LocalTokenAuthenticator(lambda: "secret"),
            policy=LOCAL_POLICY,
        )
        response = app.handle(build("POST", "/api/queue", body=b"{}"))
        self.assertEqual(response.status, 405)
        payload = document(response)
        self.assertEqual(payload["reason"], "method-not-allowed")
        self.assertEqual(payload["allow"], ["GET"])

    def test_an_unknown_path_is_not_found(self) -> None:
        response = application().handle(build("GET", "/api/nothing"))
        self.assertEqual(response.status, 404)
        self.assertEqual(document(response)["reason"], "not-found")

    def test_a_trailing_slash_reaches_the_same_route(self) -> None:
        response = application().handle(build("GET", "/api/health/"))
        self.assertEqual(response.status, 200)

    def test_a_role_that_does_not_reach_the_route_is_refused(self) -> None:
        app = application(role=ROLE_SUMMARY)
        response = app.handle(build("GET", "/api/queue"))
        self.assertEqual(response.status, 403)
        payload = document(response)
        self.assertEqual(payload["reason"], "forbidden")
        self.assertEqual(payload["details"]["required_role"], ROLE_OWNER)

    def test_a_summary_credential_still_reaches_the_summary(self) -> None:
        app = application(role=ROLE_SUMMARY)
        self.assertEqual(app.handle(build("GET", "/api/summary")).status, 200)
        self.assertEqual(app.handle(build("GET", "/api/health")).status, 200)

    def test_capabilities_are_answered_by_the_application_itself(self) -> None:
        """A provider that supports nothing still has to describe itself.

        Otherwise the one call a frontend makes to find out what it may offer
        is the call that fails, and it has no way to tell "this deployment
        cannot launch runs" from "this deployment is broken".
        """
        provider = RecordingProvider(supported=(), reasons={"launch": "no runner"})
        app = application(provider=provider, role=ROLE_SUMMARY)
        response = app.handle(build("GET", "/api/capabilities"))
        self.assertEqual(response.status, 200)
        payload = document(response)
        self.assertEqual(payload["mode"], MODE_LOCAL)
        self.assertEqual(provider.calls, [])
        by_operation = {row["operation"]: row for row in payload["operations"]}
        self.assertTrue(by_operation[CAPABILITY_OPERATION]["available"])
        self.assertEqual(by_operation["launch"]["reason"], "no runner")
        self.assertEqual(by_operation["launch"]["role"], ROLE_OWNER)
        self.assertIn("launch", payload["unavailable_operations"])
        self.assertEqual(len(payload["routes"]), len(ROUTES))

    def test_the_capability_reason_is_the_refusal_sentence(self) -> None:
        """Greying a control out owes the operator the click's own explanation."""
        provider = RecordingProvider(supported=(), reasons={"launch": "no runner"})
        app = application(provider=provider)
        advertised = {
            row["operation"]: row["reason"]
            for row in document(app.handle(build("GET", "/api/capabilities")))[
                "operations"
            ]
        }
        response = app.handle(build("POST", "/api/launch", body=b"{}"))
        self.assertEqual(response.status, 503)
        payload = document(response)
        self.assertEqual(payload["reason"], "operation-unavailable")
        self.assertEqual(payload["error"], advertised["launch"])
        self.assertEqual(payload["details"]["operation"], "launch")
        self.assertEqual(provider.calls, [])

    def test_a_safe_method_never_reads_a_body(self) -> None:
        provider = RecordingProvider()
        app = application(provider=provider)
        app.handle(build("GET", "/api/queue", body=b'{"ignored": true}'))
        self.assertEqual(provider.calls[0].body, {})

    def test_every_json_answer_carries_the_shared_headers(self) -> None:
        app = application()
        for request in (
            build("GET", "/api/health"),
            build("GET", "/api/nothing"),
            build("POST", "/api/unlock", body=b"{}"),
        ):
            response = app.handle(request)
            with self.subTest(path=request.path):
                for name, value in SECURITY_HEADERS.items():
                    self.assertEqual(response.headers[name], value)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(
                    response.content_type, "application/json; charset=utf-8"
                )

    def test_a_refused_detail_read_keeps_its_own_status(self) -> None:
        from asl_workbench.details import REASON_NOT_FOUND, DetailError

        provider = RecordingProvider(
            raises=DetailError(REASON_NOT_FOUND, "no such artifact")
        )
        response = application(provider=provider).handle(build("GET", "/api/state"))
        self.assertEqual(response.status, 404)
        self.assertEqual(document(response)["reason"], REASON_NOT_FOUND)


class PolicyTests(unittest.TestCase):
    """The whole difference between loopback and the private network."""

    def remote(self, **kwargs: Any) -> Application:
        return application(policy=REMOTE_POLICY, **kwargs)

    def signed(self, **extra: str) -> dict[str, str]:
        headers = {
            "Origin": "http://127.0.0.1:4321",
            "X-Workbench-Request": "1",
            "Sec-Fetch-Site": "same-origin",
        }
        headers.update(extra)
        return headers

    def test_a_remote_mutation_needs_its_origin_proved(self) -> None:
        app = self.remote()
        self.assertEqual(
            app.handle(
                build("POST", "/api/unlock", headers=self.signed(), body=b"{}")
            ).status,
            200,
        )
        for broken in (
            {"Sec-Fetch-Site": "cross-site"},
            {"X-Workbench-Request": ""},
            {"Origin": "http://evil.example"},
        ):
            with self.subTest(header=sorted(broken)[0]):
                response = app.handle(
                    build(
                        "POST", "/api/unlock", headers=self.signed(**broken), body=b"{}"
                    )
                )
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    document(response)["error"], "cross-site request refused"
                )

    def test_a_remote_read_needs_no_origin(self) -> None:
        self.assertEqual(self.remote().handle(build("GET", "/api/queue")).status, 200)

    def test_loopback_asks_for_no_origin_at_all(self) -> None:
        response = application().handle(build("POST", "/api/unlock", body=b"{}"))
        self.assertEqual(response.status, 200)

    def test_a_chunked_body_is_refused_before_it_is_read(self) -> None:
        response = self.remote().handle(
            build(
                "POST",
                "/api/unlock",
                headers=self.signed(**{"Transfer-Encoding": "chunked"}),
            )
        )
        self.assertEqual(response.status, 411)
        self.assertEqual(document(response)["reason"], "length-required")
        self.assertTrue(response.close, "a refused body ends the connection")

    def test_a_remote_mutation_must_declare_its_length(self) -> None:
        response = self.remote().handle(
            build("POST", "/api/unlock", headers=self.signed())
        )
        self.assertEqual(response.status, 411)

    def test_loopback_keeps_its_historic_answer_to_an_empty_post(self) -> None:
        """No length on loopback is an empty body, not a framing refusal.

        The local surface has always let an authorized POST arrive with nothing
        in it and refused it for what was missing from the *request* -- a
        plan_dir, say. Turning that into a 411 would change every one of those
        messages for no gain on a socket bound to localhost.
        """
        provider = RecordingProvider()
        response = application(provider=provider).handle(build("POST", "/api/unlock"))
        self.assertEqual(response.status, 200)
        self.assertEqual(provider.calls[0].body, {})

    def test_a_body_must_be_declared_json(self) -> None:
        response = application().handle(
            build(
                "POST",
                "/api/unlock",
                headers={"Content-Type": "text/plain", "Content-Length": "2"},
                body=b"{}",
            )
        )
        self.assertEqual(response.status, 415)
        self.assertEqual(document(response)["reason"], "unsupported-media-type")
        self.assertTrue(response.close)

    def test_a_charset_parameter_does_not_break_the_content_type(self) -> None:
        response = application().handle(
            build(
                "POST",
                "/api/unlock",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Content-Length": "2",
                },
                body=b"{}",
            )
        )
        self.assertEqual(response.status, 200)

    def test_an_oversize_body_is_refused_on_its_declaration(self) -> None:
        policy = RequestPolicy(max_body_bytes=8)
        app = application(policy=policy)
        response = app.handle(
            build("POST", "/api/unlock", body=b'{"text": "far too long"}')
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(document(response)["reason"], "request-too-large")
        self.assertTrue(response.close)

    def test_a_content_length_that_is_not_a_number_is_a_bad_request(self) -> None:
        response = application().handle(
            build("POST", "/api/unlock", headers={"Content-Length": "many"})
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(document(response)["reason"], "invalid-request")

    def test_a_body_that_is_not_a_json_object_is_refused(self) -> None:
        for raw in (b"[1, 2]", b'"text"', b"not json"):
            with self.subTest(body=raw):
                response = application().handle(build("POST", "/api/unlock", body=raw))
                self.assertEqual(response.status, 400)

    def test_loopback_says_what_failed_and_the_private_network_does_not(self) -> None:
        """The operator owns the process; a prober owns nothing.

        Same failure, two disclosure levels, and the level is a policy field
        rather than a branch in a handler -- which is what makes it reviewable.
        """
        provider = RecordingProvider(raises=RuntimeError("secret path /Users/x"))
        local = application(provider=provider)
        remote = application(provider=provider, policy=REMOTE_POLICY)
        request = build("GET", "/api/queue")
        local_payload = document(local.handle(request))
        remote_payload = document(remote.handle(request))
        self.assertEqual(local_payload["error"], "secret path /Users/x")
        self.assertEqual(remote_payload["error"], "the request could not be completed")
        self.assertEqual(local_payload["reason"], "internal-error")
        self.assertEqual(remote_payload["reason"], "internal-error")

    def test_a_route_is_concealed_from_a_caller_who_proved_nothing(self) -> None:
        local = application(role=ROLE_PUBLIC)
        remote = application(role=ROLE_PUBLIC, policy=REMOTE_POLICY)
        request = build("GET", "/api/registry")
        self.assertEqual(local.handle(request).status, 403)
        remote_response = remote.handle(request)
        self.assertEqual(remote_response.status, 404)
        self.assertEqual(document(remote_response)["reason"], "not-found")

    def test_a_caller_who_proved_a_role_is_told_which_role_is_required(self) -> None:
        """Concealment stops at the callers it was protecting the route from.

        A summary credential on an owner route is the wrong credential, not a
        wrong URL, and 404 would send its holder looking for a typo. The
        Companion drew the line here and the Homepage container's access check
        reads it back from inside the container, so the two modes agree on this
        one and only the anonymous caller above is kept guessing.
        """
        for policy in (LOCAL_POLICY, REMOTE_POLICY):
            with self.subTest(policy="remote" if policy is REMOTE_POLICY else "local"):
                app = application(role=ROLE_SUMMARY, policy=policy)
                response = app.handle(build("GET", "/api/registry"))
                self.assertEqual(response.status, 403)
                payload = document(response)
                self.assertEqual(payload["reason"], "forbidden")
                self.assertEqual(payload["details"]["required_role"], ROLE_OWNER)
                self.assertEqual(payload["details"]["role"], ROLE_SUMMARY)
