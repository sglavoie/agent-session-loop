"""Shared fixtures for test_asl_overseer_e2e."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    clock,
    overseer_cli,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_resume,
)
from tests import support
from tests import support_asl_overseer_e2e as fixtures
from tests import support_asl_overseer_world as world


class _WorldCase(unittest.TestCase):
    """One authorized plan, one excluded project, one person's instruction."""

    #: The ``amx`` the world is built around. ``None`` means the binary this
    #: module built from source. The paired smoke gate subclasses these fixtures
    #: with the installed one, so the world stays defined in one place.
    binary: Path | None = None

    def setUp(self) -> None:
        self.world = world._World(self, binary=self.binary)
        self.session = self.world.write_transcript(
            fixtures.AUTHORIZED,
            [
                ("user", fixtures.STANDING_INSTRUCTION),
                ("assistant", "understood; I will checkpoint and answer routinely"),
            ],
        )
        self.excluded_session = self.world.write_transcript(
            fixtures.EXCLUDED,
            [
                (
                    "user",
                    f"the deploy key for staging is {fixtures.SECRET}",
                ),
                ("assistant", "noted"),
            ],
        )
        (self.world.repos[fixtures.EXCLUDED] / "credentials.txt").write_text(
            f"{fixtures.SECRET}\n", encoding="utf-8"
        )
        self.plan = self.world.write_run("plan-tt")
        self.harness = self.world.start_fake_harness()
        self.world.hold_lock(self.plan, self.harness.pid)

    # -- the loop ----------------------------------------------------------

    def watch(self, *extra: str, watcher: str = fixtures.WATCHER) -> dict[str, Any]:
        status, payload = self.world.invoke("watch", "--watcher-id", watcher, *extra)
        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        return payload

    def inspect(self, plan: Path | None = None, *extra: str) -> dict[str, Any]:
        status, payload = self.world.invoke(
            "inspect",
            str(plan or self.plan),
            "--kind",
            "question_pending",
            "--summary",
            "a paused run is waiting on somebody",
            *extra,
        )
        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        return payload

    def observed(self, plan: Path | None = None) -> dict[str, Any]:
        self.watch()
        return self.inspect(plan)

    def allowing(
        self,
        *operations: str,
        index: int = 0,
        corroborating: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """The claim a host writes, optionally resting on more than one turn.

        ``corroborating`` is what carries a run declared over two repositories:
        each project needs a person to have spoken in it, and citing the second
        conversation is how the claim says so.
        """
        claim: dict[str, Any] = {
            "statement_id": self.world.statement_id(self.session, index),
            "topic": "operations",
            "stance": "allow",
            "operations": list(operations),
            "reading": "the user authorized routine work on their runs",
        }
        if corroborating:
            claim["corroborating"] = list(corroborating)
        return [claim]

    def reviewed(self, packet: dict[str, Any], *, plan: Path | None = None) -> str:
        """The digest naming the record a host was shown, which binds its claims.

        Claims are a reading of something, and only the reading that prints the
        statements issues a name for what was read. Every claim-bearing step
        below carries one, the way a host has to.
        """
        status, listing = self.world.invoke(
            "decide",
            str(plan or self.plan),
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--list-statements",
        )
        assert status == 0, listing
        return str(listing["evidence_digest"])

    def decide(
        self, packet: dict[str, Any], operation: str, claims: list[dict[str, Any]]
    ) -> tuple[int, dict[str, Any]]:
        return self.world.invoke(
            "decide",
            str(self.plan),
            "--operation",
            operation,
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--reviewed-evidence",
            self.reviewed(packet),
            "--claims-file",
            self.world.claims_file(claims),
        )

    def act(
        self,
        packet: dict[str, Any],
        operation: str,
        claims: list[dict[str, Any]],
        *extra: str,
        plan: Path | None = None,
        watcher: str = fixtures.WATCHER,
        reviewed: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        return self.world.invoke(
            "act",
            str(plan or self.plan),
            "--watcher-id",
            watcher,
            "--operation",
            operation,
            "--run-id",
            packet["run_id"],
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--token",
            packet["token"],
            "--scope",
            packet["scope"],
            "--reviewed-evidence",
            self.reviewed(packet, plan=plan) if reviewed is None else reviewed,
            "--claims-file",
            self.world.claims_file(claims),
            *extra,
        )

    # -- reading the world back --------------------------------------------

    def _candidate(self, plan: Path | None = None) -> overseer_discovery.Candidate:
        inventory = overseer_discovery.build_inventory(state_dir=self.world.state_root)
        wanted = str(plan or self.plan)
        return next(item for item in inventory.candidates if item.plan_dir == wanted)

    def _porcelain(self, alias: str) -> list[str]:
        return [
            line
            for line in support.git_stdout(
                self.world.repos[alias], "status", "--porcelain", "-uall"
            ).splitlines()
            if line
        ]

    def _porcelain_committed(self, alias: str) -> list[str]:
        """Commits carrying an overseer checkpoint trailer, newest first."""
        return [
            line
            for line in support.git_stdout(
                self.world.repos[alias],
                "log",
                "--format=%H",
                "--grep=checkpoint",
            ).splitlines()
            if line
        ]

    # -- driving a resume --------------------------------------------------

    def _dispatch_resume(self, packet: dict[str, Any]) -> overseer_dispatch.Dispatch:
        """The resume path, one level below the leaf and otherwise unchanged.

        :func:`overseer_cli._adapters` builds the resume adapter without a
        ``launch`` seam, so an authorized resume through the leaf would spawn a
        real ``asl run`` against the fixture's plan. Everything else -- the
        ledger, the lease, the policy client, the binary, the state corpus --
        is the leaf's own.
        """
        self.launched: list[str] = []
        plan_dir = str(self.plan)
        ledger = self.world.open_ledger(state_dir=self.world.state_root)
        candidate = self._candidate()
        repositories = overseer_policy_client.repositories_for(candidate.repositories)
        action = overseer_dispatch.Action(
            plan_dir=plan_dir,
            run_id=packet["run_id"],
            incident_id=packet["packet"]["incident_id"],
            scope=packet["scope"],
            token=packet["token"],
            repositories=repositories,
            payload=overseer_dispatch.Resume(session_id=""),
        )

        def launch(context: overseer_resume.LaunchContext) -> overseer_resume.Launched:
            """Start a fake harness and move the run, as a resumed runner does."""
            child = self.world.start_fake_harness()
            self.launched.append(context.plan_dir)
            record = self.world.read_state(self.plan)
            record["paused"] = False
            record["pause_reason"] = None
            record["active_turn"] = {
                "session_id": "1",
                "turn_index": 4,
                "last_activity_at": clock.utc_iso_now(),
            }
            self.world.write_state(self.plan, record)
            return overseer_resume.Launched(pid=child.pid, argv=("fake", "resume"))

        registry = overseer_resume.adapters(
            authority=self._ruling,
            state_dir=self.world.state_root,
            launch=launch,
        )
        return overseer_dispatch.dispatch(
            action=action,
            watcher_id=fixtures.WATCHER,
            ledger=ledger,
            adapters=registry,
            authorize=self.world.authorize,
            state_dir=self.world.state_root,
        )

    def _ruling(self, action: overseer_dispatch.Action) -> overseer_decisions.Ruling:
        """The ruling the leaf would have reached, reached the same way."""
        scope = overseer_decisions.Scope(
            projects=tuple(item.path for item in action.repositories),
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident=str(action.incident_id),
        )
        history = self.world.read_history(
            plan_dir=action.plan_dir,
            repositories=action.repositories,
            timeout=30.0,
        )
        statements = overseer_decisions.statements_from_history(history)
        register = overseer_decisions.register(
            statements=statements,
            claims=(
                overseer_decisions.Claim(
                    statement_id=self.world.statement_id(self.session, 0),
                    topic="operations",
                    stance="allow",
                    scope=scope,
                    operations=("resume",),
                    reading="the user authorized routine work on their runs",
                ),
            ),
        )
        return overseer_decisions.resolve(
            register=register,
            request=overseer_decisions.Request(
                operation=action.payload.operation, scope=scope
            ),
        )
