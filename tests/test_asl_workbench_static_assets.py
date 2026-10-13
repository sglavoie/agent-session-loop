"""Cover the bundled frontend's asset serving, MIME types and path safety.

Split from tests/test_asl_workbench_static.py."""

import unittest
import urllib.parse

from agent_session_loop import (
    workbench_server,
)
from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request,
    _http_request_raw,
)


class WorkbenchStaticServingTests(support_static.WorkbenchStaticCase):
    """Cover the bundled frontend's asset serving, MIME types and path safety."""

    def test_static_dir_ships_required_assets(self) -> None:
        static_dir = workbench_server.STATIC_DIR
        self.assertTrue(static_dir.is_dir(), str(static_dir))
        for relative in ("index.html", "styles.css", "app.js"):
            self.assertTrue(
                (static_dir / relative).is_file(),
                f"missing bundled asset: {relative}",
            )

    def test_root_serves_html_shell(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(self._base_url(server) + "/")
        self.assertEqual(200, status)
        decoded = body.decode("utf-8")
        self.assertIn("<title>ASL Workbench</title>", decoded)
        self.assertIn("/static/styles.css", decoded)
        self.assertIn("/static/app.js", decoded)
        content_type = headers.get("Content-Type") or headers.get("content-type")
        self.assertIsNotNone(content_type)
        self.assertIn("text/html", content_type)
        cache = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual("no-store", cache)

    def test_static_css_served_with_css_mime(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(
            self._base_url(server) + "/static/styles.css"
        )
        self.assertEqual(200, status)
        decoded = body.decode("utf-8")
        self.assertIn(".wb-row", decoded)
        content_type = headers.get("Content-Type") or headers.get("content-type")
        self.assertIsNotNone(content_type)
        self.assertIn("text/css", content_type)

    def test_static_css_makes_hidden_authoritative(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request_raw(
            self._base_url(server) + "/static/styles.css"
        )
        self.assertEqual(200, status)
        decoded = body.decode("utf-8")
        self.assertIn("[hidden]", decoded)
        self.assertIn("display: none !important;", decoded)

    def test_static_js_served_with_js_mime(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(
            self._base_url(server) + "/static/app.js"
        )
        self.assertEqual(200, status)
        self.assertIn("./modules/", body.decode("utf-8"))
        content_type = headers.get("Content-Type") or headers.get("content-type")
        self.assertIsNotNone(content_type)
        self.assertIn("javascript", content_type)
        # A module the entry imports is served the same way; the mime matters
        # more here than on the entry, because a browser refuses to execute a
        # module script served as anything else.
        status, module_body, module_headers = _http_request_raw(
            self._base_url(server) + "/static/modules/queue.js"
        )
        self.assertEqual(200, status)
        module_source = module_body.decode("utf-8")
        self.assertIn("renderQueue", module_source)
        module_type = module_headers.get("Content-Type") or module_headers.get(
            "content-type"
        )
        self.assertIsNotNone(module_type)
        self.assertIn("javascript", module_type)
        decoded = self._shell_source(server)
        self.assertIn("renderQueue", decoded)
        self.assertIn("FILTER_GROUPS", decoded)

    def test_static_events_highlight_agent_mux_delivery_health(self) -> None:
        server = self._start_server()
        script = self._shell_source(server)
        self.assertIn('event.name === "agent_mux_delivery"', script)
        self.assertIn("wb-event-row--delivery-failure", script)
        self.assertIn("wb-event-row--delivery-degraded", script)

        status, css_body, _ = _http_request_raw(
            self._base_url(server) + "/static/styles.css"
        )
        self.assertEqual(200, status)
        stylesheet = css_body.decode("utf-8")
        self.assertIn(".wb-event-row--delivery-failure", stylesheet)
        self.assertIn(".wb-event-row--delivery-degraded", stylesheet)

    def test_static_missing_returns_json_not_found(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            self._base_url(server) + "/static/does-not-exist.css"
        )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])
        self.assertEqual("not-found", body["reason"])

    def test_static_path_traversal_rejected(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            self._base_url(server) + "/static/../workbench_server.py"
        )
        # urllib normalizes ``..`` segments before sending so the server sees
        # ``/workbench_server.py`` which is not a known route. The defense in
        # depth still matters for clients that do not normalize; test the
        # server-side check directly via the handler helper below.
        self.assertIn(status, (400, 404))
        self.assertFalse(body["ok"])

    def test_static_path_traversal_blocked_at_handler(self) -> None:
        # urllib normalizes ``..`` in URLs before the request leaves the
        # client, so a unit test against the helper exercises the guard.
        server = self._start_server()
        bad_paths = [
            "../workbench_server.py",
            "..%2Fworkbench_server.py",
            "/etc/passwd",
            "subdir/../../../etc/passwd",
        ]
        for relative in bad_paths:
            encoded = urllib.parse.quote(relative, safe="")
            status, body, _ = _http_request(
                self._base_url(server) + f"/static/{encoded}"
            )
            self.assertIn(status, (400, 404), msg=f"path={relative} status={status}")
            self.assertFalse(body["ok"], msg=f"path={relative}")

    def test_static_post_falls_through_to_404(self) -> None:
        # Static serving only accepts GET; POSTs reach the routes table and
        # 404 because /static/... is not a registered POST endpoint.
        server = self._start_server()
        status, body, _ = _http_request(
            self._base_url(server) + "/static/app.js",
            method="POST",
            body={},
            headers={"Authorization": "Bearer ignored"},
        )
        self.assertEqual(404, status)
        self.assertFalse(body["ok"])


if __name__ == "__main__":
    unittest.main()
