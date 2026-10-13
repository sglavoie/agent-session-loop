"""Evidence freshness and its invalidation."""

from __future__ import annotations

import json
import re
from typing import Any

from agent_session_loop import (
    mailbox_vocabulary,
    overseer_cli,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import WATCHER


class EvidenceFreshnessTests(_OverseerCase):
    """A hold typed while an action is in flight has to stop the action.

    Re-deriving the ruling at dispatch time is what catches a revoked grant, and
    on its own it does not catch a *new* statement: the claims cite what the
    host read, an unclaimed hold never becomes a decision, and the second
    reading of the same claims therefore agrees with the first however the
    record moved. These tests drive the three windows that matters in --- between
    the listing and the claims written from it, between ``decide`` and ``act``,
    and inside a single dispatch --- the refusal that closes them when nothing
    was bound at all, and the case that must keep working, which is evidence
    that did not move.

    They also read the refusals themselves, in both renderings and in full. A
    refusal that names the record it observed hands the claims it just refused
    the one thing they were missing, so the answer would undo itself on the next
    invocation: the binding a host holds has to have come from reading a record,
    and a refusal is not a reading.
    """

    def test_decide_publishes_the_digest_of_the_record_it_read(self) -> None:
        """The token a host carries to `act` comes from the leaf that read."""
        packet = self.observed()
        reviewed = self._reviewed(packet)

        _ruled, payload = self._decide(
            packet,
            reviewed=reviewed,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertTrue(reviewed.startswith("ev1-"))
        self.assertEqual(reviewed, payload["evidence_digest"])

    def test_unchanged_reviewed_evidence_remains_usable(self) -> None:
        """The ordinary path: the host reviewed it, nothing moved, it acts."""
        packet = self.observed()
        _status, decided = self._decide(
            packet,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        status, payload = self._act(packet, decided["evidence_digest"])

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"])
        self.assertFalse(payload["evidence"]["changed"])
        self.assertEqual(decided["evidence_digest"], payload["evidence"]["observed"])

    def test_a_hold_arriving_between_decide_and_act_stops_the_action(self) -> None:
        """The finding: a fresh hold nobody claimed, and the old claim standing.

        The claim still cites the statement it always cited, that statement is
        still in force, and every guard that re-resolves the same claims answers
        the same way. What changed is the record the reading was made against.
        """
        packet = self.observed()
        _status, decided = self._decide(
            packet,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )
        self.fleet.transcript.turns.append(
            ("user", "hold this run. do not resume or send guidance.")
        )

        status, payload = self._act(packet, decided["evidence_digest"])

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("evidence_changed", payload["dispatch"]["detail"])
        self.assertTrue(payload["evidence"]["changed"])
        self.assertEqual(
            (),
            tuple(
                message
                for message in self.fleet.messages(self.plan)
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ),
        )

    def test_claims_decide_rejects_changed_listing_evidence(self) -> None:
        """The earliest window: the listing, then the hold, then the claims.

        ``--list-statements`` shows the host a record and names it. The host
        reads it and writes claims, and a hold lands while it is doing so. The
        claims are honest about what they read; they are simply a reading of a
        record that no longer exists. ``decide`` has to refuse rather than
        re-read, and --- the part that matters downstream --- it must not hand
        back the *new* record's name, or the stale claims would arrive at
        ``act`` carrying a digest that matches and be dispatched as though the
        hold had been reviewed.
        """
        packet = self.observed()
        listed = self._reviewed(packet)
        self.fleet.transcript.turns.append(
            ("user", "hold this run. do not resume or send guidance.")
        )

        status, decided = self._decide(
            packet,
            reviewed=listed,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, decided)
        self.assertFalse(decided["authorized"])
        self.assertEqual("evidence_changed", decided["ruling"]["refusal"])
        self.assertIsNone(
            decided["evidence_digest"],
            "a refused reading must not mint a digest the stale claims can reuse",
        )

        status, payload = self._act(packet, listed)

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual(
            (),
            tuple(
                message
                for message in self.fleet.messages(self.plan)
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ),
            "guidance crossed a hold the host never read",
        )

    def test_a_hold_arriving_during_preparation_stops_the_action(self) -> None:
        """The other window, and the one a host cannot close by being careful.

        The digest is the one the listing named, and it still matches when the
        adapter is asked to prepare. The hold lands during preparation, so only
        the second authority check --- the one immediately before the adapter
        performs --- can see it.
        """
        packet = self.observed()
        reviewed = self._reviewed(packet)
        claims = self.fleet.claims_file(
            self,
            self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
        )
        baseline = len(self.fleet.transcript.calls)

        def hold_lands(read: int) -> None:
            if read == baseline + 1:
                self.fleet.transcript.turns.append(
                    ("user", "hold this run. do not resume or send guidance.")
                )

        self.fleet.transcript.on_read = hold_lands

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
            "--reviewed-evidence",
            reviewed,
            "--claims-file",
            claims,
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertIn("evidence_changed", payload["dispatch"]["detail"])
        self.assertTrue(payload["evidence"]["changed"])

    def test_an_invocation_naming_no_digest_is_refused(self) -> None:
        """Claims about a record nobody named are not a reading of anything.

        The leaf could read the record itself and call that the binding, which
        is what it used to do. That writes the host's reading for it, and
        quietly absorbs every hold that arrived before the invocation into the
        record the host is then said to have reviewed. So it refuses, and says
        which reading would issue a digest.
        """
        packet = self.observed()
        claims = self.fleet.claims_file(
            self,
            self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
        )

        status, payload = self.act(
            packet, "--text", "carry on", "--claims-file", claims
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertIn("evidence_unbound", payload["dispatch"]["detail"])
        self.assertFalse(payload["evidence"]["bound"])
        self.assertEqual("", payload["evidence"]["reviewed"])
        self.assertNotIn(
            mailbox_vocabulary.KIND_GUIDANCE,
            [message.kind for message in self.fleet.messages(self.plan)],
        )

    def test_a_decide_naming_no_digest_is_refused_and_publishes_nothing(self) -> None:
        """The same refusal one leaf earlier, so `act` is not the only guard."""
        packet = self.observed()

        status, payload = self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--claims-file",
            self.fleet.claims_file(
                self,
                self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
            ),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertEqual("evidence_unbound", payload["ruling"]["refusal"])
        self.assertIsNone(payload["evidence_digest"])

    def test_a_refused_decide_furnishes_no_replacement_binding(self) -> None:
        """The refusal must not issue the binding it is refusing for lacking.

        Withholding ``evidence_digest`` is only the obvious half. The summary
        says why the reading is unusable, the escalation asks a human about it,
        and the printed lines say both again --- and a digest spelled out in any
        of them is the new record's name in the hand of a host holding the old
        record's claims, which is the whole of what the binding is for. So the
        entire answer is read, in both renderings, and anything in it that looks
        like a binding is tried against ``act`` before being asserted absent.
        """
        packet = self.observed()
        listed = self._reviewed(packet)
        claims = self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance")
        self.fleet.transcript.turns.append(
            ("user", "hold this run. do not resume or send guidance.")
        )

        status, refused = self._decide(packet, reviewed=listed, claims=claims)
        printed_status, printed = self.fleet.render(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--reviewed-evidence",
            listed,
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, refused)
        self.assertEqual("evidence_changed", refused["ruling"]["refusal"])
        self.assertEqual(overseer_cli.EXIT_REFUSED, printed_status, printed)
        self.assertIn("evidence_changed", printed)
        self._assert_no_reusable_binding(packet, listed, refused, printed)

    def test_a_refused_act_furnishes_no_replacement_binding(self) -> None:
        """The same rule at the mutating leaf, whose answer says more.

        ``act`` reports what its authority checks found, so it is the leaf with
        an actual field for the record it read. What it publishes there is the
        binding the caller brought and the checks confirmed, or nothing: a
        record the host never read is reported as a record that moved and is
        never named, in the payload or in the line printed beside it.
        """
        packet = self.observed()
        reviewed = self._reviewed(packet)
        self.fleet.transcript.turns.append(
            ("user", "hold this run. do not resume or send guidance.")
        )

        status, refused = self._act(packet, reviewed)
        printed_status, printed = self.fleet.render(
            "act",
            str(self.plan),
            "--watcher-id",
            WATCHER,
            "--operation",
            "guidance",
            "--run-id",
            packet["run_id"],
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--token",
            packet["token"],
            "--scope",
            packet["scope"],
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

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, refused)
        self.assertFalse(refused["dispatch"]["performed"])
        self.assertTrue(refused["evidence"]["changed"])
        self.assertEqual(
            "",
            refused["evidence"]["observed"],
            "a refused act must not name the record its claims were not read from",
        )
        self.assertEqual(overseer_cli.EXIT_REFUSED, printed_status, printed)
        self.assertIn("evidence moved", printed)
        self._assert_no_reusable_binding(packet, reviewed, refused, printed)

    def test_an_operation_without_claims_needs_no_binding(self) -> None:
        """Nothing was read, so there is no reading to bind.

        A claimless invocation authorizes nothing anyway; refusing it
        ``evidence_unbound`` would replace an honest ``no_decision`` with a
        complaint about a flag that could not have helped.
        """
        packet = self.observed()

        status, payload = self._decide(packet, reviewed="", claims=[])

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("no_decision", payload["ruling"]["refusal"])

    # -- helpers -----------------------------------------------------------

    #: What an evidence binding looks like from outside, which is how a host
    #: reading an answer would recognize one.
    BINDING = re.compile(r"ev1-[0-9a-f]{32}")

    def _bindings(self, answer: dict[str, Any] | str, reviewed: str) -> set[str]:
        """Every binding an answer names that its caller did not already hold."""
        rendered = answer if isinstance(answer, str) else json.dumps(answer)
        return set(self.BINDING.findall(rendered)) - {reviewed}

    def _assert_no_reusable_binding(
        self,
        packet: dict[str, Any],
        reviewed: str,
        *answers: dict[str, Any] | str,
    ) -> None:
        """No answer hands back a binding, and none it could hand back works.

        Both halves are asserted rather than one: the reuse attempt is the
        property that matters --- a value that authorizes nothing is not a
        binding --- and the emptiness is what keeps the property from resting
        on every future consumer of the refusal being careful with it.
        """
        offered = set().union(*(self._bindings(answer, reviewed) for answer in answers))
        for replacement in sorted(offered):
            _status, retried = self._act(packet, replacement)
            self.assertFalse(
                retried["dispatch"]["performed"],
                f"a refusal furnished a usable replacement binding: {replacement}",
            )
        self.assertEqual(
            set(),
            offered,
            "a refusal named a record the host never read; the stale claims "
            "could carry that name to `act` as though it had been reviewed",
        )
        self.assertEqual(
            (),
            tuple(
                message
                for message in self.fleet.messages(self.plan)
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ),
            "guidance crossed a hold on a binding no listing ever issued",
        )

    def _reviewed(self, packet: dict[str, Any]) -> str:
        return self.fleet.reviewed(
            self.plan, "--incident-id", str(packet["packet"]["incident_id"])
        )

    def _act(self, packet: dict[str, Any], digest: str) -> tuple[int, dict[str, Any]]:
        return self.act(
            packet,
            "--text",
            "carry on",
            "--reviewed-evidence",
            digest,
            "--claims-file",
            self.fleet.claims_file(
                self,
                self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
            ),
        )

    def _decide(
        self,
        packet: dict[str, Any],
        *,
        claims: list[dict[str, Any]],
        reviewed: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if reviewed is None:
            reviewed = self._reviewed(packet)
        return self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--reviewed-evidence",
            reviewed,
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )
