"""The `verify` operation against real completion contracts."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    events,
    overseer_cli,
    overseer_discovery,
    overseer_verify,
    plans_files,
    plans_registry,
    state,
    workspaces,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import ACTION, PLAN_SESSION, REGISTERED_ORIGIN

from .support import write_multi_repo_plan

ASL_TEST_PORTABLE = True


class VerificationOperationTests(_OverseerCase):
    """That `verify` runs a plan's own check, and refuses anything else.

    The two tests are one claim read from both sides. A declared check runs and
    its real result is reported, so the operation is not a stub that always
    refuses; an undeclared name is refused, so it is not a shell that always
    obliges. Either half alone is satisfiable by a mistake.

    The contract is a real session file and the check is run by the real
    :func:`agent_session_loop.verification.run_check`, which is what makes the
    pass/fail a fact about the filesystem rather than about a fake. A
    ``file_exists`` check keeps that true without spawning anything, so this
    class stays inside `test_the_loop_spawns_no_process`'s promise.

    Three more tests read the same operation against a *registered* plan, whose
    context is built from a manifest --- a file in a repository, which a turn
    can rewrite while an action is in flight. One shows the registered plan
    verifying normally, so the other two are known to refuse because the
    manifest grew a participant and not because registered mode never worked
    here; the other two expand it, once between the observation and the action
    and once inside the dispatch itself, and require the check never to run.

    The last two remove the manifest instead of growing it, which is the edit
    that makes the plan *legacy* rather than differently registered. A legacy
    reading binds no registry at all, so these are the cases where the binding
    above would simply stop applying. The ordinary `asl verify` CLI remains
    the control for supported legacy verification; overseer discovery excludes
    real legacy runs because they declare no authorized repository set.
    """

    def setUp(self) -> None:
        super().setUp()
        self.fleet.repository_root.mkdir(parents=True, exist_ok=True)
        self.declared = self.fleet.declare_checks(
            self.plan,
            {"type": "file_exists", "name": "marker", "path": "marker.txt"},
        )
        self.marker = self.fleet.repository_root / "marker.txt"
        #: The record of a run discovery observed with no registry, kept before
        #: `_register` overwrites it so a test can put a genuinely legacy
        #: observation back and read the same plan the other way.
        legacy_record = json.loads(self._record().read_text(encoding="utf-8"))
        # Real legacy runs publish none of these workspace fields. An arbitrary
        # registry digest with malformed declarations is not legacy provenance.
        for key in ("workspace_root", "registry_digest", "repositories"):
            legacy_record.pop(key, None)
        self.legacy_record = json.dumps(legacy_record)
        #: Every directory Git was asked about, so a test can show that an
        #: unpermitted repository was refused rather than merely not verified in.
        self.inspected: list[str] = []
        self._register("scripts")
        self.enterContext(self._answering_git())

    def test_authorized_verify_runs_declared_check(self) -> None:
        self.marker.write_text("present\n", encoding="utf-8")
        packet = self.observed()

        status, payload = self.act(
            packet,
            "--check",
            "marker",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self._claims(),
            operation="verify",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIsNone(payload["dispatch"]["refusal"])
        self.assertIn("marker", payload["dispatch"]["detail"])
        self.assertIsNone(payload["escalation"])

    def test_a_declared_check_that_fails_reports_the_failure_not_a_refusal(
        self,
    ) -> None:
        """`performed` false with no refusal: the check ran and the answer is no."""
        packet = self.observed()

        status, payload = self.act(
            packet,
            "--check",
            "marker",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self._claims(),
            operation="verify",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertIsNone(payload["dispatch"]["refusal"])
        self.assertIn("check_failed", payload["dispatch"]["detail"])

    def test_undeclared_verify_is_refused(self) -> None:
        self.marker.write_text("present\n", encoding="utf-8")
        packet = self.observed()

        status, payload = self.act(
            packet,
            "--check",
            "rm -rf /",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self._claims(),
            operation="verify",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        detail = payload["dispatch"]["detail"]
        self.assertIn("check_undeclared", detail)
        # The refusal names what the plan does declare, so an operator reads a
        # menu rather than being invited to guess again.
        for name in self.declared:
            self.assertIn(name, detail)

    def test_an_unauthorized_verify_never_reaches_the_check(self) -> None:
        """No claims, so the check is not run and nothing is reported about it."""
        packet = self.observed()

        status, payload = self.act(packet, "--check", "marker", operation="verify")

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("authority_refused: no_decision", payload["dispatch"]["detail"])
        self.assertNotIn("check_failed", payload["dispatch"]["detail"])

    def test_a_registered_plan_verifies_its_own_declared_check(self) -> None:
        """The control the two drift tests rest on: registered mode does work.

        Without it, a refusal on an expanded manifest proves nothing --- it
        would read the same if a registered plan could never be verified here
        at all.
        """
        self.marker.write_text("present\n", encoding="utf-8")
        self._register("scripts")
        packet = self.observed()
        reviewed, claims = self._reviewed(packet), self._claims()

        with self._answering_git():
            status, payload = self.act(
                packet,
                "--check",
                "marker",
                "--reviewed-evidence",
                reviewed,
                "--claims-file",
                claims,
                operation="verify",
            )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIsNone(payload["dispatch"]["refusal"])
        self.assertIn("marker", payload["dispatch"]["detail"])

    def test_manifest_expansion_after_inspect_refuses_verify(self) -> None:
        """A repository added to the plan after the action was authorized.

        The run's own record is untouched, so every guard that reads it --- the
        scope digest, the declared repository set, the action token --- still
        agrees. Only the manifest moved, and the named check still resolves in
        the one repository AgentMux permitted, which is exactly the shape that
        would slip through a scope test asking only about the selected check.
        """
        self.marker.write_text("present\n", encoding="utf-8")
        self._register("scripts")
        packet = self.observed()
        reviewed, claims = self._reviewed(packet), self._claims()

        self._write_manifest("scripts", "excluded")

        with self._answering_git():
            status, payload = self.act(
                packet,
                "--check",
                "marker",
                "--reviewed-evidence",
                reviewed,
                "--claims-file",
                claims,
                operation="verify",
            )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        dispatch = payload["dispatch"]
        self.assertFalse(dispatch["performed"])
        self.assertEqual("preparation_declined", dispatch["refusal"])
        self.assertIn("registry_moved", dispatch["detail"])
        self.assertIn("excluded", dispatch["detail"])
        self._assert_the_check_never_ran(dispatch["detail"])
        # Refused before the workspace was resolved, so the repository nobody
        # permitted was never even inspected.
        self.assertNotIn(str(self.fleet.root / "excluded"), self.inspected)

    def test_manifest_expansion_during_preparation_refuses_verify(self) -> None:
        """The same repository, added while the action was already in flight.

        The dispatcher asks AgentMux twice about one action --- once before the
        adapter prepares and once after --- so expanding the manifest on the
        second ask puts the change squarely between preparation and execution.
        The absent `refusal` is what proves the execution refused on its own
        re-reading: a preparation that declined comes back as
        `preparation_declined`, and only a receipt from an adapter that ran and
        said no comes back bare.
        """
        self.marker.write_text("present\n", encoding="utf-8")
        self._register("scripts")
        packet = self.observed()
        reviewed, claims = self._reviewed(packet), self._claims()
        self._expand_between_the_dispatcher_two_asks()

        with self._answering_git():
            status, payload = self.act(
                packet,
                "--check",
                "marker",
                "--reviewed-evidence",
                reviewed,
                "--claims-file",
                claims,
                operation="verify",
            )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        dispatch = payload["dispatch"]
        self.assertFalse(dispatch["performed"])
        self.assertIsNone(dispatch["refusal"], dispatch["detail"])
        self.assertIn("registry_moved", dispatch["detail"])
        self.assertIn("excluded", dispatch["detail"])
        self._assert_the_check_never_ran(dispatch["detail"])

    def test_registry_removal_after_inspect_refuses_verify(self) -> None:
        """The manifest deleted, and the contract made legacy-valid with it.

        Removing only the manifest leaves a contract naming an alias no registry
        declares, which is refused for its own unrelated reason. Removing the
        alias too is the shape one turn actually leaves behind, and it is the
        one that reaches the mode question: every record-derived guard still
        agrees --- same digest, same declared repositories, same action token
        --- and the declared check resolves cleanly under the single repository
        AgentMux permitted. Only the mode moved, from a registry this action was
        bound to towards none at all.

        The second half restores a real legacy observation, whose workspace
        fields are absent. Overseer discovery excludes it, while the ordinary
        public verify command still verifies the same plan and legacy contract.
        """
        self.marker.write_text("present\n", encoding="utf-8")
        self._register("scripts")
        packet = self.observed()
        reviewed, claims = self._reviewed(packet), self._claims()

        self._drop_the_registry()

        with self._answering_git():
            status, payload = self.act(
                packet,
                "--check",
                "marker",
                "--reviewed-evidence",
                reviewed,
                "--claims-file",
                claims,
                operation="verify",
            )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        dispatch = payload["dispatch"]
        self.assertFalse(dispatch["performed"])
        self.assertEqual("preparation_declined", dispatch["refusal"])
        self.assertIn("registry_moved", dispatch["detail"])
        self._assert_the_check_never_ran(dispatch["detail"])

        self._forget_the_registry_observation()
        inventory = overseer_discovery.build_inventory(state_dir=self.fleet.state_root)
        legacy = next(c for c in inventory.candidates if c.plan_dir == str(self.plan))
        self.assertFalse(legacy.eligible)
        self.assertIsNone(overseer_verify._observed_registry(legacy))
        # The ordinary public verify command is the supported legacy workflow;
        # overseer discovery has always excluded runs without declared roots.
        output = io.StringIO()
        args = self.fleet.parser.parse_args(
            [
                "verify",
                str(self.plan),
                "--session",
                PLAN_SESSION,
                "--cwd",
                str(self.fleet.repository_root),
                "--json",
            ]
        )
        with contextlib.redirect_stdout(output):
            status = cli.resolve_handler(args)(args)
        self.assertEqual(0, status, output.getvalue())
        self.assertIn("marker", output.getvalue())

    def test_inconsistent_registry_cannot_authorize_legacy_downgrade(self) -> None:
        for timing in ("after-inspect", "during-preparation"):
            for field, value in (
                ("path", "other"),
                ("origin", "github.com/example/other"),
                ("path", ""),
            ):
                with self.subTest(timing=timing, field=field, value=value):
                    case = VerificationOperationTests()
                    case.setUp()
                    try:
                        case._assert_registry_corruption_refused(timing, field, value)
                    finally:
                        case.doCleanups()

    def _assert_registry_corruption_refused(
        self, timing: str, field: str, value: str
    ) -> None:
        self.marker.write_text("present\n", encoding="utf-8")
        packet = self.observed()
        reviewed, claims = self._reviewed(packet), self._claims()

        def corrupt() -> None:
            self._drop_the_registry()
            record = json.loads(self._record().read_text(encoding="utf-8"))
            record["repositories"][0][field] = value
            self._record().write_text(json.dumps(record), encoding="utf-8")

        if timing == "after-inspect":
            corrupt()
        else:
            asks: list[str] = []

            def on_ask(access: str) -> None:
                asks.append(access)
                if access == ACTION and asks.count(ACTION) == 2:
                    corrupt()

            self.fleet.policy.on_ask = on_ask
        status, payload = self.act(
            packet,
            "--check",
            "marker",
            "--reviewed-evidence",
            reviewed,
            "--claims-file",
            claims,
            operation="verify",
        )
        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        dispatch = payload["dispatch"]
        self.assertFalse(dispatch["performed"])
        self.assertIn("registry_moved", dispatch["detail"])
        self.assertIn("inconsistent registry evidence", dispatch["detail"])
        self.assertNotIn("contract_moved", dispatch["detail"])
        self._assert_the_check_never_ran(dispatch["detail"])

    def test_registry_removal_during_preparation_refuses_verify(self) -> None:
        """The same removal, made while the action was already in flight.

        Preparation reads a registered plan and binds its registry; the manifest
        and the alias go between the dispatcher's two asks, so execution
        re-reads a plan that is legacy now. Both have to go together, here as
        after inspect: the contract reader rejects a `repo` on a plan with no
        registry, so a manifest removed on its own never reaches the mode
        question at all.

        Which means this test is about *which* refusal, and that is the point.
        A contract whose alias vanished is `contract_moved`, and its remedy is
        to prepare again against the contract as it now reads --- which is
        exactly the retry that would then verify a registered run as a legacy
        one. The mode binding has to answer first, and name the registry, so the
        remedy is fresh discovery instead.

        The absent `refusal` is what proves the execution refused on its own
        reading rather than inheriting a declined preparation.
        """
        self.marker.write_text("present\n", encoding="utf-8")
        self._register("scripts")
        packet = self.observed()
        reviewed, claims = self._reviewed(packet), self._claims()
        self._drop_the_registry_between_the_dispatcher_two_asks()

        with self._answering_git():
            status, payload = self.act(
                packet,
                "--check",
                "marker",
                "--reviewed-evidence",
                reviewed,
                "--claims-file",
                claims,
                operation="verify",
            )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        dispatch = payload["dispatch"]
        self.assertFalse(dispatch["performed"])
        self.assertIsNone(dispatch["refusal"], dispatch["detail"])
        self.assertIn("registry_moved", dispatch["detail"])
        self.assertNotIn("contract_moved", dispatch["detail"])
        self._assert_the_check_never_ran(dispatch["detail"])

    def _register(self, *aliases: str) -> None:
        """Make the plan a registered one whose record agrees with its manifest.

        The agreement is the point: same digest, same aliases, same resolved
        roots is what discovery persists for a registered run, and it is what
        the two drift tests then break from one side only.
        """
        self._write_manifest(*aliases)
        # Registered checks name their repository; the legacy fixture's check
        # has no alias and could not resolve under a registry at all.
        self.declared = self.fleet.declare_checks(
            self.plan,
            {
                "type": "file_exists",
                "name": "marker",
                "path": "marker.txt",
                "repo": "scripts",
            },
        )
        registry = plans_registry.read_repository_registry(self.plan)
        if registry is None:
            raise AssertionError("the manifest just written declares no registry")
        record_path = (
            state.plan_state_dir(self.plan, self.fleet.state_root)
            / events.STATE_FILE_NAME
        )
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["registry_digest"] = registry.digest
        record["repositories"] = [
            {
                "alias": repository.alias,
                "path": repository.path,
                "origin": repository.origin,
                "resolved_root": str(
                    plans_files.canonicalize_path(self.fleet.root / repository.path)
                ),
            }
            for repository in registry
        ]
        record_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    def _write_manifest(self, *aliases: str) -> None:
        """Declare exactly these repositories, and nothing else, in the plan."""
        for alias in aliases:
            (self.fleet.root / alias).mkdir(parents=True, exist_ok=True)
        write_multi_repo_plan(
            self.plan,
            ("pending",),
            specs=tuple(
                (alias, alias, REGISTERED_ORIGIN.format(alias=alias))
                for alias in aliases
            ),
        )

    def _drop_the_registry(self) -> None:
        """Make the plan legacy, contract included, the way one turn would.

        Both halves matter. Without the manifest the plan declares no registry;
        without the alias beside it the surviving check would be refused as
        unresolvable, and the refusal under test would never be reached.
        """
        self._drop_the_manifest()
        self.declared = self.fleet.declare_checks(
            self.plan,
            {"type": "file_exists", "name": "marker", "path": "marker.txt"},
        )

    def _drop_the_manifest(self) -> None:
        (self.plan / "manifest.md").unlink()

    def _forget_the_registry_observation(self) -> None:
        """Put back the record of a run discovery observed with no registry."""
        self._record().write_text(self.legacy_record, encoding="utf-8")

    def _record(self) -> Path:
        return (
            state.plan_state_dir(self.plan, self.fleet.state_root)
            / events.STATE_FILE_NAME
        )

    def _drop_the_registry_between_the_dispatcher_two_asks(self) -> None:
        asked: list[str] = []

        def drop(access: str) -> None:
            asked.append(access)
            # On the ask itself, not merely while its count stands: the content
            # ask that follows leaves the count at two, and a removal is not an
            # idempotent rewrite --- doing it twice raises rather than repeats.
            if access == ACTION and asked.count(ACTION) == 2:
                self._drop_the_registry()

        self.fleet.policy.on_ask = drop

    def _expand_between_the_dispatcher_two_asks(self) -> None:
        asked: list[str] = []

        def expand(access: str) -> None:
            asked.append(access)
            if asked.count(ACTION) == 2:
                self._write_manifest("scripts", "excluded")

        self.fleet.policy.on_ask = expand

    def _answering_git(self) -> Any:
        """Answer Git for the fixture's directories, recording every question.

        Resolving a registered workspace runs `git` in each declared repository,
        and nothing here is a checkout. Recording the questions is what lets a
        test say the unpermitted repository was never inspected, which is a
        stronger claim than saying no check ran inside it.
        """

        def git_stdout(root: Path, *args: str) -> str:
            self.inspected.append(str(root))
            if args[0] == "rev-parse":
                return str(root)
            return REGISTERED_ORIGIN.format(alias=root.name)

        return mock.patch.object(workspaces, "_git_stdout", git_stdout)

    def _assert_the_check_never_ran(self, detail: str) -> None:
        """No verdict was reached, and the marker proves one was available."""
        self.assertTrue(self.marker.exists())
        self.assertNotIn("passed", detail)
        self.assertNotIn("check_failed", detail)

    def _reviewed(self, packet: dict[str, Any]) -> str:
        return self.fleet.reviewed(
            self.plan, "--incident-id", str(packet["packet"]["incident_id"])
        )

    def _claims(self) -> str:
        return self.fleet.claims_file(
            self,
            self.fleet.allowing(self.fleet.transcript.statement_id(), "verify"),
        )
