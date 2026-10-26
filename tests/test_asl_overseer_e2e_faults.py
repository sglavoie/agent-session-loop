"""Test asl overseer e2e faults."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    bridge_protocol,
    intervention,
    mailbox_vocabulary,
    overseer_cli,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store_models,
)
from tests import support_asl_overseer_assertions as assertions
from tests import support_asl_overseer_e2e as fixtures
from tests.support_asl_overseer_e2e import setUpModule as setUpModule
from tests.support_asl_overseer_e2e import tearDownModule as tearDownModule

ASL_TEST_LANE = "integration"


class FaultQualificationTests(assertions._WorldCase):
    """Each injected fault becomes a refusal that names itself, and nothing else.

    The assertion each of these makes twice is that the refusal is the *named*
    one and that the world did not move. A fault that produces some refusal is
    not qualified: an action stopped by the wrong guard is an action whose real
    guard was never exercised, and the two are indistinguishable from the exit
    status alone.
    """

    def test_an_opt_in_withdrawn_after_the_decision_stops_the_action(self) -> None:
        """The decision was sound when it was made. It is not a licence."""
        self.world.change(fixtures.AUTHORIZED)
        self.world.publish_turn(self.plan, (fixtures.AUTHORIZED,))
        packet = self.observed()
        status, decided = self.decide(packet, "commit", self.allowing("commit"))
        self.assertEqual(overseer_cli.EXIT_OK, status, decided)
        self.assertTrue(decided["authorized"])

        reviewed = self.reviewed(packet)
        self.world.write_config(authorized=False)
        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit"),
            "--message",
            "checkpoint work in progress",
            reviewed=reviewed,
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("unauthorized", payload["dispatch"]["refusal"])
        self.assertEqual([], self._porcelain_committed(fixtures.AUTHORIZED))

    def test_authorization_that_moves_during_a_dispatch_supersedes_it(self) -> None:
        """The recheck is two answers compared, so only a change between them.

        The configuration is rewritten while the adapter is preparing, and
        rewritten to something that still authorizes *this* plan: an unrelated
        project is withdrawn. A rewrite that revoked this one would be refused
        as ``unauthorized`` by the second check and would never reach the
        comparison this guard is.
        """
        packet = self.observed()
        # Bound before the hook is armed, so the rewrite lands inside the
        # dispatch rather than inside the reading that names the record.
        reviewed = self.reviewed(packet)

        def rewrite(argv: list[str]) -> None:
            if "history" in argv:
                self.world.on_call = None
                self.world.write_config(second=False)

        self.world.on_call = rewrite
        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
            reviewed=reviewed,
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("policy_superseded", payload["dispatch"]["refusal"])
        self.assertEqual(
            [],
            [
                message
                for message in self.world.messages(self.plan)
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ],
        )

    def test_configuration_the_binary_cannot_read_is_never_read_past(self) -> None:
        """Unreadable is not permissive. The answer is unavailable, so it is no."""
        packet = self.observed()
        reviewed = self.reviewed(packet)
        self.world.corrupt_config()

        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_ACTION,
            repositories=overseer_policy_client.repositories_for(
                self._candidate().repositories
            ),
            timeout=30.0,
        )
        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
            reviewed=reviewed,
        )

        self.assertFalse(decision.allowed)
        self.assertFalse(decision.available)
        self.assertIn(decision.failure, overseer_policy_client.FAILURE_REASONS)
        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("policy_unavailable", payload["dispatch"]["refusal"])
        self.assertEqual((), self.world.messages(self.plan)[1:])

    def test_a_binary_answering_another_contract_is_not_trusted(self) -> None:
        """A deployed pair that disagrees on the contract refuses on both sides."""
        packet = self.observed()
        reviewed = self.reviewed(packet)
        self.world.install_mismatched_binary()

        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_ACTION,
            repositories=overseer_policy_client.repositories_for(
                self._candidate().repositories
            ),
            timeout=30.0,
        )
        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
            reviewed=reviewed,
        )

        self.assertEqual("unsupported_contract", decision.failure)
        self.assertFalse(decision.available)
        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("policy_unavailable", payload["dispatch"]["refusal"])

    def test_a_group_that_cannot_stage_every_repository_commits_none(self) -> None:
        """The whole group is staged before any of it is committed.

        A locked index in the second repository stops the group in preparation,
        so the refusal arrives before the first commit rather than between the
        two. Nothing is committed and nothing is discarded: the work is still in
        both worktrees, staged where it got that far.
        """
        in_second = self._two_repository_run()
        (self.world.repos[fixtures.SECOND] / ".git" / "index.lock").write_text(
            "", encoding="utf-8"
        )
        packet = self.observed()

        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit", corroborating=(in_second,)),
            "--message",
            "checkpoint work in progress",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("staging_failed", payload["dispatch"]["detail"])
        self.assertIsNotNone(payload["escalation"])
        self.assertEqual([], self._porcelain_committed(fixtures.AUTHORIZED))
        self.assertEqual([], self._porcelain_committed(fixtures.SECOND))
        for alias in (
            fixtures.AUTHORIZED,
            fixtures.SECOND,
        ):
            self.assertTrue(any("work.txt" in line for line in self._porcelain(alias)))

    def test_a_commit_stopped_halfway_reports_the_half_it_made(self) -> None:
        """A partial group is reported, not repaired and not rolled back.

        Both repositories stage; the second cannot update its branch, so the
        group stops with one commit made. The commit that landed stays landed --
        the durable group is what a later retry reconciles against, and rolling
        a checkpoint back to make the group look tidy would be the one thing
        the requirements forbid.
        """
        in_second = self._two_repository_run()
        self._seal_refs(fixtures.SECOND)
        packet = self.observed()

        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit", corroborating=(in_second,)),
            "--message",
            "checkpoint work in progress",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertIsNotNone(payload["escalation"])
        self.assertEqual(1, len(self._porcelain_committed(fixtures.AUTHORIZED)))
        self.assertEqual([], self._porcelain_committed(fixtures.SECOND))

    def test_a_dispatch_left_in_doubt_blocks_the_next_one_until_it_settles(
        self,
    ) -> None:
        """A row a dead process left is answered before anything is retried."""
        self.world.change(fixtures.AUTHORIZED)
        self.world.publish_turn(self.plan, (fixtures.AUTHORIZED,))
        packet = self.observed()
        message = "checkpoint work in progress"
        self._strand_a_commit(packet, message)
        unreadable = self.world.repos[fixtures.AUTHORIZED] / ".git"
        unreadable.rename(unreadable.with_name(".git-away"))

        status, blocked = self.act(
            packet, "commit", self.allowing("commit"), "--message", message
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, blocked)
        self.assertEqual("outcome_uncertain", blocked["dispatch"]["refusal"])

        unreadable.with_name(".git-away").rename(unreadable)
        status, settled = self.world.invoke("reconcile", str(self.plan))

        self.assertEqual(overseer_cli.EXIT_OK, status, settled)
        self.assertTrue(settled["settled"])
        self.assertEqual(
            ["failed"], [item["receipt"]["outcome"] for item in settled["outcomes"]]
        )

        status, retried = self.act(
            packet, "commit", self.allowing("commit"), "--message", message
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, retried)
        self.assertEqual(1, len(self._porcelain_committed(fixtures.AUTHORIZED)))

    def test_a_second_supervisor_cannot_act_on_a_plan_it_does_not_hold(self) -> None:
        """Two supervisors, one plan: the lease decides, and it says who holds it."""
        packet = self.observed()

        sweep = self.watch(watcher=fixtures.SECOND_WATCHER)["sweeps"][0]
        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
            watcher=fixtures.SECOND_WATCHER,
        )

        self.assertEqual(
            ["owned_elsewhere"], [item["reason"] for item in sweep["excluded"]]
        )
        self.assertEqual([], sweep["enrolled"])
        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("ownership_lost", payload["dispatch"]["refusal"])
        self.assertIn(fixtures.WATCHER, payload["dispatch"]["detail"])
        self.assertEqual(
            [],
            [
                message
                for message in self.world.messages(self.plan)
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ],
        )

    def test_a_fleet_past_the_display_cap_is_still_reported_whole(self) -> None:
        """Discovery is unbounded where the queue view is capped, on purpose."""
        for index in range(bridge_protocol.MAX_ROWS + 4):
            self.world.write_run(f"fleet-{index:04d}")
        expected = bridge_protocol.MAX_ROWS + 5

        status, payload = self.world.invoke("scan")

        self.assertEqual(overseer_cli.EXIT_OK, status, payload["considered"])
        self.assertTrue(payload["complete"])
        self.assertGreater(expected, bridge_protocol.MAX_ROWS)
        self.assertEqual(expected, payload["considered"])
        self.assertEqual(expected, len(payload["candidates"]))

    # -- helpers -----------------------------------------------------------

    def _two_repository_run(self) -> str:
        """Redeclare the run over both writable repositories, both dirty.

        Authority follows the repositories rather than the directory above
        them, so a run over two projects needs a person to have spoken in each.
        The second repository gets its own conversation and the id of that turn
        is returned, to be cited beside the first.
        """
        self.world.write_run(
            "plan-tt",
            aliases=(
                fixtures.AUTHORIZED,
                fixtures.SECOND,
            ),
        )
        second = self.world.write_transcript(
            fixtures.SECOND,
            [("user", fixtures.STANDING_INSTRUCTION)],
        )
        for alias in (
            fixtures.AUTHORIZED,
            fixtures.SECOND,
        ):
            self.world.change(alias)
        self.world.publish_turn(
            self.plan,
            (fixtures.AUTHORIZED, fixtures.SECOND),
        )
        return self.world.statement_id(second, 0)

    def _seal_refs(self, alias: str) -> None:
        """Let a repository stage and stop it committing.

        The branch cannot be locked, so `git commit` fails after the index is
        built and before anything moves -- which is a commit that failed, not a
        repository that was never asked.
        """
        heads = self.world.repos[alias] / ".git" / "refs" / "heads"
        heads.chmod(0o500)
        self.addCleanup(heads.chmod, 0o700)

    def _strand_a_commit(
        self, packet: dict[str, Any], message: str
    ) -> overseer_store_models.ActionRecord:
        """Leave exactly the trace a process that died mid-commit leaves.

        The attempt is written before the commit and settled after it, so a
        process killed between the two leaves an ``attempted`` row and nothing
        else. That row is written here directly, because the alternative is
        killing a real dispatcher at a real instant and hoping it was that one.
        """
        ledger = self.world.open_ledger(state_dir=self.world.state_root)
        candidate = self._candidate()
        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_ACTION,
            repositories=overseer_policy_client.repositories_for(
                candidate.repositories
            ),
            timeout=30.0,
        )
        facts = intervention.inspect_run(
            str(self.plan), state_dir=self.world.state_root
        )
        return ledger.attempt_action(
            grant=decision.grant(),
            plan_dir=str(self.plan),
            operation="commit",
            target=overseer_dispatch.Commit(message=message).target(),
            incident_id=int(packet["packet"]["incident_id"]),
            progress_token=overseer_dispatch.progress_token(facts),
        )


if __name__ == "__main__":
    unittest.main()
