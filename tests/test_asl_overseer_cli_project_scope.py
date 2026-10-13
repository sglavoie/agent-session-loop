"""Project scope and registered-workspace resolution."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    overseer_cli,
    overseer_decisions,
)
from tests.asl_overseer_cli_fleet import _Fleet
from tests.asl_overseer_cli_support import WATCHER


class ProjectScopeTests(unittest.TestCase):
    """Which project a decision is about, when a plan sits under a workspace.

    A run names a workspace root and the repositories checked out beneath it,
    and those are not interchangeable. AgentMux authorizes repositories and
    scopes a transcript to the directory its conversation was started in, so
    the project a statement speaks about is a repository. A scope built from
    the workspace above them would be a project nobody ever spoke about: every
    repository decision would read as widening, and the reverse -- a scope that
    matched the workspace loosely -- would let a decision typed in one checkout
    reach a sibling checkout the person never mentioned. The fixture keeps the
    workspace, the two repositories and the plan directory all distinct so
    neither confusion can pass unnoticed.
    """

    def setUp(self) -> None:
        self.fleet = _Fleet(self)
        self.workspace = self.fleet.root
        self.scripts_root = self.fleet.repository_root
        self.api_root = self.fleet.root / "api"
        self.plan = self.fleet.write_run("plan-alpha")
        self.fleet.ask_question(self.plan)
        self.shared = self.fleet.write_run(
            "plan-both",
            repositories=[
                self._declaration("scripts", self.scripts_root),
                self._declaration("api", self.api_root),
            ],
        )
        self.fleet.ask_question(self.shared)

    def test_a_repository_decision_reaches_a_plan_below_the_workspace(self) -> None:
        """One repository, and the decision made in it is about it."""
        packet = self._observe(self.plan)

        status, payload = self._decide(
            self.plan, packet, self._allowing(self.fleet.transcript.statement_id())
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["authorized"])
        self.assertEqual([], payload["rejected_claims"])
        scope = payload["ruling"]["scope"]
        self.assertEqual([str(self.scripts_root)], scope["projects"])
        self.assertNotEqual(str(self.workspace), scope["project"])

    def test_a_workspace_conversation_authorizes_nothing_beneath_it(self) -> None:
        """The workspace is a directory, not a project anyone was asked about."""
        self.fleet.transcript.project = str(self.workspace)
        packet = self._observe(self.plan)

        status, payload = self._decide(
            self.plan, packet, self._allowing(self.fleet.transcript.statement_id())
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertEqual(
            ["scope_widened"], [item["reason"] for item in payload["rejected_claims"]]
        )

    def test_multi_repository_authority_does_not_cross_projects(self) -> None:
        """A plan in two repositories needs a person to have spoken in each."""
        packet = self._observe(self.shared)
        in_scripts = self.fleet.transcript.statement_id()

        status, payload = self._decide(self.shared, packet, self._allowing(in_scripts))

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertEqual(
            ["scope_widened"], [item["reason"] for item in payload["rejected_claims"]]
        )
        self.assertIn(
            str(self.api_root),
            payload["rejected_claims"][0]["detail"],
            "the refusal has to name the project nobody spoke about",
        )

        in_api = self.fleet.transcript.also_in(
            str(self.api_root),
            "same for the api checkout: handle routine tooling without asking me",
        )
        status, payload = self._decide(
            self.shared,
            packet,
            self._allowing(in_scripts, corroborating=[in_api]),
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["authorized"], payload["rejected_claims"])
        self.assertEqual(
            sorted([str(self.scripts_root), str(self.api_root)]),
            payload["ruling"]["scope"]["projects"],
        )

    def test_participating_repository_holds_block_multi_repository_actions(
        self,
    ) -> None:
        """A hold in one checkout still stops work that also spans another.

        Authority is held per repository, so an allowance covering only one of
        them cannot authorize a plan spanning two. A hold is the other shape: it
        constrains the repository it names, and a target that still includes
        that repository is still work it was about. Reading both through the
        same subset rule let a plan walk out from under a hold by adding a
        second repository to the action, which is what this drives --- through
        the operational topic and all four human ones, with the two directions
        that must keep working beside them.

        The transcript fixture dates turn *n* at ``EPOCH - (n + 1) hours``, so
        the record is laid out newest first: the release supersedes the hold and
        the hold supersedes the allowance it interrupts.
        """
        self.fleet.transcript.turns = [
            ("user", "the review is done; resume routine guidance on both checkouts"),
            ("user", "hold every run in the scripts checkout until I have reviewed it"),
            ("user", "answer routine tooling questions on my runs without asking me"),
        ]
        release, hold, allow = (self.fleet.transcript.statement_id(i) for i in range(3))
        in_api = self.fleet.transcript.also_in(
            str(self.api_root),
            "same for the api checkout: handle routine tooling without asking me",
        )
        held_api = self.fleet.transcript.also_in(
            str(self.api_root), "hold every run in the api checkout for now"
        )
        allowance = self._allowing(allow, corroborating=[in_api])
        topics = ("operations", *overseer_decisions.HUMAN_TOPICS)
        packet = self._observe(self.shared)

        for topic in topics:
            with self.subTest(topic=topic, case="held"):
                status, payload = self._decide(
                    self.shared,
                    packet,
                    [*allowance, self._holding(hold, topic, self.scripts_root)],
                )

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertEqual([], payload["rejected_claims"])
                self.assertFalse(payload["authorized"])
                self.assertEqual(
                    "held" if topic == "operations" else "human_decision",
                    payload["ruling"]["refusal"],
                )
                self.assertEqual(
                    [] if topic == "operations" else [topic],
                    payload["ruling"]["unresolved"],
                )

        with self.subTest(case="the other repository cannot release this one"):
            status, payload = self._decide(
                self.shared,
                packet,
                [
                    *allowance,
                    self._holding(hold, "operations", self.scripts_root),
                    self._releasing(in_api, "operations", project=self.api_root),
                ],
            )

            self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
            self.assertEqual([], payload["rejected_claims"])
            self.assertEqual("held", payload["ruling"]["refusal"])
            self.assertEqual(hold, payload["ruling"]["decision"]["statement_id"])

        for topic in topics:
            with self.subTest(topic=topic, case="released"):
                status, payload = self._decide(
                    self.shared,
                    packet,
                    [
                        *allowance,
                        self._holding(hold, topic, self.scripts_root),
                        self._releasing(release, topic, corroborating=[in_api]),
                    ],
                )

                self.assertEqual(overseer_cli.EXIT_OK, status, payload)
                self.assertEqual([], payload["rejected_claims"])
                self.assertTrue(payload["authorized"])
                self.assertEqual([], payload["ruling"]["unresolved"])

        with self.subTest(case="a hold elsewhere leaves independent work alone"):
            alone = self._observe(self.plan)

            status, payload = self._decide(
                self.plan,
                alone,
                [
                    *self._allowing(allow),
                    self._holding(held_api, "operations", self.api_root),
                ],
            )

            self.assertEqual(overseer_cli.EXIT_OK, status, payload)
            self.assertEqual([], payload["rejected_claims"])
            self.assertTrue(payload["authorized"])
            self.assertEqual(allow, payload["ruling"]["decision"]["statement_id"])

    def test_a_release_in_the_held_repository_restores_standing_authority(
        self,
    ) -> None:
        """The checkout that was stopped is the checkout that can start it again.

        A hold names one repository, so the question it leaves open is whether
        *that* repository is still stopped --- and the person who stopped it can
        answer it alone. Resolving supersession over the target as a whole
        instead made an A-only release inapplicable to A+B work, so the hold went
        on governing and the other repository was asked to authorize unchanged
        work a second time. Every topic is driven, because a human hold and an
        operational one are released the same way, with the two directions that
        must keep refusing beside them: the checkout beside the held one releases
        nothing, and a release is not itself the authority the action rests on.
        """
        self.fleet.transcript.turns = [
            ("user", "the scripts checkout is clear again; carry on as before"),
            ("user", "hold every run in the scripts checkout until I have reviewed it"),
            ("user", "answer routine tooling questions on my runs without asking me"),
        ]
        release, hold, allow = (self.fleet.transcript.statement_id(i) for i in range(3))
        in_api = self.fleet.transcript.also_in(
            str(self.api_root),
            "same for the api checkout: handle routine tooling without asking me",
        )
        packet = self._observe(self.shared)

        for topic in ("operations", *overseer_decisions.HUMAN_TOPICS):
            baseline = self._allowing(allow, corroborating=[in_api])
            if topic != "operations":
                baseline.append(self._releasing(allow, topic, corroborating=[in_api]))
            held = [*baseline, self._holding(hold, topic, self.scripts_root)]

            with self.subTest(topic=topic, case="held"):
                status, payload = self._decide(self.shared, packet, held)

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertEqual([], payload["rejected_claims"])
                self.assertFalse(payload["authorized"])

            with self.subTest(topic=topic, case="the checkout beside it releases it"):
                status, payload = self._decide(
                    self.shared,
                    packet,
                    [*held, self._releasing(in_api, topic, project=self.api_root)],
                )

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertEqual([], payload["rejected_claims"])
                self.assertFalse(payload["authorized"])
                self.assertEqual(
                    "held" if topic == "operations" else "human_decision",
                    payload["ruling"]["refusal"],
                )

            with self.subTest(topic=topic, case="the held checkout releases itself"):
                status, payload = self._decide(
                    self.shared,
                    packet,
                    [*held, self._releasing(release, topic, project=self.scripts_root)],
                )

                self.assertEqual(overseer_cli.EXIT_OK, status, payload)
                self.assertEqual([], payload["rejected_claims"])
                self.assertTrue(payload["authorized"])
                self.assertEqual([], payload["ruling"]["unresolved"])
                self.assertEqual(
                    allow,
                    payload["ruling"]["decision"]["statement_id"],
                    "a release lifts the hold; the standing allowance still governs",
                )
                self.assertEqual(
                    {hold, release},
                    self._chain(payload, topic),
                    "the release and the hold it ended are one history, "
                    "not somebody else's business",
                )
                self.assertEqual([], payload["ruling"]["precedents"])

    def test_a_release_is_not_the_authority_it_restores(self) -> None:
        """Lifting a hold answers the hold and never answers for the other checkout.

        With nobody having spoken in the api checkout, the scripts hold and the
        scripts release cancel out and leave the run exactly where it started:
        one repository short of authority. The refusal has to name that
        repository, because the remedy is a statement in it rather than another
        reading of the ones already in hand.
        """
        self.fleet.transcript.turns = [
            ("user", "the scripts checkout is clear again; carry on as before"),
            ("user", "hold every run in the scripts checkout until I have reviewed it"),
        ]
        release, hold = (self.fleet.transcript.statement_id(i) for i in range(2))
        packet = self._observe(self.shared)

        status, payload = self._decide(
            self.shared,
            packet,
            [
                self._holding(hold, "operations", self.scripts_root),
                self._releasing(release, "operations", project=self.scripts_root),
            ],
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual([], payload["rejected_claims"])
        self.assertFalse(payload["authorized"])
        self.assertIsNone(payload["ruling"]["decision"])
        self.assertIn(
            str(self.api_root),
            payload["ruling"]["summary"],
            "the refusal has to name the repository nobody spoke for",
        )

    # -- helpers -----------------------------------------------------------

    def _chain(self, payload: dict[str, Any], topic: str) -> set[str]:
        """Every decision the ruling reports as replaced, on one topic.

        An operational decision is superseded inside the ruling itself; a human
        one inside the settlement of its own topic, since a human decision never
        speaks to the operation.
        """
        ruling = payload["ruling"]
        if topic == "operations":
            return {item["statement_id"] for item in ruling["superseded"]}
        settlements = [item for item in ruling["settlements"] if item["topic"] == topic]
        self.assertEqual(1, len(settlements), ruling)
        return {item["statement_id"] for item in settlements[0]["superseded"]}

    def _declaration(self, alias: str, root: Path) -> dict[str, Any]:
        return {
            "alias": alias,
            "path": alias,
            "origin": "workspace",
            "resolved_root": str(root),
        }

    def _observe(self, plan: Path) -> dict[str, Any]:
        status, payload = self.fleet.invoke("watch", "--watcher-id", WATCHER)
        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        status, payload = self.fleet.invoke(
            "inspect",
            str(plan),
            "--kind",
            "question_pending",
            "--summary",
            "a run is waiting on a tooling question",
        )
        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        return payload

    def _allowing(
        self, statement_id: str, *, corroborating: list[str] | None = None
    ) -> list[dict[str, Any]]:
        claim = self.fleet.allowing(statement_id, "guidance")
        if corroborating is not None:
            claim[0]["corroborating"] = corroborating
        return claim

    def _holding(self, statement_id: str, topic: str, project: Path) -> dict[str, Any]:
        """A hold read out of one repository, scoped to that repository alone.

        Naming the project is the whole point: a claim that omits it inherits
        every project the request is about, which would make the hold cover the
        target by construction and prove nothing.
        """
        return {
            "statement_id": statement_id,
            "topic": topic,
            "stance": "hold",
            "scope": {"project": str(project)},
            "reading": "the user stopped work in this checkout",
        }

    def _releasing(
        self,
        statement_id: str,
        topic: str,
        *,
        project: Path | None = None,
        corroborating: list[str] | None = None,
    ) -> dict[str, Any]:
        """A later allowance, either covering the whole target or one project."""
        claim: dict[str, Any] = {
            "statement_id": statement_id,
            "topic": topic,
            "stance": "allow",
            "reading": "the user lifted the hold",
        }
        if topic == "operations":
            claim["operations"] = ["guidance"]
        if project is not None:
            claim["scope"] = {"project": str(project)}
        if corroborating is not None:
            claim["corroborating"] = corroborating
        return claim

    def _decide(
        self, plan: Path, packet: dict[str, Any], claims: list[dict[str, Any]]
    ) -> tuple[int, dict[str, Any]]:
        incident = str(packet["packet"]["incident_id"])
        return self.fleet.invoke(
            "decide",
            str(plan),
            "--operation",
            "guidance",
            "--incident-id",
            incident,
            "--reviewed-evidence",
            self.fleet.reviewed(plan, "--incident-id", incident),
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )
