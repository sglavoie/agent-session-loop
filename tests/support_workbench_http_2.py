"""Focused test_workbench_http test group 2."""

from __future__ import annotations

from .support_workbench_http_base import (
    IDENTITY_HEADER,
    LOCAL_POLICY,
    MODE_REMOTE,
    NOW,
    PI_SITE,
    REMOTE_POLICY,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    ROLE_PUBLIC,
    ROLE_SUMMARY,
    ROUTES_BY_KEY,
    Any,
    Application,
    Call,
    CredentialStore,
    HttpRefusal,
    LocalTokenAuthenticator,
    Mapping,
    OwnerIdentityAuthenticator,
    Path,
    Principal,
    RecordingProvider,
    StaticBundle,
    TemporaryDirectory,
    application,
    build,
    container_access_checks,
    document,
    issue,
    role_satisfies,
    unittest,
)


class CallTests(unittest.TestCase):
    """The parsers both providers share, and the refusals they already spoke."""

    def call(self, target: str, body: Mapping[str, Any] | None = None) -> Call:
        route = ROUTES_BY_KEY[("GET", "/api/events")]
        return Call(
            route=route,
            request=build("GET", target),
            principal=Principal(role=ROLE_OWNER),
            body=dict(body or {}),
        )

    def test_a_missing_query_parameter_names_itself(self) -> None:
        with self.assertRaises(HttpRefusal) as caught:
            self.call("/api/events").require_query("plan_dir")
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("plan_dir", caught.exception.message)

    def test_a_blank_query_parameter_is_not_a_value(self) -> None:
        with self.assertRaises(HttpRefusal):
            self.call("/api/events?plan_dir=%20").require_query("plan_dir")
        self.assertIsNone(self.call("/api/events?run_id=").optional_query("run_id"))

    def test_integers_are_bounded_in_both_directions(self) -> None:
        self.assertEqual(
            self.call("/api/events?limit=5").bounded_int(
                "limit", default=200, maximum=2000
            ),
            5,
        )
        self.assertEqual(
            self.call("/api/events").bounded_int("limit", default=200, maximum=2000),
            200,
        )
        with self.assertRaises(HttpRefusal):
            self.call("/api/events?limit=-1").bounded_int(
                "limit", default=200, maximum=2000
            )
        with self.assertRaises(HttpRefusal):
            self.call("/api/events?limit=9999").bounded_int(
                "limit", default=200, maximum=2000
            )
        with self.assertRaises(HttpRefusal):
            self.call("/api/events?limit=soon").bounded_int(
                "limit", default=200, maximum=2000
            )

    def test_repeated_parameters_are_all_available(self) -> None:
        call = self.call("/api/events?id=a&id=b")
        self.assertEqual(call.query_values("id"), ("a", "b"))
        self.assertEqual(call.require_query("id"), "a")

    def test_a_body_field_must_be_the_type_it_claims(self) -> None:
        call = self.call("/api/events", {"text": 4, "ok": "yes"})
        with self.assertRaises(HttpRefusal):
            call.require_string("text")
        with self.assertRaises(HttpRefusal):
            call.optional_string("text")
        self.assertEqual(call.require_string("ok"), "yes")
        self.assertIsNone(call.optional_string("missing"))

    def test_an_idempotency_key_is_trimmed_and_bounded(self) -> None:
        """Whitespace is stripped, because a key that differs only by spacing
        reads as a second submission here and as one submission to whoever
        typed it."""
        call = self.call("/api/events", {"operation_id": "  abc  "})
        self.assertEqual(call.bounded_id("operation_id", maximum=8), "abc")
        with self.assertRaises(HttpRefusal):
            self.call("/api/events", {"operation_id": "   "}).bounded_id(
                "operation_id", maximum=8
            )
        with self.assertRaises(HttpRefusal):
            self.call("/api/events", {"operation_id": "x" * 9}).bounded_id(
                "operation_id", maximum=8
            )


class AuthenticatorTests(unittest.TestCase):
    """Two boundaries, and the asymmetry in the local one is deliberate."""

    def test_a_local_read_needs_no_token_and_a_local_write_does(self) -> None:
        authenticator = LocalTokenAuthenticator(lambda: "secret")
        read = ROUTES_BY_KEY[("GET", "/api/queue")]
        write = ROUTES_BY_KEY[("POST", "/api/unlock")]
        self.assertTrue(authenticator.authenticate(build("GET", "/"), read).is_owner)
        with self.assertRaises(HttpRefusal) as caught:
            authenticator.authenticate(build("POST", "/"), write)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.reason, "unauthorized")
        granted = authenticator.authenticate(
            build("POST", "/", headers={"Authorization": "Bearer secret"}), write
        )
        self.assertTrue(granted.is_owner)

    def test_a_wrong_local_token_writes_nothing(self) -> None:
        provider = RecordingProvider()
        app = Application(
            provider, LocalTokenAuthenticator(lambda: "secret"), policy=LOCAL_POLICY
        )
        response = app.handle(
            build(
                "POST",
                "/api/unlock",
                headers={"Authorization": "Bearer wrong"},
                body=b"{}",
            )
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(provider.calls, [])

    def test_the_token_is_read_at_request_time(self) -> None:
        """The server mints it after this object is built, so it is a callable."""
        token = {"value": "first"}
        authenticator = LocalTokenAuthenticator(lambda: token["value"])
        write = ROUTES_BY_KEY[("POST", "/api/unlock")]
        token["value"] = "second"
        granted = authenticator.authenticate(
            build("POST", "/", headers={"Authorization": "Bearer second"}), write
        )
        self.assertTrue(granted.is_owner)

    def test_the_identity_header_makes_the_owner(self) -> None:
        """The proof is the header the private network's proxy set.

        It names no credential, so the principal carries none: an owner proved
        by identity is not a credential id somebody could later revoke.
        """
        store = CredentialStore()
        authenticator = OwnerIdentityAuthenticator(store, owner="sglavoie@example")
        route = ROUTES_BY_KEY[("GET", "/api/queue")]
        principal = authenticator.authenticate(
            build("GET", "/", headers={IDENTITY_HEADER: "sglavoie@example"}), route
        )
        self.assertEqual(principal.role, ROLE_OWNER)
        self.assertTrue(principal.is_owner)
        self.assertIsNone(principal.credential_id)

    def test_a_stranger_at_the_identity_header_is_not_the_owner(self) -> None:
        authenticator = OwnerIdentityAuthenticator(
            CredentialStore(), owner="sglavoie@example"
        )
        route = ROUTES_BY_KEY[("GET", "/api/queue")]
        principal = authenticator.authenticate(
            build("GET", "/", headers={IDENTITY_HEADER: "someone@else"}), route
        )
        self.assertEqual(principal.role, ROLE_PUBLIC)

    def test_a_summary_credential_stays_a_summary_credential(self) -> None:
        store = CredentialStore()
        credential, token = issue(ROLE_SUMMARY, created_at=NOW, label="status pane")
        store.add(credential)
        authenticator = OwnerIdentityAuthenticator(store, owner="sglavoie@example")
        route = ROUTES_BY_KEY[("GET", "/api/summary")]
        principal = authenticator.authenticate(
            build("GET", "/", headers={"Authorization": f"Bearer {token}"}), route
        )
        self.assertEqual(principal.role, ROLE_SUMMARY)
        self.assertEqual(principal.credential_id, credential.credential_id)

    def test_proving_nothing_is_public_rather_than_refused(self) -> None:
        """The refusal belongs to whatever they then ask for, not to arriving."""
        authenticator = OwnerIdentityAuthenticator(CredentialStore(), owner=None)
        route = ROUTES_BY_KEY[("GET", "/api/health")]
        principal = authenticator.authenticate(build("GET", "/"), route)
        self.assertEqual(principal.role, ROLE_PUBLIC)
        self.assertFalse(principal.is_owner)

    def test_a_credential_that_was_not_accepted_is_told_so(self) -> None:
        """Offering something broken is not the same as offering nothing.

        The Companion answered 401 here and the unified service has to as well,
        or the only thing an expired token gets is the answer a typo gets.
        """
        authenticator = OwnerIdentityAuthenticator(CredentialStore(), owner=None)
        route = ROUTES_BY_KEY[("GET", "/api/summary")]
        with self.assertRaises(HttpRefusal) as caught:
            authenticator.authenticate(
                build("GET", "/", headers={"Authorization": "Bearer nonsense"}), route
            )
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(caught.exception.reason, "unauthorized")

    def test_a_public_route_does_not_examine_a_credential_at_all(self) -> None:
        """The route a monitor polls answers the same way to everyone.

        It asks for no proof, so an unusable proof offered alongside is not what
        would make the request fail -- and a health check that started answering
        401 to a client with a stale token would take the monitor down with it.
        """
        authenticator = OwnerIdentityAuthenticator(CredentialStore(), owner=None)
        route = ROUTES_BY_KEY[("GET", "/v1/health")]
        principal = authenticator.authenticate(
            build("GET", "/", headers={"Authorization": "Bearer nonsense"}), route
        )
        self.assertEqual(principal.role, ROLE_PUBLIC)

    def test_an_owner_proved_by_identity_outranks_a_broken_token(self) -> None:
        authenticator = OwnerIdentityAuthenticator(
            CredentialStore(), owner="sglavoie@example"
        )
        route = ROUTES_BY_KEY[("GET", "/api/registry")]
        principal = authenticator.authenticate(
            build(
                "GET",
                "/",
                headers={
                    IDENTITY_HEADER: "sglavoie@example",
                    "Authorization": "Bearer nonsense",
                },
            ),
            route,
        )
        self.assertEqual(principal.role, ROLE_OWNER)

    def test_the_uplink_reads_the_connector_credential_not_the_identity(self) -> None:
        """The publisher's own route is the one identity cannot speak for.

        Tailscale Serve sets the identity header on everything the tailnet
        sends, and the connector publishes over Serve, so every frame it sends
        arrives carrying the owner's login. Reading identity first turns the
        publisher into an owner, and an owner is not a connector: the uplink
        refuses its own connector and the Mac silently backs off forever. This
        is the production symptom the cutover hit, reduced to the one call that
        decides it.
        """
        store = CredentialStore()
        credential, token = issue(ROLE_CONNECTOR, created_at=NOW, subject="mac-studio")
        store.add(credential)
        authenticator = OwnerIdentityAuthenticator(store, owner="sglavoie@example")
        route = ROUTES_BY_KEY[("POST", "/v1/ingest")]
        principal = authenticator.authenticate(
            build(
                "POST",
                "/v1/ingest",
                headers={
                    IDENTITY_HEADER: "sglavoie@example",
                    "Authorization": f"Bearer {token}",
                },
            ),
            route,
        )
        self.assertEqual(principal.role, ROLE_CONNECTOR)
        self.assertEqual(principal.credential_id, credential.credential_id)
        self.assertTrue(role_satisfies(principal.role, route.role))

    def test_the_owner_still_cannot_reach_the_uplink_by_identity_alone(self) -> None:
        """Suppressing identity there opens nothing: it only stops a downgrade.

        The owner arriving at the uplink with nothing but Serve's header proves
        no connector credential, so they are public and the route refuses them
        -- which is what should happen, and what used to happen behind a 403
        that named the owner role instead of naming the absence of a token.
        """
        authenticator = OwnerIdentityAuthenticator(
            CredentialStore(), owner="sglavoie@example"
        )
        route = ROUTES_BY_KEY[("POST", "/v1/ingest")]
        principal = authenticator.authenticate(
            build(
                "POST",
                "/v1/ingest",
                headers={IDENTITY_HEADER: "sglavoie@example"},
            ),
            route,
        )
        self.assertEqual(principal.role, ROLE_PUBLIC)
        self.assertFalse(role_satisfies(principal.role, route.role))


class ContainerAccessTests(unittest.TestCase):
    """The four answers the Homepage container checks for, from the application.

    This is the one boundary in the system that is verified by a third party on
    every integration run: :func:`asl_workbench.integrations.verify_container`
    executes :func:`~asl_workbench.integrations.container_access_script` inside
    the Homepage container and refuses the integration if any row disagrees.
    Driving the same table through the application here is what makes that a
    test failure rather than a cutover failure -- which is precisely how it was
    found, one session late.
    """

    def probe_application(self) -> tuple[Application, str]:
        store = CredentialStore()
        credential, token = issue(ROLE_SUMMARY, created_at=NOW, label="homepage")
        store.add(credential)
        application = Application(
            RecordingProvider(mode=MODE_REMOTE),
            OwnerIdentityAuthenticator(store, owner="sglavoie@example"),
            policy=REMOTE_POLICY,
        )
        return application, token

    def test_every_row_of_the_container_check_is_the_answer_it_expects(self) -> None:
        app, summary_token = self.probe_application()
        for path, kind, expected in container_access_checks(PI_SITE):
            token = summary_token if kind == "token" else kind
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            with self.subTest(path=path, credential=kind):
                response = app.handle(build("GET", path, headers=headers))
                self.assertEqual(response.status, expected)

    def test_the_table_still_describes_four_distinct_questions(self) -> None:
        # A row that silently collapsed into another would keep passing while
        # covering less; the interesting property is that the four answers are
        # not the same answer.
        rows = container_access_checks(PI_SITE)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row[2] for row in rows}, {200, 401, 403})


class StaticTests(unittest.TestCase):
    """The bundle both modes serve, and the cache policy it serves it under."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        (self.root / "index.html").write_text("<!doctype html>", encoding="utf-8")
        (self.root / "app.js").write_text("export const a = 1;\n", encoding="utf-8")
        self.app = application(static=StaticBundle(self.root))

    def test_the_shell_is_never_stored(self) -> None:
        response = self.app.handle(build("GET", "/"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        self.assertIn("'self'", response.headers["Content-Security-Policy"])

    def test_an_asset_revalidates_and_answers_304(self) -> None:
        first = self.app.handle(build("GET", "/static/app.js"))
        self.assertEqual(first.status, 200)
        self.assertEqual(first.headers["Cache-Control"], "no-cache, must-revalidate")
        etag = first.headers["ETag"]
        second = self.app.handle(
            build("GET", "/static/app.js", headers={"If-None-Match": etag})
        )
        self.assertEqual(second.status, 304)
        self.assertFalse(second.has_body)
        self.assertFalse(second.sends_content_length)

    def test_a_weak_validator_is_still_a_match(self) -> None:
        etag = self.app.handle(build("GET", "/static/app.js")).headers["ETag"]
        response = self.app.handle(
            build("GET", "/static/app.js", headers={"If-None-Match": f"W/{etag}"})
        )
        self.assertEqual(response.status, 304)

    def test_a_path_that_escapes_the_bundle_is_refused(self) -> None:
        secret = self.root.parent / "secret.txt"
        secret.write_text("no", encoding="utf-8")
        self.addCleanup(secret.unlink)
        response = self.app.handle(build("GET", "/static/../secret.txt"))
        self.assertIn(response.status, (400, 404))
        self.assertNotIn(b"no", response.body)

    def test_a_missing_asset_is_not_found(self) -> None:
        response = self.app.handle(build("GET", "/static/absent.js"))
        self.assertEqual(response.status, 404)
        self.assertEqual(document(response)["reason"], "not-found")

    def test_a_post_to_the_shell_is_not_served_as_a_file(self) -> None:
        response = self.app.handle(build("POST", "/", body=b"{}"))
        self.assertEqual(response.status, 404)

    def test_an_application_without_a_bundle_serves_no_shell(self) -> None:
        response = application().handle(build("GET", "/"))
        self.assertEqual(response.status, 404)
