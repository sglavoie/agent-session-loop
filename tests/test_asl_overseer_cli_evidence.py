"""The evidence asymmetry between relays and transcripts, and escalation."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

from agent_session_loop import (
    mailbox_vocabulary,
    overseer_cli,
    overseer_decisions,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import GENERATED_CLASS


class EvidenceBoundaryTests(_OverseerCase):
    """Where an authorization can come from, and where it structurally cannot."""

    def test_the_mailbox_yields_no_authorizing_voice_at_all(self) -> None:
        """The asymmetry this whole leaf is shaped around, stated directly."""
        self.assertEqual(("user_instruction",), overseer_decisions.AUTHORIZING_VOICES)
        self.assertNotIn(
            "user_instruction",
            set(overseer_decisions.SUPERVISION_VOICES.values()),
            "a mailbox record names no author and must never authorize",
        )

    def test_a_claim_on_a_mailbox_record_is_rejected_as_unauthored(self) -> None:
        packet = self.observed()
        supervision = self._mailbox_statement_id(packet)

        status, payload = self._decide(
            packet, claims=self.fleet.allowing(supervision, "guidance")
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertEqual("no_decision", payload["ruling"]["refusal"])
        self.assertEqual(
            ["not_user_authored"],
            [item["reason"] for item in payload["rejected_claims"]],
        )

    def test_reading_the_mailbox_alone_can_never_authorize(self) -> None:
        """`--no-history` is a state in which nothing is authorizable."""
        packet = self.observed()

        status, payload = self._decide(
            packet,
            "--no-history",
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertIsNone(payload["history"])
        self.assertEqual(
            ["unknown_statement"],
            [item["reason"] for item in payload["rejected_claims"]],
        )
        self.assertEqual([], self.fleet.transcript.calls)

    def test_an_unreadable_transcript_is_reported_not_read_as_silence(self) -> None:
        packet = self.observed()
        self.fleet.transcript.failure = "client_timeout"

        status, payload = self._decide(
            packet,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertFalse(payload["history"]["available"])
        self.assertEqual("client_timeout", payload["history"]["failure"])

    def test_a_generated_turn_does_not_speak_for_the_person(self) -> None:
        """A `user` turn another agent wrote is a prompt, not an instruction."""
        packet = self.observed()
        self.fleet.transcript.session_class = GENERATED_CLASS

        status, payload = self._decide(
            packet,
            "--generated-class",
            GENERATED_CLASS,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertFalse(payload["authorized"])
        self.assertEqual(
            ["not_user_authored"],
            [item["reason"] for item in payload["rejected_claims"]],
        )

    def test_the_default_invocation_refuses_an_automated_conversation(self) -> None:
        """No flags: AgentMux's own classification has to be enough.

        `--generated-class` above is the caller remembering. This is the same
        refusal with nothing remembered, which is what makes the provenance
        boundary a boundary rather than an option.
        """
        for session_class in (
            overseer_decisions.SESSION_CLASS_AUTOMATED,
            overseer_decisions.SESSION_CLASS_UNKNOWN,
            "",
        ):
            with self.subTest(session_class=session_class):
                self.fleet.transcript.session_class = session_class
                packet = self.observed()

                status, payload = self._decide(
                    packet,
                    claims=self.fleet.allowing(
                        self.fleet.transcript.statement_id(), "guidance"
                    ),
                )

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertFalse(payload["authorized"])
                self.assertEqual(
                    ["not_user_authored"],
                    [item["reason"] for item in payload["rejected_claims"]],
                )
                self.assertEqual("no_decision", payload["ruling"]["refusal"])

    def test_the_default_invocation_still_authorizes_a_main_conversation(self) -> None:
        """The positive half: fail-closed must not mean fail-always."""
        self.fleet.transcript.session_class = overseer_decisions.SESSION_CLASS_MAIN
        packet = self.observed()

        status, payload = self._decide(
            packet,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["authorized"])
        self.assertEqual([], payload["rejected_claims"])

    def test_an_automated_conversation_dispatches_nothing_through_act(self) -> None:
        """`act` re-derives authority, so the classification has to reach it too."""
        packet = self.observed()
        self.fleet.transcript.session_class = overseer_decisions.SESSION_CLASS_AUTOMATED
        # Bound to the record as it stands, so nothing here turns on freshness:
        # the refusal has to come from who wrote the conversation.
        reviewed = self._reviewed(packet)

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
            "--reviewed-evidence",
            reviewed,
            "--claims-file",
            self.fleet.claims_file(
                self,
                self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
            ),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"], payload["dispatch"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIsNotNone(payload["escalation"])
        self.assertNotIn(
            mailbox_vocabulary.KIND_GUIDANCE,
            [message.kind for message in self.fleet.messages(self.plan)],
        )

    def test_a_transcript_statement_cannot_be_stretched_to_another_plan(self) -> None:
        """A transcript names no plan, so a claim has to narrow, not widen."""
        packet = self.observed()
        claims = self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance")
        claims[0]["scope"] = {"project": str(self.fleet.root / "somewhere-else")}

        status, payload = self._decide(packet, claims=claims)

        self.assertEqual(
            ["scope_widened"],
            [item["reason"] for item in payload["rejected_claims"]],
        )
        self.assertFalse(payload["authorized"])

    def test_authority_is_re_derived_from_evidence_at_dispatch_time(self) -> None:
        """`act` reads the transcript again rather than trusting `decide`."""
        packet = self.observed()
        self._decide(
            packet,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )
        reviewed = self._reviewed(packet)
        before = len(self.fleet.transcript.calls)

        self.act(
            packet,
            "--text",
            "carry on",
            "--reviewed-evidence",
            reviewed,
            "--claims-file",
            self.fleet.claims_file(
                self,
                self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
            ),
        )

        self.assertGreater(len(self.fleet.transcript.calls), before)

    # -- helpers -----------------------------------------------------------

    def _mailbox_statement_id(self, packet: dict[str, Any]) -> str:
        _status, listing = self.fleet.invoke(
            "decide",
            str(self.plan),
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--list-statements",
        )
        return next(
            item["statement_id"]
            for item in listing["statements"]
            if item["statement_id"].startswith(overseer_decisions.STATE_SPACE)
        )

    def _reviewed(self, packet: dict[str, Any], *extra: str) -> str:
        return self.fleet.reviewed(
            self.plan,
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            *extra,
        )

    def _decide(
        self,
        packet: dict[str, Any],
        *extra: str,
        claims: list[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--reviewed-evidence",
            self._reviewed(packet, *extra),
            "--claims-file",
            self.fleet.claims_file(self, claims),
            *extra,
        )


class EscalationTests(_OverseerCase):
    """What a refusal produces, and what it deliberately cannot produce."""

    def test_an_escalation_has_no_default_no_timeout_and_no_deadline(self) -> None:
        """Silence is not consent, enforced by there being nowhere to say it."""
        names = {field.name for field in fields(overseer_cli.Escalation)}

        self.assertEqual({"reason", "question", "blocked", "independent"}, names)
        payload = overseer_cli.Escalation(reason="held", question="?").payload()
        for forbidden in ("default", "timeout", "deadline", "expires_at"):
            self.assertNotIn(forbidden, payload)

    def test_a_check_no_plan_declares_is_refused_and_escalated(self) -> None:
        """A fully authorized verify still cannot invent the check it runs.

        The meaningful escalation for `verify`: authority is not the missing
        piece, the plan is. A human said to handle routine tooling, and the
        name still resolves to nothing, so the answer is a question rather than
        a command assembled to satisfy the request.
        """
        self.fleet.declare_checks(
            self.plan, {"type": "file_exists", "name": "marker", "path": "marker.txt"}
        )
        packet = self.observed()

        status, payload = self.act(
            packet,
            "--check",
            "unit",
            "--reviewed-evidence",
            self.fleet.reviewed(
                self.plan, "--incident-id", str(packet["packet"]["incident_id"])
            ),
            "--claims-file",
            self.fleet.claims_file(
                self,
                self.fleet.allowing(self.fleet.transcript.statement_id(), "verify"),
            ),
            operation="verify",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("check_undeclared", payload["dispatch"]["detail"])
        self.assertEqual("preparation_declined", payload["escalation"]["reason"])

    def test_an_escalation_names_the_blocked_work_and_what_may_continue(self) -> None:
        """Dependent work stops; independent authorized work does not."""
        other = self.fleet.write_run("plan-beta")
        packet = self.observed()

        _status, payload = self.act(packet, "--check", "unit", operation="verify")

        escalation = payload["escalation"]
        self.assertEqual([f"{self.plan}::verify"], escalation["blocked"])
        self.assertEqual([str(other)], escalation["independent"])

    def test_an_unsettled_decision_escalates_rather_than_proceeding(self) -> None:
        packet = self.observed()

        status, payload = self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("no_decision", payload["escalation"]["reason"])
        self.assertIn("May the overseer guidance", payload["escalation"]["question"])

    def test_a_human_hold_is_reported_as_a_hold_and_never_worked_around(self) -> None:
        packet = self.observed()
        claims = self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance")
        claims[0]["stance"] = "hold"

        status, payload = self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--reviewed-evidence",
            self.fleet.reviewed(
                self.plan, "--incident-id", str(packet["packet"]["incident_id"])
            ),
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("held", payload["ruling"]["refusal"])
        self.assertEqual("held", payload["escalation"]["reason"])
