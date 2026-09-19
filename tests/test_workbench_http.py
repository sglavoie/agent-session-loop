"""One application, two modes, and the differences written down rather than lived.

Before this session there were two servers. The local one built its route table
inside a request handler; the Companion declared its own, with its own roles,
its own refusals, and its own idea of what a bounded read was. Both answered
about the same plans, and the only way to know whether they agreed was to read
them side by side. :mod:`asl_workbench.http` is now the one implementation, and
the modes differ in exactly three declared places -- a
:class:`~asl_workbench.http.Provider`, an
:class:`~asl_workbench.http.Authenticator`, and a
:class:`~asl_workbench.http.RequestPolicy`.

So these tests are about what is *shared* at least as much as about what each
mode does:

* :class:`EnvelopeTests` pins the portable envelope builders against
  ``agent_session_loop.responses``. The portable half may not import the CLI,
  so "these two agree" is a claim that has to be asserted rather than arranged;
  a divergence here is a scriptable client reading a different shape depending
  on which machine answered.
* :class:`RouteTableTests` holds the properties the table must have to be the
  single source: one role per operation, an alias that cannot weaken the route
  it aliases, and a method list a 405 can be built from.
* :class:`PipelineTests` drives :class:`~asl_workbench.http.Application` with a
  recording provider, and asserts the order of the pipeline as much as its
  answers -- routing before authentication, because the local server has always
  answered 405 to a wrong method regardless of the token, and a capability
  report the provider cannot decline to give.
* :class:`PolicyTests` is the whole difference between loopback and the private
  network, one field at a time: framing, content type, size, origin, and
  whether an unexpected failure is allowed to say what it was.
* :class:`AuthenticatorTests` covers both boundaries, including the one that
  looks like an oversight and is not: local reads are token-free, and the
  reason is in :class:`~asl_workbench.http.LocalTokenAuthenticator`'s docstring.
* :class:`StaticTests` covers the bundle both modes serve, because a shell
  cached across a server upgrade is the bug this cache policy exists to prevent.
* :class:`RemoteProviderTests` drives :mod:`asl_workbench.service` against a
  real store: what a Pi can answer, what it refuses and why, how each of the six
  read outcomes is reported, and that a resubmitted operation id is one
  mutation rather than two.

Nothing here binds a socket or spawns a process. The transport adapter is
``agent_session_loop.workbench_server``, and ``tests/test_asl_workbench_server.py``
drives it over a real loopback server; what is left to assert here is the
application it adapts.
"""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agent_session_loop import responses
from asl_workbench import service as workbench_service
from asl_workbench.credentials import (
    IDENTITY_HEADER,
    ROLE_CONNECTOR,
    ROLE_OWNER,
    ROLE_PUBLIC,
    ROLE_SUMMARY,
    CredentialStore,
    issue,
)
from asl_workbench.http import (
    CAPABILITY_OPERATION,
    LOCAL_POLICY,
    MODE_LOCAL,
    MODE_REMOTE,
    OPERATION_ROLES,
    OPERATIONS,
    REMOTE_POLICY,
    ROUTES,
    ROUTES_BY_KEY,
    SECURITY_HEADERS,
    Application,
    Call,
    HttpRefusal,
    LocalTokenAuthenticator,
    OwnerIdentityAuthenticator,
    Principal,
    Request,
    RequestPolicy,
    Response,
    Route,
    StaticBundle,
    error_payload,
    methods_for_path,
    operation_role,
    role_satisfies,
    success_payload,
    unavailable_from,
)
from asl_workbench.observation import ObservationPolicy
from asl_workbench.operations import (
    METHOD_STOP_AFTER_CURRENT,
    OPERATION_PENDING,
    REASON_OPERATION_CONFLICT,
)
from asl_workbench.remote_reads import READ_OUTCOMES, ReadAnswer
from asl_workbench.store import WorkbenchStore

# A fixed moment. Every clock in this file is an argument, so a test asserts
# about a decision rather than racing one.
NOW = 1_700_000_000.0


def build(
    method: str,
    target: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes = b"",
) -> Request:
    """One request, with its body already decided and bounded."""
    supplied = dict(headers or {})
    if body and "Content-Length" not in supplied:
        supplied["Content-Length"] = str(len(body))
    if body and "Content-Type" not in supplied:
        supplied["Content-Type"] = "application/json"
    supplied.setdefault("Host", "127.0.0.1:4321")
    return Request.build(method, target, supplied, read=lambda length: body[:length])


def document(response: Response) -> dict[str, Any]:
    parsed = json.loads(response.body.decode("utf-8"))
    assert isinstance(parsed, dict)
    return parsed


class RecordingProvider:
    """A provider that carries nothing out and remembers being asked."""

    def __init__(
        self,
        *,
        mode: str = MODE_LOCAL,
        supported: tuple[str, ...] | None = None,
        reasons: Mapping[str, str] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.mode = mode
        self._supported = OPERATIONS if supported is None else supported
        self._reasons = dict(reasons or {})
        self._raises = raises
        self.calls: list[Call] = []

    def unavailable(self, operation: str) -> str | None:
        return unavailable_from(
            self._supported, reasons=self._reasons, default="not carried out here"
        )(operation)

    def invoke(self, call: Call) -> Response:
        self.calls.append(call)
        if self._raises is not None:
            raise self._raises
        return Response.success(
            reason="ok", operation=call.operation, body=dict(call.body)
        )


class FixedAuthenticator:
    """Whatever role the test says, with no proof involved."""

    def __init__(self, role: str = ROLE_OWNER) -> None:
        self.role = role

    def authenticate(self, request: Request, route: Route) -> Principal:
        del request, route
        return Principal(role=self.role)


def application(
    *,
    provider: RecordingProvider | None = None,
    role: str = ROLE_OWNER,
    policy: RequestPolicy = LOCAL_POLICY,
    static: StaticBundle | None = None,
) -> Application:
    return Application(
        provider or RecordingProvider(),
        FixedAuthenticator(role),
        policy=policy,
        static=static,
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
        public = [route.key for route in ROUTES if route.role == ROLE_PUBLIC]
        self.assertEqual(public, [("GET", "/api/health")])

    def test_every_mutation_requires_the_owner(self) -> None:
        for route in ROUTES:
            if route.mutating:
                with self.subTest(route=route.key):
                    self.assertEqual(route.role, ROLE_OWNER)

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

    def test_a_forbidden_route_is_concealed_on_the_private_network(self) -> None:
        local = application(role=ROLE_SUMMARY)
        remote = application(role=ROLE_SUMMARY, policy=REMOTE_POLICY)
        request = build("GET", "/api/registry")
        self.assertEqual(local.handle(request).status, 403)
        remote_response = remote.handle(request)
        self.assertEqual(remote_response.status, 404)
        self.assertEqual(document(remote_response)["reason"], "not-found")


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


class FakeDetailLane:
    """A read lane that answers whatever the test says, and remembers the ask."""

    def __init__(self, answer: ReadAnswer) -> None:
        self.answer = answer
        self.asks: list[dict[str, Any]] = []

    def fetch(self, **kwargs: Any) -> ReadAnswer:
        self.asks.append(dict(kwargs))
        return self.answer


class FakeSubmissionLane:
    """A mutation lane that records what would have gone down the link."""

    def __init__(self) -> None:
        self.handed: list[dict[str, Any]] = []

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        del now
        self.handed.append(dict(payload))


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

    def provider(self, **kwargs: Any) -> workbench_service.RemoteProvider:
        defaults: dict[str, Any] = {
            "reads": self.reads,
            "mutations": self.mutations,
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
            provider.unavailable("launch"),
            workbench_service.UNAVAILABLE_REASONS["launch"],
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
            | set(workbench_service._STORE_OPERATIONS)
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


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
