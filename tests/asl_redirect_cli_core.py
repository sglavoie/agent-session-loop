"""Focused portion of tests/test_asl_redirect_cli.py."""

from .asl_redirect_cli_support import *


class WiringTests(RedirectCliTestCase):
    """The three subcommands are reachable, with the documented defaults."""

    def test_redirect_is_wired_to_its_handler(self) -> None:
        args = self.parser.parse_args(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )
        self.assertEqual("redirect_cli:control_redirect_from_args", args.handler_ref)
        self.assertIs(
            asl_cli.resolve_handler(args), redirect_cli.control_redirect_from_args
        )
        self.assertIsNone(args.operation_id)
        self.assertIsNone(args.ttl)
        self.assertIsNone(args.wait_timeout)
        self.assertFalse(args.text_stdin)

    def test_redirect_status_is_wired_and_reads_by_default(self) -> None:
        args = self.parser.parse_args(
            ["control", "redirect-status", str(self.plan_dir)]
        )
        self.assertEqual(
            "redirect_cli:control_redirect_status_from_args", args.handler_ref
        )
        self.assertIsNone(args.redirect_id)
        self.assertFalse(args.wait_acknowledged)

    def test_recover_conversation_is_wired(self) -> None:
        args = self.parser.parse_args(
            ["control", "recover-conversation", str(self.plan_dir)]
        )
        self.assertEqual(
            "redirect_cli:control_recover_conversation_from_args", args.handler_ref
        )
        self.assertIs(
            asl_cli.resolve_handler(args),
            redirect_cli.control_recover_conversation_from_args,
        )

    def test_every_stage_has_a_description(self) -> None:
        # The payload reads STAGE_DESCRIPTIONS by stage name, so a stage added
        # without one would raise on the operation it describes.
        self.assertEqual(set(redirect_cli.STAGES), set(redirect_cli.STAGE_DESCRIPTIONS))


class SuccessfulRedirectTests(RedirectCliTestCase):
    """A correction the runner takes, reported as interrupting."""

    def test_an_accepted_redirect_exits_zero_and_names_the_turn(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        code, payload = self.redirect("--text", FEEDBACK, "--timeout", "10")

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual("accepted", payload["outcome"])
        self.assertEqual(redirect_cli.STAGE_INTERRUPTING, payload["stage"])
        self.assertEqual(RUN_ID, payload["target"]["run_id"])
        self.assertEqual(SESSION_ID, payload["target"]["session_id"])
        self.assertEqual(1, payload["target"]["turn_index"])
        self.assertIsNotNone(payload["operation_id"])

    def test_text_output_names_the_operation_and_the_stage(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        code, out, err = self.run_cli(
            [
                "control",
                "redirect",
                str(self.plan_dir),
                "--text",
                FEEDBACK,
                "--timeout",
                "10",
            ]
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual("", err)
        operation_id = self.journal().requests[0].operation_id
        self.assertIn(operation_id, out)
        self.assertIn(redirect_cli.STAGE_INTERRUPTING, out)
        self.assertIn(SESSION_ID, out)

    def test_the_correction_text_never_appears_in_the_payload(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        _, payload = self.redirect("--text", FEEDBACK, "--timeout", "10")

        # The journal keeps the words; every projection keeps the digest. A CLI
        # that echoed them back would put operator feedback into whatever log
        # captured the command's output.
        self.assertNotIn(FEEDBACK, json.dumps(payload))
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            payload["request"]["feedback_digest"],
        )

    def test_the_correction_can_be_read_from_stdin(self) -> None:
        self.make_live_run()
        self.accept_when_pending(self.target())

        code, payload = self.redirect("--text-stdin", "--timeout", "10", stdin=FEEDBACK)

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            payload["request"]["feedback_digest"],
        )

    def test_a_body_must_be_supplied_exactly_once(self) -> None:
        self.make_live_run()

        code, payload = self.redirect()

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_MISSING_TEXT, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_two_body_sources_are_refused_by_the_parser(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            self.parser.parse_args(
                [
                    "control",
                    "redirect",
                    str(self.plan_dir),
                    "--text",
                    FEEDBACK,
                    "--text-stdin",
                ]
            )
        self.assertEqual(2, raised.exception.code)

    def test_a_blank_correction_is_refused_before_anything_is_persisted(self) -> None:
        self.make_live_run()

        code, payload = self.redirect("--text", "   ")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_MISSING_TEXT, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_out_of_range_timeout_is_refused_before_anything_is_persisted(
        self,
    ) -> None:
        self.make_live_run()

        code, payload = self.redirect("--text", FEEDBACK, "--timeout", "3600")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_cli.ERROR_INVALID_TIMEOUT, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_an_invalid_ttl_is_reported_under_the_journals_own_reason(self) -> None:
        self.make_live_run()

        code, payload = self.redirect("--text", FEEDBACK, "--ttl", "0")

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_store_vocab.ERROR_INVALID_TTL, payload["reason"])
        self.assertEqual((), self.journal().requests)


class UnavailableControlTests(RedirectCliTestCase):
    """Corrections nothing can carry, and the exit code that says retry later."""

    def test_an_unserved_endpoint_settles_the_request_and_exits_two(self) -> None:
        self.make_unservable_run()

        code, payload = self.redirect("--text", FEEDBACK)
        facts = self.facts(payload)

        self.assertEqual(redirect_cli.EXIT_UNAVAILABLE, code)
        self.assertEqual(redirect_admission.OUTCOME_UNAVAILABLE, facts["outcome"])
        self.assertEqual(redirect_cli.STAGE_BLOCKED, facts["stage"])
        self.assertEqual(
            redirect_store_vocab.REASON_URGENT_CHANNEL_UNAVAILABLE, payload["reason"]
        )
        # Persisted *and* settled: the operator gets an id for the record, and
        # no later runner can pick it up.
        self.assertIsNotNone(facts["operation_id"])
        request = self.journal().request(facts["operation_id"])
        self.assertEqual(redirect_store_vocab.STATE_REJECTED, request.state)

    def test_an_unavailable_submission_says_the_request_will_not_be_acted_on(
        self,
    ) -> None:
        self.make_unservable_run()

        code, out, err = self.run_cli(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )

        self.assertEqual(redirect_cli.EXIT_UNAVAILABLE, code)
        self.assertEqual("", out)
        self.assertIn("durably settled", err)

    def test_no_run_at_all_is_a_refusal_rather_than_an_unavailable_channel(
        self,
    ) -> None:
        self.write_state(run_id=None)

        code, payload = self.redirect("--text", FEEDBACK)

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(redirect_admission.REFUSE_NO_ACTIVE_RUN, payload["reason"])
        self.assertEqual((), self.journal().requests)

    def test_text_output_names_why_a_redirect_was_refused(self) -> None:
        """The JSON carried the reason; the text used to say only "rejected"."""
        self.write_state(run_id=None)

        code, out, err = self.run_cli(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual("", out)
        self.assertIn(redirect_admission.REFUSE_NO_ACTIVE_RUN, err)

    def test_a_paused_run_is_sent_to_the_mailbox_instead(self) -> None:
        self.write_state(paused=True)
        self.write_lock()

        code, out, err = self.run_cli(
            ["control", "redirect", str(self.plan_dir), "--text", FEEDBACK]
        )

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertIn("mailbox", err)
        self.assertEqual((), self.journal().requests)

    def test_an_uncertain_submission_returns_its_id_and_exits_three(self) -> None:
        self.make_live_run()

        # A zero wait cannot observe an acceptance, which is exactly the state
        # an operator hits when the runner is slow: persisted, unanswered, still
        # claimable.
        code, payload = self.redirect("--text", FEEDBACK, "--timeout", "0")
        facts = self.facts(payload)

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertEqual(redirect_admission.OUTCOME_UNCERTAIN, facts["outcome"])
        self.assertEqual(redirect_cli.STAGE_SUBMITTED, facts["stage"])
        operation_id = facts["operation_id"]
        self.assertIsNotNone(operation_id)
        self.assertFalse(self.journal().request(operation_id).terminal)

    def test_an_uncertain_submission_is_told_to_reconcile_not_resubmit(self) -> None:
        self.make_live_run()

        code, out, err = self.run_cli(
            [
                "control",
                "redirect",
                str(self.plan_dir),
                "--text",
                FEEDBACK,
                "--timeout",
                "0",
            ]
        )

        self.assertEqual(redirect_cli.EXIT_UNRESOLVED, code)
        self.assertIn("redirect-status", err)
        self.assertIn("--operation-id", err)

    def test_retrying_an_uncertain_id_reconciles_rather_than_duplicating(self) -> None:
        self.make_live_run()
        _, first = self.redirect("--text", FEEDBACK, "--timeout", "0")
        operation_id = self.facts(first)["operation_id"]
        self.accept_when_pending(self.target())

        code, second = self.redirect(
            "--text",
            FEEDBACK,
            "--operation-id",
            operation_id,
            "--timeout",
            "10",
        )

        self.assertEqual(redirect_cli.EXIT_OK, code)
        self.assertEqual(operation_id, second["operation_id"])
        self.assertEqual(1, len(self.journal().requests))

    def test_reusing_an_id_for_different_words_is_refused(self) -> None:
        self.make_live_run()
        _, first = self.redirect("--text", FEEDBACK, "--timeout", "0")

        code, payload = self.redirect(
            "--text",
            "something else entirely",
            "--operation-id",
            self.facts(first)["operation_id"],
            "--timeout",
            "0",
        )

        self.assertEqual(redirect_cli.EXIT_REFUSED, code)
        self.assertEqual(
            redirect_store_vocab.ERROR_INVALID_OPERATION_ID, payload["reason"]
        )
        self.assertEqual(1, len(self.journal().requests))


__all__ = ["io", "json", "os", "socket", "tempfile", "threading", "time", "unittest", "redirect_stderr", "redirect_stdout", "Path", "mock", "asl_cli", "clock", "events", "redirect_ack", "redirect_ack_attempt", "redirect_admission", "redirect_cli", "redirect_recovery", "redirect_store_carry", "redirect_store_delivery", "redirect_store_journal", "redirect_store_models", "redirect_store_requests", "redirect_store_validation", "redirect_store_vocab", "_write_lock_file", "_write_state_payload", "live_redirect_endpoint", "write_plan", "RUN_ID", "SESSION_ID", "FEEDBACK", "RedirectCliTestCase", "WiringTests", "SuccessfulRedirectTests", "UnavailableControlTests"]  # fmt: skip
