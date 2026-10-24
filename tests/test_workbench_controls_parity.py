"""SurfaceParityTests coverage."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    bridge_protocol,
    control_cli,
    intervention,
    redirect_admission,
    redirect_cli,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_vocab,
    workbench_controls,
)
from agent_session_loop.bridge_session import BridgeSession
from asl_workbench import controls
from tests.workbench_controls_support import (
    DEAD_PID,
    EDITED_FEEDBACK,
    FEEDBACK,
    OPERATION_ID,
    RECOVERY_PAUSE_REASON,
    ControlsTestCase,
)

ASL_TEST_LANE = "integration"


class SurfaceParityTests(ControlsTestCase):
    """The browser and the connector reach the same queue and the same journal."""

    def http_redirect(self, **overrides: Any) -> tuple[int, dict, dict]:
        body: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": OPERATION_ID,
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        body.update(overrides)
        return self.post("/api/control/redirect", body)

    def bridge_redirect(
        self, session: BridgeSession | None = None, **overrides: Any
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "plan_dir": str(self.plan_dir),
            "operation_id": OPERATION_ID,
            "action_token": self.action_token(),
            "text": FEEDBACK,
            "wait_seconds": 0.05,
        }
        params.update(overrides)
        return self.frame(
            self.handshaken() if session is None else session,
            intervention.ACTION_REDIRECT,
            params,
            request_id="req-redirect",
        )

    def test_both_surfaces_queue_the_same_stop_command(self) -> None:
        self.live_run()

        status, body, _ = self.post(
            "/api/control/stop-after-current",
            {"plan_dir": str(self.plan_dir), "action_token": self.action_token()},
        )
        self.assertEqual(202, status, body)
        http_command = self.queued().accepted[0].command

        self.frame(
            self.handshaken(),
            intervention.ACTION_STOP_AFTER_CURRENT,
            {
                "plan_dir": str(self.plan_dir),
                "operation_id": "amx-op-stop",
                "action_token": self.action_token(),
            },
            request_id="req-stop",
        )
        bridge_command = self.queued().accepted[0].command

        self.assertEqual(http_command, bridge_command)
        self.assertEqual(
            controls.queue_command_for(controls.CONTROL_STOP_AFTER_CURRENT),
            bridge_command,
        )

    def test_both_surfaces_report_the_same_abort_note(self) -> None:
        self.live_run()

        status, body, _ = self.post(
            "/api/control/abort",
            {"plan_dir": str(self.plan_dir), "action_token": self.action_token()},
        )
        self.assertEqual(202, status, body)

        activity = workbench_controls.inspect_activity(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(activity.note, body["note"])
        self.assertEqual(control_cli.ABORT_WILL_APPLY_NOTE, body["note"])

    def test_a_correction_submitted_in_the_browser_replays_for_the_connector(
        self,
    ) -> None:
        """The journal is the receipt, so a client that reconnects to the other
        surface entirely still learns what became of what it submitted.
        """
        self.live_run()
        self.accept_when_pending()
        status, body, _ = self.http_redirect(wait_seconds=5.0)
        self.assertEqual(202, status, body)

        response = self.bridge_redirect()

        result = response["result"]
        self.assertTrue(result["replayed"])
        self.assertEqual(OPERATION_ID, result["operation_id"])
        self.assertEqual(1, len(self.journal().requests))

    def test_an_edited_retry_is_refused_by_both_with_the_same_durable_reason(
        self,
    ) -> None:
        self.live_run()
        self.accept_when_pending()
        self.assertEqual(202, self.http_redirect(wait_seconds=5.0)[0])

        http_status, http_body, _ = self.http_redirect(text=EDITED_FEEDBACK)
        bridge_response = self.bridge_redirect(text=EDITED_FEEDBACK)

        self.assertEqual(409, http_status)
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT, http_body["reason"]
        )
        self.assertEqual(
            redirect_store_vocab.ERROR_REPLAY_CONFLICT,
            bridge_response["error"]["details"]["reason"],
        )
        self.assertEqual(
            redirect_store_vocab.CONFLICT_FEEDBACK,
            bridge_response["error"]["details"]["conflict"],
        )
        self.assertEqual(1, len(self.journal().requests))

    def test_a_wait_above_the_ceiling_is_refused_by_both(self) -> None:
        self.live_run()
        over = redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS + 1

        http_status, _, _ = self.http_redirect(wait_seconds=over)
        bridge_response = self.bridge_redirect(wait_seconds=over)

        self.assertEqual(400, http_status)
        self.assertEqual(
            bridge_protocol.ERROR_INVALID_REQUEST, bridge_response["error"]["code"]
        )
        self.assertEqual((), self.journal().requests)

    def test_both_reads_report_an_interrupted_correction_at_the_same_stage(
        self,
    ) -> None:
        self.live_run()
        self.accept_when_pending()
        self.assertEqual(202, self.http_redirect(wait_seconds=5.0)[0])
        redirect_store_carry.carry_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.deliver_request(
            self.plan_dir,
            OPERATION_ID,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=DEAD_PID)

        status, http_body, _ = self.get(
            f"/api/redirect-status?plan_dir={self.plan_dir}"
        )
        bridge_body = self.frame(
            self.bridge_session(),
            bridge_protocol.METHOD_REDIRECT_STATUS,
            {"plan_dir": str(self.plan_dir)},
            request_id="req-status",
        )["result"]

        self.assertEqual(200, status, http_body)
        self.assertEqual(
            redirect_cli.STAGE_UNCERTAIN, http_body["operations"][0]["stage"]
        )
        self.assertEqual(
            redirect_cli.STAGE_UNCERTAIN, bridge_body["operations"][0]["stage"]
        )
        self.assertEqual([OPERATION_ID], bridge_body["uncertain_operation_ids"])

    def test_both_recovery_reads_name_the_same_decision_and_carries(self) -> None:
        self.live_run()
        self.submit()
        self.write_state(paused=True, pause_reason=RECOVERY_PAUSE_REASON)
        self.write_lock(pid=DEAD_PID, redirect_endpoint=None)

        status, http_body, _ = self.get(
            f"/api/conversation-recovery?plan_dir={self.plan_dir}"
        )
        bridge_body = self.frame(
            self.bridge_session(),
            bridge_protocol.METHOD_RECOVER_CONVERSATION,
            {"plan_dir": str(self.plan_dir)},
            request_id="req-recovery",
        )["result"]

        self.assertEqual(200, status, http_body)
        self.assertEqual(http_body["decision"], bridge_body["decision"])
        self.assertEqual(http_body["one_use"], bridge_body["one_use"])
        self.assertEqual(
            [OPERATION_ID],
            [item["operation_id"] for item in bridge_body["retained_operations"]],
        )


if __name__ == "__main__":
    unittest.main()
