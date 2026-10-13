"""Workbench server tests: launch and reply endpoints.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import json
from unittest import mock

from agent_session_loop import (
    workbench_launch,
    workbench_registry_models,
)
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
)


class WorkbenchServerLaunchReplyTests(WorkbenchServerTestCase):
    """Launch and reply endpoints."""

    def test_launch_endpoint_uses_workbench_helper(self) -> None:
        server = self._start_server()
        sentinel = workbench_launch.LaunchResult(
            pid=4242,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=False,
            reply_length=None,
            reply_origin=None,
            state_dir=self.state_dir,
        )
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=sentinel
        ) as patched:
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/launch",
                method="POST",
                body={"plan_dir": str(self.canonical_plan_dir)},
                headers=self._auth(server),
            )
        self.assertEqual(200, status)
        self.assertEqual(4242, body["launch"]["pid"])
        patched.assert_called_once()
        kwargs = patched.call_args.kwargs
        self.assertEqual(self.state_dir, kwargs["state_dir"])
        self.assertIsNone(kwargs["profile"])

    def test_launch_endpoint_maps_invalid_plan_error(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/launch",
            method="POST",
            body={"plan_dir": str(self.tmp_path / "does-not-exist")},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual(workbench_launch.LAUNCH_REASON_INVALID_PLAN, body["reason"])

    def test_reply_endpoint_invokes_resume_helper(self) -> None:
        server = self._start_server()
        sentinel = workbench_launch.LaunchResult(
            pid=99,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=True,
            reply_length=5,
            reply_origin="stdin",
            state_dir=self.state_dir,
        )
        with mock.patch.object(
            workbench_launch, "resume_plan_with_reply", return_value=sentinel
        ) as patched:
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/reply",
                method="POST",
                body={
                    "plan_dir": str(self.canonical_plan_dir),
                    "reply_text": "go-ok",
                },
                headers=self._auth(server),
            )
        self.assertEqual(200, status)
        self.assertEqual(99, body["launch"]["pid"])
        self.assertTrue(body["launch"]["reply_provided"])
        kwargs = patched.call_args.kwargs
        self.assertEqual("go-ok", kwargs["reply_text"])

    def test_reply_endpoint_requires_reply_text(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/reply",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("reply_text", body["error"])

    def test_reply_response_never_echoes_reply_text(self) -> None:
        # The reply endpoint must never persist or echo the operator's
        # answer text; only the length + origin marker are surfaced.
        server = self._start_server()
        sensitive_reply = "TOKEN-XYZ-must-not-leak"
        sentinel = workbench_launch.LaunchResult(
            pid=77,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical_plan_dir,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=True,
            reply_length=len(sensitive_reply),
            reply_origin="stdin",
            state_dir=self.state_dir,
        )
        with mock.patch.object(
            workbench_launch, "resume_plan_with_reply", return_value=sentinel
        ):
            status, body, _ = _http_request(
                f"{self._base_url(server)}/api/reply",
                method="POST",
                body={
                    "plan_dir": str(self.canonical_plan_dir),
                    "reply_text": sensitive_reply,
                },
                headers=self._auth(server),
            )

        self.assertEqual(200, status)
        encoded = json.dumps(body)
        self.assertNotIn(
            sensitive_reply,
            encoded,
            msg="reply text leaked into the response body",
        )
        self.assertTrue(body["launch"]["reply_provided"])
        self.assertEqual(len(sensitive_reply), body["launch"]["reply_length"])
        self.assertEqual("stdin", body["launch"]["reply_origin"])
