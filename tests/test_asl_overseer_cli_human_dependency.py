"""Human-dependency handling and reconciliation permissions."""

from __future__ import annotations

from typing import Any

from agent_session_loop import (
    mailbox_vocabulary,
    overseer_cli,
    overseer_decisions,
    overseer_store_models,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import ACTION


class HumanDependencyTests(_OverseerCase):
    """That a human question reaches the CLI at all.

    A human topic never speaks to an operation, so an accepted decision about
    acceptance, scope, design or safety is invisible to the ruling unless the
    request raises it. The CLI used to raise nothing, which made the whole
    ``HUMAN_TOPICS`` half of the decision layer unreachable from an invocation:
    a safety hold the host read out of the conversation, submitted as a claim
    and accepted into the register, still authorized the operation. These tests
    drive both halves of the repair --- the topic the register itself raises
    because a decision was claimed on it, and the topic ``--raises`` names
    because the host knows nobody has answered it --- across all four topics,
    through ``decide`` and through every recheck ``act`` performs.
    """

    STATEMENTS = {
        "acceptance": "the shard-timeout fix is what I asked for. it is accepted.",
        "scope": "this run may widen to the packaging repository. that is in scope.",
        "design": "the contract-reading adapter is the design I want here.",
        "safety": "the migration this run is paused on is safe to perform.",
    }

    def test_a_settled_topic_leaves_authorized_work_authorized(self) -> None:
        """The dependency is a question to answer, not a second veto.

        A topic somebody settled is raised and then satisfied, so the operation
        the same person allowed still goes through --- otherwise every claim a
        host submitted about a human question would cost it the operation.
        """
        packet = self.observed()

        for topic in overseer_decisions.HUMAN_TOPICS:
            with self.subTest(topic=topic):
                status, payload = self._decide(packet, topic=topic, stance="allow")

                self.assertEqual(overseer_cli.EXIT_OK, status, payload)
                self.assertTrue(payload["authorized"])
                self.assertEqual([], payload["ruling"]["unresolved"])
                self.assertEqual(
                    [topic],
                    [item["topic"] for item in payload["ruling"]["settlements"]],
                )

    def test_a_claimed_hold_outranks_a_general_operational_allowance(self) -> None:
        """The finding, per topic: the hold is in the register and it now bites.

        The operational allowance is still the governing decision --- nothing
        about the hold changes which decision speaks to the operation --- and
        the request is refused anyway, which is the ordering the decision layer
        always had and the CLI could never reach.
        """
        packet = self.observed()

        for topic in overseer_decisions.HUMAN_TOPICS:
            with self.subTest(topic=topic):
                status, payload = self._decide(packet, topic=topic, stance="hold")

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertFalse(payload["authorized"])
                self.assertEqual("human_decision", payload["ruling"]["refusal"])
                self.assertEqual([topic], payload["ruling"]["unresolved"])
                self.assertEqual("operations", payload["ruling"]["decision"]["topic"])
                self.assertEqual("allow", payload["ruling"]["decision"]["stance"])

    def test_a_raised_topic_nobody_settled_blocks_the_operation(self) -> None:
        """The other direction: no decision on the topic at all.

        Deriving dependencies from the register alone would raise only questions
        somebody already answered, which is the useless half. `--raises` is how
        a host says the question is open, and an open question is not an
        allowance.
        """
        packet = self.observed()

        for topic in overseer_decisions.HUMAN_TOPICS:
            with self.subTest(topic=topic):
                status, payload = self._decide(packet, raises=topic)

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertEqual("human_decision", payload["ruling"]["refusal"])
                self.assertEqual([topic], payload["ruling"]["unresolved"])
                self.assertIn(topic, payload["ruling"]["summary"])

    def test_a_claimed_hold_stops_the_action_and_writes_nothing(self) -> None:
        """Through `act`, where the ruling is re-derived rather than carried."""
        packet = self.observed()
        claims = self.fleet.claims_file(
            self, self._claims(topic="safety", stance="hold")
        )

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            claims,
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("human_decision", payload["dispatch"]["detail"])
        self.assertEqual((), self._guidance())

    def test_a_raised_topic_stops_the_action_before_it_prepares(self) -> None:
        """`--raises` reaches the ruling the dispatcher asks for, not just `decide`.

        The dispatcher checks authority twice, and the flag is read once and
        handed to both, so neither pass can re-derive an authorization the other
        refused. Only the first pass runs here --- an action refused before its
        adapter prepares never reaches the second --- so what this pins is that
        the transcript was read exactly once and nothing was appended.
        """
        packet = self.observed()
        claims = self.fleet.claims_file(
            self, self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance")
        )
        reviewed = self._reviewed(packet)
        baseline = len(self.fleet.transcript.calls)

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
            "--raises",
            "acceptance",
            "--reviewed-evidence",
            reviewed,
            "--claims-file",
            claims,
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertIn("human_decision", payload["dispatch"]["detail"])
        self.assertEqual(baseline + 1, len(self.fleet.transcript.calls))
        self.assertEqual((), self._guidance())

    def test_a_settled_topic_the_invocation_raised_still_acts(self) -> None:
        """The whole point: naming the question is how a host clears it."""
        packet = self.observed()
        claims = self.fleet.claims_file(
            self, self._claims(topic="acceptance", stance="allow")
        )

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
            "--raises",
            "acceptance",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            claims,
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"])
        self.assertEqual(1, len(self._guidance()))

    # -- helpers -----------------------------------------------------------

    def _claims(self, *, topic: str, stance: str) -> list[dict[str, Any]]:
        """The operational allowance, plus one reading of one human question.

        The human statement is appended rather than replacing the record, so the
        allowance that would otherwise authorize the operation is still there to
        be outranked.
        """
        self.fleet.transcript.turns[1:] = [("user", self.STATEMENTS[topic])]
        return [
            *self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
            {
                "statement_id": self.fleet.transcript.statement_id(1),
                "topic": topic,
                "stance": stance,
                "operations": [],
                "reading": f"the user spoke to the {topic} question for this run",
            },
        ]

    def _guidance(self) -> tuple[Any, ...]:
        return tuple(
            message
            for message in self.fleet.messages(self.plan)
            if message.kind == mailbox_vocabulary.KIND_GUIDANCE
        )

    def _reviewed(self, packet: dict[str, Any]) -> str:
        return self.fleet.reviewed(
            self.plan, "--incident-id", str(packet["packet"]["incident_id"])
        )

    def _decide(
        self,
        packet: dict[str, Any],
        *,
        topic: str = "",
        stance: str = "allow",
        raises: str = "",
    ) -> tuple[int, dict[str, Any]]:
        claims = (
            self._claims(topic=topic, stance=stance)
            if topic
            else self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance")
        )
        # After `_claims`, which appends the human statement: the record a host
        # reviews is the one its claims are about.
        return self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            *(("--raises", raises) if raises else ()),
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )


class ReconciliationPermissionTests(_OverseerCase):
    """That `reconcile` asks before it looks, and looks at nothing when refused.

    `reconcile` is the standalone door into the same content `act` reads --
    state files, mailboxes, Git history -- reached without an incident, a token
    or a lease. Every test here starts from one `attempted` row, because a row
    left in doubt is the only thing that makes the leaf do any work at all, and
    it is also the thing a refusal must not quietly settle.
    """

    def setUp(self) -> None:
        super().setUp()
        self.watch()
        self.record = self._leave_in_doubt()

    def test_a_permitted_reconcile_asks_first_and_then_reads_the_run(self) -> None:
        status, payload = self.fleet.invoke("reconcile", str(self.plan))

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["authorized"])
        self.assertTrue(payload["settled"])
        self.assertEqual([str(self.plan)], self.fleet.reads)
        self.assertEqual(1, self.fleet.policy.asked_under(ACTION))
        self.assertEqual("executed", self._status())

    def test_a_revoked_opt_in_settles_nothing_and_reads_nothing(self) -> None:
        self.fleet.policy.refused.add(str(self.plan))

        status, payload = self.fleet.invoke("reconcile", str(self.plan))

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual("unauthorized", payload["reason"])
        self.assertFalse(payload["authorized"])
        self.assertEqual([], self.fleet.reads)

    def test_an_unreachable_policy_settles_nothing_and_reads_nothing(self) -> None:
        """Unreachable is a different answer from refused, and refuses alike."""
        self.fleet.policy.unavailable.add(str(self.plan))

        status, payload = self.fleet.invoke("reconcile", str(self.plan))

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("policy_unavailable", payload["reason"])
        self.assertEqual([], self.fleet.reads)

    def test_a_refusal_leaves_the_already_dispatched_action_in_doubt(self) -> None:
        """The row survives the refusal, because the dispatch really happened.

        "Already-dispatched actions are not automatically undone" and "a refused
        policy reads nothing" have to hold at once: the refusal may not settle
        the row, and it may not abandon it either.
        """
        self.fleet.policy.refused.add(str(self.plan))

        self.fleet.invoke("reconcile", str(self.plan))

        self.assertEqual("attempted", self._status())
        ledger = self.fleet.open_ledger(state_dir=str(self.fleet.state_root))
        in_doubt = ledger.in_doubt_actions(plan_dir=str(self.plan))
        self.assertEqual([self.record.action_id], [item.action_id for item in in_doubt])

    def test_a_refusal_leaves_a_settled_record_alone(self) -> None:
        """A revoked opt-in does not reopen what an earlier run already closed."""
        self.fleet.invoke("reconcile", str(self.plan))
        self.fleet.policy.refused.add(str(self.plan))

        status, _ = self.fleet.invoke("reconcile", str(self.plan))

        self.assertEqual(overseer_cli.EXIT_REFUSED, status)
        self.assertEqual("executed", self._status())

    # -- helpers -----------------------------------------------------------

    def _leave_in_doubt(self) -> overseer_store_models.ActionRecord:
        """The trace a dispatch that died mid-flight leaves behind.

        Written straight to the ledger rather than through `act`, because a
        dispatch that completes settles its own row: the state under test is by
        definition the one no successful invocation produces. The progress token
        is one the run has since left, so a permitted reconcile has an
        unambiguous answer -- `executed` -- and the test is about who was asked
        before that answer was reached, not about which answer it was.
        """
        ledger = self.fleet.open_ledger(state_dir=str(self.fleet.state_root))
        decision = self.fleet.policy(
            plan_dir=str(self.plan),
            access=ACTION,
            repositories=(),
            timeout=1.0,
        )
        ledger.note_policy(config_fingerprint=decision.config_fingerprint)
        grant = decision.grant()
        assert grant is not None
        record = ledger.attempt_action(
            grant=grant,
            plan_dir=str(self.plan),
            operation="resume",
            target="run",
            progress_token="the-situation-the-run-has-since-left",
        )
        self.fleet.policy.asked.clear()
        self.fleet.reads.clear()
        return record

    def _status(self) -> str:
        ledger = self.fleet.open_ledger(state_dir=str(self.fleet.state_root))
        settled = ledger.action(self.record.action_id)
        assert settled is not None
        return settled.status
