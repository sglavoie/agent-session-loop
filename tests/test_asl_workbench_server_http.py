"""Workbench server tests: server binding, auth, routing, and cache headers.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import socket
import urllib.parse
from unittest import mock

from agent_session_loop import workbench_server
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
        status, body, headers = _http_request(
            f"{self._base_url(server)}/api/registry/pin",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
        )
        self.assertEqual(401, status)
        self.assertFalse(body["ok"])
        self.assertEqual("unauthorized", body["reason"])
        self.assertEqual("Bearer", headers.get("WWW-Authenticate"))

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
        self.assertEqual(", ".join(body["allow"]), headers.get("Allow"))

    def test_response_has_no_store_cache_control(self) -> None:
        server = self._start_server()
        _, _, headers = _http_request(f"{self._base_url(server)}/api/health")
        self.assertEqual("no-store", headers.get("Cache-Control"))

    def _raw_exchange(
        self, server: workbench_server.WorkbenchHTTPServer, request_line: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Send one request line verbatim, past what ``http.client`` validates."""
        with socket.create_connection(
            (server.bound_host, server.bound_port), timeout=10
        ) as sock:
            sock.sendall(request_line + b"\r\nHost: 127.0.0.1\r\n\r\n")
            chunks: list[bytes] = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        return int(lines[0].split()[1]), headers, body

    def _post_partial_body(
        self,
        server: workbench_server.WorkbenchHTTPServer,
        *,
        declared: int,
        sent: bytes,
        finish: bool,
    ) -> tuple[int, dict]:
        """Declare one length, send fewer bytes, and read the answer."""
        with socket.create_connection(
            (server.bound_host, server.bound_port), timeout=10
        ) as sock:
            sock.sendall(
                b"POST /api/registry/pin HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                + f"Authorization: Bearer {server.token}\r\n".encode("ascii")
                + f"Content-Length: {declared}\r\n\r\n".encode("ascii")
                + sent
            )
            if finish:
                sock.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(body)

    def test_a_body_cut_short_is_refused_rather_than_parsed(self) -> None:
        server = self._start_server()
        status, payload = self._post_partial_body(
            server, declared=64, sent=b"{}", finish=True
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", payload["reason"])
        self.assertIn("2 of 64 declared bytes", payload["error"])

    def test_a_body_that_stalls_is_a_request_timeout_not_a_server_error(
        self,
    ) -> None:
        server = self._start_server()
        handler = workbench_server.WorkbenchRequestHandler
        with mock.patch.object(handler, "timeout", 0.3):
            status, payload = self._post_partial_body(
                server, declared=64, sent=b"{", finish=False
            )
        self.assertEqual(408, status)
        self.assertEqual("request-timeout", payload["reason"])
        self.assertNotIn("timed out", payload["error"])

    def test_head_on_the_shell_answers_like_get_without_a_body(self) -> None:
        server = self._start_server()
        status, headers, body = self._raw_exchange(server, b"HEAD / HTTP/1.0")
        self.assertEqual(200, status)
        self.assertEqual(b"", body)
        self.assertIn("text/html", headers.get("content-type", ""))

    def test_head_on_an_api_route_answers_like_get_without_a_body(self) -> None:
        server = self._start_server()
        status, headers, body = self._raw_exchange(server, b"HEAD /api/health HTTP/1.0")
        self.assertEqual(200, status)
        self.assertEqual(b"", body)
        self.assertIn("application/json", headers.get("content-type", ""))

    def test_a_stdlib_refusal_names_its_own_status(self) -> None:
        server = self._start_server()
        status, _, body = self._raw_exchange(server, b"BREW / HTTP/1.0")
        self.assertEqual(501, status)
        payload = json.loads(body)
        self.assertEqual("not-implemented", payload["reason"])
        self.assertNotIn("BREW", payload["error"])

    def test_an_unparseable_target_is_a_refusal_rather_than_a_dropped_connection(
        self,
    ) -> None:
        # `urllib.parse` raises on an unclosed IPv6 literal, before the
        # application's refusal exit: the answer has to come from the transport,
        # and nothing about the target may reach the log or the reply.
        server = self._start_server()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status, _, body = self._raw_exchange(
                server, b"GET http://[::1/secret-plan HTTP/1.0"
            )
        self.assertEqual(400, status)
        payload = json.loads(body)
        self.assertEqual("invalid-request", payload["reason"])
        self.assertNotIn("secret-plan", payload["error"])
        self.assertEqual("", stderr.getvalue())
        # The server is still answering.
        status, _, _ = _http_request(f"{self._base_url(server)}/api/health")
        self.assertEqual(200, status)

    def test_a_nul_in_a_static_path_is_a_refusal_rather_than_a_crash(self) -> None:
        server = self._start_server()
        status, _, body = self._raw_exchange(server, b"GET /static/app\x00.js HTTP/1.0")
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", json.loads(body)["reason"])
