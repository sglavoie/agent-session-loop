"""Workbench server tests: server binding, auth, routing, and cache headers.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import http.client
import json
import urllib.parse

from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
)

ASL_TEST_LANE = "integration"


class WorkbenchServerHTTPTests(WorkbenchServerTestCase):
    """Binding, auth, routing, and cache behaviour of the HTTP server."""

    def test_server_binds_to_ephemeral_port(self) -> None:
        server = self._start_server(token="fixed-token-1")
        self.assertEqual("127.0.0.1", server.bound_host)
        self.assertNotEqual(0, server.bound_port)
        self.assertEqual("fixed-token-1", server.token)

    def test_health_endpoint_does_not_require_token(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/health")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertEqual("workbench", body["command"])
        self.assertEqual("ok", body["reason"])

    def test_token_generated_when_not_provided(self) -> None:
        server = self._start_server()
        # Default tokens should be url-safe and non-empty.
        self.assertTrue(server.token)
        self.assertGreaterEqual(len(server.token), 32)

    def test_artifacts_endpoint_describes_known_paths(self) -> None:
        server = self._start_server()
        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/artifacts?{query}"
        )
        self.assertEqual(200, status)
        artifacts = body["artifacts"]
        self.assertIn("state_path", artifacts)
        self.assertIn("events_path", artifacts)
        # No runner state exists yet so these are not present.
        self.assertFalse(artifacts["state_path"]["exists"])

    def test_mutating_endpoint_rejects_missing_token(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
        )
        self.assertEqual(401, status)
        self.assertFalse(body["ok"])
        self.assertEqual("unauthorized", body["reason"])

    def test_mutating_endpoint_rejects_wrong_token(self) -> None:
        server = self._start_server(token="correct-token")
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", body["reason"])

    def test_rebound_host_is_refused_before_any_read(self) -> None:
        server = self._start_server()
        connection = http.client.HTTPConnection(
            server.bound_host, server.bound_port, timeout=10
        )
        self.addCleanup(connection.close)
        connection.request("GET", "/api/plans", headers={"Host": "evil.example"})
        response = connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(403, response.status)
        self.assertEqual("forbidden", body["reason"])

    def test_refusal_before_the_body_is_read_ends_the_connection(self) -> None:
        """An unread body must not become the next request on a keep-alive link.

        The 401 is decided from the headers alone, so the JSON after them is
        still on the socket. Keeping the connection would hand it to the next
        request's parser.
        """
        server = self._start_server()
        connection = http.client.HTTPConnection(
            server.bound_host, server.bound_port, timeout=10
        )
        self.addCleanup(connection.close)
        payload = json.dumps({"plan_dir": str(self.canonical_plan_dir)})
        connection.request(
            "POST",
            "/api/registry/pin",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(401, response.status)
        self.assertEqual("close", response.getheader("Connection"))
        self.assertTrue(response.will_close)

    def test_post_with_malformed_json_returns_400(self) -> None:
        server = self._start_server()
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self._base_url(server)}/api/registry/pin",
            data=b"{not valid json",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {server.token}",
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(400, ctx.exception.code)
        body = json.loads(ctx.exception.read())
        self.assertEqual("invalid-request", body["reason"])

    def test_post_with_missing_required_field_returns_400(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/stop-after-current",
            method="POST",
            body={},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("plan_dir", body["error"])

    def test_unknown_route_returns_404(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(f"{self._base_url(server)}/api/does-not-exist")
        self.assertEqual(404, status)
        self.assertEqual("not-found", body["reason"])

    def test_wrong_method_returns_405_with_allow(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request(
            f"{self._base_url(server)}/api/queue",
            method="POST",
            body={},
            headers=self._auth(server),
        )
        self.assertEqual(405, status)
        self.assertEqual("method-not-allowed", body["reason"])
        self.assertIn("GET", body["allow"])

    def test_response_has_no_store_cache_control(self) -> None:
        server = self._start_server()
        _, _, headers = _http_request(f"{self._base_url(server)}/api/health")
        self.assertEqual("no-store", headers.get("Cache-Control"))
