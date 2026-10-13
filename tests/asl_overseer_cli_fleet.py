"""The fleet fixture and base case for the overseer CLI test modules."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from agent_session_loop import (
    cli,
    clock,
    events,
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_models,
    overseer_cli,
    overseer_discovery,
    overseer_store,
    plans_files,
    plans_session_ids,
    state,
)
from tests.asl_overseer_cli_support import (
    EPOCH,
    HARNESS,
    PLAN_SESSION,
    WATCHER,
    _Clock,
    _Policy,
    _Transcript,
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
        # `plans_files.canonicalize_path` resolves the macOS `/var` symlink and the
        # state directory name is a hash of the canonical path, so the fixture
        # has to agree with it before anything is written.
        self.root = plans_files.canonicalize_path(tmp.name)
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
        plan_dir = plans_files.canonicalize_path(self._make_plan_dir(name))
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
        (plan_dir / plans_session_ids.session_id_to_filename(PLAN_SESSION)).write_text(
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
        message = mailbox_appenders.append_question(
            plan_dir, text, state_dir=self.state_root, run_id=f"{plan_dir.name}-run-1"
        )
        return message.id

    def messages(self, plan_dir: Path) -> tuple[mailbox_models.MailboxMessage, ...]:
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

    def parse(self, *argv: str, as_json: bool = True) -> argparse.Namespace:
        """Parse through the real parser, so the flags under test are the ones
        an operator types rather than a namespace the test invented."""
        return self.parser.parse_args(
            [
                "overseer",
                *argv,
                "--state-dir",
                str(self.state_root),
                *(("--json",) if as_json else ()),
            ]
        )

    def _run(self, args: argparse.Namespace) -> tuple[int, str]:
        # `resolve_handler` is typed for the shape every leaf shares. The
        # overseer leaves additionally take the keyword-only `seams` this
        # fixture injects, so the widening lives here rather than in the
        # production signature, where it would stop describing the leaves.
        handler = cast("Callable[..., int]", cli.resolve_handler(args))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            status = handler(args, seams=self.seams())
        return status, stream.getvalue()

    def invoke(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Run one leaf the way ``asl`` would, and read its JSON envelope."""
        status, written = self._run(self.parse(*argv))
        return status, json.loads(written)

    def render(self, *argv: str) -> tuple[int, str]:
        """The same leaf without ``--json``, read the way an operator reads it.

        A structured payload is not the whole of what a leaf hands back: the
        printed lines reach the same host, and a value withheld from one and
        spelled out in the other is not withheld.
        """
        return self._run(self.parse(*argv, as_json=False))

    def claims_file(self, case: unittest.TestCase, claims: list[dict[str, Any]]) -> str:
        path = self.root / f"claims-{len(list(self.root.glob('claims-*.json')))}.json"
        path.write_text(json.dumps(claims), encoding="utf-8")
        case.addCleanup(lambda: path.unlink(missing_ok=True))
        return str(path)

    def reviewed(self, plan_dir: Path, *extra: str) -> str:
        """The digest of the record a host was actually shown.

        Claims are a reading of something, and the only reading that issues a
        digest is the one that prints the statements. Tests bind their claims
        the way a host has to: list first, carry what the listing named. Any
        flag that narrows the reading --- ``--incident-id``, ``--no-history``,
        ``--generated-class`` --- has to be passed here too, because it changes
        the record and so changes its name.
        """
        status, payload = self.invoke(
            "decide", str(plan_dir), "--list-statements", *extra
        )
        assert status == 0, payload
        return str(payload["evidence_digest"])

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
