from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_bridge_redirect_support import *


class StatusProjectionTests(RedirectBridgeTestCase):
    """What a status read says about a plan, and what it declines to say."""

    def test_status_selects_by_operation_id(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        redirect_admission.submit_redirect(
            self.plan_dir,
            SECOND_FEEDBACK,
            state_dir=self.state_dir,
            operation_id="amx-op-second",
            wait_seconds=0.0,
        )

        everything = self.status()["result"]
        selected = self.status(operation_ids=["amx-op-second"])["result"]

        self.assertEqual(
            ["amx-op-9c31aa", "amx-op-second"],
            [operation["operation_id"] for operation in everything["operations"]],
        )
        self.assertEqual(
            ["amx-op-second"],
            [operation["operation_id"] for operation in selected["operations"]],
        )

    def test_status_names_the_turn_the_run_is_on_now(self) -> None:
        """The correction is settled against a turn; the run may have moved."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(turn_index=3)

        result = self.status()["result"]

        self.assertEqual(self.target(turn_index=3).to_payload(), result["live_target"])
        self.assertEqual(self.target().to_payload(), result["operations"][0]["target"])
        # Accepted against a turn the run has left: the turn is gone and a
        # successor owes the correction.
        self.assertEqual(redirect_cli.STAGE_STOPPED, result["operations"][0]["stage"])

    def test_status_over_a_plan_with_no_journal_is_an_empty_answer(self) -> None:
        """Never having submitted anything is an answer, not a failure."""
        self.live_run()

        response = self.status()

        self.assertEqual([], problems(response, "redirect_status_response"))
        self.assertEqual([], response["result"]["operations"])
        self.assertTrue(response["result"]["journal_complete"])
        self.assertFalse(response["result"]["torn_tail"])

    def test_status_carries_the_action_token_a_submission_needs(self) -> None:
        """The read a client makes first hands it what the mutation requires."""
        self.live_run()

        self.assertEqual(self.action_token(), self.status()["result"]["action_token"])

    def test_a_torn_journal_is_reported_rather_than_hidden(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        journal_path = self.journal().journal_path
        with open(journal_path, "a", encoding="utf-8") as handle:
            handle.write('{"kind": "submitted", "v')

        result = self.status()["result"]

        self.assertFalse(result["journal_complete"])
        self.assertTrue(result["torn_tail"])

    def test_the_projection_window_is_bounded_and_says_so(self) -> None:
        """A long-lived plan truncates from the front and reports it."""
        self.live_run()
        for index in range(bridge_protocol.MAX_REDIRECT_OPERATIONS + 2):
            redirect_admission.submit_redirect(
                self.plan_dir,
                f"correction {index}",
                state_dir=self.state_dir,
                operation_id=f"amx-op-{index:03d}",
                wait_seconds=0.0,
            )

        result = self.status()["result"]

        self.assertEqual(
            bridge_protocol.MAX_REDIRECT_OPERATIONS, len(result["operations"])
        )
        self.assertTrue(result["truncated"])
        # Truncated from the front: the newest corrections are the ones an
        # operator is asking about.
        self.assertEqual(
            f"amx-op-{bridge_protocol.MAX_REDIRECT_OPERATIONS + 1:03d}",
            result["operations"][-1]["operation_id"],
        )
        self.assertEqual([], problems(self.status(), "redirect_status_response"))


class BodyFreedomTests(RedirectBridgeTestCase):
    """The bridge never holds an operator's words, so it cannot leak them."""

    def frames(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            ("receipt", self.submit(request_id="req-6")),
            ("status", self.status()),
            ("recovery", self.recovery()),
        ]

    def test_no_redirect_frame_carries_the_correction_text(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)

        for name, frame in self.frames():
            with self.subTest(frame=name):
                self.assertNotIn(FEEDBACK, json.dumps(frame))

    def test_no_redirect_frame_carries_a_body_bearing_key(self) -> None:
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        forbidden = set(
            contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)[
                "forbidden_property_names"
            ]
        )

        for name, frame in self.frames():
            pending: list[Any] = [frame]
            while pending:
                node = pending.pop()
                if isinstance(node, dict):
                    with self.subTest(frame=name):
                        self.assertEqual(set(), forbidden & set(node))
                    pending.extend(node.values())
                elif isinstance(node, list):
                    pending.extend(node)

    def test_a_correction_is_identified_by_digest_and_size(self) -> None:
        """What a client needs is *which* correction, not what it said."""
        self.live_run()
        self.accept_when_pending()

        operation = self.submit(wait_seconds=5.0)["result"]["operation"]

        self.assertEqual(
            redirect_store_validation.feedback_digest(FEEDBACK),
            operation["feedback_digest"],
        )
        self.assertEqual(len(FEEDBACK.encode("utf-8")), operation["feedback_bytes"])

    def test_a_refusal_message_never_quotes_the_correction(self) -> None:
        self.live_run()
        session = self.handshaken()

        response = self.request(
            session,
            intervention.ACTION_REDIRECT,
            self.redirect_params(action_token="0" * 32),
            request_id="req-2",
        )

        self.assertNotIn(FEEDBACK, json.dumps(response))


class RecoveryProjectionTests(RedirectBridgeTestCase):
    """Which recovery a pause is waiting on, reported and never performed."""

    def test_a_conversation_pause_hands_an_operator_the_decision(self) -> None:
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )
        self.write_lock()

        response = self.recovery()
        result = response["result"]

        self.assertEqual([], problems(response, "recovery_options_response"))
        self.assertTrue(result["recovery_required"])
        self.assertEqual(
            intervention.RECOVERY_DECISION_FRESH_CONVERSATION, result["decision"]
        )
        self.assertFalse(result["auto_resume"])
        self.assertEqual(intervention.RECOVERY_AUTHORIZED_BY, result["authorized_by"])
        self.assertTrue(result["one_use"])

    def test_the_commands_are_the_ones_the_cli_renders(self) -> None:
        """Three surfaces reporting this pause must not print three commands."""
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )
        self.write_lock()
        # Rendered against the resolved plan directory the bridge validated,
        # not the one the test typed: a symlinked temporary root would
        # otherwise make two identical renderers look like two answers.
        expected = redirect_cli.recovery_commands(
            intervention.inspect_run(self.plan_dir, state_dir=self.state_dir).plan_dir,
            str(self.state_dir),
            decision=intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
        )

        result = self.recovery()["result"]

        self.assertEqual(expected["inspect_command"], result["inspect_command"])
        self.assertEqual(
            expected["authorization_command"], result["authorization_command"]
        )
        self.assertEqual(redirect_cli.FRESH_CONVERSATION_NOTE, result["note"])

    def test_a_blocked_successor_has_no_authorization_to_offer(self) -> None:
        """No text an operator sends makes a second worker safe to start."""
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
        )
        self.write_lock()

        result = self.recovery()["result"]

        self.assertEqual(
            intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE, result["decision"]
        )
        self.assertIsNone(result["authorization_command"])
        self.assertFalse(result["one_use"])
        self.assertIsNone(result["note"])
        self.assertIsNotNone(result["inspect_command"])

    def test_an_ordinary_pause_names_itself_and_hands_over_nothing(self) -> None:
        """An unexplained null would read as a second question."""
        self.write_state(paused=True, turn_index=None, pause_reason="agent-question")
        self.write_lock()

        result = self.recovery()["result"]

        self.assertFalse(result["recovery_required"])
        self.assertEqual("agent-question", result["pause_reason"])
        self.assertIsNone(result["decision"])
        self.assertFalse(result["auto_resume"])

    def test_a_running_plan_is_waiting_on_no_decision(self) -> None:
        self.live_run()

        result = self.recovery()["result"]

        self.assertFalse(result["recovery_required"])
        self.assertIsNone(result["pause_reason"])
        self.assertIsNone(result["decision"])

    def test_reporting_a_recovery_performs_none_of_it(self) -> None:
        """The one-use authorization must never live in a replayable request."""
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )
        self.write_lock()

        def corpus() -> list[tuple[str, int]]:
            return sorted(
                (str(path.relative_to(self.state_dir)), path.stat().st_mtime_ns)
                for path in Path(self.state_dir).rglob("*")
                if path.is_file()
            )

        before = corpus()

        self.recovery()
        self.recovery(request_id="req-5")

        self.assertEqual(before, corpus())
        # Not even the journal the read walks: reporting a recovery must not
        # be the thing that creates the record it reports on.
        self.assertFalse(Path(self.journal().journal_path).exists())

    def test_a_fresh_start_reports_the_corrections_it_would_inherit(self) -> None:
        """An operator authorizing one should not meet them in the next prompt."""
        self.live_run()
        self.accept_when_pending()
        self.submit(wait_seconds=5.0)
        self.write_state(
            paused=True,
            turn_index=None,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
        )

        result = self.recovery()["result"]

        self.assertEqual(
            ["amx-op-9c31aa"],
            [operation["operation_id"] for operation in result["retained_operations"]],
        )
        self.assertNotIn(FEEDBACK, json.dumps(result))


class VocabularyTests(RedirectBridgeTestCase):
    """Served frames draw only on the vocabularies the contract set closed."""

    def schema_enum(self, name: str) -> list[Any]:
        schema = contract.load_schema(contract.COMPONENT_BRIDGE_PROTOCOL)
        return list(schema["definitions"][name]["enum"])

    def test_the_projected_stage_vocabulary_is_the_one_the_cli_prints(self) -> None:
        self.assertEqual(list(redirect_cli.STAGES), self.schema_enum("RedirectStage"))

    def test_the_projected_status_vocabulary_is_the_durable_one(self) -> None:
        self.assertEqual(
            list(redirect_recovery.OPERATION_STATUSES),
            self.schema_enum("RedirectStatus"),
        )

    def test_the_journal_state_vocabulary_is_the_journals_own(self) -> None:
        self.assertEqual(
            list(redirect_store_vocab.REQUEST_STATES),
            self.schema_enum("RedirectJournalState"),
        )

    def test_the_recovery_decisions_are_the_shared_two(self) -> None:
        self.assertEqual(
            [None, *intervention.RECOVERY_DECISIONS],
            self.schema_enum("RecoveryDecision"),
        )

    def test_the_refusal_reasons_cover_admission_and_settlement(self) -> None:
        declared = set(self.schema_enum("RedirectReason"))
        self.assertLessEqual(set(redirect_admission.REFUSAL_REASONS), declared)
        self.assertLessEqual(set(redirect_store_vocab.SETTLEMENT_REASONS), declared)

    def test_a_redirect_status_is_not_an_operation_receipt_status(self) -> None:
        """A redirect outlives the session that submitted it; a receipt does not."""
        self.assertNotEqual(
            set(redirect_recovery.OPERATION_STATUSES),
            set(bridge_protocol.OPERATION_STATUSES),
        )
        self.assertEqual(
            list(bridge_protocol.OPERATION_STATUSES),
            self.schema_enum("OperationStatus"),
        )
