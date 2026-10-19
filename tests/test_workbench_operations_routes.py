"""Launch routes record accountable mutations and respect the plan lock."""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    plans_files,
    workbench_launch,
    workbench_operations,
    workbench_registry_models,
)
from asl_workbench import operations
from tests.support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture


class LaunchRouteTests(WorkbenchAPIFixture, unittest.TestCase):
    """The two spawning routes admit through the journal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))
        self.plan_dir = self.plans_root / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans_files.canonicalize_path(self.plan_dir)
        self.server = self.start_workbench_server()
        self.journal_path = workbench_operations.journal_path_for_plan(
            self.canonical, self.state_dir
        )

    def _sentinel(self, *, pid: int = 4242, reply: bool = False) -> Any:
        return workbench_launch.LaunchResult(
            pid=pid,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical,
            profile_payload=workbench_registry_models.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=5 if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def _post(self, route: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, payload, _ = _http_request(
            f"{self.base_url(self.server)}{route}",
            method="POST",
            body=body,
            headers=self.auth_headers(self.server),
        )
        return status, payload

    def _rows(self) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            self.canonical, state_dir=self.state_dir
        )

    def _hold_the_plan(self) -> None:
        """Make a live run of this test process: a lock and the state it wrote."""
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "paused": False,
                "harness": "claude",
                "run_id": "20260619T140000Z-1",
                "turn_index": 1,
                "run_started_at": "2026-06-19T14:00:00+00:00",
            },
        )
        _write_lock_file(
            self.canonical,
            self.state_dir,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )

    def test_a_launch_is_recorded_and_accounted_for(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._sentinel()
        ):
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual(200, status)
        self.assertEqual(4242, body["launch"]["pid"])
        rows = self._rows()
        self.assertEqual(["launch-1"], [row["operation_id"] for row in rows])
        self.assertEqual(operations.OPERATION_CONSUMED, rows[0]["status"])
        self.assertEqual(operations.METHOD_LAUNCH, rows[0]["method"])
        self.assertIsNotNone(rows[0]["dispatched_at"])

    def test_a_launch_without_an_id_is_still_accounted_for(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._sentinel()
        ):
            status, _ = self._post("/api/launch", {"plan_dir": str(self.canonical)})
        self.assertEqual(200, status)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(operations.OPERATION_CONSUMED, rows[0]["status"])

    def test_a_resent_launch_id_does_not_spawn_twice(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._sentinel()
        ) as patched:
            first_status, first = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
            second_status, second = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual((200, 200), (first_status, second_status))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["launch"], second["launch"])
        patched.assert_called_once()
        self.assertEqual(1, len(self._rows()))

    def test_a_launch_id_reused_for_a_reply_is_refused(self) -> None:
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._sentinel()
        ):
            self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "shared-id"},
            )
        with mock.patch.object(
            workbench_launch,
            "resume_plan_with_reply",
            return_value=self._sentinel(reply=True),
        ) as patched:
            status, body = self._post(
                "/api/reply",
                {
                    "plan_dir": str(self.canonical),
                    "launch_id": "shared-id",
                    "reply_text": "go-ok",
                },
            )
        self.assertEqual(409, status)
        self.assertEqual(operations.REASON_OPERATION_CONFLICT, body["reason"])
        self.assertEqual(operations.CONFLICT_METHOD, body["details"]["conflict"])
        patched.assert_not_called()

    def test_a_refused_launch_is_closed_out_rather_than_left_open(self) -> None:
        error = workbench_launch.LaunchError(
            workbench_launch.LAUNCH_REASON_INVALID_PLAN, "no such plan"
        )
        with mock.patch.object(workbench_launch, "launch_plan", side_effect=error):
            status, _ = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual(400, status)
        rows = self._rows()
        self.assertEqual(operations.OPERATION_REJECTED, rows[0]["status"])
        self.assertEqual(operations.DETAIL_LAUNCH_REFUSED, rows[0]["detail_code"])
        # Closed out, so the next submission is not blocked by a mutation that
        # demonstrably did not happen.
        self.assertEqual(
            [],
            workbench_operations.open_operations(
                self.canonical, state_dir=self.state_dir
            ),
        )

    def test_the_reply_text_never_reaches_the_journal(self) -> None:
        secret = "the-operator-typed-this"
        with mock.patch.object(
            workbench_launch,
            "resume_plan_with_reply",
            return_value=self._sentinel(reply=True),
        ):
            status, _ = self._post(
                "/api/reply",
                {
                    "plan_dir": str(self.canonical),
                    "launch_id": "reply-1",
                    "reply_text": secret,
                },
            )
        self.assertEqual(200, status)
        raw = self.journal_path.read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertIn(b"reply-1", raw)
        for forbidden in operations.FORBIDDEN_PERSISTED_KEYS:
            with self.subTest(field=forbidden):
                self.assertNotIn(f'"{forbidden}"'.encode(), raw)

    def test_the_plan_lock_still_refuses_a_live_run(self) -> None:
        # Admission is a gate in front of the lock, not a replacement: a plan a
        # run already holds is refused by the ownership check, with the reason
        # operators already know.
        self._hold_the_plan()
        with mock.patch.object(
            workbench_launch, "launch_plan", return_value=self._sentinel()
        ) as patched:
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual(409, status)
        self.assertEqual("run-already-active", body["reason"])
        patched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
