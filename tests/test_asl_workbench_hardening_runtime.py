"""Disappearing-file, concurrency, caching, and shutdown hardening.

Split from tests/test_asl_workbench_static.py."""

import argparse
import errno
import io
import json
import os
import signal
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans_files,
    state,
    workbench_details_logs,
    workbench_registry,
    workbench_server,
)
from tests import support_asl_workbench_static as support_static
from tests.support import (
    _http_request,
    _http_request_raw,
    write_plan,
)

ASL_TEST_LANE = "integration"


class WorkbenchHardeningTests(support_static.WorkbenchHardeningCase):
    """Disappearing-file, concurrency, caching, and shutdown hardening."""

    # -- Cache-Control / ETag ----------------------------------------------

    def test_static_asset_emits_etag_and_revalidate_cache_control(self) -> None:
        server = self._start_server()
        status, body, headers = _http_request_raw(
            self._base_url(server) + "/static/app.js"
        )
        self.assertEqual(200, status)
        self.assertGreater(len(body), 0)
        cache_control = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual("no-cache, must-revalidate", cache_control)
        etag = headers.get("ETag") or headers.get("etag")
        self.assertIsNotNone(etag)
        self.assertTrue(etag.startswith('"') and etag.endswith('"'), etag)

    def test_static_asset_returns_304_when_etag_matches(self) -> None:
        server = self._start_server()
        # First request: capture the ETag.
        _, _, headers = _http_request_raw(self._base_url(server) + "/static/styles.css")
        etag = headers.get("ETag") or headers.get("etag")
        self.assertIsNotNone(etag)
        # Second request with If-None-Match should short-circuit to 304.
        status, body, headers2 = _http_request_raw(
            self._base_url(server) + "/static/styles.css",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(304, status)
        self.assertEqual(b"", body)
        echo = headers2.get("ETag") or headers2.get("etag")
        self.assertEqual(etag, echo)

    def test_static_asset_serves_full_body_on_stale_etag(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request_raw(
            self._base_url(server) + "/static/styles.css",
            headers={"If-None-Match": '"stale-etag"'},
        )
        self.assertEqual(200, status)
        self.assertGreater(len(body), 0)

    def test_root_html_keeps_no_store_cache_control(self) -> None:
        # Regression: the HTML shell must never be cached so a server
        # upgrade is not masked by stale UI.
        server = self._start_server()
        _, _, headers = _http_request_raw(self._base_url(server) + "/")
        cache_control = headers.get("Cache-Control") or headers.get("cache-control")
        self.assertEqual("no-store", cache_control)
        self.assertIsNone(headers.get("ETag") or headers.get("etag"))

    # -- Disappearing-file races -------------------------------------------

    def test_logs_endpoint_returns_404_when_file_disappears_mid_read(self) -> None:
        log_path = self.plan_state_dir / "stdout.log"
        log_path.write_text("hello world\n", encoding="utf-8")
        self._write_state_with_turn(stdout_path=log_path)
        server = self._start_server()
        canonical_log = plans_files.canonicalize_path(log_path)
        original_open = workbench_details_logs._open_regular_file

        def race_open(self_path: Path, *args, **kwargs):
            if plans_files.canonicalize_path(self_path) == canonical_log:
                self_path.unlink()
            return original_open(self_path, *args, **kwargs)

        query = urllib.parse.urlencode(
            {"plan_dir": str(self.canonical_plan_dir), "stream": "stdout"}
        )
        with mock.patch.object(workbench_details_logs, "_open_regular_file", race_open):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/logs?{query}"
            )
        self.assertEqual(404, status)
        self.assertFalse(log_path.exists())
        self.assertFalse(body["ok"])
        self.assertEqual("not-found", body["reason"])

    def test_last_message_endpoint_returns_404_when_file_disappears(self) -> None:
        last_msg_path = self.plan_state_dir / "last-message.txt"
        last_msg_path.write_text("paused", encoding="utf-8")
        state_path = self.plan_state_dir / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "active_turn": {
                        "run_id": "run-lm",
                        "turn_index": 1,
                        "session_id": "1",
                        "last_message_path": str(last_msg_path),
                    },
                    "active_verification": None,
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        canonical_msg = plans_files.canonicalize_path(last_msg_path)
        original_open = workbench_details_logs._open_regular_file

        def race_open(self_path: Path, *args, **kwargs):
            if plans_files.canonicalize_path(self_path) == canonical_msg:
                self_path.unlink()
            return original_open(self_path, *args, **kwargs)

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        with mock.patch.object(workbench_details_logs, "_open_regular_file", race_open):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/last-message?{query}"
            )
        self.assertEqual(404, status)
        self.assertFalse(last_msg_path.exists())
        self.assertEqual("not-found", body["reason"])

    def test_state_endpoint_handles_state_file_disappearing_mid_read(self) -> None:
        state_path = self.plan_state_dir / "state.json"
        state_path.write_text(
            json.dumps({"run_id": "run-x", "active_turn": None}),
            encoding="utf-8",
        )
        server = self._start_server()
        canonical_state = plans_files.canonicalize_path(state_path)
        original_open = Path.open

        def race_open(self_path: Path, *args, **kwargs):
            if plans_files.canonicalize_path(self_path) == canonical_state:
                raise FileNotFoundError(
                    errno.ENOENT, "vanished", os.fspath(canonical_state)
                )
            return original_open(self_path, *args, **kwargs)

        query = urllib.parse.urlencode({"plan_dir": str(self.canonical_plan_dir)})
        with mock.patch.object(Path, "open", race_open):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/state?{query}"
            )
        self.assertEqual(200, status)
        self.assertFalse(body["state"]["present"])
        self.assertIsNone(body["state"]["error"])

    # -- Concurrent registry updates ---------------------------------------

    def test_concurrent_pin_updates_do_not_lose_entries(self) -> None:
        server = self._start_server()
        # Pre-create unique plan dirs so each pin canonicalizes cleanly.
        plan_dirs: list[Path] = []
        for index in range(6):
            plan_dir = self.tmp_path / "plans" / f"concurrent-{index}"
            write_plan(plan_dir, ("pending",))
            plan_dirs.append(plans_files.canonicalize_path(plan_dir))

        url = f"{self._base_url(server)}/api/registry/pin"
        auth = {"Authorization": f"Bearer {server.token}"}
        errors: list[str] = []

        def pin(target: Path) -> None:
            try:
                status, body, _ = _http_request(
                    url,
                    method="POST",
                    body={"plan_dir": str(target), "label": target.name},
                    headers=auth,
                )
                if status != 200 or not body.get("ok"):
                    errors.append(f"{target.name}: {status} {body!r}")
            except Exception as error:
                errors.append(f"{target.name}: {error!r}")

        threads = [threading.Thread(target=pin, args=(target,)) for target in plan_dirs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

        registry = workbench_registry.load_registry(self.registry_path)
        persisted_plan_dirs = {pin.plan_dir for pin in registry.pins}
        for target in plan_dirs:
            self.assertIn(str(target), persisted_plan_dirs)

    # -- Lock-aware control surface ---------------------------------------

    def test_unlock_endpoint_removes_stale_lock(self) -> None:
        # Seed a stale lock owned by a non-existent pid so unlock can
        # remove it without --force.
        lock_path = state.lock_path_for_plan(self.canonical_plan_dir, self.state_dir)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": "test",
                    "pid": 999_999,
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers={"Authorization": f"Bearer {server.token}"},
        )
        self.assertEqual(200, status, msg=body)
        self.assertTrue(body["had_lock"])
        self.assertTrue(body["removed"])
        self.assertTrue(body["stale"])
        self.assertFalse(lock_path.exists())

    # -- Shutdown / browser-open wiring -----------------------------------

    def test_install_shutdown_handlers_routes_signals_to_server_shutdown(
        self,
    ) -> None:
        # Validate the handler installation logic by inspecting captured
        # signal.signal calls rather than actually sending signals.
        server = self._start_server()
        captured: dict[int, object] = {}
        previous_handlers: dict[int, object] = {}

        original_signal = signal.signal

        def recording_signal(signum, handler):
            previous = original_signal(signum, signal.SIG_DFL)
            previous_handlers[signum] = previous
            captured[signum] = handler
            return previous

        try:
            # Patched where the behaviour lives rather than where it is named:
            # the installer moved to the shared transport so the Pi's runtime
            # gets the same SIGTERM, and `workbench_server` keeps the historic
            # private name pointing at it.
            with mock.patch(
                "asl_workbench.server.signal.signal",
                side_effect=recording_signal,
            ):
                restore = workbench_server._install_shutdown_handlers(server)
            self.assertIn(signal.SIGINT, captured)
            self.assertIn(signal.SIGTERM, captured)
            # Invoke the captured SIGTERM handler and confirm it triggers
            # ``server.shutdown()`` from a daemon thread; the call must
            # not raise.
            shutdown_called = threading.Event()
            original_shutdown = server.shutdown

            def fake_shutdown() -> None:
                shutdown_called.set()

            server.shutdown = fake_shutdown  # type: ignore[assignment]
            try:
                captured[signal.SIGTERM](signal.SIGTERM, None)
                self.assertTrue(shutdown_called.wait(timeout=2.0))
            finally:
                server.shutdown = original_shutdown  # type: ignore[assignment]
            # restore() should return the original handlers without raising.
            with mock.patch(
                "asl_workbench.server.signal.signal",
                side_effect=original_signal,
            ):
                restore()
        finally:
            # Defensive: ensure default handlers come back even if the
            # test failed mid-way.
            for signum, previous in previous_handlers.items():
                with suppress(ValueError, OSError):
                    original_signal(signum, previous)

    def test_serve_from_args_invokes_open_browser_with_token_url(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(url: str) -> bool:
            captured["url"] = url
            return True

        def fake_serve(server: workbench_server.WorkbenchHTTPServer) -> None:
            captured["bound_port"] = server.bound_port

        def fake_install(server: workbench_server.WorkbenchHTTPServer):
            captured["installed"] = True

            def restore() -> None:
                captured["restored"] = True

            return restore

        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=True,
            json=True,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=fake_open,
                serve_forever=fake_serve,
                install_signal_handlers=fake_install,
            )
        self.assertEqual(0, result)
        url = captured.get("url")
        self.assertIsInstance(url, str)
        self.assertIn("token=", url)
        self.assertIn("127.0.0.1", url)
        self.assertIsNotNone(captured.get("bound_port"))
        self.assertTrue(captured.get("installed"))
        self.assertTrue(captured.get("restored"))

    def test_serve_from_args_skips_open_when_flag_unset(self) -> None:
        captured: dict[str, object] = {}

        def fake_open(url: str) -> bool:
            captured["url"] = url
            return True

        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=False,
            json=True,
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=fake_open,
                serve_forever=lambda srv: None,
                install_signal_handlers=lambda srv: lambda: None,
            )
        self.assertEqual(0, result)
        self.assertNotIn("url", captured)

    def test_serve_from_args_closes_server_even_on_keyboard_interrupt(self) -> None:
        # ``serve_forever`` raising KeyboardInterrupt is the operator's
        # Ctrl+C path. The CLI must still release the listening socket.
        bound_port_holder: dict[str, int] = {}

        def fake_serve(server: workbench_server.WorkbenchHTTPServer) -> None:
            bound_port_holder["port"] = server.bound_port
            raise KeyboardInterrupt

        args = argparse.Namespace(
            host="127.0.0.1",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=False,
            json=True,
        )
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=lambda url: True,
                serve_forever=fake_serve,
                install_signal_handlers=lambda srv: lambda: None,
            )
        self.assertEqual(0, result)
        # The socket should be releasable now; rebinding to the same
        # ephemeral port should not collide because server_close() ran.
        # (We cannot rebind the exact port reliably across platforms;
        # asserting result==0 confirms the finally branch executed.)
        self.assertIn("port", bound_port_holder)

    def test_serve_from_args_refuses_a_non_loopback_host(self) -> None:
        # Local reads need no token, so the bind address is the access control:
        # a LAN-reachable interface would serve every plan to any peer.
        served: list[object] = []
        args = argparse.Namespace(
            host="0.0.0.0",
            port=0,
            state_dir=str(self.state_dir),
            plan_root=None,
            open=False,
            json=True,
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            result = workbench_server.workbench_serve_from_args(
                args,
                open_browser=lambda url: True,
                serve_forever=served.append,
                install_signal_handlers=lambda srv: lambda: None,
            )
        self.assertEqual(1, result)
        self.assertEqual([], served)
        self.assertEqual("non-loopback-host", json.loads(stdout.getvalue())["reason"])

    def test_the_server_itself_refuses_a_non_loopback_host(self) -> None:
        # Not only the CLI: any caller that builds a server gets the same rule,
        # and it is refused before a socket is bound.
        for host in ("0.0.0.0", "::", "192.168.1.5"):
            with self.subTest(host=host):
                with (
                    mock.patch.object(
                        workbench_server.WorkbenchHTTPServer, "server_bind"
                    ) as bind,
                    self.assertRaises(ValueError),
                ):
                    workbench_server.create_server(
                        host=host, port=0, state_dir=self.state_dir
                    )
                bind.assert_not_called()

    def test_loopback_hosts_are_recognised(self) -> None:
        for host in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"):
            with self.subTest(host=host):
                self.assertTrue(workbench_server.is_loopback_host(host))
        for host in ("0.0.0.0", "::", "192.168.1.5", "example.com", ""):
            with self.subTest(host=host):
                self.assertFalse(workbench_server.is_loopback_host(host))


if __name__ == "__main__":
    unittest.main()
