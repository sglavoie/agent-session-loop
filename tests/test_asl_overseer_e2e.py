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

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from agent_session_loop import (
    bridge_protocol,
    cli,
    clock,
    events,
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_models,
    mailbox_vocabulary,
    overseer_cli,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_history,
    overseer_policy_client,
    overseer_resume,
    overseer_store_ledger,
    overseer_store_models,
    plans_files,
    plans_snapshot,
    process_tree,
    state,
)
from tests import support

#: Set to any non-empty value other than a falsehood to make a missing AgentMux
#: source tree, a missing Go toolchain or a failed build fail this module rather
#: than skip it. Mirrors the paired installed gate in the AgentMux justfile.
INTEGRATION_ENV = "AMX_ASL_INTEGRATION"

#: Points at an AgentMux checkout when it is not where the search below looks.
SOURCE_ENV = "AMX_SOURCE_ROOT"

#: How long the one-off `go build` may take. Generous because it may be cold.
BUILD_TIMEOUT_SECONDS = 300.0

WATCHER = "overseer-e2e"
SECOND_WATCHER = "overseer-e2e-other"
HARNESS = "claude"
# Configuration validation requires one glyph per harness a profile names.
HARNESS_EMOJI = "\N{FOX FACE}"
PROFILE = "overseer-e2e"

#: The authorized repository, named after the checkpoint recovery the reviewed
#: incident happened in.
AUTHORIZED = "tt"

#: A second configured project, opted *out*. Everything about it is a control.
EXCLUDED = "vendor"

#: A second *authorized* repository, so a run can declare two of them and a
#: commit across them can be stopped halfway.
SECOND = "tt-docs"

#: Planted in the excluded project's conversation and in a file in its
#: repository. Identity discovery may read the bytes around it; nothing may
#: hand it back.
SECRET = "vendor-secret-8f3a1c-never-disclosed"

#: What a person actually said, in the conversation AgentMux authorizes. It is
#: the only kind of evidence that can authorize anything: a mailbox record shows
#: an instruction arrived, never who wrote it.
STANDING_INSTRUCTION = (
    "go ahead and checkpoint work in progress and answer routine tooling "
    "questions on my runs without waiting for me"
)

#: The later word that supersedes the earlier one on the same topic.
LATER_HOLD = "actually hold off on committing anything in tt until I have looked at it"

_BUILD: dict[str, Any] = {}


# -- the gate -----------------------------------------------------------------


def _required() -> bool:
    value = os.environ.get(INTEGRATION_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _candidate_sources() -> tuple[Path, ...]:
    named = os.environ.get(SOURCE_ENV, "").strip()
    if named:
        return (Path(named).expanduser(),)
    beside = support.REPO_ROOT.parent
    return (
        beside / "sglavoie_agentmux",
        beside / "agentmux",
        beside / "1_dev_projects" / "sglavoie_agentmux",
    )


def _locate_source() -> Path | None:
    """Find an AgentMux checkout, or report that there is none to build."""
    for candidate in _candidate_sources():
        overseer = candidate / "cmd" / "overseer"
        if (candidate / "go.mod").is_file() and overseer.is_dir():
            return candidate.resolve()
    return None


def _unavailable(reason: str) -> None:
    """Skip this module, or fail it when the paired gate was asked for."""
    detail = (
        f"{reason}; set {SOURCE_ENV} to an AgentMux checkout to run the paired "
        "end-to-end suite"
    )
    if _required():
        raise AssertionError(f"{INTEGRATION_ENV} is set but {detail}")
    raise unittest.SkipTest(detail)


def setUpModule() -> None:
    """Build the binary under test once, before any fixture needs it."""
    source = _locate_source()
    if source is None:
        _unavailable("no AgentMux source tree was found beside this checkout")
        return
    go = shutil.which("go")
    if not go:
        _unavailable("no `go` toolchain is on PATH")
        return
    workdir = tempfile.TemporaryDirectory(prefix="asl-overseer-e2e-amx-")
    binary = Path(workdir.name) / "amx"
    completed = subprocess.run(
        [go, "build", "-o", str(binary), "."],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=BUILD_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0 or not binary.is_file():
        workdir.cleanup()
        _unavailable(
            f"`go build` in {source} failed: {completed.stderr.strip()[:2000]}"
        )
        return
    _BUILD["workdir"] = workdir
    _BUILD["source"] = source
    _BUILD["binary"] = binary


def tearDownModule() -> None:
    workdir = _BUILD.pop("workdir", None)
    if workdir is not None:
        workdir.cleanup()


# -- the world ----------------------------------------------------------------


class _World:
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
        self.binary = Path(binary) if binary is not None else Path(_BUILD["binary"])
        self.calls: list[dict[str, str]] = []
        self.on_call: Callable[[list[str]], None] | None = None
        self.repos: dict[str, Path] = {}
        for alias in (AUTHORIZED, SECOND, EXCLUDED):
            path = self.root / "repos" / alias
            support.init_git_repo(path)
            self.repos[alias] = path
        self.workspace_root = str(self.repos[AUTHORIZED])
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
            f'{HARNESS} = "{HARNESS_EMOJI}"',
            "",
            "[[profiles]]",
            f'name = "{PROFILE}"',
            f'harness = "{HARNESS}"',
            f'home_dir = "{self.home}"',
            f'color = "{color}"',
            "",
        ]
        for alias, opted_in in (
            (AUTHORIZED, authorized),
            (SECOND, second),
            (EXCLUDED, excluded),
        ):
            lines.extend(
                [
                    "[[projects]]",
                    f'title = "{alias}"',
                    f'directory = "{self.repos[alias]}"',
                    f'profile = "{PROFILE}"',
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
        return f"{overseer_decisions.TRANSCRIPT_SPACE}:{HARNESS}:{session_id}#{index}"

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
        aliases: tuple[str, ...] = (AUTHORIZED,),
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
            "harness": HARNESS,
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
                "harness": HARNESS,
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

    def change(self, alias: str, name: str = "work.txt", text: str = "wip\n") -> None:
        path = self.repos[alias] / name
        path.write_text(text, encoding="utf-8")

    def publish_turn(
        self, plan_dir: Path, aliases: tuple[str, ...], *, turn_index: int = 1
    ) -> None:
        """Publish the write-guard evidence a checkpoint may commit from."""
        record = {
            "turn_index": turn_index,
            "session_id": "1",
            "workspace_write_guard": {
                "changed": {
                    alias: {
                        "head_before": support.git_stdout(
                            self.repos[alias], "rev-parse", "HEAD"
                        ),
                        "head_after": support.git_stdout(
                            self.repos[alias], "rev-parse", "HEAD"
                        ),
                        "qualified_paths": [f"{alias}:work.txt"],
                    }
                    for alias in aliases
                }
            },
        }
        run_dir = (
            state.plan_state_dir(plan_dir, self.state_root)
            / state.RUNS_DIR_NAME
            / f"{plan_dir.name}-run-1"
            / f"turn-{turn_index:04d}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(record), encoding="utf-8"
        )

    # -- processes ---------------------------------------------------------

    def start_fake_harness(self) -> subprocess.Popen[bytes]:
        """A real process standing in for the harness a lock names.

        The overseer never talks to it. What it needs to be is a pid this host's
        process table reports as alive, because that -- not any field in the
        lock file -- is what makes a lock active and a run owned.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(child)
        return child

    def _stop_children(self) -> None:
        for child in self.children:
            with contextlib.suppress(Exception):
                child.kill()
            with contextlib.suppress(Exception):
                child.wait(timeout=10)
        self.children.clear()

    def hold_lock(self, plan_dir: Path, pid: int) -> Path:
        """Publish a lock naming ``pid`` as the run's owner."""
        snapshot = plans_snapshot.inspect_plan(plan_dir)
        lock_path = state.lock_path_for_plan(snapshot.plan_dir, self.state_root)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 3,
                    "owner": "agent-session-loop",
                    "pid": pid,
                    "hostname": socket.gethostname(),
                    "process_start": process_tree.process_birth(pid),
                    "wakeup_endpoint": None,
                    "plan_dir": str(snapshot.plan_dir),
                    "plan_path_hash": snapshot.path_hash,
                    "plan_hash": snapshot.plan_hash,
                    "repositories": [self.workspace_root],
                    "created_at": clock.utc_iso_now(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return lock_path

    def release_lock(self, plan_dir: Path) -> None:
        """Drop the lock the way a runner that paused and exited does.

        A paused run is not a running one: the worker releases the plan before
        it exits, which is the state any later resume actually meets.
        """
        snapshot = plans_snapshot.inspect_plan(plan_dir)
        state.lock_path_for_plan(snapshot.plan_dir, self.state_root).unlink(
            missing_ok=True
        )

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


class _WorldCase(unittest.TestCase):
    """One authorized plan, one excluded project, one person's instruction."""

    #: The ``amx`` the world is built around. ``None`` means the binary this
    #: module built from source. The paired smoke gate subclasses these fixtures
    #: with the installed one, so the world stays defined in one place.
    binary: Path | None = None

    def setUp(self) -> None:
        self.world = _World(self, binary=self.binary)
        self.session = self.world.write_transcript(
            AUTHORIZED,
            [
                ("user", STANDING_INSTRUCTION),
                ("assistant", "understood; I will checkpoint and answer routinely"),
            ],
        )
        self.excluded_session = self.world.write_transcript(
            EXCLUDED,
            [
                ("user", f"the deploy key for staging is {SECRET}"),
                ("assistant", "noted"),
            ],
        )
        (self.world.repos[EXCLUDED] / "credentials.txt").write_text(
            f"{SECRET}\n", encoding="utf-8"
        )
        self.plan = self.world.write_run("plan-tt")
        self.harness = self.world.start_fake_harness()
        self.world.hold_lock(self.plan, self.harness.pid)

    # -- the loop ----------------------------------------------------------

    def watch(self, *extra: str, watcher: str = WATCHER) -> dict[str, Any]:
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
        watcher: str = WATCHER,
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
            watcher_id=WATCHER,
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


# -- the paired boundary ------------------------------------------------------


class PairedBoundaryTests(_WorldCase):
    """The binary answers, and it answers the contract this client speaks."""

    def test_the_built_binary_answers_the_policy_contract(self) -> None:
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
                    alias=AUTHORIZED, path=str(self.world.repos[AUTHORIZED])
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
                    alias=AUTHORIZED, path=str(self.world.repos[AUTHORIZED])
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


# -- historical replay --------------------------------------------------------


class HistoricalReplayTests(_WorldCase):
    """The reviewed incident shapes, replayed through the whole loop."""

    def test_the_checkpoint_recovery_commits_the_runs_own_work(self) -> None:
        """The `tt` incident: a run paused with work nobody had committed."""
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
            "work.txt",
            support.git_stdout(
                self.world.repos[AUTHORIZED], "show", "--name-only", "--format=", "HEAD"
            ),
        )
        self.assertEqual([], self._porcelain(AUTHORIZED))

    def test_the_dirty_worktree_pause_is_answered_with_guidance(self) -> None:
        self.world.change(AUTHORIZED)
        packet = self.observed()

        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIn(
            mailbox_vocabulary.KIND_GUIDANCE,
            [message.kind for message in self.world.messages(self.plan)],
        )

    def test_an_open_question_is_answered_where_the_person_authorized_it(self) -> None:
        question = self.world.ask_question(self.plan, "which lint profile?")
        packet = self.observed()

        status, payload = self.act(
            packet,
            "reply",
            self.allowing("reply"),
            "--question-id",
            question,
            "--text",
            "the repository default",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertEqual(
            (),
            mailbox.unanswered_questions(self.plan, state_dir=self.world.state_root),
        )

    def test_the_stale_question_resume_stays_refused_while_it_is_open(self) -> None:
        """The stale-question incident: a pause an answer, not a restart, ends."""
        self.world.write_run(
            "plan-tt",
            pause_reason="waiting_for_reply",
            **{"pause_policy": {"user_action": "reply"}},
        )
        self.world.hold_lock(self.plan, self.harness.pid)
        self.world.ask_question(self.plan, "which lint profile?")
        packet = self.observed()

        status, payload = self.act(packet, "resume", self.allowing("resume"))

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("answer_required", payload["dispatch"]["detail"])
        self.assertIsNotNone(payload["escalation"])

    def test_the_resume_becomes_available_once_the_question_is_resolved(self) -> None:
        """The same run, after the question it waited on was superseded.

        Driven through :func:`overseer_dispatch.dispatch` rather than the leaf:
        the leaf's registry has no ``launch`` seam, so an authorized resume there
        would spawn a real runner. Everything else -- the ledger, the lease, the
        policy client, the binary, the state corpus -- is the same as the leaf's.
        """
        self.world.write_run(
            "plan-tt",
            pause_reason="waiting_for_reply",
            **{"pause_policy": {"user_action": "reply"}},
        )
        self.world.release_lock(self.plan)
        question = self.world.ask_question(self.plan, "which lint profile?")
        mailbox_appenders.append_resolution(
            self.plan,
            question,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            expected_run_id=f"{self.plan.name}-run-1",
            note="the question was superseded before anyone answered it",
            state_dir=self.world.state_root,
        )
        packet = self.observed()

        dispatch = self._dispatch_resume(packet)

        self.assertTrue(dispatch.performed, dispatch.detail)
        self.assertFalse(dispatch.escalated, dispatch.detail)
        self.assertTrue(self.launched, "the resume never reached the launcher")

    def test_an_explicit_hold_keeps_the_operation_blocked(self) -> None:
        """A person who said hold is a decision, not an absence of one."""
        packet = self.observed()
        claims = [
            {
                "statement_id": self.world.statement_id(self.session, 0),
                "topic": "safety",
                "stance": "hold",
                "reading": "the user wants to look at this before anything moves",
            }
        ]

        status, payload = self.decide(packet, "commit", claims)

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertIsNotNone(payload["escalation"])
        self.assertTrue(payload["escalation"]["question"])
        self.assertFalse(payload["escalation"]["blocked"] == [])

    def test_a_later_word_supersedes_the_earlier_one(self) -> None:
        """Two turns, the second withdrawing the first; the second governs."""
        session = self.world.write_transcript(
            AUTHORIZED,
            [("user", STANDING_INSTRUCTION), ("user", LATER_HOLD)],
            session_id="tt-superseded",
        )
        self.world.change(AUTHORIZED)
        self.world.publish_turn(self.plan, (AUTHORIZED,))
        packet = self.observed()
        claims = [
            {
                "statement_id": self.world.statement_id(session, 0),
                "topic": "operations",
                "stance": "allow",
                "operations": ["commit"],
                "reading": "the user authorized checkpoints",
            },
            {
                "statement_id": self.world.statement_id(session, 1),
                "topic": "operations",
                "stance": "hold",
                "reading": "the user later withdrew that for tt",
            },
        ]

        status, payload = self.decide(packet, "commit", claims)

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertEqual(
            [self.world.statement_id(session, 0)],
            [item["statement_id"] for item in payload["ruling"]["superseded"]],
        )
        self.assertEqual([], self._porcelain_committed(AUTHORIZED))

    def test_a_machine_conversation_authorizes_nothing_it_says(self) -> None:
        """The same sentence, in a conversation no person was in.

        Nothing here names a session class: the fixture carries the markers a
        harness writes, the built binary classifies them, and ASL reads that
        classification. Both halves of that contract are under test, which is
        why this lives with the real binary rather than beside a stub.
        """
        for session_class in (
            overseer_decisions.SESSION_CLASS_AUTOMATED,
            overseer_decisions.SESSION_CLASS_UNKNOWN,
        ):
            with self.subTest(session_class=session_class):
                session = self.world.write_transcript(
                    AUTHORIZED,
                    [("user", STANDING_INSTRUCTION)],
                    session_id=f"tt-{session_class}",
                    session_class=session_class,
                )
                packet = self.observed()
                claims = [
                    {
                        "statement_id": self.world.statement_id(session, 0),
                        "topic": "operations",
                        "stance": "allow",
                        "operations": ["commit"],
                        "reading": "read as if a person had authorized checkpoints",
                    }
                ]

                status, payload = self.decide(packet, "commit", claims)

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertFalse(payload["authorized"])
                self.assertEqual(
                    ["not_user_authored"],
                    [item["reason"] for item in payload["rejected_claims"]],
                )
                self.assertEqual([], self._porcelain_committed(AUTHORIZED))


# -- fault qualification ------------------------------------------------------


class FaultQualificationTests(_WorldCase):
    """Each injected fault becomes a refusal that names itself, and nothing else.

    The assertion each of these makes twice is that the refusal is the *named*
    one and that the world did not move. A fault that produces some refusal is
    not qualified: an action stopped by the wrong guard is an action whose real
    guard was never exercised, and the two are indistinguishable from the exit
    status alone.
    """

    def test_an_opt_in_withdrawn_after_the_decision_stops_the_action(self) -> None:
        """The decision was sound when it was made. It is not a licence."""
        self.world.change(AUTHORIZED)
        self.world.publish_turn(self.plan, (AUTHORIZED,))
        packet = self.observed()
        status, decided = self.decide(packet, "commit", self.allowing("commit"))
        self.assertEqual(overseer_cli.EXIT_OK, status, decided)
        self.assertTrue(decided["authorized"])

        self.world.write_config(authorized=False)
        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit"),
            "--message",
            "checkpoint work in progress",
        )

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("unauthorized", payload["dispatch"]["refusal"])
        self.assertEqual([], self._porcelain_committed(AUTHORIZED))

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
        (self.world.repos[SECOND] / ".git" / "index.lock").write_text(
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
        self.assertEqual([], self._porcelain_committed(AUTHORIZED))
        self.assertEqual([], self._porcelain_committed(SECOND))
        for alias in (AUTHORIZED, SECOND):
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
        self._seal_refs(SECOND)
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
        self.assertEqual(1, len(self._porcelain_committed(AUTHORIZED)))
        self.assertEqual([], self._porcelain_committed(SECOND))

    def test_a_dispatch_left_in_doubt_blocks_the_next_one_until_it_settles(
        self,
    ) -> None:
        """A row a dead process left is answered before anything is retried."""
        self.world.change(AUTHORIZED)
        self.world.publish_turn(self.plan, (AUTHORIZED,))
        packet = self.observed()
        message = "checkpoint work in progress"
        self._strand_a_commit(packet, message)
        unreadable = self.world.repos[AUTHORIZED] / ".git"
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
        self.assertEqual(1, len(self._porcelain_committed(AUTHORIZED)))

    def test_a_second_supervisor_cannot_act_on_a_plan_it_does_not_hold(self) -> None:
        """Two supervisors, one plan: the lease decides, and it says who holds it."""
        packet = self.observed()

        sweep = self.watch(watcher=SECOND_WATCHER)["sweeps"][0]
        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
            watcher=SECOND_WATCHER,
        )

        self.assertEqual(
            ["owned_elsewhere"], [item["reason"] for item in sweep["excluded"]]
        )
        self.assertEqual([], sweep["enrolled"])
        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertEqual("ownership_lost", payload["dispatch"]["refusal"])
        self.assertIn(WATCHER, payload["dispatch"]["detail"])
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
        self.world.write_run("plan-tt", aliases=(AUTHORIZED, SECOND))
        second = self.world.write_transcript(SECOND, [("user", STANDING_INSTRUCTION)])
        for alias in (AUTHORIZED, SECOND):
            self.world.change(alias)
        self.world.publish_turn(self.plan, (AUTHORIZED, SECOND))
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


# -- exclusion invariants -----------------------------------------------------


class ExclusionInvariantTests(_WorldCase):
    """The excluded project, across every operation, before and after.

    Every test here ends in the same two assertions, because the invariant is
    not about any one operation: the excluded repository and the excluded
    conversation are byte-for-byte and mtime-for-mtime what they were, and
    nothing AgentMux said back carries the secret either of them contains.
    """

    def setUp(self) -> None:
        super().setUp()
        self.before = self._excluded()

    def test_the_excluded_project_is_refused_by_name(self) -> None:
        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(
                overseer_policy_client.Repository(
                    alias=EXCLUDED, path=str(self.world.repos[EXCLUDED])
                ),
            ),
            timeout=30.0,
        )

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.available)
        self.assertIsNone(decision.failure)
        self.assertEqual(
            [EXCLUDED], [item.alias for item in decision.refused_repositories()]
        )
        self.assertIsNone(decision.grant())
        self._assert_untouched()

    def test_the_excluded_conversation_is_withheld_without_being_located(self) -> None:
        history = self.world.read_history(
            plan_dir=str(self.plan),
            repositories=overseer_policy_client.repositories_for(
                self._candidate().repositories
            ),
            timeout=30.0,
        )

        self.assertTrue(history.allowed, history.summary)
        self.assertEqual(
            [self.world.workspace_root],
            [item.directory for item in history.conversations],
        )
        self.assertEqual(
            [(HARNESS, self.excluded_session)],
            [(item.harness, item.session_id) for item in history.withheld],
        )
        for item in history.withheld:
            self.assertTrue(item.reason)
            self.assertNotIn(str(self.world.repos[EXCLUDED]), item.reason)
        self._assert_untouched()

    def test_no_operation_reaches_the_excluded_project(self) -> None:
        """Every operation in the scope, none of them touching the exclusion."""
        question = self.world.ask_question(self.plan, "which lint profile?")
        self.world.change(AUTHORIZED)
        self.world.publish_turn(self.plan, (AUTHORIZED,))
        attempts = (
            ("reply", ("--question-id", question, "--text", "the repository default")),
            ("guidance", ("--text", "commit what you have and carry on")),
            ("commit", ("--message", "checkpoint work in progress")),
            ("resume", ()),
            ("verify", ("--check", "lint")),
        )

        refusals = {}
        details = {}
        for operation, extra in attempts:
            # Re-observed each time: every operation that lands moves the run,
            # and an action carrying the previous picture is stale by design.
            _status, payload = self.act(
                self.observed(), operation, self.allowing(operation), *extra
            )
            refusals[operation] = payload["dispatch"]["refusal"]
            details[operation] = payload["dispatch"]["detail"]

        self.assertIsNone(refusals["reply"])
        self.assertIsNone(refusals["guidance"])
        self.assertIsNone(refusals["commit"])
        # This world's sessions carry no completion contract, so there is no
        # check to name and `verify` stops at the plan. That is the whole
        # point of the operation: the argv comes from a declaration or the
        # action is refused, and no `--check` string here becomes a command.
        self.assertEqual("preparation_declined", refusals["verify"])
        self.assertIn("contract_absent", details["verify"])
        # The plan's own worker is alive throughout, so `resume` is refused by
        # the census. Nothing here may reach the leaf's launcher: it has no
        # `launch` seam, and an authorized resume would start a real runner.
        self.assertEqual("preparation_declined", refusals["resume"])
        self.assertEqual(1, len(self._porcelain_committed(AUTHORIZED)))
        self._assert_untouched()

    # -- helpers -----------------------------------------------------------

    def _excluded(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The excluded repository and the excluded conversation, as bytes."""
        conversations = self.world.transcript_path(
            EXCLUDED, self.excluded_session
        ).parent
        return (
            self.world.snapshot(self.world.repos[EXCLUDED]),
            self.world.snapshot(conversations),
        )

    def _assert_untouched(self) -> None:
        self.assertEqual(self.before, self._excluded())
        self.assertNotIn(SECRET, self.world.answers())
