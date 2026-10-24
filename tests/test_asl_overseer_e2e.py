"""The overseer against a real AgentMux, and the faults that have to survive it.

Every other overseer module tests a boundary with the world injected. This one
removes the injection: `amx` is built from source at the start of the run, the
repositories are real Git checkouts, the configuration is a real `config.toml`
reached through `AMX_CONFIG`, the transcripts are real harness files under a
real profile home, the state corpus is read through the real
:func:`agent_session_loop.intervention.inspect_run`, and the processes a lock
names are real processes. What remains substituted is exactly two things, and
both are substituted *around* the real code rather than instead of it: the
``resolve_binary`` seam names the freshly built binary rather than whatever is
on ``PATH``, and the ``run_command`` seam adds ``AMX_CONFIG`` to the child's
environment instead of mutating this process's. That second wrapper is also the
instrumentation: every AgentMux invocation the overseer makes, and everything it
said back, is recorded, which is what lets the exclusion tests assert on what
was *disclosed* rather than on what was read.

*Historical replay.* ``HistoricalReplayTests`` rebuilds the incident shapes the
reviewed pauses came in as -- a checkpoint recovery on a dirty worktree, a
dirty-worktree pause answered with guidance, a stale question that outlived the
run waiting on it, an explicit hold, and a decision a later one superseded --
and drives each through the whole seven-step loop. The claim under test is the
session's own: the recoveries succeed where a person authorized them and the
escalations stay escalations. The fixtures are sanitized reconstructions, not
replayed transcripts: no incident identifier, evidence location or sanitized
corpus was ever recorded for these pauses, so what is reproducible is the
*shape* -- the pause reason, the evidence available, and the decision the shape
forces. Anything else would be a fixture claiming a provenance it does not have.

*Fault qualification.* ``FaultQualificationTests`` injects the failures that
have to become refusals rather than outcomes: an opt-in withdrawn between the
decision and the action, an opt-in withdrawn *during* a dispatch (which is the
only way to reach ``policy_superseded``, because the guard compares two answers
to the same question), configuration AgentMux cannot read, a binary answering a
contract this client does not speak, a partial multi-repository commit stopped
by a locked index, an action left in doubt by a process that died, two
supervisors contending for one lease, and a fleet past the bridge's display cap.

*Exclusion invariants.* ``ExclusionInvariantTests`` puts a second, excluded
project beside the authorized one, with its own repository and its own
conversation carrying a marked secret. Identity discovery is permitted -- amx
has to read a line of that transcript to know whose it is -- so the invariant
is not "never opened" but "never disclosed and never written": the secret
appears in no answer the overseer received, the withheld entry names nothing
that locates the conversation, and the excluded repository and its transcript
are byte-for-byte and mtime-for-mtime what they were. It is asserted across
every operation the dispatcher has an adapter for, plus the one it deliberately
does not.

The ``resume`` success path is the one place this module does not drive the
whole ``asl overseer act`` leaf. :func:`overseer_cli._adapters` builds the
resume adapter without a ``launch`` seam, so an authorized resume through the
leaf would spawn a real ``asl run`` against the fixture's plan. That path is
therefore driven one level down, through :func:`overseer_dispatch.dispatch`
itself with every other collaborator still real, and the launch replaced by one
that starts a fake harness process and moves the run the way a resumed runner
would. The refusal path -- which is what the historical incident actually was --
goes through the leaf.

This module builds a Go binary and spawns processes, so it belongs to
``INTEGRATION_MODULES`` and never to ``PORTABLE_MODULES``. It skips when the
AgentMux source is not beside this checkout, because the pair is developed
together but distributed separately. Setting ``AMX_ASL_INTEGRATION`` turns that
skip into a failure, which is the same opt-in shape as the paired installed
gate in ``sglavoie_agentmux:justfile``: a gate that silently skips is a gate
that reports success for having done nothing.
"""

from __future__ import annotations

import unittest

from agent_session_loop import (
    overseer_cli,
    overseer_decisions,
    overseer_discovery,
    overseer_policy_client,
)
from tests import support_asl_overseer_assertions as assertions
from tests import support_asl_overseer_e2e as fixtures
from tests.support_asl_overseer_e2e import setUpModule as setUpModule
from tests.support_asl_overseer_e2e import tearDownModule as tearDownModule

ASL_TEST_LANE = "integration"


class PairedBoundaryTests(assertions._WorldCase):
    """The binary answers, and it answers the contract this client speaks."""

    def test_the_built_binary_answers_the_policy_contract(self) -> None:
        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(
                overseer_policy_client.Repository(
                    alias=fixtures.AUTHORIZED,
                    path=str(self.world.repos[fixtures.AUTHORIZED]),
                ),
            ),
            timeout=30.0,
        )

        self.assertTrue(decision.allowed, decision.summary)
        self.assertTrue(decision.available)
        self.assertEqual(
            overseer_policy_client.POLICY_CONTRACT_VERSION, decision.contract_version
        )
        self.assertTrue(decision.config_fingerprint)
        self.assertTrue(decision.request_digest)
        self.assertEqual(0, decision.exit_status)

    def test_the_transcript_the_person_spoke_in_is_reachable(self) -> None:
        history = self.world.read_history(
            plan_dir=str(self.plan),
            repositories=(
                overseer_policy_client.Repository(
                    alias=fixtures.AUTHORIZED,
                    path=str(self.world.repos[fixtures.AUTHORIZED]),
                ),
            ),
            timeout=30.0,
        )

        self.assertTrue(history.allowed, history.summary)
        directories = {item.directory for item in history.conversations}
        self.assertEqual({self.world.workspace_root}, directories)
        statements = overseer_decisions.statements_from_history(history)
        self.assertIn("user_instruction", {item.voice for item in statements})

    def test_the_scope_a_statement_carries_is_the_scope_a_claim_needs(self) -> None:
        """The one alignment the whole module rests on, asserted directly.

        Both sides have to be the same identity for the same project, and that
        identity is the repository AgentMux authorized: the directory it scopes
        a conversation to, and the resolved root the run declares. Anything
        else -- a workspace holding several checkouts, most of all -- names
        something nobody was ever asked about.
        """
        history = self.world.read_history(
            plan_dir=str(self.plan),
            repositories=(
                overseer_policy_client.Repository(
                    alias=fixtures.AUTHORIZED,
                    path=str(self.world.repos[fixtures.AUTHORIZED]),
                ),
            ),
            timeout=30.0,
        )
        statement = overseer_decisions.statements_from_history(history)[0]
        inventory = overseer_discovery.build_inventory(state_dir=self.world.state_root)
        candidate = next(
            item for item in inventory.candidates if item.plan_dir == str(self.plan)
        )

        self.assertEqual(
            [item.resolved_root for item in candidate.repositories],
            [statement.scope.project],
        )
        self.assertEqual(
            (statement.scope.project,), overseer_cli.project_identities(candidate)
        )


if __name__ == "__main__":
    unittest.main()
