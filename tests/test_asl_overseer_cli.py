"""The operator surface: one whole invocation, and the doors it cannot open.

Five claims, and none of them is readable off ``overseer_cli`` alone.

*A complete invocation.* ``InvocationWorkflowTests`` drives the seven-step loop
end to end against a real state corpus, a real ledger and a real mailbox --
scan, watch, inspect, decide, act, reconcile -- and checks the artifact each
step leaves for the next one: an incident row, a scope digest, an action token,
a governing decision, an appended guidance record. Asserting the steps in
isolation would prove six commands parse; asserting the chain proves the
handoffs line up, which is the only thing an operator actually depends on.

*The evidence asymmetry.* ``EvidenceBoundaryTests`` pins the fact that makes
``decide`` non-obvious: ``SUPERVISION_VOICES`` maps every mailbox kind to a
relayed or machine voice and never to ``user_instruction``, while
``AUTHORIZING_VOICES`` is exactly ``user_instruction``. A plan's mailbox
therefore authorizes nothing on its own, no matter how imperative it reads, and
only a harness transcript can carry a person's turn. The tests assert both
halves, because either one alone reads as an accident.

*Escalation without a way out.* ``EscalationTests`` covers the refusals that
must stay refusals: a check no plan declares, a claim resting on a relay, an
unreadable transcript. The structural test is the important one --
:class:`overseer_cli.Escalation` is inspected for a ``default``, ``timeout`` or
``deadline`` field, because "silence is not consent" enforced by instruction is
enforced by nothing.

*A name, not a command.* ``VerificationOperationTests`` drives ``verify``
against a real completion contract in a real session file: a declared check
runs and reports its real result, and an undeclared name is refused with the
declared names in the refusal. Only both halves together say the operation is
neither a stub that always refuses nor a shell that always obliges.

*Permission before content.* ``ReconciliationPermissionTests`` covers the leaf
whose whole job is reading what a previous process touched, and which is
therefore the easiest place for a withdrawn opt-in to be read straight past.
The assertion is about ``_Fleet.reads`` rather than about an exit status: a
refused or unreachable answer has to leave the run unopened, not merely leave
the command failing after it looked.

*One door.* ``GuardedOperationTests`` reads this module's own AST. No adapter's
``prepare`` or ``execute`` is called here, ``overseer_dispatch.dispatch`` is,
and ``dispatch`` is deliberately absent from :class:`overseer_cli.Seams` so no
substitution can quietly add a second route to a mutation.

Every external boundary is injected -- AgentMux authorization, AgentMux
transcript evidence, run facts, the clock -- so nothing here spawns a process,
which is what keeps this module in `PORTABLE_MODULES`;
``test_the_loop_spawns_no_process`` asserts that rather than assuming it.

One fixture detail is load-bearing and worth naming: the fake clock is set
*ahead* of the wall clock. ``overseer_watch`` stamps a lease from the injected
clock, and ``overseer_dispatch`` checks that lease against ``time.time`` --
which is not a seam, on purpose. A fixture clock in the past would take a lease
that had already expired by the time ``act`` ran, and every dispatch would fail
``ownership_lost`` for a reason that has nothing to do with the test.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import inspect as inspect_module
import io
import json
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    cli,
    clock,
    events,
    intervention,
    mailbox,
    overseer_checkpoint_groups,
    overseer_cli,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_history,
    overseer_mailbox,
    overseer_policy_client,
    overseer_resume,
    overseer_store,
    overseer_verify,
    overseer_watch,
    plans,
    state,
)

from .support import assert_no_runner_import

#: The fixed moment the fake clock starts from, deliberately ahead of any real
#: "now": see the module docstring on why a lease taken in the past would make
#: every dispatch fail for the wrong reason.
EPOCH = datetime(2099, 6, 19, 12, 0, 0, tzinfo=UTC)

WATCHER = "watcher-a"
FINGERPRINT = "fp-1"
DIGEST = "rd-1"
HARNESS = "claude"
SESSION = "session-7"
#: The ASL session the fixture's run is on. Deliberately not `SESSION`: a
#: harness conversation id and a plan session id are different vocabularies, and
#: `verify` reads the second one to find the contract it may run a check from.
PLAN_SESSION = "7"
#: The access category a mutation and its reconciliation are both asked under.
ACTION = overseer_policy_client.ACCESS_ACTION

#: A conversation class whose ``user`` turns are written by an orchestrating
#: agent rather than a person, and which AgentMux does not publish. The fixture
#: names one so `--generated-class` has something to mark that the default
#: classification would have refused for a different reason.
GENERATED_CLASS = "orchestrated"


class _Clock:
    """A clock that moves only when a test says so."""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def utc_now(self) -> datetime:
        return self.now

    def utc_iso_now(self) -> str:
        return clock.isoformat(self.now)

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _Policy:
    """A stand-in for ``overseer_policy_client.authorize``.

    Records every question so a test can show a leaf asked again rather than
    reusing an answer, and can be told to refuse or to go unreachable -- which
    are different answers and have to stay different.
    """

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []
        self.refused: set[str] = set()
        self.unavailable: set[str] = set()
        self.fingerprint = FINGERPRINT
        self.digest = DIGEST

    def __call__(
        self,
        *,
        plan_dir: str,
        access: str,
        repositories: tuple[overseer_policy_client.Repository, ...],
        timeout: float,
    ) -> overseer_policy_client.Decision:
        self.asked.append({"plan_dir": plan_dir, "access": access})
        available = plan_dir not in self.unavailable
        allowed = available and plan_dir not in self.refused
        return overseer_policy_client.Decision(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            allowed=allowed,
            available=available,
            reason="opted_in" if allowed else "excluded",
            failure=None if available else "client_timeout",
            summary="1 repository" if available else "amx did not answer in time",
            config_fingerprint=self.fingerprint if available else "",
            request_digest=self.digest if available else "",
            contract_version=1 if available else None,
            exit_status=0 if allowed else 1,
            outcomes=(),
        )

    def asked_under(self, access: str) -> int:
        return sum(1 for item in self.asked if item["access"] == access)


class _Transcript:
    """A stand-in for ``overseer_history.read_history``.

    Hands back one conversation in the repository AgentMux authorized, so a
    ``user`` turn in it is the only thing in the whole fixture that can carry an
    authorization. The directory has to be the repository the run declares and
    not the workspace above it: a workspace is not a project, and a decision
    typed one level up reaches none of the projects beneath it.

    ``fail`` switches it to an answer that could not be obtained, which has to
    stay distinguishable from an answer that said nothing.

    ``on_read`` runs once an answer has been assembled, which is how a test puts
    a turn into the transcript *between* two readings of it. A person typing
    while an action is in flight is a real sequence, and reproducing it means
    changing the record at the moment the code under test is between its two
    looks at it.
    """

    def __init__(self, project: str) -> None:
        self.project = project
        self.calls: list[dict[str, Any]] = []
        self.failure: overseer_history.Failure | None = None
        self.session_class = overseer_decisions.SESSION_CLASS_MAIN
        self.on_read: Callable[[int], None] | None = None
        self.turns: list[tuple[str, str]] = [
            ("user", "answer routine tooling questions on my runs without asking me")
        ]
        self.elsewhere: list[tuple[str, str, list[tuple[str, str]]]] = []

    def __call__(self, **kwargs: Any) -> overseer_history.History:
        self.calls.append(kwargs)
        if self.failure is not None:
            return self._unavailable()
        answer = self._history(
            allowed=True,
            available=True,
            reason="opted_in",
            conversations=(
                self._conversation(SESSION, self.project, self.turns),
                *(
                    self._conversation(session, project, turns)
                    for session, project, turns in self.elsewhere
                ),
            ),
        )
        if self.on_read is not None:
            self.on_read(len(self.calls))
        return answer

    def also_in(self, project: str, text: str) -> str:
        """Add a conversation in another project, and name the turn it holds.

        A plan that spans two repositories can only be spoken for by someone
        speaking in each of them, and those are two different conversations in
        two different directories. Returns the statement id of the turn, which
        is what a claim cites.
        """
        session = f"{SESSION}-{len(self.elsewhere) + 2}"
        self.elsewhere.append((session, project, [("user", text)]))
        return f"{overseer_decisions.TRANSCRIPT_SPACE}:{HARNESS}:{session}#0"

    def statement_id(self, index: int = 0) -> str:
        return f"{overseer_decisions.TRANSCRIPT_SPACE}:{HARNESS}:{SESSION}#{index}"

    def _conversation(
        self, session: str, project: str, turns: list[tuple[str, str]]
    ) -> overseer_history.Conversation:
        excerpts = tuple(
            overseer_history.Excerpt(
                index=index,
                role=role,
                timestamp=clock.isoformat(EPOCH - timedelta(hours=index + 1)),
                text=text,
                truncated=False,
            )
            for index, (role, text) in enumerate(turns)
        )
        return overseer_history.Conversation(
            harness=HARNESS,
            session_id=session,
            profiles=(),
            directory=project,
            projects=(project,),
            path=f"{project}/.transcripts/{session}.jsonl",
            started_at=clock.isoformat(EPOCH - timedelta(days=1)),
            last_at=clock.isoformat(EPOCH - timedelta(hours=1)),
            session_class=self.session_class,
            message_count=len(excerpts),
            truncated=False,
            excerpts=excerpts,
        )

    def _unavailable(self) -> overseer_history.History:
        return self._history(
            allowed=False,
            available=False,
            reason="unreachable",
            conversations=(),
            failure=self.failure,
        )

    def _history(
        self,
        *,
        allowed: bool,
        available: bool,
        reason: str,
        conversations: tuple[overseer_history.Conversation, ...],
        failure: overseer_history.Failure | None = None,
    ) -> overseer_history.History:
        return overseer_history.History(
            plan_dir="",
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(),
            limits=overseer_history.Limits(),
            allowed=allowed,
            available=available,
            reason=reason,
            failure=failure,
            summary="1 conversation" if available else "amx did not answer",
            config_fingerprint=FINGERPRINT if available else "",
            request_digest=DIGEST if available else "",
            contract_version=1 if available else None,
            exit_status=0 if available else 1,
            outcomes=(),
            examined=len(conversations),
            truncated=False,
            conversations=conversations,
            withheld=(),
            warnings=(),
        )


class _Fleet:
    """A real ASL state corpus with every external boundary injected.

    The plan directories, the state records, the mailbox and the ledger are all
    real files, because the leaves read them through the real discovery, context
    and mailbox modules. What is faked is exactly the four things that would
    otherwise reach outside the process: AgentMux authorization, AgentMux
    transcript evidence, the run's own facts, and the clock.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # `plans.canonicalize_path` resolves the macOS `/var` symlink and the
        # state directory name is a hash of the canonical path, so the fixture
        # has to agree with it before anything is written.
        self.root = plans.canonicalize_path(tmp.name)
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.state_root = self.root / "state"
        self.clock = _Clock()
        self.policy = _Policy()
        self.repository_root = self.root / "scripts"
        self.transcript = _Transcript(str(self.repository_root))
        self.facts: dict[str, intervention.RunFacts] = {}
        self.reads: list[str] = []
        self.parser = cli.build_parser(prog="asl")
        self.ledgers: list[overseer_store.Ledger] = []
        case.addCleanup(self._close_ledgers)

    # -- corpus ------------------------------------------------------------

    def write_run(self, name: str, **overrides: Any) -> Path:
        """One paused run, recent enough for the invocation window."""
        plan_dir = plans.canonicalize_path(self._make_plan_dir(name))
        run_id = f"{name}-run-1"
        record: dict[str, Any] = {
            "run_id": run_id,
            "harness": HARNESS,
            "ok": True,
            "paused": True,
            "pause_reason": "question_pending",
            "dry_run": False,
            "plan_dir": str(plan_dir),
            "run_started_at": clock.isoformat(EPOCH - timedelta(hours=2)),
            "run_finished_at": None,
            "workspace_root": str(self.root),
            "registry_digest": "d" * 64,
            "repositories": [
                {
                    "alias": "scripts",
                    "path": "scripts",
                    "origin": "workspace",
                    "resolved_root": str(self.repository_root),
                }
            ],
            overseer_discovery.PAUSED_AT_KEY: clock.isoformat(
                EPOCH - timedelta(minutes=1)
            ),
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            ),
        }
        record.update(overrides)
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )
        self.facts[str(plan_dir)] = self._run_facts(plan_dir, run_id)
        return plan_dir

    def declare_checks(
        self, plan_dir: Path, *checks: dict[str, Any]
    ) -> tuple[str, ...]:
        """Write the session file whose completion contract declares ``checks``.

        A real session file parsed by the real contract reader, because the
        whole claim `verify` rests on is that the argv comes from a plan rather
        than from an action -- a stubbed contract would prove the adapter reads
        *something*, which is the uninteresting half.
        """
        recipe = json.dumps({"session": PLAN_SESSION, "checks": list(checks)}, indent=2)
        (plan_dir / plans.session_id_to_filename(PLAN_SESSION)).write_text(
            "\n".join(
                (
                    f"# Session {PLAN_SESSION}: Fixture",
                    "",
                    "Instructions.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    "- the declared check passes",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    recipe,
                    "```",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: pending",
                    "- **Files modified**: (none yet)",
                    "- **Key decisions**: (none yet)",
                    "- **Blockers for next session**: (none yet)",
                    "- **Notes**:",
                    "",
                )
            ),
            encoding="utf-8",
        )
        return tuple(str(check["name"]) for check in checks)

    def ask_question(self, plan_dir: Path, text: str = "which lint profile?") -> str:
        """Put a question in a run's mailbox and return its id."""
        message = mailbox.append_question(
            plan_dir, text, state_dir=self.state_root, run_id=f"{plan_dir.name}-run-1"
        )
        return message.id

    def messages(self, plan_dir: Path) -> tuple[mailbox.MailboxMessage, ...]:
        return mailbox.read_mailbox(plan_dir, state_dir=self.state_root).messages

    def _make_plan_dir(self, name: str) -> Path:
        plan_dir = self.plans_root / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plan_dir

    def _run_facts(self, plan_dir: Path, run_id: str) -> intervention.RunFacts:
        return intervention.RunFacts(
            plan_dir=plan_dir,
            run_id=run_id,
            lock_active=True,
            run_active=True,
            paused=True,
            turn_active=False,
            context_kind="waiting",
            question_id=None,
            unanswered_ids=(),
            runner_state={
                "run_id": run_id,
                "paused": True,
                "reason": "question_pending",
                "run_started_at": clock.isoformat(EPOCH - timedelta(hours=2)),
                "run_finished_at": None,
                "active_turn": {
                    "session_id": PLAN_SESSION,
                    "turn_index": 3,
                    "last_activity_at": clock.isoformat(EPOCH - timedelta(minutes=1)),
                },
            },
        )

    # -- seams -------------------------------------------------------------

    def inspect_run(self, plan_dir: Any, **_: Any) -> intervention.RunFacts:
        # Recorded because opening a run's state and mailbox is content access:
        # `ReconciliationPermissionTests` asserts a refused plan is never read,
        # which is a claim about this list rather than about an exit status.
        self.reads.append(str(plan_dir))
        return self.facts[str(plan_dir)]

    def open_ledger(self, **kwargs: Any) -> overseer_store.Ledger:
        """A real ledger, tracked so the fixture closes what the leaves open.

        A leaf opens a ledger and lets the process own it, which is right for a
        CLI and wrong for a test that runs thirty of them in one interpreter.
        Owning the lifetime here keeps the connections from piling up without
        changing what the leaf does.
        """
        ledger = overseer_store.open_ledger(**kwargs)
        self.ledgers.append(ledger)
        return ledger

    def _close_ledgers(self) -> None:
        for ledger in self.ledgers:
            with contextlib.suppress(Exception):
                ledger.close()
        self.ledgers.clear()

    def seams(self) -> overseer_cli.Seams:
        return overseer_cli.Seams(
            open_ledger=self.open_ledger,
            authorize=self.policy,
            read_history=self.transcript,
            inspect_run=self.inspect_run,
            utc_now=self.clock.utc_now,
            utc_iso_now=self.clock.utc_iso_now,
            sleep=self.clock.advance,
        )

    # -- driving -----------------------------------------------------------

    def parse(self, *argv: str) -> argparse.Namespace:
        """Parse through the real parser, so the flags under test are the ones
        an operator types rather than a namespace the test invented."""
        return self.parser.parse_args(
            ["overseer", *argv, "--state-dir", str(self.state_root), "--json"]
        )

    def invoke(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Run one leaf the way ``asl`` would, and read its JSON envelope."""
        args = self.parse(*argv)
        # `resolve_handler` is typed for the shape every leaf shares. The
        # overseer leaves additionally take the keyword-only `seams` this
        # fixture injects, so the widening lives here rather than in the
        # production signature, where it would stop describing the leaves.
        handler = cast("Callable[..., int]", cli.resolve_handler(args))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = handler(args, seams=self.seams())
        return status, json.loads(stream.getvalue())

    def claims_file(self, case: unittest.TestCase, claims: list[dict[str, Any]]) -> str:
        path = self.root / f"claims-{len(list(self.root.glob('claims-*.json')))}.json"
        path.write_text(json.dumps(claims), encoding="utf-8")
        case.addCleanup(lambda: path.unlink(missing_ok=True))
        return str(path)

    def allowing(self, statement_id: str, *operations: str) -> list[dict[str, Any]]:
        """The claim shape a host writes: one statement, read one way."""
        return [
            {
                "statement_id": statement_id,
                "topic": "operations",
                "stance": "allow",
                "operations": list(operations),
                "reading": "the user said to handle routine tooling without asking",
            }
        ]


class _OverseerCase(unittest.TestCase):
    """One watched plan, observed and leased, ready to be inspected."""

    def setUp(self) -> None:
        self.fleet = _Fleet(self)
        self.plan = self.fleet.write_run("plan-alpha")
        self.question = self.fleet.ask_question(self.plan)

    def watch(self, *extra: str) -> dict[str, Any]:
        status, payload = self.fleet.invoke("watch", "--watcher-id", WATCHER, *extra)
        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        return payload

    def inspect(self, plan: Path | None = None) -> dict[str, Any]:
        status, payload = self.fleet.invoke(
            "inspect",
            str(plan or self.plan),
            "--kind",
            "question_pending",
            "--summary",
            "a run is waiting on a tooling question",
        )
        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        return payload

    def observed(self) -> dict[str, Any]:
        """Watch and inspect, returning the packet the next step needs."""
        self.watch()
        return self.inspect()

    def act(
        self,
        packet: dict[str, Any],
        *extra: str,
        operation: str = "guidance",
    ) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "act",
            str(self.plan),
            "--watcher-id",
            WATCHER,
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
            *extra,
        )


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
            "--claims-file",
            self._claims(),
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIsNone(payload["escalation"])
        kinds = [message.kind for message in self.fleet.messages(self.plan)]
        self.assertIn(mailbox.KIND_GUIDANCE, kinds)

    def test_reconcile_has_nothing_in_doubt_after_a_clean_dispatch(self) -> None:
        packet = self.observed()
        self._decide(packet, "guidance")
        self.act(packet, "--text", "carry on", "--claims-file", self._claims())

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

    def _leave_in_doubt(self) -> overseer_store.ActionRecord:
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

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
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
            mailbox.KIND_GUIDANCE,
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
        before = len(self.fleet.transcript.calls)

        self.act(
            packet,
            "--text",
            "carry on",
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
            "--claims-file",
            self.fleet.claims_file(self, claims),
            *extra,
        )


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

    # -- helpers -----------------------------------------------------------

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

    def _decide(
        self, plan: Path, packet: dict[str, Any], claims: list[dict[str, Any]]
    ) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "decide",
            str(plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )


class EvidenceFreshnessTests(_OverseerCase):
    """A hold typed while an action is in flight has to stop the action.

    Re-deriving the ruling at dispatch time is what catches a revoked grant, and
    on its own it does not catch a *new* statement: the claims cite what the
    host read, an unclaimed hold never becomes a decision, and the second
    reading of the same claims therefore agrees with the first however the
    record moved. These tests drive the two windows that matters in --- between
    ``decide`` and ``act``, and inside a single dispatch --- and the case that
    must keep working, which is evidence that did not move.
    """

    def test_decide_publishes_the_digest_of_the_record_it_read(self) -> None:
        """The token a host carries to `act` comes from the leaf that read."""
        packet = self.observed()

        _status, listing = self.fleet.invoke(
            "decide",
            str(self.plan),
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--list-statements",
        )
        _ruled, payload = self._decide(
            packet,
            claims=self.fleet.allowing(
                self.fleet.transcript.statement_id(), "guidance"
            ),
        )

        self.assertTrue(listing["evidence_digest"].startswith("ev1-"))
        self.assertEqual(listing["evidence_digest"], payload["evidence_digest"])

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
                if message.kind == mailbox.KIND_GUIDANCE
            ),
        )

    def test_a_hold_arriving_during_preparation_stops_the_action(self) -> None:
        """The other window, and the one a host cannot close by being careful.

        No flag is passed here: the first authority check pins what it saw, so
        the hold that lands while the adapter is preparing is caught by the
        second check even though the invocation named no digest.
        """
        packet = self.observed()
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
            packet, "--text", "carry on", "--claims-file", claims
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertIn("evidence_changed", payload["dispatch"]["detail"])
        self.assertTrue(payload["evidence"]["changed"])

    def test_an_invocation_naming_no_digest_reports_what_it_pinned(self) -> None:
        """The binding is opt-in, so the unbound invocation still acts and says so."""
        packet = self.observed()
        claims = self.fleet.claims_file(
            self,
            self.fleet.allowing(self.fleet.transcript.statement_id(), "guidance"),
        )

        status, payload = self.act(
            packet, "--text", "carry on", "--claims-file", claims
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"])
        self.assertFalse(payload["evidence"]["changed"])
        self.assertTrue(payload["evidence"]["reviewed"].startswith("ev1-"))

    # -- helpers -----------------------------------------------------------

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
    ) -> tuple[int, dict[str, Any]]:
        return self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )


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
            packet, "--text", "carry on", "--claims-file", claims
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
        baseline = len(self.fleet.transcript.calls)

        status, payload = self.act(
            packet,
            "--text",
            "carry on",
            "--raises",
            "acceptance",
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
            if message.kind == mailbox.KIND_GUIDANCE
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
        return self.fleet.invoke(
            "decide",
            str(self.plan),
            "--operation",
            "guidance",
            "--incident-id",
            str(packet["packet"]["incident_id"]),
            *(("--raises", raises) if raises else ()),
            "--claims-file",
            self.fleet.claims_file(self, claims),
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
            "--claims-file",
            self.fleet.claims_file(self, claims),
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("held", payload["ruling"]["refusal"])
        self.assertEqual("held", payload["escalation"]["reason"])


class GuardedOperationTests(_OverseerCase):
    """That the guarded dispatcher is the only route to a mutation."""

    def test_no_adapter_is_prepared_or_executed_in_this_module(self) -> None:
        """Read the module's own AST: there is no second door to find."""
        tree = ast.parse(Path(overseer_cli.__file__).read_text(encoding="utf-8"))
        called = {
            node.func.attr
            for node in ast.walk(tree)
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
    """

    def setUp(self) -> None:
        super().setUp()
        self.fleet.repository_root.mkdir(parents=True, exist_ok=True)
        self.declared = self.fleet.declare_checks(
            self.plan,
            {"type": "file_exists", "name": "marker", "path": "marker.txt"},
        )
        self.marker = self.fleet.repository_root / "marker.txt"

    def test_authorized_verify_runs_declared_check(self) -> None:
        self.marker.write_text("present\n", encoding="utf-8")
        packet = self.observed()

        status, payload = self.act(
            packet,
            "--check",
            "marker",
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

    def _claims(self) -> str:
        return self.fleet.claims_file(
            self,
            self.fleet.allowing(self.fleet.transcript.statement_id(), "verify"),
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
