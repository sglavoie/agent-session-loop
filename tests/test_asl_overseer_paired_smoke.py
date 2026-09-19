"""The *installed* pair, exercised end to end from the ASL side.

``tests.test_asl_overseer_e2e`` builds ``amx`` from the checkout beside this one
and judges that. This module judges what an operator actually has: the ``amx``
on ``PATH`` and the ``asl`` wrapper on ``PATH``. The two questions are different
and both matter. A checkout that passes the source gate says the pair is
consistent *as written*; only this one says the pair is consistent *as deployed*,
which is the pair that runs against real plans. A version skew between them is
invisible to every other gate in either repository.

It is deliberately built on the source gate's fixtures rather than beside them.
:class:`~tests.test_asl_overseer_e2e._World` already constructs the isolation
this gate needs -- its own Git repositories, its own state root, its own
``config.toml`` reached through ``AMX_CONFIG``, its own profile home and
transcripts, and real but fake harness processes -- and reusing it is what makes
the two gates comparable: same world, same assertions, a different claim about
which pair they are a statement about. The only substitution is the one seam
that has to move, ``_World.binary``.

What it covers is the loop's five load-bearing steps against that installed
pair: *policy* and *history* (the two contracts the two programs share),
*watch* (discovery reaching the binary at all), *checkpoint* (an authorized
mutation that lands), and *resume* (an authorized restart). Each is one command
the operator's own runs go through.

*Compatibility, both directions.* The installed binary must answer the contract
this client speaks, and a component that does not must be refused rather than
read past. The refusal case is staged with a shim over the real binary rather
than by installing an old one, because uninstalling the operator's working
AgentMux to prove a refusal would be a worse trade than simulating the skew.

This module spawns processes and needs an installed pair, so it belongs to
``INTEGRATION_MODULES`` and never to ``PORTABLE_MODULES``. It skips when there
is no installed pair to judge, because a checkout is a legitimate place to have
none -- including a checkout whose ``asl`` on ``PATH`` is some *other*
checkout's, which is what a second copy of the repository looks like from here.
Setting ``AMX_ASL_OVERSEER_SMOKE`` turns that skip into a failure, which is what
``just overseer-integration`` in ``sglavoie_agentmux`` sets: a gate that silently
skips is a gate that reports success for having done nothing.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    mailbox,
    overseer_cli,
    overseer_decisions,
    overseer_history,
    overseer_policy_client,
)
from tests import support
from tests.test_asl_overseer_e2e import (
    AUTHORIZED,
    _WorldCase,
)

#: Set to any non-empty value other than a falsehood to make a missing installed
#: pair fail this module rather than skip it. Mirrors ``AMX_ASL_INTEGRATION`` in
#: the source gate and the paired installed gate in the AgentMux justfile.
SMOKE_ENV = "AMX_ASL_OVERSEER_SMOKE"

#: Names the installed ``amx`` when it is not on this shell's ``PATH``. The
#: AgentMux recipe sets it, because ``just`` recipes do not necessarily inherit
#: the interactive ``PATH`` that put ``~/.local/bin`` first.
BINARY_ENV = "AMX_INSTALLED_BINARY"

_INSTALLED: dict[str, Any] = {}


# -- the gate -----------------------------------------------------------------


def _required() -> bool:
    value = os.environ.get(SMOKE_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _locate_binary() -> Path | None:
    """Find the installed ``amx``, the way the policy client finds it."""
    named = os.environ.get(BINARY_ENV, "").strip()
    if named:
        candidate = Path(named).expanduser()
        return candidate if os.access(candidate, os.X_OK) else None
    found = shutil.which(overseer_policy_client.AMX_BINARY)
    return Path(found) if found else None


def _locate_wrapper() -> Path | None:
    """Find the installed ``asl`` wrapper, whatever it is a link to."""
    found = shutil.which("asl")
    return Path(found).resolve() if found else None


def _unavailable(reason: str) -> None:
    """Skip this module, or fail it when the paired smoke gate was asked for."""
    detail = (
        f"{reason}; install both programs and rerun, or set {BINARY_ENV} to the "
        "installed amx, to run the paired smoke gate"
    )
    if _required():
        raise AssertionError(f"{SMOKE_ENV} is set but {detail}")
    raise unittest.SkipTest(detail)


def setUpModule() -> None:
    """Locate the installed pair once, before any fixture reaches for it.

    Presence is an availability question and answered here; compatibility is
    the assertion and answered by the tests. An installed AgentMux too old to
    have the commands at all is therefore a failure of
    :class:`InstalledCompatibilityTests`, not a skip -- which is the point of
    installing before smoking.
    """
    binary = _locate_binary()
    if binary is None:
        _unavailable("no executable `amx` is installed")
        return
    wrapper = _locate_wrapper()
    if wrapper is None:
        _unavailable("no `asl` wrapper is on PATH")
        return
    if not wrapper.is_relative_to(support.REPO_ROOT):
        # Which checkout the wrapper belongs to is availability, not
        # compatibility: an `asl` from somewhere else is not this pair installed
        # badly, it is this pair not installed at all, and judging it would
        # report on a version neither half of this checkout wrote. A copy of the
        # repository at a second path -- a worktree, a workbench, a release
        # staging tree -- is exactly that case, and a legitimate one.
        _unavailable(
            f"the installed `asl` wrapper resolves to {wrapper}, which is "
            f"outside {support.REPO_ROOT} and so belongs to another checkout"
        )
        return
    _INSTALLED["binary"] = binary
    _INSTALLED["wrapper"] = wrapper


class _InstalledCase(_WorldCase):
    """The source gate's world, built around the installed binary instead."""

    def setUp(self) -> None:
        self.binary = Path(_INSTALLED["binary"])
        super().setUp()


# -- what the gate is a statement about ---------------------------------------


class InstalledPairTests(_InstalledCase):
    """Which two artifacts this module's other assertions are about.

    Asserted rather than assumed, because every claim below is only worth what
    the identification of the pair is worth. A smoke gate that quietly ran
    against a checkout's own sources while reporting on the installation would
    be worse than no gate.
    """

    def test_the_installed_wrapper_resolves_to_the_package_under_test(self) -> None:
        """The ASL half: `asl` on PATH is this checkout, not another one.

        ``setUpModule`` now declines to run the gate at all when this does not
        hold, so reaching this assertion and failing it would mean the two
        disagree about what "installed" means. Asserted here anyway, because the
        claim every other test in the module rests on should be visible as a
        claim rather than only as a skip condition.
        """
        wrapper = Path(_INSTALLED["wrapper"])

        self.assertTrue(
            wrapper.is_relative_to(support.REPO_ROOT),
            f"installed `asl` resolves to {wrapper}, outside {support.REPO_ROOT}",
        )

    def test_the_agentmux_half_is_the_installed_binary(self) -> None:
        """The AgentMux half: every invocation goes to the installed file."""
        self.observed()

        self.assertTrue(self.world.calls, "the overseer never invoked AgentMux")
        for call in self.world.calls:
            self.assertTrue(
                call["argv"].startswith(str(_INSTALLED["binary"])),
                f"invocation did not use the installed binary: {call['argv']}",
            )

    def test_the_world_the_gate_runs_in_is_the_operators_in_nothing(self) -> None:
        """Repositories, state, configuration and processes are all throwaway.

        The gate mutates Git repositories, commits to them, writes run state and
        restarts runs. Every one of those is destructive somewhere, so the thing
        worth asserting directly is that the somewhere is a temporary directory
        this test owns and deletes.
        """
        self.observed()

        for repository in self.world.repos.values():
            self.assertTrue(repository.is_relative_to(self.world.root))
        self.assertTrue(self.world.state_root.is_relative_to(self.world.root))
        self.assertTrue(self.world.plans_root.is_relative_to(self.world.root))
        self.assertTrue(self.world.home.is_relative_to(self.world.root))
        self.assertTrue(self.world.config_path.is_relative_to(self.world.root))
        for call in self.world.calls:
            self.assertNotIn(str(Path.home() / ".config" / "agent-mux"), call["argv"])
        self.assertIsNone(self.harness.poll(), "the fake harness is not running")


# -- the two shared contracts -------------------------------------------------


class InstalledCompatibilityTests(_InstalledCase):
    """The installed binary answers the contracts this client speaks."""

    def test_the_installed_binary_answers_the_policy_contract(self) -> None:
        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(
                overseer_policy_client.Repository(
                    alias=AUTHORIZED, path=str(self.world.repos[AUTHORIZED])
                ),
            ),
            timeout=30.0,
        )

        self.assertTrue(decision.available, decision.summary)
        self.assertIsNone(decision.failure, decision.summary)
        self.assertEqual(
            overseer_policy_client.POLICY_CONTRACT_VERSION, decision.contract_version
        )
        self.assertTrue(decision.allowed, decision.summary)

    def test_the_installed_binary_answers_the_history_contract(self) -> None:
        history = self.world.read_history(
            plan_dir=str(self.plan),
            repositories=(
                overseer_policy_client.Repository(
                    alias=AUTHORIZED, path=str(self.world.repos[AUTHORIZED])
                ),
            ),
            timeout=30.0,
        )

        self.assertTrue(history.available, history.summary)
        self.assertIsNone(history.failure, history.summary)
        self.assertEqual(
            overseer_history.HISTORY_CONTRACT_VERSION, history.contract_version
        )
        self.assertTrue(history.allowed, history.summary)
        self.assertIn(
            "user_instruction",
            {
                statement.voice
                for statement in overseer_decisions.statements_from_history(history)
            },
        )


class OlderComponentTests(_InstalledCase):
    """A component older than its partner is refused, on both contracts.

    Version 1 is the first contract either side published, so a genuinely older
    AgentMux is not one reporting a lower number -- there is no lower number. It
    is one whose payload carries no contract block at all, because it predates
    the block. That is what is staged here, and what has to read as unavailable
    rather than as permission.
    """

    def test_an_older_component_is_refused_on_the_policy_contract(self) -> None:
        self.world.install_predating_binary()

        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(
                overseer_policy_client.Repository(
                    alias=AUTHORIZED, path=str(self.world.repos[AUTHORIZED])
                ),
            ),
            timeout=30.0,
        )

        self.assertEqual("unsupported_contract", decision.failure)
        self.assertFalse(decision.available)
        self.assertFalse(decision.allowed)

    def test_an_older_component_is_refused_on_the_history_contract(self) -> None:
        self.world.install_predating_binary()

        history = self.world.read_history(
            plan_dir=str(self.plan),
            repositories=(
                overseer_policy_client.Repository(
                    alias=AUTHORIZED, path=str(self.world.repos[AUTHORIZED])
                ),
            ),
            timeout=30.0,
        )

        self.assertEqual("unsupported_contract", history.failure)
        self.assertFalse(history.available)
        self.assertFalse(history.allowed)
        self.assertEqual((), history.conversations)

    def test_an_older_component_leaves_the_action_undone(self) -> None:
        """The refusal is the outcome, not a step on the way to one."""
        self.world.change(AUTHORIZED)
        self.world.publish_turn(self.plan, (AUTHORIZED,))
        packet = self.observed()
        self.world.install_predating_binary()

        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit"),
            "--message",
            "chore: checkpoint work in progress",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("policy_unavailable", payload["dispatch"]["refusal"])
        self.assertEqual([], self._porcelain_committed(AUTHORIZED))
        self.assertEqual(["?? work.txt"], self._porcelain(AUTHORIZED))


# -- the loop, against what is installed --------------------------------------


class InstalledLoopTests(_InstalledCase):
    """Watch, checkpoint and resume, each through the installed pair."""

    def test_watch_finds_the_paused_run_and_inspect_carries_the_evidence(
        self,
    ) -> None:
        """Discovery and the evidence packet, each authorized before it reads.

        Both steps ask ``overseer policy`` and neither asks ``overseer history``:
        the packet is the run's own state corpus, and the transcript evidence a
        decision rests on is fetched at decision time by ``decide`` and ``act``.
        Asserting the absence keeps that boundary visible here rather than only
        in the module that draws it.
        """
        watched = self.watch()
        after_watch = len(self.world.calls)
        packet = self.inspect()

        enrolled = [
            item["plan_dir"]
            for sweep in watched["sweeps"]
            for item in sweep["enrolled"]
        ]

        self.assertIn(str(self.plan), enrolled)
        self.assertTrue(packet["token"])
        self.assertTrue(packet["packet"]["incident_id"])
        self.assertGreater(after_watch, 0, "the sweep never asked for authorization")
        self.assertGreater(len(self.world.calls), after_watch)
        for call in self.world.calls:
            self.assertIn("overseer policy", call["argv"])
            self.assertNotIn("overseer history", call["argv"])

    def test_an_authorized_checkpoint_commits_the_runs_own_work(self) -> None:
        """A checkpoint an installed pair authorizes, and the commit it lands."""
        self.world.change(AUTHORIZED)
        self.world.publish_turn(self.plan, (AUTHORIZED,))
        packet = self.observed()

        status, decision = self.decide(packet, "commit", self.allowing("commit"))
        self.assertEqual(overseer_cli.EXIT_OK, status, decision)
        self.assertTrue(decision["authorized"], decision)

        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit"),
            "--message",
            "chore: checkpoint work in progress",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIn(
            "overseer history",
            "\n".join(call["argv"] for call in self.world.calls),
            "the decision never fetched the evidence it rests on",
        )
        self.assertIn(
            "work.txt",
            support.git_stdout(
                self.world.repos[AUTHORIZED],
                "show",
                "--name-only",
                "--format=",
                "HEAD",
            ),
        )
        self.assertEqual([], self._porcelain(AUTHORIZED))

    def test_an_authorized_resume_restarts_the_run(self) -> None:
        """The resume path, with the launcher replaced and nothing else.

        Driven through :meth:`_dispatch_resume` for the reason the source gate
        drives it there: the leaf's resume adapter has no ``launch`` seam, so an
        authorized resume through the leaf would spawn a real ``asl run`` against
        the fixture's plan. The policy client, the binary and the ledger are the
        installed pair's own.
        """
        self.world.write_run(
            "plan-tt",
            pause_reason="waiting_for_reply",
            **{"pause_policy": {"user_action": "reply"}},
        )
        self.world.release_lock(self.plan)
        question = self.world.ask_question(self.plan, "which lint profile?")
        mailbox.append_resolution(
            self.plan,
            question,
            mailbox.RESOLUTION_ACTION_CONTINUE,
            expected_run_id=f"{self.plan.name}-run-1",
            note="the question was superseded before anyone answered it",
            state_dir=self.world.state_root,
        )
        packet = self.observed()

        dispatch = self._dispatch_resume(packet)

        self.assertTrue(dispatch.performed, dispatch.detail)
        self.assertFalse(dispatch.escalated, dispatch.detail)
        self.assertEqual([str(self.plan)], self.launched)
        self.assertFalse(self.world.read_state(self.plan)["paused"])


if __name__ == "__main__":
    unittest.main()
