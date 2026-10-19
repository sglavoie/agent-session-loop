"""Shared fixtures for test_asl_overseer_e2e."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from agent_session_loop import (
    cli,
    clock,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_models,
    overseer_cli,
    overseer_decisions,
    overseer_discovery,
    overseer_history,
    overseer_policy_client,
    overseer_store_ledger,
    plans_files,
    state,
)
from tests import support
from tests import support_asl_overseer_e2e as fixtures


class _World(fixtures._WorldProcesses):
    """One temporary universe: repositories, configuration, corpus, binary.

    Everything a leaf reads is a real artifact of the kind it reads in
    production. The two seams that remain are the binary's location and the
    child environment, and both are here rather than in the module under test
    precisely so nothing under test has to know it is being tested.

    ``binary`` names the ``amx`` under test. It defaults to the one this module
    built from source, which is what a development checkout should be judged
    against. The paired smoke gate in ``tests.test_asl_overseer_paired_smoke``
    passes the *installed* one instead: same world, same assertions, a different
    claim about which pair they are a statement about.
    """

    def __init__(self, case: unittest.TestCase, *, binary: Path | None = None) -> None:
        workdir = tempfile.TemporaryDirectory(prefix="asl-overseer-e2e-")
        case.addCleanup(workdir.cleanup)
        self.case = case
        # `plans_files.canonicalize_path` resolves the macOS `/var` symlink. Every
        # path in this fixture is canonical from the start, because the scope a
        # transcript statement carries is the directory AgentMux reports and the
        # scope a claim carries is the run's declared repository roots: the two
        # are compared for equality, so one uncanonicalized string rejects every
        # claim in the module with `scope_widened`.
        self.root = plans_files.canonicalize_path(workdir.name)
        self.state_root = self.root / "state"
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir(parents=True, exist_ok=True)
        self.home = self.root / "harness-home"
        self.config_path = self.root / "config.toml"
        self.binary = (
            Path(binary) if binary is not None else Path(fixtures._BUILD["binary"])
        )
        self.calls: list[dict[str, str]] = []
        self.on_call: Callable[[list[str]], None] | None = None
        self.repos: dict[str, Path] = {}
        for alias in (
            fixtures.AUTHORIZED,
            fixtures.SECOND,
            fixtures.EXCLUDED,
        ):
            path = self.root / "repos" / alias
            support.init_git_repo(path)
            self.repos[alias] = path
        self.workspace_root = str(self.repos[fixtures.AUTHORIZED])
        self.write_config()
        self.parser = cli.build_parser(prog="asl")
        self.ledgers: list[overseer_store_ledger.Ledger] = []
        self.children: list[subprocess.Popen[bytes]] = []
        case.addCleanup(self._stop_children)
        case.addCleanup(self._close_ledgers)

    # -- configuration -----------------------------------------------------

    def write_config(
        self,
        *,
        authorized: bool = True,
        second: bool = True,
        excluded: bool = False,
        color: str = "blue",
    ) -> None:
        """Write the operator's configuration as AgentMux will read it.

        Read on every invocation by design, so rewriting it here is how a
        withdrawn opt-in reaches the next question rather than the next restart.
        The three opt-ins are separate because the fingerprint AgentMux reports
        covers every declaration: withdrawing one project is how a test moves
        that fingerprint without moving the answer about another.
        """
        lines = [
            "[harness_emojis]",
            f'{fixtures.HARNESS} = "{fixtures.HARNESS_EMOJI}"',
            "",
            "[[profiles]]",
            f'name = "{fixtures.PROFILE}"',
            f'harness = "{fixtures.HARNESS}"',
            f'home_dir = "{self.home}"',
            f'color = "{color}"',
            "",
        ]
        for alias, opted_in in (
            (fixtures.AUTHORIZED, authorized),
            (fixtures.SECOND, second),
            (fixtures.EXCLUDED, excluded),
        ):
            lines.extend(
                [
                    "[[projects]]",
                    f'title = "{alias}"',
                    f'directory = "{self.repos[alias]}"',
                    f'profile = "{fixtures.PROFILE}"',
                    f"asl_overseer = {str(opted_in).lower()}",
                    "",
                ]
            )
        self.config_path.write_text("\n".join(lines), encoding="utf-8")

    def corrupt_config(self) -> None:
        """Leave configuration AgentMux cannot parse where it will look."""
        self.config_path.write_text('[[profiles]\nname = "broken\n', encoding="utf-8")

    # -- transcripts -------------------------------------------------------

    #: What a Claude record has to carry for AgentMux to classify the
    #: conversation it belongs to. The markers are the real ones the harness
    #: writes, not a session class named directly: the point of these fixtures
    #: is that the built binary does the classifying.
    CLASS_MARKERS: dict[str, dict[str, Any]] = {
        overseer_decisions.SESSION_CLASS_MAIN: {"entrypoint": "cli"},
        overseer_decisions.SESSION_CLASS_AUTOMATED: {
            "entrypoint": "cli",
            "isSidechain": True,
        },
        overseer_decisions.SESSION_CLASS_UNKNOWN: {},
    }

    def write_transcript(
        self,
        alias: str,
        turns: list[tuple[str, str]],
        *,
        session_id: str = "",
        session_class: str = overseer_decisions.SESSION_CLASS_MAIN,
    ) -> str:
        """Write one Claude conversation under the configured profile home.

        It is a person's conversation by default, because that is what an
        overseer reads when it looks for something a human said.
        """
        directory = str(self.repos[alias])
        encoded = "-" + directory.strip("/").replace("/", "-")
        project_dir = self.home / "projects" / encoded
        project_dir.mkdir(parents=True, exist_ok=True)
        session_id = session_id or f"{alias}-session"
        started = datetime.now(UTC) - timedelta(hours=len(turns) + 1)
        markers = self.CLASS_MARKERS[session_class]
        lines = [
            json.dumps(
                {
                    "type": role,
                    "timestamp": (started + timedelta(minutes=index)).strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z"
                    ),
                    "cwd": directory,
                    "sessionId": session_id,
                    "message": {"role": role, "content": text},
                    **markers,
                }
            )
            for index, (role, text) in enumerate(turns)
        ]
        (project_dir / f"{session_id}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return session_id

    def statement_id(self, session_id: str, index: int) -> str:
        return (
            f"{overseer_decisions.TRANSCRIPT_SPACE}:"
            f"{fixtures.HARNESS}:{session_id}#{index}"
        )

    def transcript_path(self, alias: str, session_id: str) -> Path:
        directory = str(self.repos[alias])
        encoded = "-" + directory.strip("/").replace("/", "-")
        return self.home / "projects" / encoded / f"{session_id}.jsonl"

    # -- seams -------------------------------------------------------------

    def resolve_binary(self, name: str) -> str | None:
        if name != overseer_policy_client.AMX_BINARY:
            return None
        return str(self.binary)

    def run_command(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """Run one AgentMux invocation, isolated and recorded.

        ``AMX_CONFIG`` is added to the child's environment rather than to this
        process's, so a test that rewrites configuration mid-dispatch changes
        what the *next* child reads without changing anything global. Recording
        the answer here is what makes the exclusion assertions possible: they
        are about what the overseer was told, and this is where it is told.
        """
        completed = subprocess.run(
            argv, env={**os.environ, "AMX_CONFIG": str(self.config_path)}, **kwargs
        )
        self.calls.append(
            {
                "argv": " ".join(str(item) for item in argv),
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
            }
        )
        if self.on_call is not None:
            self.on_call([str(item) for item in argv])
        return completed

    def install_mismatched_binary(self) -> None:
        """Point the client at a binary answering a contract it cannot speak.

        It is the real binary's real answer with the contract version moved,
        which is what a version skew between a deployed pair looks like: the
        command is there, it runs, it answers, and the answer is to a question
        under a contract this side does not implement.
        """
        self._install_contract_shim(
            "contract = data.get('contract')",
            "if isinstance(contract, dict):",
            "    contract['version'] = "
            f"{overseer_policy_client.POLICY_CONTRACT_VERSION + 1}",
        )

    def install_predating_binary(self) -> None:
        """Point the client at a component from before the contract existed.

        The other direction of the same skew, and the one an operator actually
        meets: an AgentMux older than the ASL beside it answers the commands and
        publishes no contract block at all, because it predates the block. There
        is no version 0 to report -- version 1 is the first -- so an older
        component is exactly a payload with nothing to check the version of, and
        the client has to read that as unavailable rather than as permission.
        """
        self._install_contract_shim("data.pop('contract', None)")

    def _install_contract_shim(self, *mutation: str) -> None:
        """Wrap the current binary in a shim that rewrites what it published.

        The shim runs the real binary and edits only the envelope's ``contract``
        block, so everything the payload says about the request it answered
        stays the real answer to the real question. That is the point: the skew
        under test is a disagreement about the contract, not a fabricated reply.
        """
        shim = self.root / "amx-shim"
        shim.write_text(
            "\n".join(
                [
                    f"#!{sys.executable}",
                    "import json, subprocess, sys",
                    f"real = {str(self.binary)!r}",
                    "done = subprocess.run(",
                    "    [real, *sys.argv[1:]], capture_output=True, text=True",
                    ")",
                    "sys.stderr.write(done.stderr)",
                    "try:",
                    "    parsed = json.loads(done.stdout)",
                    "except ValueError:",
                    "    sys.stdout.write(done.stdout)",
                    "    raise SystemExit(done.returncode)",
                    "data = parsed.get('data') or {}",
                    *mutation,
                    "sys.stdout.write(json.dumps(parsed))",
                    "raise SystemExit(done.returncode)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        shim.chmod(0o755)
        self.binary = shim

    def authorize(self, **kwargs: Any) -> overseer_policy_client.Decision:
        return overseer_policy_client.authorize(
            **kwargs,
            resolve_binary=self.resolve_binary,
            run_command=self.run_command,
        )

    def read_history(self, **kwargs: Any) -> overseer_history.History:
        return overseer_history.read_history(
            **kwargs,
            resolve_binary=self.resolve_binary,
            run_command=self.run_command,
        )

    def open_ledger(self, **kwargs: Any) -> overseer_store_ledger.Ledger:
        ledger = overseer_store_ledger.open_ledger(**kwargs)
        self.ledgers.append(ledger)
        return ledger

    def seams(self) -> overseer_cli.Seams:
        return overseer_cli.Seams(
            open_ledger=self.open_ledger,
            authorize=self.authorize,
            read_history=self.read_history,
        )

    def _close_ledgers(self) -> None:
        for ledger in self.ledgers:
            with contextlib.suppress(Exception):
                ledger.close()
        self.ledgers.clear()

    # -- the corpus --------------------------------------------------------

    def write_run(
        self,
        name: str,
        *,
        aliases: tuple[str, ...] = (fixtures.AUTHORIZED,),
        pause_reason: str = "question_pending",
        session_id: str = "1",
        **overrides: Any,
    ) -> Path:
        """One paused run, written the way the runner writes one."""
        plan_dir = plans_files.canonicalize_path(self._plan_dir(name))
        run_id = f"{name}-run-1"
        repositories = [
            {
                "alias": alias,
                "path": f"repos/{alias}",
                "origin": "workspace",
                "resolved_root": str(self.repos[alias]),
            }
            for alias in aliases
        ]
        now = clock.utc_now()
        record: dict[str, Any] = {
            "run_id": run_id,
            "harness": fixtures.HARNESS,
            "ok": True,
            "present": True,
            "paused": True,
            "pause_reason": pause_reason,
            "dry_run": False,
            "plan_dir": str(plan_dir),
            "run_started_at": clock.isoformat(now - timedelta(hours=2)),
            "run_finished_at": None,
            "workspace_root": self.workspace_root,
            "registry_digest": "d" * 64,
            "repositories": repositories,
            "active_turn": {
                "session_id": session_id,
                "turn_index": 3,
                "last_activity_at": clock.isoformat(now - timedelta(minutes=1)),
            },
            "run_config": {
                "cwd": self.workspace_root,
                "harness": fixtures.HARNESS,
                "registry_digest": "d" * 64,
                "repositories": repositories,
                "workspace_root": self.workspace_root,
                "state_dir_override": str(self.state_root),
            },
            overseer_discovery.PAUSED_AT_KEY: clock.isoformat(
                now - timedelta(minutes=1)
            ),
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            ),
        }
        record.update(overrides)
        self.write_state(plan_dir, record)
        return plan_dir

    def write_state(self, plan_dir: Path, record: dict[str, Any]) -> None:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )

    def read_state(self, plan_dir: Path) -> dict[str, Any]:
        path = state.plan_state_dir(plan_dir, self.state_root) / events.STATE_FILE_NAME
        return json.loads(path.read_text(encoding="utf-8"))

    def _plan_dir(self, name: str) -> Path:
        """A plan directory `plans_snapshot.inspect_plan` will actually accept.

        The lock records the plan's own hashes, so the plan has to be a real
        one: an empty directory is refused before a lock can name it.
        """
        path = self.plans_root / name
        support.write_plan(path, ("complete", "pending"))
        return path

    def ask_question(self, plan_dir: Path, text: str) -> str:
        return mailbox_appenders.append_question(
            plan_dir,
            text,
            state_dir=self.state_root,
            run_id=f"{plan_dir.name}-run-1",
        ).id

    def messages(self, plan_dir: Path) -> tuple[mailbox_models.MailboxMessage, ...]:
        return mailbox.read_mailbox(plan_dir, state_dir=self.state_root).messages

    # -- turn artifacts ----------------------------------------------------

    # -- processes ---------------------------------------------------------

    # -- driving -----------------------------------------------------------

    def parse(self, *argv: str) -> Any:
        return self.parser.parse_args(
            ["overseer", *argv, "--state-dir", str(self.state_root), "--json"]
        )

    def invoke(self, *argv: str) -> tuple[int, dict[str, Any]]:
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

    def claims_file(self, claims: list[dict[str, Any]]) -> str:
        path = self.root / f"claims-{len(list(self.root.glob('claims-*.json')))}.json"
        path.write_text(json.dumps(claims), encoding="utf-8")
        return str(path)

    # -- instrumentation ---------------------------------------------------

    def snapshot(self, root: Path) -> dict[str, tuple[int, int, str]]:
        """Size, mtime and content digest of every file under ``root``."""
        return {
            str(path.relative_to(root)): (
                path.stat().st_size,
                path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def answers(self) -> str:
        """Everything AgentMux said back, across every invocation so far."""
        return "\n".join(call["stdout"] + call["stderr"] for call in self.calls)
