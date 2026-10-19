"""Whole-invocation workflow, the single dispatch door and exit statuses."""

from __future__ import annotations

import ast
import inspect as inspect_module
import subprocess
import unittest
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    mailbox_vocabulary,
    overseer_checkpoint_groups,
    overseer_cli,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_mailbox,
    overseer_resume,
    overseer_verify,
    overseer_watch,
)
from tests.asl_overseer_cli_fleet import _OverseerCase
from tests.asl_overseer_cli_support import WATCHER

from .support import assert_no_runner_import


class InvocationWorkflowTests(_OverseerCase):
    """The seven steps, in order, with each step's output feeding the next."""

    def test_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_cli)

    def test_every_leaf_the_skill_names_is_reachable_from_the_parser(self) -> None:
        """The skill documents six commands; the parser has to offer six."""
        for leaf in ("scan", "watch", "inspect", "decide", "act", "reconcile"):
            with self.subTest(leaf=leaf):
                args = self.fleet.parse(
                    *_MINIMAL_ARGV[leaf](str(self.plan), self.question)
                )
                handler = cli.resolve_handler(args)
                self.assertIs(
                    getattr(overseer_cli, f"{leaf}_from_args"),
                    handler,
                    f"asl overseer {leaf} does not reach overseer_cli",
                )

    def test_scan_sees_the_run_without_asking_agentmux(self) -> None:
        status, payload = self.fleet.invoke("scan")

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["complete"])
        self.assertEqual(1, payload["eligible"])
        self.assertEqual(
            [str(self.plan)], [item["plan_dir"] for item in payload["candidates"]]
        )
        self.assertEqual(
            [], self.fleet.policy.asked, "discovery must not ask for permission"
        )

    def test_watch_asks_agentmux_and_takes_the_lease(self) -> None:
        payload = self.watch()

        sweep = payload["sweeps"][0]
        self.assertEqual(
            [str(self.plan)], [item["plan_dir"] for item in sweep["enrolled"]]
        )
        self.assertEqual([], sweep["excluded"])
        self.assertEqual(1, self.fleet.policy.asked_under(overseer_watch.WATCH_ACCESS))

    def test_inspect_opens_an_incident_and_captures_the_action_identity(self) -> None:
        self.watch()

        payload = self.inspect()

        self.assertTrue(payload["authorized"])
        self.assertTrue(payload["fresh"])
        self.assertGreaterEqual(payload["packet"]["incident_id"], 1)
        self.assertTrue(payload["token"])
        self.assertEqual(
            overseer_context.scope_digest(self._candidate()), payload["scope"]
        )
        self.assertEqual(
            {
                "chars": overseer_context.MAX_EXCERPT_CHARS,
                "records": overseer_context.MAX_EXCERPT_RECORDS,
            },
            payload["expansion_limits"],
        )

    def test_expansion_is_bounded_and_re_asks_for_permission(self) -> None:
        """Context grows because the host named a source, under a ceiling."""
        packet = self.observed()
        before = len(self.fleet.policy.asked)

        status, payload = self.fleet.invoke(
            "inspect",
            str(self.plan),
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--expand",
            "questions",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertEqual(["questions"], [item["kind"] for item in payload["excerpts"]])
        self.assertLessEqual(
            payload["excerpts"][0]["size"], overseer_context.MAX_EXCERPT_CHARS
        )
        self.assertGreater(len(self.fleet.policy.asked), before)

    def test_decide_lists_the_statements_a_claim_has_to_cite(self) -> None:
        packet = self.observed()

        status, payload = self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--list-statements",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["listed"])
        ids = [item["statement_id"] for item in payload["statements"]]
        self.assertIn(self.fleet.transcript.statement_id(), ids)
        self.assertTrue(
            any(item.startswith(overseer_decisions.STATE_SPACE) for item in ids),
            "the mailbox record should be listed too, as citable corroboration",
        )

    def test_decide_authorizes_on_the_statement_the_host_cited(self) -> None:
        packet = self.observed()

        status, payload = self._decide(packet, "guidance")

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["authorized"])
        self.assertEqual([], payload["rejected_claims"])
        self.assertIsNone(payload["escalation"])

    def test_act_dispatches_the_operation_and_the_mailbox_shows_it(self) -> None:
        packet = self.observed()
        self._decide(packet, "guidance")

        status, payload = self.act(
            packet,
            "--text",
            "the lint profile is the repo default; carry on",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self._claims(),
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIsNone(payload["escalation"])
        kinds = [message.kind for message in self.fleet.messages(self.plan)]
        self.assertIn(mailbox_vocabulary.KIND_GUIDANCE, kinds)

    def test_reconcile_has_nothing_in_doubt_after_a_clean_dispatch(self) -> None:
        packet = self.observed()
        self._decide(packet, "guidance")
        self.act(
            packet,
            "--text",
            "carry on",
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self._claims(),
        )

        status, payload = self.fleet.invoke("reconcile", str(self.plan))

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["settled"])
        self.assertEqual([], payload["outcomes"])

    def test_releasing_hands_the_lease_back(self) -> None:
        self.watch()

        status, payload = self.fleet.invoke(
            "watch", "--watcher-id", WATCHER, "--release"
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertEqual(1, payload["released"])

    def test_a_later_sweep_enrols_a_run_that_started_mid_invocation(self) -> None:
        """`watch while invoked` means a run that appears later joins."""
        self.watch()
        self.fleet.write_run("plan-beta")

        payload = self.watch()

        enrolled = {item["plan_dir"] for item in payload["sweeps"][0]["enrolled"]}
        self.assertEqual(
            {str(self.plan), str(self.fleet.plans_root / "plan-beta")}, enrolled
        )

    # -- helpers -----------------------------------------------------------

    def _candidate(self) -> overseer_discovery.Candidate:
        inventory = overseer_discovery.build_inventory(
            state_dir=str(self.fleet.state_root)
        )
        return next(
            item for item in inventory.candidates if item.plan_dir == str(self.plan)
        )

    def _claims(self) -> str:
        return self.fleet.claims_file(
            self,
            self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance", "reply"
            ),
        )

    def _reviewed(self, packet: dict[str, Any]) -> str:
        return self.fleet.reviewed(
            self.plan, "--incident-id", str(packet["packet"]["incident_id"])
        )

    def _decide(
        self, packet: dict[str, Any], operation: str
    ) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            operation,
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--reviewed-evidence",
            self._reviewed(packet),
            "--claims-file",
            self._claims(),
        )


#: Minimal argv per leaf, for the reachability test. Kept beside the tests that
#: use it rather than inside the class, so adding a leaf breaks one list.
_MINIMAL_ARGV: dict[str, Any] = {
    "scan": lambda plan, question: ("scan",),
    "watch": lambda plan, question: ("watch", "--watcher-id", WATCHER),
    "inspect": lambda plan, question: ("inspect", plan),
    "decide": lambda plan, question: ("decide", plan),
    "act": lambda plan, question: (
        "act",
        plan,
        "--watcher-id",
        WATCHER,
        "--operation",
        "guidance",
        "--run-id",
        "r",
        "--incident-id",
        "1",
        "--token",
        "t",
        "--scope",
        "s",
    ),
    "reconcile": lambda plan, question: ("reconcile", plan),
}


class GuardedOperationTests(_OverseerCase):
    """That the guarded dispatcher is the only route to a mutation."""

    def test_no_adapter_is_prepared_or_executed_in_this_module(self) -> None:
        """Read the module's own AST: there is no second door to find."""
        facade = Path(overseer_cli.__file__)
        sources = [facade, *sorted(facade.parent.glob("overseer_cli_*.py"))]
        called = {
            node.func.attr
            for path in sources
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        self.assertNotIn("prepare", called)
        self.assertNotIn("execute", called)
        self.assertIn("dispatch", called)

    def test_the_dispatcher_is_not_a_seam_a_test_could_replace(self) -> None:
        names = {field.name for field in fields(overseer_cli.Seams)}

        self.assertNotIn("dispatch", names)
        self.assertNotIn("registry", names)

    def test_the_registry_holds_only_operations_decisions_can_authorize(self) -> None:
        registry = self._registry()

        self.assertLessEqual(set(registry.adapters), set(overseer_cli.OPERATIONS))
        self.assertEqual(
            set(overseer_decisions.OPERATIONS), set(overseer_cli.OPERATIONS)
        )

    def test_verify_comes_from_the_contract_reading_adapter(self) -> None:
        """The check name is looked up in a plan, so a plan is what it takes."""
        adapter = self._registry().adapter("verify")

        self.assertIsNotNone(adapter)
        self.assertEqual(overseer_verify.__name__, type(adapter).__module__)

    def test_no_seam_can_hand_the_verify_adapter_a_command(self) -> None:
        """`Verify` names a check; nothing on the payload or the seams is argv.

        The property this module has to keep is not "the adapter reads a
        contract" but "there is no other place the argv could come from". Both
        halves are structural: the payload has one field and it is a name, and
        the record this module substitutes its world through has no way to
        express a command either.
        """
        self.assertEqual(
            ["check"],
            [field.name for field in fields(overseer_dispatch.Verify)],
        )
        seams = {field.name for field in fields(overseer_cli.Seams)}
        for forbidden in ("run_check", "argv", "command", "shell", "read_contract"):
            self.assertNotIn(forbidden, seams)

    def test_commit_comes_from_the_durable_group_checkpointer(self) -> None:
        """A crash between two repositories is the case the group exists for."""
        adapter = self._registry().adapter("commit")

        self.assertIsNotNone(adapter)
        self.assertEqual(overseer_checkpoint_groups.__name__, type(adapter).__module__)

    def test_the_mail_and_resume_operations_come_from_their_own_modules(self) -> None:
        registry = self._registry()

        self.assertEqual(
            {
                "reply": overseer_mailbox.__name__,
                "guidance": overseer_mailbox.__name__,
                "resume": overseer_resume.__name__,
            },
            {
                name: type(registry.adapter(name)).__module__
                for name in ("reply", "guidance", "resume")
            },
        )

    def test_the_loop_spawns_no_process(self) -> None:
        """Every boundary is injected, so nothing here reaches for a shell."""
        with (
            mock.patch.object(subprocess, "run", side_effect=AssertionError("spawned")),
            mock.patch.object(
                subprocess, "Popen", side_effect=AssertionError("spawned")
            ),
        ):
            packet = self.observed()
            self.fleet.invoke("scan")
            self.fleet.invoke(
                "decide",
                str(self.plan),
                "--incident-id",
                str(packet["packet"]["incident_id"]),
                "--list-statements",
            )
            self.act(packet, "--text", "carry on")
            self.fleet.invoke("reconcile", str(self.plan))

    def test_the_five_operations_are_the_documented_operational_scope(self) -> None:
        self.assertEqual(
            ("reply", "guidance", "verify", "commit", "resume"),
            overseer_cli.OPERATIONS,
        )

    def _registry(self) -> overseer_dispatch.Registry:
        def authority(_action: overseer_dispatch.Action) -> None:
            return None

        # Built the way the leaves build it; never called here, which is the
        # point -- the assertions are about what is registered, not what it does.
        return overseer_cli._adapters(
            authority=authority,
            ledger=self.fleet.open_ledger(state_dir=str(self.fleet.state_root)),
            state_dir=str(self.fleet.state_root),
            seams=self.fleet.seams(),
            timeout=1.0,
        )


class ExitStatusTests(_OverseerCase):
    """Three answers, so an `&&` chain fails closed instead of reading on."""

    def test_a_yes_exits_zero(self) -> None:
        status, _payload = self.fleet.invoke("scan")

        self.assertEqual(0, status)

    def test_a_refusal_exits_one(self) -> None:
        packet = self.observed()

        status, _payload = self.act(packet, "--check", "unit", operation="verify")

        self.assertEqual(1, status)

    def test_a_malformed_invocation_exits_two(self) -> None:
        """A plan discovery never saw is the wrong question, not a refusal."""
        status, payload = self.fleet.invoke(
            "inspect", str(self.fleet.plans_root / "plan-nobody-has")
        )

        self.assertEqual(2, status)
        self.assertEqual("plan_unobserved", payload["reason"])

    def test_a_partial_view_is_a_refusal_not_a_quiet_success(self) -> None:
        """An incomplete sweep must never read as an empty fleet."""
        signature = inspect_module.signature(overseer_cli.scan_from_args)

        self.assertIn("seams", signature.parameters)
        with mock.patch.object(self.fleet, "seams", return_value=self._partial_seams()):
            status, payload = self.fleet.invoke("scan")

        self.assertEqual(1, status)
        self.assertFalse(payload["complete"])

    def _partial_seams(self) -> overseer_cli.Seams:
        def build_inventory(**kwargs: Any) -> overseer_discovery.Inventory:
            inventory = overseer_discovery.build_inventory(**kwargs)
            return overseer_discovery.Inventory(
                state_root=inventory.state_root,
                candidates=inventory.candidates,
                warnings=("one plan's state directory could not be read",),
                complete=False,
            )

        seams = self.fleet.seams()
        return overseer_cli.Seams(
            open_ledger=seams.open_ledger,
            build_inventory=build_inventory,
            authorize=seams.authorize,
            inspect_run=seams.inspect_run,
            read_mailbox=seams.read_mailbox,
            read_history=seams.read_history,
            utc_now=seams.utc_now,
            utc_iso_now=seams.utc_iso_now,
            sleep=seams.sleep,
        )


if __name__ == "__main__":
    unittest.main()
