"""Focused portion of tests/test_asl_redirect_cli.py."""

from .asl_redirect_cli_core import *


class StaleTargetTests(RedirectCliTestCase):
    """Naming a turn the run has already left."""

    def test_a_turn_the_run_has_left_is_refused_as_stale(self) -> None:
        self.make_live_run(turn_index=4)

        code, payload = self.redirect("--text", FEEDBACK, "--turn", "3")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_a_stale_session_is_refused_even_when_the_turn_index_matches(self) -> None:
        self.make_live_run(turn_index=2)

        code, payload = self.redirect("--text", FEEDBACK, "--session", "session-8")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_admission.REFUSE_STALE_TARGET, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_naming_the_live_turn_is_accepted(self) -> None:
        self.make_live_run(turn_index=4)
        self.accept_when_pending(self.target(turn_index=4))

        code, payload = self.redirect(
            "--text",
            FEEDBACK,
            "--session",
            SESSION_ID,
            "--turn",
            "4",
            "--timeout",
            "10",
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(4, payload["target"]["turn_index"])
        self.assertEqual(4, payload["expected_target"]["turn_index"])

    def test_an_unstated_expectation_stays_unstated(self) -> None:
        self.make_live_run(turn_index=4)
        self.accept_when_pending(self.target(turn_index=4))

        _, payload = self.redirect("--text", FEEDBACK, "--timeout", "10")

        self.assertIsNone(payload["expected_target"])

    def test_a_partial_expectation_is_filled_from_the_live_turn(self) -> None:
        self.make_live_run(turn_index=4)
        self.accept_when_pending(self.target(turn_index=4))

        _, payload = self.redirect("--text", FEEDBACK, "--turn", "4", "--timeout", "10")

        # Only the turn was stated; the run and session come from the live turn
        # so the check is exactly as narrow as what the operator said.
        self.assertEqual(RUN_ID, payload["expected_target"]["run_id"])
        self.assertEqual(SESSION_ID, payload["expected_target"]["session_id"])


class RedirectStatusTests(RedirectCliTestCase):
    """Looking up corrections without touching them."""

    def submitted(
        self, *, turn_index: int = 1
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(turn_index=turn_index),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )

    def delivered(self, *, turn_index: int) -> redirect_store_models.RedirectRequest:
        """A correction a worker on ``turn_index`` was actually launched with."""
        request = self.submitted()
        continuation = self.target(turn_index=turn_index)
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=continuation,
            state_dir=self.state_dir,
        )
        redirect_store_delivery.deliver_request(
            self.plan_dir,
            request.operation_id,
            continuation=continuation,
            state_dir=self.state_dir,
        )
        return request

    def test_an_empty_journal_reports_nothing_and_exits_zero(self) -> None:
        self.make_live_run()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual([], payload["operations"])
        self.assertTrue(payload["complete"])

    def test_a_submitted_correction_against_a_live_run_is_submitted(self) -> None:
        self.make_live_run()
        request = self.submitted()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        operation = payload["operations"][0]
        self.assertEqual(request.operation_id, operation["operation_id"])
        self.assertEqual(redirect_recovery.STATUS_SUBMITTED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_SUBMITTED, operation["stage"])

    def test_the_same_correction_with_no_run_left_is_unaccepted(self) -> None:
        # No lock and no live run: nothing is going to answer it.
        request = self.submitted()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        operation = payload["operations"][0]
        self.assertEqual(request.operation_id, operation["operation_id"])
        self.assertEqual(redirect_recovery.STATUS_UNANSWERED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_UNACCEPTED, operation["stage"])

    def test_an_accepted_correction_on_the_live_turn_is_interrupting(self) -> None:
        self.make_live_run()
        request = self.submitted()
        redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=self.target(),
            state_dir=self.state_dir,
        )

        _, payload = self.status()

        self.assertEqual(
            redirect_cli.STAGE_INTERRUPTING, payload["operations"][0]["stage"]
        )

    def test_the_same_record_once_the_turn_is_gone_is_stopped(self) -> None:
        self.make_live_run()
        request = self.submitted()
        redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=self.target(),
            state_dir=self.state_dir,
        )
        # The run moved to the successor turn; the journal record did not change.
        self.write_state(turn_index=2)

        _, payload = self.status()

        operation = payload["operations"][0]
        self.assertEqual(redirect_recovery.STATUS_ACCEPTED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_STOPPED, operation["stage"])

    def test_a_delivered_correction_on_the_live_attempt_is_resuming(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.delivered(turn_index=2)

        code, payload = self.status()

        operation = payload["operations"][0]
        self.assertEqual(redirect_recovery.STATUS_UNCERTAIN, operation["status"])
        self.assertEqual(redirect_cli.STAGE_RESUMING, operation["stage"])
        # Resuming is not an open question, so it does not force reconciliation.
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual([], payload["uncertain_operation_ids"])
        self.assertEqual(request.operation_id, operation["operation_id"])

    def test_a_carried_correction_not_yet_launched_is_also_resuming(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.submitted()
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )

        code, payload = self.status()

        operation = payload["operations"][0]
        # The prompt holds it and the run is on that turn. Nothing has launched
        # yet, so the durable status is still ``accepted`` -- but an operator
        # asking where their words are is being told the same thing either way.
        self.assertEqual(redirect_recovery.STATUS_ACCEPTED, operation["status"])
        self.assertEqual(redirect_cli.STAGE_RESUMING, operation["stage"])
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual([], payload["uncertain_operation_ids"])

    def test_a_delivered_correction_whose_attempt_is_gone_exits_three(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.delivered(turn_index=2)
        self.write_state(turn_index=3)

        code, payload = self.status()

        operation = payload["operations"][0]
        self.assertEqual(redirect_cli.STAGE_UNCERTAIN, operation["stage"])
        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertEqual([request.operation_id], payload["uncertain_operation_ids"])

    def test_a_carry_that_never_launched_is_owed_rather_than_in_doubt(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.submitted()
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        self.write_state(turn_index=3)

        code, payload = self.status()

        # No worker was ever started with it, so there is nothing to reconcile:
        # the successor owes it, which is exactly ``stopped``.
        operation = payload["operations"][0]
        self.assertEqual(redirect_cli.STAGE_STOPPED, operation["stage"])
        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual([], payload["uncertain_operation_ids"])
        self.assertEqual(request.operation_id, operation["operation_id"])

    def test_an_acknowledged_correction_is_acknowledged(self) -> None:
        self.make_live_run(turn_index=2)
        request = self.acknowledged_request()

        code, payload = self.status()

        self.assertEqual(redirect_cli.EXIT_OK, code)
        operation = payload["operations"][0]
        self.assertEqual(request.operation_id, operation["operation_id"])
        self.assertEqual(redirect_cli.STAGE_ACKNOWLEDGED, operation["stage"])

    def test_a_settled_correction_is_rejected_and_an_unavailable_one_is_blocked(
        self,
    ) -> None:
        self.make_live_run()
        ended = self.submitted()
        redirect_store_requests.reject_request(
            self.plan_dir,
            ended.operation_id,
            reason=redirect_store_vocab.REASON_RUN_ENDED,
            state_dir=self.state_dir,
        )
        unavailable = redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback="a second correction",
            state_dir=self.state_dir,
        )
        redirect_store_requests.reject_request(
            self.plan_dir,
            unavailable.operation_id,
            reason=redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE,
            state_dir=self.state_dir,
        )

        _, payload = self.status()

        stages = {
            operation["operation_id"]: operation["stage"]
            for operation in payload["operations"]
        }
        self.assertEqual(redirect_cli.STAGE_REJECTED, stages[ended.operation_id])
        self.assertEqual(redirect_cli.STAGE_BLOCKED, stages[unavailable.operation_id])

    def test_an_unknown_id_is_refused_rather_than_reported_empty(self) -> None:
        self.make_live_run()
        self.submitted()

        code, payload = self.status("--redirect-id", "op-nobody-wrote")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_store_vocab.ERROR_UNKNOWN_REQUEST, payload["reason"])

    def test_named_ids_narrow_the_report(self) -> None:
        self.make_live_run()
        first = self.submitted()
        redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback="a second correction",
            state_dir=self.state_dir,
        )

        _, payload = self.status("--redirect-id", first.operation_id)

        self.assertEqual(
            [first.operation_id],
            [operation["operation_id"] for operation in payload["operations"]],
        )

    def test_reading_the_status_changes_nothing(self) -> None:
        self.make_live_run()
        self.submitted()
        before = redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        ).read_bytes()

        self.status()
        self.status()

        after = redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        ).read_bytes()
        self.assertEqual(before, after)

    def acknowledged_request(self) -> redirect_store_models.RedirectRequest:
        """One correction carried into turn 2 and receipted by its worker."""
        request = redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
        )
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=self.target(turn_index=2),
            state_dir=self.state_dir,
        )
        return redirect_store_delivery.acknowledge_request(
            self.plan_dir,
            request.operation_id,
            attempt=self.target(turn_index=2),
            state_dir=self.state_dir,
        )


__all__ = ["io", "json", "os", "socket", "tempfile", "threading", "time", "unittest", "redirect_stderr", "redirect_stdout", "Path", "mock", "asl_cli", "clock", "events", "redirect_ack", "redirect_admission", "redirect_cli", "redirect_recovery", "redirect_store_carry", "redirect_store_delivery", "redirect_store_journal", "redirect_store_models", "redirect_store_requests", "redirect_store_validation", "redirect_store_vocab", "_write_lock_file", "_write_state_payload", "live_redirect_endpoint", "write_plan", "RUN_ID", "SESSION_ID", "FEEDBACK", "RedirectCliTestCase", "WiringTests", "SuccessfulRedirectTests", "UnavailableControlTests", "StaleTargetTests", "RedirectStatusTests"]  # fmt: skip
