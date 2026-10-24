"""Workbench server tests: control command and unlock endpoints.

Split from tests/test_asl_workbench_server.py."""

from __future__ import annotations

import json
import os

from agent_session_loop import (
    control_cli,
    control_queue,
    events,
    state,
)
from tests.asl_workbench_server_support import (
    WorkbenchServerTestCase,
)
from tests.support import (
    _http_request,
    _write_lock_file,
)

ASL_TEST_LANE = "integration"


class WorkbenchServerControlTests(WorkbenchServerTestCase):
    """Control command and unlock endpoints."""

    def test_control_stop_after_current_enqueues_command(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/stop-after-current",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertTrue(body["ok"])
        queued = body["queued_command"]
        self.assertEqual(
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            queued["command"],
        )
        queue_path = control_queue.queue_path_for_plan(
            self.canonical_plan_dir, self.state_dir
        )
        self.assertTrue(queue_path.is_file())
        lines = queue_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(1, len(lines))
        record = json.loads(lines[0])
        self.assertEqual(queued["id"], record["id"])

    def test_control_abort_enqueues_command_with_cli_payload_shape(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertTrue(body["ok"])
        self.assertFalse(body["run_active"])
        self.assertFalse(body["turn_active"])
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS, body["grace_seconds"]
        )
        self.assertIsNone(body["abort_reason"])
        self.assertEqual(control_cli.NO_ACTIVE_RUN_NOTE, body["note"])
        queued = body["queued_command"]
        self.assertEqual(control_queue.COMMAND_REQUEST_ABORT_TURN, queued["command"])
        queue_path = control_queue.queue_path_for_plan(
            self.canonical_plan_dir, self.state_dir
        )
        record = json.loads(queue_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(queued["id"], record["id"])
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS, record["grace_seconds"]
        )

    def test_control_abort_accepts_grace_and_reason(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "grace_seconds": 30,
                "reason": "supervisor stop",
            },
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertEqual(30, body["grace_seconds"])
        self.assertEqual("supervisor stop", body["abort_reason"])
        self.assertEqual(30, body["queued_command"]["grace_seconds"])
        self.assertEqual("supervisor stop", body["queued_command"]["reason"])

    def test_control_abort_reports_active_turn(self) -> None:
        _write_lock_file(self.canonical_plan_dir, self.state_dir, pid=os.getpid())
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(
                {
                    "plan_dir": str(self.canonical_plan_dir),
                    "active_turn": {"session": "1", "turn_index": 1},
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(202, status)
        self.assertTrue(body["run_active"])
        self.assertTrue(body["turn_active"])
        self.assertEqual(control_cli.ABORT_WILL_APPLY_NOTE, body["note"])

    def test_control_abort_degrades_note_without_active_turn(self) -> None:
        _write_lock_file(self.canonical_plan_dir, self.state_dir, pid=os.getpid())
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(202, status)
        self.assertTrue(body["run_active"])
        self.assertFalse(body["turn_active"])
        self.assertEqual(control_cli.ABORT_DEGRADES_NOTE, body["note"])

    def test_control_abort_sees_run_behind_symlinked_plan_path(self) -> None:
        # The lock records the canonical plan directory, so an uncanonical
        # request path must not make an active run look absent.
        link_root = self.tmp_path / "link"
        link_root.symlink_to(self.tmp_path / "plans", target_is_directory=True)
        linked_plan_dir = link_root / "alpha"
        _write_lock_file(self.canonical_plan_dir, self.state_dir, pid=os.getpid())
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(linked_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(202, status)
        self.assertTrue(body["run_active"])
        self.assertEqual(control_cli.ABORT_DEGRADES_NOTE, body["note"])

    def test_control_abort_rejects_grace_the_cli_rejects(self) -> None:
        # Same bounds and the same stable reasons the CLI reports, because
        # both surfaces validate through control_queue.append_command.
        server = self._start_server()
        for grace in (
            0,
            control_queue.MAX_ABORT_GRACE_SECONDS + 1,
            "120",
            True,
        ):
            with self.subTest(grace=grace):
                status, body, _ = _http_request(
                    f"{self._base_url(server)}/api/control/abort",
                    method="POST",
                    body={
                        "plan_dir": str(self.canonical_plan_dir),
                        "grace_seconds": grace,
                    },
                    headers=self._auth(server),
                )
                self.assertEqual(400, status)
                self.assertEqual(control_queue.REJECT_INVALID_GRACE, body["reason"])
        queue_path = control_queue.queue_path_for_plan(
            self.canonical_plan_dir, self.state_dir
        )
        self.assertFalse(queue_path.exists())

    def test_control_abort_rejects_empty_reason(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir), "reason": ""},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual(control_queue.REJECT_INVALID_REASON, body["reason"])

    def test_control_abort_requires_plan_dir(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/abort",
            method="POST",
            body={},
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-request", body["reason"])
        self.assertIn("plan_dir", body["error"])

    def test_control_select_harness_validates_name(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/select-harness",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "harness": "bogus",
            },
            headers=self._auth(server),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid-harness", body["reason"])

    def test_control_select_harness_accepts_supported_name(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/control/select-harness",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "harness": "codex",
            },
            headers=self._auth(server),
        )
        self.assertEqual(202, status)
        self.assertEqual(
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            body["queued_command"]["command"],
        )
        self.assertEqual("codex", body["queued_command"]["harness"])

    def test_unlock_rejects_active_lock_with_409(self) -> None:
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-06-24T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )

        self.assertEqual(409, status)
        self.assertEqual("active-lock", body["reason"])
        self.assertTrue(lock_path.exists())

    def test_unlock_force_removes_active_lock(self) -> None:
        plan_state = state.plan_state_dir(self.canonical_plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.canonical_plan_dir),
                    "created_at": "2026-06-24T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        server = self._start_server()

        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={
                "plan_dir": str(self.canonical_plan_dir),
                "force": True,
            },
            headers=self._auth(server),
        )

        self.assertEqual(200, status)
        self.assertTrue(body["removed"])
        self.assertFalse(lock_path.exists())

    def test_unlock_idempotent_when_no_lock(self) -> None:
        server = self._start_server()
        status, body, _ = _http_request(
            f"{self._base_url(server)}/api/unlock",
            method="POST",
            body={"plan_dir": str(self.canonical_plan_dir)},
            headers=self._auth(server),
        )
        self.assertEqual(200, status)
        self.assertFalse(body["had_lock"])
        self.assertFalse(body["removed"])
