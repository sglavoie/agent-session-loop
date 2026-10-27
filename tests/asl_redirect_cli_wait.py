"""Focused portion of tests/test_asl_redirect_cli.py."""

from .asl_redirect_cli_status import *


class AcknowledgmentWaitTests(RedirectCliTestCase):
    """Waiting for a worker receipt, and what a timeout is allowed to mean."""

    def carried(self) -> redirect_store_models.RedirectRequest:
        request = redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )
        return redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

    def test_a_receipt_already_recorded_completes_the_wait(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()
        redirect_store_delivery.acknowledge_request(
            self.plan_dir,
            request.operation_id,
            attempt=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

        code, payload = self.status(
            "--redirect-id",
            request.operation_id,
            "--wait-acknowledged",
            "--timeout",
            "0",
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        wait = payload["acknowledgment_wait"]
        self.assertEqual([request.operation_id], wait["acknowledged"])
        self.assertEqual([], wait["outstanding"])
        self.assertTrue(wait["complete"])

    def test_an_unreceipted_correction_times_out_without_retracting_anything(
        self,
    ) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()

        code, payload = self.status(
            "--redirect-id",
            request.operation_id,
            "--wait-acknowledged",
            "--timeout",
            "0",
        )

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        wait = payload["acknowledgment_wait"]
        self.assertEqual([request.operation_id], wait["outstanding"])
        # The wait is an observation: the correction is still carried, still
        # in the worker's prompt, and still unsettled.
        self.assertTrue(wait["observational"])
        carried = self.journal().request(request.operation_id)
        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, carried.state)
        self.assertTrue(carried.carried)

    def test_the_timeout_text_says_the_worker_still_holds_it(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()

        code, out, err = self.run_cli(
            [
                "control",
                "redirect-status",
                str(self.plan_dir),
                "--redirect-id",
                request.operation_id,
                "--wait-acknowledged",
                "--timeout",
                "0",
            ]
        )

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertEqual("", err)
        self.assertIn("still holds them", out)

    def test_waiting_without_a_live_attempt_is_refused(self) -> None:
        self.carried()

        code, payload = self.status("--wait-acknowledged", "--timeout", "0")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_ack_attempt.ERROR_NO_LIVE_ATTEMPT, payload["reason"])

    def test_a_timeout_without_the_wait_flag_is_a_usage_error(self) -> None:
        self.make_live_run()

        code, payload = self.status("--timeout", "5")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_INVALID_TIMEOUT, payload["reason"])

    def test_an_out_of_range_wait_timeout_is_refused(self) -> None:
        self.make_live_run(turn_index=2)

        code, payload = self.status("--wait-acknowledged", "--timeout", "100000")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_INVALID_TIMEOUT, payload["reason"])

    def test_the_report_is_re_read_after_the_wait(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.carried()
        attempt = self.target(turn_index=2)

        def acknowledge_during_wait(*args, **kwargs):
            redirect_store_delivery.acknowledge_request(
                self.plan_dir,
                request.operation_id,
                attempt=attempt,
                state_dir=self.state_dir,
            )
            return redirect_ack.AcknowledgmentWait(
                attempt=redirect_ack_attempt.attempt_token(attempt),
                waited_seconds=0.5,
                acknowledged=(request.operation_id,),
            )

        with mock.patch.object(
            redirect_ack, "wait_for_acknowledgment", acknowledge_during_wait
        ):
            code, payload = self.status(
                "--redirect-id", request.operation_id, "--wait-acknowledged"
            )

        # The receipt arrived during the wait, so the stage reported has to be
        # the one the receipt produced rather than the one that motivated it.
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(
            redirect_cli.STAGE_ACKNOWLEDGED, payload["operations"][0]["stage"]
        )


class FreshConversationRecoveryTests(RedirectCliTestCase):
    """The one-use override: explained, never performed, never persisted."""

    def pause_on_recovery(self) -> None:
        self.write_state(
            paused=True,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )

    def test_a_recovery_pause_is_reported_with_its_exact_relaunch(self) -> None:
        self.pause_on_recovery()

        code, payload = self.recover()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertTrue(payload["recovery_required"])
        self.assertEqual(
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            payload["pause_reason"],
        )
        command = payload["fresh_conversation_command"]
        self.assertIn("--resume", command)
        self.assertIn("--fresh-conversation", command)
        self.assertIn(str(self.state_dir), command)

    def test_the_command_is_printed_rather_than_run(self) -> None:
        self.pause_on_recovery()
        before = (self.state_dir / "plans").exists()

        code, out, err = self.run_cli(
            ["control", "recover-conversation", str(self.plan_dir)]
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual("", err)
        self.assertIn("--fresh-conversation", out)
        self.assertIn("one-use", out)
        # The ledger flags read as words, not Python reprs.
        self.assertRegex(
            out, r"Conversation ledger: readable (yes|no), trustworthy (yes|no), "
        )
        self.assertNotIn("=True", out)
        self.assertNotIn("=False", out)
        # Nothing was authorized: no run started, no state written.
        self.assertEqual(before, (self.state_dir / "plans").exists())

    def test_the_override_is_never_written_down(self) -> None:
        self.pause_on_recovery()
        state_path = self.plan_state_path()
        before = state_path.read_bytes()

        self.recover()

        self.assertEqual(before, state_path.read_bytes())
        self.assertNotIn("fresh_conversation", before.decode("utf-8"))

    def test_a_plan_that_is_not_paused_still_gets_the_route(self) -> None:
        self.make_live_run()

        code, payload = self.recover()

        # Reported honestly as not required, but the command is still shown:
        # an operator checking ahead of time should not have to guess it.
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertFalse(payload["recovery_required"])
        self.assertIn("--fresh-conversation", payload["fresh_conversation_command"])

    def test_corrections_a_fresh_start_would_carry_are_named(self) -> None:
        self.pause_on_recovery()
        request = redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )

        code, payload = self.recover()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(
            [request.operation_id],
            [operation["operation_id"] for operation in payload["retained_operations"]],
        )

    def test_the_ledger_health_is_reported(self) -> None:
        self.pause_on_recovery()

        _, payload = self.recover()

        ledger = payload["conversations"]
        self.assertIn("readable", ledger)
        self.assertIn("trustworthy", ledger)

    def plan_state_path(self) -> Path:
        from agent_session_loop import state as state_module

        return state_module.plan_state_dir(self.plan_dir, self.state_dir) / "state.json"


class StageProjectionTests(unittest.TestCase):
    """The one derivation this module owns, checked without a filesystem."""

    def outcome(self, **overrides) -> redirect_recovery.OperationOutcome:
        target = redirect_store_models.RedirectTarget(
            run_id=RUN_ID, session_id=SESSION_ID, turn_index=1
        )
        payload = {
            "operation_id": "op-1",
            "status": redirect_recovery.STATUS_ACCEPTED,
            "target": target,
            "journal_state": redirect_store_vocab.STATE_ACCEPTED,
            "feedback_digest": redirect_store_validation.feedback_digest(FEEDBACK),
            "feedback_bytes": len(FEEDBACK.encode("utf-8")),
            "created_at": clock.utc_iso_now(),
            "expires_at": clock.utc_iso_now(),
        }
        payload.update(overrides)
        return redirect_recovery.OperationOutcome(**payload)

    def test_delivered_has_no_stage_of_its_own_and_reads_as_uncertain(self) -> None:
        # No projection emits ``delivered``; if one ever did, it is a carried,
        # unreceipted correction, which is what ``uncertain`` already means.
        stage = redirect_cli.stage_for_operation(
            self.outcome(status=redirect_recovery.STATUS_DELIVERED),
            live_target=None,
        )
        self.assertEqual(redirect_cli.STAGE_UNCERTAIN, stage)

    def test_an_expired_request_reads_as_rejected(self) -> None:
        stage = redirect_cli.stage_for_operation(
            self.outcome(status=redirect_recovery.STATUS_EXPIRED),
            live_target=None,
        )
        self.assertEqual(redirect_cli.STAGE_REJECTED, stage)

    def test_a_different_run_on_the_same_turn_index_is_not_the_same_turn(self) -> None:
        other_run = redirect_store_models.RedirectTarget(
            run_id="run-somebody-else", session_id=SESSION_ID, turn_index=1
        )
        stage = redirect_cli.stage_for_operation(self.outcome(), live_target=other_run)
        self.assertEqual(redirect_cli.STAGE_STOPPED, stage)


if __name__ == "__main__":
    unittest.main()

__all__ = ["io", "json", "os", "socket", "tempfile", "threading", "time", "unittest", "redirect_stderr", "redirect_stdout", "Path", "mock", "asl_cli", "clock", "events", "redirect_ack", "redirect_ack_attempt", "redirect_admission", "redirect_cli", "redirect_recovery", "redirect_store_carry", "redirect_store_delivery", "redirect_store_journal", "redirect_store_models", "redirect_store_requests", "redirect_store_validation", "redirect_store_vocab", "_write_lock_file", "_write_state_payload", "live_redirect_endpoint", "write_plan", "RUN_ID", "SESSION_ID", "FEEDBACK", "RedirectCliTestCase", "WiringTests", "SuccessfulRedirectTests", "UnavailableControlTests", "StaleTargetTests", "RedirectStatusTests", "AcknowledgmentWaitTests", "FreshConversationRecoveryTests", "StageProjectionTests"]  # fmt: skip
