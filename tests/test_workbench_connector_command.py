"""The HTTPS channel and the ``asl workbench connect`` command.

Split from tests/test_workbench_connector.py."""

from __future__ import annotations

import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from agent_session_loop import (
    workbench_connector,
)
from asl_workbench import connector_support as portable_support
from tests.workbench_connector_support import (
    CONNECTOR_ID,
    TARGET_ID,
    FakeChannel,
    StubSource,
    cycle_with,
)

ASL_TEST_PORTABLE = True


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]


class _FakeConnection:
    def __init__(self, *, status: int = 202, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[dict[str, Any]] = []
        self.closed = 0

    def request(self, method: str, path: str, *, body: bytes, headers: Any) -> None:
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": dict(headers)}
        )

    def getresponse(self) -> _FakeResponse:
        return _FakeResponse(self.status, self.body)

    def close(self) -> None:
        self.closed += 1


class HttpsChannelTests(unittest.TestCase):
    """One bounded POST per frame, and nothing clever."""

    def channel(self, connection: Any) -> workbench_connector.HttpsChannel:
        return workbench_connector.HttpsChannel(
            origin="https://workbench.example",
            token="secret-token",
            connect=lambda: connection,
        )

    def test_a_frame_is_posted_to_the_ingest_route_with_a_bearer_token(self) -> None:
        connection = _FakeConnection()
        self.channel(connection).send(b'{"kind":"observation"}')
        sent = connection.requests[0]
        self.assertEqual("POST", sent["method"])
        self.assertEqual(workbench_connector.INGEST_PATH, sent["path"])
        self.assertEqual("Bearer secret-token", sent["headers"]["Authorization"])
        self.assertEqual("application/json", sent["headers"]["Content-Type"])
        self.assertEqual("22", sent["headers"]["Content-Length"])
        self.assertEqual(1, connection.closed)

    def test_both_accepted_statuses_are_accepted(self) -> None:
        for status in workbench_connector.ACCEPTED_STATUSES:
            self.channel(_FakeConnection(status=status)).send(b"{}")

    def test_a_refused_status_is_a_channel_error(self) -> None:
        for status in (400, 401, 409, 500):
            with self.assertRaises(portable_support.ChannelError):
                self.channel(_FakeConnection(status=status)).send(b"{}")

    def test_a_connection_that_cannot_be_made_is_a_channel_error(self) -> None:
        channel = workbench_connector.HttpsChannel(
            origin="https://workbench.example",
            token="t",
            connect=lambda: (_ for _ in ()).throw(OSError("no route to host")),
        )
        with self.assertRaises(portable_support.ChannelError):
            channel.send(b"{}")

    def test_a_request_that_fails_mid_flight_closes_the_connection(self) -> None:
        connection = _FakeConnection()
        connection.request = mock.Mock(  # type: ignore[method-assign]
            side_effect=OSError("broken pipe")
        )
        with self.assertRaises(portable_support.ChannelError):
            self.channel(connection).send(b"{}")
        self.assertEqual(1, connection.closed)

    def test_an_oversized_response_is_a_channel_error(self) -> None:
        from asl_workbench.transport import MAX_FRAME_BYTES

        connection = _FakeConnection(body=b"x" * (MAX_FRAME_BYTES + 10))
        with self.assertRaises(portable_support.ChannelError):
            self.channel(connection).send(b"{}")

    def test_an_origin_that_is_not_https_is_refused_at_construction(self) -> None:
        for origin in ("http://workbench.example", "https://", "not a url", ""):
            with self.assertRaises(workbench_connector.ConnectorSetupError):
                workbench_connector.HttpsChannel(origin=origin, token="t")

    def test_an_explicit_port_is_honoured(self) -> None:
        channel = workbench_connector.HttpsChannel(
            origin="https://workbench.example:8443", token="t"
        )
        self.assertEqual(8443, channel.port)
        self.assertEqual(
            443,
            workbench_connector.HttpsChannel(
                origin="https://workbench.example", token="t"
            ).port,
        )


# -- The command ------------------------------------------------------------


class _Args:
    def __init__(self, **fields: Any) -> None:
        defaults: dict[str, Any] = {
            "target_id": TARGET_ID,
            "origin": "https://workbench.example",
            "token_file": None,
            "connector_id": CONNECTOR_ID,
            "state_dir": None,
            "plan_root": None,
            "heartbeat_interval": None,
            "observation_interval": None,
            "json": True,
        }
        defaults.update(fields)
        for name, value in defaults.items():
            setattr(self, name, value)


class CommandTests(unittest.TestCase):
    """``asl workbench connect`` from parsed arguments, with no socket."""

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.state_dir = Path(self._temporary.name) / "state"
        self.state_dir.mkdir(parents=True)
        self.channel = FakeChannel()
        self.source = StubSource([cycle_with()])

    def run_command(self, **overrides: Any) -> tuple[int, dict[str, Any]]:
        args = _Args(state_dir=str(self.state_dir), **overrides)
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=lambda connector, stop: None,
                install_signal_handlers=lambda stop: lambda: None,
            )
        text = stream.getvalue().strip()
        return code, json.loads(text) if text else {}

    def test_a_start_reports_its_identity_capabilities_and_lock(self) -> None:
        code, payload = self.run_command()
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("connecting", payload["reason"])
        self.assertEqual(CONNECTOR_ID, payload["connector_id"])
        self.assertEqual(TARGET_ID, payload["target_id"])
        self.assertEqual(
            list(portable_support.CONNECTOR_CAPABILITIES), payload["capabilities"]
        )
        self.assertEqual(
            str(workbench_connector.connector_lock_path(self.state_dir)),
            payload["lock"],
        )

    def test_the_lock_is_released_when_the_command_returns(self) -> None:
        self.run_command()
        self.assertFalse(
            workbench_connector.connector_lock_path(self.state_dir).exists()
        )

    def test_a_second_connector_is_refused_rather_than_publishing_a_rival_view(
        self,
    ) -> None:
        lock = portable_support.SingleInstanceLock(
            workbench_connector.connector_lock_path(self.state_dir)
        )
        lock.acquire()
        self.addCleanup(lock.release)
        code, payload = self.run_command()
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("connector-locked", payload["reason"])

    def test_a_missing_target_is_a_setup_refusal_that_leaves_no_lock(self) -> None:
        code, payload = self.run_command(target_id=None)
        self.assertEqual(1, code)
        self.assertEqual("setup-failed", payload["reason"])
        self.assertFalse(
            workbench_connector.connector_lock_path(self.state_dir).exists()
        )

    def test_the_publication_loop_receives_the_connector_and_a_stop_event(self) -> None:
        seen: dict[str, Any] = {}

        def publish(connector: Any, stop: Any) -> None:
            seen["connector"] = connector
            seen["stop"] = stop

        args = _Args(state_dir=str(self.state_dir))
        with redirect_stdout(io.StringIO()):
            workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=publish,
                install_signal_handlers=lambda stop: lambda: None,
            )
        self.assertEqual(CONNECTOR_ID, seen["connector"].connector_id)
        self.assertIsInstance(seen["stop"], threading.Event)
        self.assertEqual(portable_support.STATE_STOPPED, seen["connector"].state)

    def test_an_interrupt_during_the_loop_is_a_clean_exit(self) -> None:
        args = _Args(state_dir=str(self.state_dir))

        def publish(connector: Any, stop: Any) -> None:
            raise KeyboardInterrupt

        with redirect_stdout(io.StringIO()):
            code = workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=publish,
                install_signal_handlers=lambda stop: lambda: None,
            )
        self.assertEqual(0, code)
        self.assertFalse(
            workbench_connector.connector_lock_path(self.state_dir).exists()
        )

    def test_the_signal_handlers_are_restored_on_the_way_out(self) -> None:
        restored: list[bool] = []
        args = _Args(state_dir=str(self.state_dir))
        with redirect_stdout(io.StringIO()):
            workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=lambda connector, stop: None,
                install_signal_handlers=lambda stop: lambda: restored.append(True),
            )
        self.assertEqual([True], restored)

    def test_the_human_form_prints_a_line_instead_of_a_payload(self) -> None:
        args = _Args(state_dir=str(self.state_dir), json=False)
        stream = io.StringIO()
        with redirect_stdout(stream):
            workbench_connector.workbench_connect_from_args(
                args,
                channel=self.channel,
                source=self.source,
                publish=lambda connector, stop: None,
                install_signal_handlers=lambda stop: lambda: None,
            )
        self.assertIn(CONNECTOR_ID, stream.getvalue())
        self.assertIn(TARGET_ID, stream.getvalue())

    def test_the_intervals_the_operator_asked_for_are_the_ones_used(self) -> None:
        connector = workbench_connector.build_connector(
            _Args(heartbeat_interval=5.0, observation_interval=90.0),
            channel=self.channel,
            source=self.source,
        )
        self.assertEqual(5.0, connector._heartbeat_interval)
        self.assertEqual(90.0, connector._observation_interval)

    def test_a_non_positive_interval_is_a_setup_refusal(self) -> None:
        with self.assertRaises(workbench_connector.ConnectorSetupError):
            workbench_connector.build_connector(
                _Args(heartbeat_interval=-1.0),
                channel=self.channel,
                source=self.source,
            )

    def test_a_channel_needs_an_origin_and_a_token_file(self) -> None:
        for overrides in ({"origin": None}, {"token_file": None}):
            with self.assertRaises(workbench_connector.ConnectorSetupError):
                workbench_connector.build_connector(_Args(**overrides))
