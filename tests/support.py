"""Shared fixtures and helpers for the agent_session_loop test suite.

Split out of tests/test_agent_session_loop.py; imported by the
tests/test_asl_*.py subsystem modules.
"""

from __future__ import annotations

import ast
import atexit
import errno
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, suppress
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_delivery,
    authoring,
    config,
    control_signal,
    events,
    plans,
    prepare_models,
    prepare_packet,
    process_tree,
    review,
    review_session_body,
    run_control,
    state,
    workbench,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# The whole test suite runs with real AgentMux notification disabled so no
# test — nor any nested `python -m agent_session_loop` subprocess it spawns,
# which inherits this environment — can reach a live daemon through a real
# `amx` binary on PATH. Notifier-specific tests opt back in with
# `agent_mux_notify_enabled()` and point delivery at a fake executable or a
# mocked subprocess layer.
os.environ[agent_mux_delivery.NOTIFY_ENV_VAR] = agent_mux_delivery.NOTIFY_OFF


@contextmanager
def agent_mux_notify_enabled(mode: str = "auto"):
    """Re-enable AgentMux notification inside a notifier-specific test."""
    with patched_env(**{agent_mux_delivery.NOTIFY_ENV_VAR: mode}):
        yield


#: How a module would name the runner in an import, however it is spelled:
#: ``import runner``, ``from . import runner``, ``from .runner import x``,
#: ``import agent_session_loop.runner``. Every form ends in this component.
_RUNNER_MODULE_NAME = "runner"

#: The dotted name an `importlib.import_module` call would pass as a string,
#: which no AST import node would show. Matched by equality, so a docstring
#: that merely mentions the module is not an import.
_RUNNER_DOTTED_NAME = "agent_session_loop.runner"


def assert_no_runner_import(case: unittest.TestCase, module: ModuleType) -> None:
    """Assert an extracted boundary module never reaches back into the runner.

    The extraction holds only while the dependency runs one way: what a module
    needs from the loop arrives as a parameter, a ``Protocol`` seam, or a
    callback. Each per-module test calls this in one line rather than copying
    the assertion, so a new extraction adds a line instead of a paragraph.

    The module's source is parsed rather than its namespace inspected, so an
    import buried in a function body -- the lazy form that would satisfy a
    namespace check -- is caught too, as is the dotted name passed to
    ``importlib`` as a string.
    """
    source = Path(module.__file__ or "").read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import | ast.ImportFrom):
            imported = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
            if any(name.split(".")[-1] == _RUNNER_MODULE_NAME for name in imported):
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
        elif isinstance(node, ast.Constant) and node.value == _RUNNER_DOTTED_NAME:
            offenders.append(f"line {node.lineno}: {_RUNNER_DOTTED_NAME!r}")
    case.assertEqual(
        offenders,
        [],
        f"{module.__name__} imports the runner: " + "; ".join(offenders),
    )


def called_names(node: ast.AST) -> set[str]:
    """Every name called under ``node``, whether bare or through an attribute.

    Matched on the parse tree rather than the text, because a name can occur as
    a substring of a longer call -- ``open(`` inside ``_record_review_reopen(``
    -- and that false positive is the kind that gets a real assertion deleted
    rather than fixed.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


FAKE_CODEX_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_codex.py"


HARNESS_FAILURE_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "harness_failures"


CODEX_USAGE_LIMIT_FIXTURE = HARNESS_FAILURE_FIXTURE_DIR / "codex_usage_limit.txt"


CLAUDE_SESSION_LIMIT_FIXTURE = HARNESS_FAILURE_FIXTURE_DIR / "claude_session_limit.txt"


class TTYText(io.StringIO):
    def __init__(self, value: str = "", *, tty: bool = True, fd: int = 10) -> None:
        super().__init__(value)
        self._tty = tty
        self._fd = fd

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        return self._fd


@contextmanager
def patched_env(**updates: str | None):
    originals = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def write_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    contracts_by_id = contracts_by_id or {}
    for number, status_value in enumerate(statuses, start=1):
        contract_block = contracts_by_id.get(str(number), "")
        body = [
            f"# Session {number}: Test",
            "",
            "Instructions.",
            "",
        ]
        if contract_block:
            body.extend([contract_block, ""])
        body.extend(
            [
                "## Handoff",
                "",
                f"- **Status**: {status_value}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (plan_dir / f"session-{number}.md").write_text(
            "\n".join(body),
            encoding="utf-8",
        )


def write_plan_with_ids(
    plan_dir: Path,
    statuses: tuple[tuple[str, str], ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    contracts_by_id = contracts_by_id or {}
    for session_id, status_value in statuses:
        display_label = plans.session_id_display_label(session_id)
        contract_block = contracts_by_id.get(session_id, "")
        body = [
            f"# {display_label}: Test",
            "",
            "Instructions.",
            "",
        ]
        if contract_block:
            body.extend([contract_block, ""])
        body.extend(
            [
                "## Handoff",
                "",
                f"- **Status**: {status_value}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (plan_dir / plans.session_id_to_filename(session_id)).write_text(
            "\n".join(body),
            encoding="utf-8",
        )


def install_fake_codex(tmp_dir: Path) -> Path:
    return install_fake_harness(tmp_dir, "codex")


def install_fake_harness(tmp_dir: Path, executable_name: str) -> Path:
    fake_path = tmp_dir / "codex"
    if executable_name != "codex":
        fake_path = tmp_dir / executable_name
    shutil.copyfile(FAKE_CODEX_FIXTURE, fake_path)
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path


def install_fake_amx(tmp_dir: Path) -> tuple[Path, Path]:
    fake_path = tmp_dir / "amx"
    log_path = tmp_dir / "fake-amx.jsonl"
    fake_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                "import json",
                "import os",
                "import sys",
                "import uuid",
                "",
                "log_path = os.environ.get('FAKE_AMX_LOG')",
                "if log_path:",
                "    Path(log_path).parent.mkdir(parents=True, exist_ok=True)",
                "    with Path(log_path).open('a', encoding='utf-8') as handle:",
                "        handle.write(json.dumps({",
                "            'argv': sys.argv,",
                "            'cwd': os.getcwd(),",
                "            'stdin': sys.stdin.read(),",
                "        }, separators=(',', ':')) + '\\n')",
                "if '--json' in sys.argv:",
                "    request_id = ''",
                "    if '--request-id' in sys.argv:",
                "        request_id = sys.argv[sys.argv.index('--request-id') + 1]",
                "    print(json.dumps({",
                "        'schema_version': 1,",
                "        'status': 'accepted',",
                "        'retryable': False,",
                "        'reason': 'accepted',",
                "        'trace_id': str(uuid.uuid4()),",
                "        'request_id': request_id,",
                "    }, separators=(',', ':')))",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path, log_path


#: Generation bound the commit-timeout probes drive the fake harness with. It
#: is a hang guard, not a performance assertion: the bound has to outlast the
#: whole cold start it races -- an `asl commit` subprocess that stages, captures
#: the diff and only then spawns the fixture, which starts an interpreter of its
#: own before publishing the grandchild PID the probes read. That costs seconds
#: rather than milliseconds while the sharded suite saturates the machine, and
#: too tight a bound kills the process tree before the PID is published, which
#: reads as a flaky test rather than as the overload it is.
GENERATION_TIMEOUT_PROBE_SECONDS = 15


def fake_commit_harness_env(
    tmp_dir: Path, *, log_path: Path | None = None
) -> dict[str, str]:
    """Point the runner's `asl commit` subprocess at the fake codex harness.

    Auto-commit re-enters this checkout as `python -m agent_session_loop
    commit`, which resolves its message-generation harness from
    `harnesses.json` under `XDG_CONFIG_HOME`. A test that does not otherwise
    configure a harness home needs this or the commit would reach whatever
    real CLI the developer has installed. `log_path` opts into the fixture's
    per-commit JSONL record.
    """
    config_home = tmp_dir / "commit-config"
    config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fake_codex = install_fake_codex(tmp_dir)
    config_path.write_text(
        json.dumps({"codex": {"executable": str(fake_codex)}}),
        encoding="utf-8",
    )
    env = {"XDG_CONFIG_HOME": str(config_home)}
    if log_path is not None:
        env["FAKE_CODEX_COMMIT_LOG"] = str(log_path)
    return env


_GIT_TEMPLATE_LOCK = threading.Lock()
_git_template_dir: Path | None = None
#: Passed on the command line, not written to the repository's config, so the
#: copies stay identical to what ``git init`` produced. Without them ``git
#: commit`` may start detached auto-maintenance in the template, whose lock
#: files appear and vanish under a concurrent copy.
_TEMPLATE_GIT_OPTIONS = ("-c", "maintenance.auto=false", "-c", "gc.auto=0")


def _build_git_template() -> Path:
    """The one-commit repository every ``init_git_repo`` call copies.

    Five git subprocesses per repository dominated the suite's system time, so
    they run once per process and each caller gets a copy. The template lives
    in its own temporary directory, removed when the process exits.
    """
    global _git_template_dir
    with _GIT_TEMPLATE_LOCK:
        if _git_template_dir is not None:
            return _git_template_dir
        root = Path(tempfile.mkdtemp(prefix="asl-test-git-template-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        for args in (
            ["init"],
            ["config", "user.email", "agent-session-loop@example.com"],
            ["config", "user.name", "Agent Session Loop Tests"],
        ):
            subprocess.run(
                ["git", *_TEMPLATE_GIT_OPTIONS, *args],
                cwd=repo_dir,
                text=True,
                capture_output=True,
                check=True,
            )
        (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
        for args in (["add", "README.md"], ["commit", "-m", "Initial commit"]):
            subprocess.run(
                ["git", *_TEMPLATE_GIT_OPTIONS, *args],
                cwd=repo_dir,
                text=True,
                capture_output=True,
                check=True,
            )
        _git_template_dir = repo_dir
        return repo_dir


def init_git_repo(repo_dir: Path) -> None:
    """Make ``repo_dir`` a git repository with one commit of ``README.md``.

    Copies a per-process template, so every repository a process creates has
    the same initial commit SHA.
    """
    shutil.copytree(_build_git_template(), repo_dir, symlinks=True, dirs_exist_ok=True)


DEFAULT_MULTI_REPO_SPECS: tuple[tuple[str, str, str], ...] = (
    ("SVC", "tundra_svc", "git@github.com:tundra/SVC.git"),
    ("OAK", "tundra_OAK", "git@github.com:tundra/OAK.git"),
    ("OWL", "tundra_OWL_J", "git@github.com:tundra/OWL.J.git"),
    ("Kiln", "tundra_Kiln", "git@github.com:tundra/Kiln.git"),
)


#: Two clones of one remote, checked out side by side. Repository identity is
#: the resolved Git top-level, so these are two repositories even though
#: ``workspaces.origins_equal`` holds between them.
KILN_TWIN_REPO_SPECS: tuple[tuple[str, str, str], ...] = (
    ("Kiln", "tundra_Kiln", "git@github.com:tundra/Kiln.git"),
    (
        "KilnTesting",
        "tundra_Kiln_testing",
        "git@github.com:tundra/Kiln.git",
    ),
)


def init_multi_repo_workspace(
    workspace_dir: Path,
    specs: tuple[tuple[str, str, str], ...] = DEFAULT_MULTI_REPO_SPECS,
) -> dict[str, Path]:
    """Create a non-Git workspace with sibling Git repositories and origins."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    repositories: dict[str, Path] = {}
    for alias, relative_path, origin in specs:
        repository = workspace_dir / relative_path
        init_git_repo(repository)
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=repository,
            text=True,
            capture_output=True,
            check=True,
        )
        repositories[alias] = repository
    return repositories


def write_multi_repo_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
    specs: tuple[tuple[str, str, str], ...] = DEFAULT_MULTI_REPO_SPECS,
) -> None:
    """Write a minimal registered plan with valid per-session scopes."""
    write_plan(plan_dir, statuses, contracts_by_id=contracts_by_id)
    registry_rows = "\n".join(
        f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
    )
    (plan_dir / "manifest.md").write_text(
        "# Manifest\n\n## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        f"{registry_rows}\n",
        encoding="utf-8",
    )
    aliases = ", ".join(alias for alias, _path, _origin in specs)
    for number in range(1, len(statuses) + 1):
        path = plan_dir / f"session-{number}.md"
        text = path.read_text(encoding="utf-8")
        heading, remainder = text.split("\n", 1)
        path.write_text(
            f"{heading}\n\n**Reads repositories**: {aliases}\n"
            f"**Writes repositories**: {aliases}\n{remainder}",
            encoding="utf-8",
        )


def git_stdout(repo_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fail_open_for_path(target: Path):
    canonical_target = plans.canonicalize_path(target)
    original_open = Path.open

    def failing_open(path: Path, *args, **kwargs):
        if plans.canonicalize_path(path) == canonical_target:
            raise PermissionError(
                errno.EACCES, "permission denied", os.fspath(canonical_target)
            )
        return original_open(path, *args, **kwargs)

    return mock.patch.object(Path, "open", failing_open)


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


#: A grandchild of a verification command check: it publishes its PID, waits to
#: be orphaned, and only then records a late side effect. It inherits the
#: check's captured pipes, so surviving it also keeps the reader threads that
#: drain those pipes from ever seeing EOF.
_TREE_GRANDCHILD_SOURCE = (
    "import os, pathlib, sys, time\n"
    "name, root = sys.argv[1], pathlib.Path(sys.argv[2])\n"
    "pending = root / 'pending' / (name + '-grandchild')\n"
    "pending.write_text(str(os.getpid()))\n"
    "os.replace(pending, root / 'grandchildren' / name)\n"
    "parent = os.getppid()\n"
    "while os.getppid() == parent:\n"
    "    time.sleep(0.01)\n"
    "(root / 'late' / name).write_text('orphaned')\n"
    "time.sleep(60)\n"
)

#: The command a check runs: spawn a grandchild, wait until it is live, publish
#: this process's PID under `started/`, then outlive any termination window.
#: A fourth argv entry of `detached` makes the grandchild call `setsid`, which
#: takes it out of the check's process group and so out of reach of `killpg`.
_TREE_PARENT_SOURCE = (
    "import os, pathlib, subprocess, sys, time\n"
    "name, root, child = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]\n"
    "detached = len(sys.argv) > 4 and sys.argv[4] == 'detached'\n"
    "subprocess.Popen(\n"
    "    [sys.executable, child, name, str(root)], start_new_session=detached\n"
    ")\n"
    "while not (root / 'grandchildren' / name).exists():\n"
    "    time.sleep(0.01)\n"
    "pending = root / 'pending' / name\n"
    "pending.write_text(str(os.getpid()))\n"
    "os.replace(pending, root / 'started' / name)\n"
    "time.sleep(60)\n"
)


def write_process_tree_scripts(root: Path) -> tuple[Path, Path]:
    """Lay out the marker directories and both tree scripts under ``root``.

    A ``started/<name>`` marker is written only once that check's grandchild is
    live, so waiting for it is enough to know the whole tree is running.
    """
    for name in ("pending", "started", "grandchildren", "late"):
        (root / name).mkdir(parents=True, exist_ok=True)
    parent = root / "spawn_tree.py"
    parent.write_text(_TREE_PARENT_SOURCE, encoding="utf-8")
    grandchild = root / "tree_grandchild.py"
    grandchild.write_text(_TREE_GRANDCHILD_SOURCE, encoding="utf-8")
    return parent, grandchild


def wait_for_process_exit(pid: int, *, timeout: float = 15.0) -> bool:
    """Report whether ``pid`` is still alive after waiting up to ``timeout``.

    A killed process is reaped by its parent (or, once orphaned, by init)
    shortly after the signal lands, so this polls rather than reading the PID
    table once. ``True`` means the process outlived the wait.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() > deadline:
            return True
        time.sleep(0.02)


def assert_marked_pids_exited(
    marker_dir: Path, *, label: str, timeout: float = 15.0
) -> None:
    """Assert every PID published under ``marker_dir`` is gone."""
    for marker in sorted(marker_dir.iterdir()):
        pid = int(marker.read_text(encoding="utf-8"))
        if wait_for_process_exit(pid, timeout=timeout):
            raise AssertionError(
                f"{label} for {marker.name} (pid {pid}) is still alive"
            )


def assert_marked_pids_alive(marker_dir: Path, *, label: str) -> None:
    """Assert every PID published under ``marker_dir`` is still running.

    No polling: the tree scripts sleep well past any assertion window, so a
    process that is gone here was terminated rather than merely slow.
    """
    for marker in sorted(marker_dir.iterdir()):
        pid = int(marker.read_text(encoding="utf-8"))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            raise AssertionError(
                f"{label} for {marker.name} (pid {pid}) is gone"
            ) from None


def kill_marked_pids(marker_dir: Path) -> None:
    """Kill every PID published under ``marker_dir``, ignoring the dead ones.

    Cleanup for tests that deliberately let a detached descendant survive: it
    outlives the test process otherwise.
    """
    if not marker_dir.exists():
        return
    for marker in sorted(marker_dir.iterdir()):
        try:
            pid = int(marker.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _baseline_contract_text(json_block: str) -> str:
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "- something",
            "",
            "### Verify",
            "",
            "```json",
            json_block,
            "```",
            "",
            "## Handoff",
            "- **Status**: pending",
        ]
    )


def _command_contract(
    argv: str,
    *,
    session: str = "1",
    name: str = "cmd",
    expectation: str = "pass",
) -> str:
    return _contract_block(
        (
            f'{{"type": "command", "argv": {argv}, "name": "{name}", '
            f'"baseline_expectation": "{expectation}"}}'
        ),
        session=session,
    )


class FakeRunControlListener:
    def __init__(
        self,
        *,
        stop_requested: bool = False,
        actions_on_start: tuple[run_control.RunControlAction, ...] = (),
        status_on_start: tuple[run_control.RunControlAction, ...] = (),
        state_path: Path | None = None,
        events_path: Path | None = None,
        current_harness: str | None = None,
    ) -> None:
        self.actions_on_start = actions_on_start
        self.status_on_start = status_on_start
        self.state_path = state_path
        self.events_path = events_path
        self.on_action: run_control.ActionCallback | None = None
        self.on_status: run_control.StatusCallback | None = None
        self.start_calls = 0
        self.stop_calls = 0
        self.consume_calls = 0
        self.abort_consume_calls = 0
        self.state_exists_at_start = False
        self.events_exists_at_start = False
        self.state = run_control.RunControlState(current_harness=current_harness)
        if stop_requested:
            self.state.request_stop_after_current()

    def start(self) -> bool:
        self.start_calls += 1
        self.state_exists_at_start = (
            self.state_path.exists() if self.state_path is not None else False
        )
        self.events_exists_at_start = (
            self.events_path.exists() if self.events_path is not None else False
        )
        if self.on_action is not None:
            for action in self.actions_on_start:
                self.on_action(action)
        if self.on_status is not None:
            for action in self.status_on_start:
                self.on_status(action)
        return True

    def stop(self) -> None:
        self.stop_calls += 1

    def consume_stop_after_current(self) -> run_control.RunControlAction | None:
        # The runner peeks the shared state at every checkpoint and consumes
        # only when it honors the request, so the fake has to route both
        # through the real state rather than faking the consume side.
        self.consume_calls += 1
        return self.state.consume_stop_after_current()

    def consume_abort_turn(self) -> run_control.RunControlAction | None:
        self.abort_consume_calls += 1
        return self.state.consume_abort_turn()


def _contract_block(
    check_payload: str, *, session: str = "1", parallel: bool = False
) -> str:
    parallel_field = ', "parallel": true' if parallel else ""
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "- recipe runs",
            "",
            "### Verify",
            "",
            "```json",
            f'{{"session": "{session}"{parallel_field}, "checks": [{check_payload}]}}',
            "```",
        ]
    )


def _write_loop_ready_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
    state: plans.ReadinessState = "loop-ready",
) -> plans.ReadinessMetadata:
    """Write a plan with valid contracts and a synced readiness block."""
    contract_blocks: dict[str, str] = {}
    for number in range(1, len(statuses) + 1):
        key = str(number)
        contract_blocks[key] = (contracts_by_id or {}).get(
            key,
            _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{number}"}}',
                session=key,
            ),
        )
    write_plan(plan_dir, statuses, contracts_by_id=contract_blocks)
    expected = plans.stable_readiness_sha256(plan_dir)
    metadata = plans.ReadinessMetadata(
        state=state,
        readiness_sha256=expected,
        checked_at="2026-06-19T00:00:00Z",
    )
    plans.write_readiness_metadata(plan_dir, metadata)
    return metadata


_FIX_CATEGORIES = {
    "plan-recreate",
    "filename-rename",
    "manifest-update",
    "plan-edit",
    "invocation-fix",
    "env-install",
    "env-state",
    "session-recipe-edit",
    "informational",
}


def _structured_review_finding(
    *,
    finding_id: str = "F1",
    severity: str = "high",
    **overrides: object,
) -> dict[str, Any]:
    """Return a schema-complete structured review finding for tests."""
    finding: dict[str, Any] = {
        "id": finding_id,
        "severity": severity,
        "title": f"Finding {finding_id} title",
        "evidence": [{"path": "agent_session_loop/runner.py", "symbol": "run_loop"}],
        "explanation": f"Explanation for {finding_id}.",
        "repair_group": "group-1",
        "done_criteria": ["The issue is fixed and covered by a test."],
        "verification_checks": [
            {
                "type": "command",
                "name": f"verify-{finding_id}",
                "argv": ["python3", "-c", "pass"],
            }
        ],
    }
    finding.update(overrides)
    return finding


def write_completed_review_plan(
    plan_dir: Path,
    findings: list[dict[str, Any]],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> Path:
    """Write a two-session plan ending in one completed generated review."""
    write_plan_with_ids(
        plan_dir,
        (("1", "complete"), ("2", "complete")),
        contracts_by_id=contracts_by_id,
    )
    review_body = review_session_body.build_generated_review_session_body(
        plans.parse_session_id("2"),
        review_harness="codex",
        plan_dir=plan_dir,
    ).replace("- **Status**: pending", "- **Status**: complete")
    (plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
    if findings:
        high = sum(1 for finding in findings if finding["severity"] == "high")
        med = sum(1 for finding in findings if finding["severity"] == "med")
        low = sum(1 for finding in findings if finding["severity"] == "low")
        status_line = (
            f"Status: {len(findings)} Findings ({high} high, {med} med, {low} low)"
        )
        outcome = "findings"
    else:
        status_line = review.REVIEW_STATUS_ALL_CLEAR
        outcome = "all-clear"
    (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
        f"# Final review\n\n{status_line}\n", encoding="utf-8"
    )
    (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
        json.dumps({"version": 2, "outcome": outcome, "findings": findings}),
        encoding="utf-8",
    )
    return plans.canonicalize_path(plan_dir)


def write_review_cycle_event(
    plan_dir: Path,
    state_root: Path,
    payload: dict[str, Any],
) -> Path:
    """Append one review-cycle fixture event and return its event path."""
    event_path = state.plan_state_dir(plan_dir, state_root) / events.EVENTS_FILE_NAME
    events.append_event(event_path, events.Event(events.REVIEW_CYCLE_EVENT, payload))
    return event_path


def _write_plan_dir(plan_dir: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    return plan_dir


def _seed_archivable_plan(
    tmp_path: Path,
    *,
    with_pin: bool = True,
    with_state: bool = True,
    create_plan: bool = True,
) -> tuple[Path, Path, Path]:
    """Seed a plan (optionally pinned, with ASL state) ready to archive.

    Returns ``(state_dir, registry_path, canonical_plan_dir)``.
    """
    state_dir = tmp_path / "state"
    registry_path = tmp_path / workbench.REGISTRY_FILE_NAME
    plan_dir = tmp_path / "plans" / "alpha"
    if create_plan:
        _write_plan_dir(plan_dir)
    canonical = plans.canonicalize_path(plan_dir)
    if with_pin:
        seeded = workbench.upsert_pin(
            workbench.load_registry(registry_path),
            canonical,
            label="Alpha",
            profile=workbench.LaunchProfile(harness="codex"),
        )
        workbench.save_registry(seeded, registry_path)
    if with_state:
        plan_state = state.plan_state_dir(canonical, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        (plan_state / "state.json").write_text(
            json.dumps({"plan_dir": str(canonical)}), encoding="utf-8"
        )
    return state_dir, registry_path, canonical


_WORKBENCH_DEAD_PID = 4_242_424


def _make_workbench_plan(tmp_path: Path, name: str = "plan-alpha") -> Path:
    plan_dir = tmp_path / name
    write_plan(plan_dir, ("pending", "pending"))
    return plans.canonicalize_path(plan_dir)


def _write_state_payload(
    plan_dir: Path,
    state_root: Path,
    payload: dict[str, Any],
) -> Path:
    plan_state = state.plan_state_dir(plan_dir, state_root)
    plan_state.mkdir(parents=True, exist_ok=True)
    state_path = plan_state / events.STATE_FILE_NAME
    state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return state_path


def _write_raw_state_payload(
    plan_dir: Path,
    state_root: Path,
    payload: str,
) -> Path:
    plan_state = state.plan_state_dir(plan_dir, state_root)
    plan_state.mkdir(parents=True, exist_ok=True)
    state_path = plan_state / events.STATE_FILE_NAME
    state_path.write_text(payload, encoding="utf-8")
    return state_path


def _write_lock_file(
    plan_dir: Path,
    state_root: Path,
    *,
    pid: int,
    hostname: str | None = None,
    process_start: str | None = None,
    wakeup_endpoint: Path | str | None = None,
    redirect_endpoint: Path | str | None = None,
) -> Path:
    """Write a lock naming ``pid``, legacy-shaped unless a field is given.

    The default omits every optional field, which is what makes the lock
    unwakeable: with no ``wakeup_endpoint`` a producer has nothing to write to
    and reports ``no-endpoint``. Tests that need a lock a producer really wakes
    pass an endpoint served by :func:`live_wakeup_endpoint`.

    ``redirect_endpoint`` is the same for the urgent redirect channel, and its
    absence is what makes a lock refuse redirects rather than merely delay
    them; :func:`live_redirect_endpoint` serves one for tests that need
    delivery to land.
    """
    plan_state = state.plan_state_dir(plan_dir, state_root)
    plan_state.mkdir(parents=True, exist_ok=True)
    lock_path = plan_state / state.LOCK_FILE_NAME
    payload: dict[str, Any] = {
        "pid": pid,
        "plan_dir": str(plan_dir),
        "created_at": "2026-06-19T00:00:00+00:00",
    }
    if hostname is not None:
        payload["hostname"] = hostname
    if process_start is not None:
        payload["process_start"] = process_start
    if wakeup_endpoint is not None:
        payload["wakeup_endpoint"] = str(wakeup_endpoint)
    if redirect_endpoint is not None:
        payload["redirect_endpoint"] = str(redirect_endpoint)
    lock_path.write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    return lock_path


def _write_live_lock_file(
    plan_dir: Path,
    state_root: Path,
    *,
    wakeup_endpoint: Path | str | None = None,
) -> Path:
    """Write a lock describing this process as the live owner.

    Records the current host and this pid's real birth time. Those fields are
    what the staleness check reads; delivery itself needs ``wakeup_endpoint``,
    so a caller that wants a producer's wakeup to land passes one from
    :func:`live_wakeup_endpoint`.
    """
    return _write_lock_file(
        plan_dir,
        state_root,
        pid=os.getpid(),
        hostname=socket.gethostname(),
        process_start=process_tree.process_birth(os.getpid()),
        wakeup_endpoint=wakeup_endpoint,
    )


@contextmanager
def live_wakeup_endpoint(plan_dir: Path, state_root: Path):
    """Serve a plan's wakeup FIFO from this process for the block's duration.

    Yields the endpoint path to record in a lock and the event a delivered
    wakeup sets. This is the production reader, so a test that writes to the
    yielded path exercises exactly what a runner would receive.
    """
    event = threading.Event()
    with control_signal.open_wakeup_endpoint(
        state.plan_state_dir(plan_dir, state_root), event
    ) as path:
        if path is None:
            raise unittest.SkipTest("this filesystem cannot host a wakeup FIFO")
        yield path, event


@contextmanager
def live_redirect_endpoint(plan_dir: Path, state_root: Path):
    """Serve a plan's urgent redirect FIFO from this process for the block.

    The redirect counterpart of :func:`live_wakeup_endpoint`, and the reason a
    test can distinguish "the run is live" from "the run serves redirects": a
    lock may name one endpoint, both, or neither.
    """
    event = threading.Event()
    with control_signal.open_redirect_endpoint(
        state.plan_state_dir(plan_dir, state_root), event
    ) as path:
        if path is None:
            raise unittest.SkipTest("this filesystem cannot host a redirect FIFO")
        yield path, event


def _discovered(plan_dir: Path, **overrides) -> workbench.DiscoveredPlan:
    kwargs = {
        "plan_dir": plan_dir,
        "sources": ("state",),
        "label": None,
        "profile": None,
        "exists": True,
    }
    kwargs.update(overrides)
    return workbench.DiscoveredPlan(**kwargs)


class _FakePopen:
    """Minimal Popen stand-in that records argv/cwd/env and captures stdin."""

    def __init__(
        self,
        argv,
        *,
        stdin=None,
        stdout=None,
        stderr=None,
        env=None,
        cwd=None,
        text=False,
        start_new_session=False,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.text = text
        # Recorded, not ignored: whether the child gets its own session is the
        # difference between a run that survives its launcher's shutdown and
        # one the launcher's signal takes down with it.
        self.start_new_session = start_new_session
        self.pid = 4242
        self.returncode: int | None = None
        self._stdin_buffer = io.StringIO() if text else io.BytesIO()

        class _StdinProxy:
            def __init__(self, buf, owner):
                self._buf = buf
                self._owner = owner
                self.closed = False

            def write(self, data):
                self._buf.write(data)

            def close(self):
                self.closed = True
                self._owner.stdin_text = self._buf.getvalue()

        if stdin == subprocess.PIPE:
            self.stdin = _StdinProxy(self._stdin_buffer, self)
        else:
            self.stdin = None

        self.stdin_text: str | bytes = "" if text else b""

    def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - only used on error paths
        if self.returncode is None:
            self.returncode = -9


class _BrokenStdinFakePopen(_FakePopen):
    """Variant whose stdin raises BrokenPipeError on the first write."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        class _BrokenStdin:
            closed = False

            def write(self, _data):
                raise BrokenPipeError("simulated broken pipe")

            def close(self):
                self.closed = True

        if self.stdin is not None:
            self.stdin = _BrokenStdin()


def _http_request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict, dict]:
    """Make an HTTP request against the Workbench server.

    Returns ``(status, parsed_json, response_headers)``. JSON-decode errors
    bubble up as test failures via raising.
    """
    import urllib.error
    import urllib.request

    data = None
    request_headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        data = payload
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(payload))
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
        response_headers = dict(error.headers.items())
    text = raw.decode("utf-8") if raw else ""
    parsed = json.loads(text) if text else {}
    return status, parsed, response_headers


def _http_request_raw(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes, dict]:
    """Make an HTTP request and return ``(status, body_bytes, headers)``.

    Mirrors :func:`_http_request` but does not attempt to JSON-decode the
    response body. Use for static-asset tests where the response is HTML,
    CSS, or JS rather than JSON.
    """
    import urllib.error
    import urllib.request

    request_headers = {"Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read()
        response_headers = dict(error.headers.items())
    return status, raw, response_headers


def _load_script_module(module_name: str, *relative_parts: str):
    script_path = REPO_ROOT.joinpath(*relative_parts)
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_prepare_plan(plan_dir: Path, session_ids: tuple[str, ...] = ("1",)) -> None:
    """Write a minimal canonical plan for prepare/expansion tests."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    for session_id in session_ids:
        body = "\n".join(
            [
                f"# {plans.session_id_display_label(session_id)}: Test",
                "",
                authoring.CONTEXT_REFERENCE_LINE,
                "",
                "---",
                "",
                "Update the retry helper in `foo/bar.py` to use `load_config`.",
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- The helper in `foo/bar.py` passes tests.",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": session_id,
                        "checks": [{"name": "c1", "type": "command", "argv": ["true"]}],
                    }
                ),
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Files modified**: (none yet)",
                "- **Key decisions**: (none yet)",
                "- **Blockers for next session**: (none)",
                "- **Notes**: remember `foo/bar.py` cleanup",
                "",
            ]
        )
        (plan_dir / plans.session_id_to_filename(session_id)).write_text(
            body, encoding="utf-8"
        )


def _validator_payload(**session_overrides: object) -> dict:
    """A minimal valid validator payload for one session, with overrides."""
    session: dict[str, Any] = {
        "session_id": "1",
        "title": "Test",
        "claims": [
            {
                "claim": "load_config",
                "claim_type": "S",
                "location": "foo/config.py",
                "line": 12,
                "to_be_created": False,
            }
        ],
        "issues": [],
        "details": [],
        "patterns": [],
        "dependencies": [],
        "contract_checked": True,
        "contract_problems": [],
        "corrections": [],
    }
    session.update(session_overrides)
    return {
        "version": prepare_models.LEGACY_PREPARE_VALIDATION_VERSION,
        "codebase_root": "/repo",
        "sessions": [session],
    }


def _write_prepare_source_plan(
    path: Path,
    *,
    session_count: int = 2,
    omit_contract_for: tuple[str, ...] = (),
    check_json_by_id: dict[str, dict] | None = None,
    body_by_id: dict[str, str] | None = None,
    heading_by_id: dict[str, str] | None = None,
) -> Path:
    """Write a source plan file with explicit sessions and inline contracts."""
    lines = ["# Prepare Demo Plan", "", "Shared context for the demo plan.", ""]
    for number in range(1, session_count + 1):
        session_id = str(number)
        heading = (heading_by_id or {}).get(
            session_id, f"## Session {session_id}: Step {session_id}"
        )
        lines.extend([heading, ""])
        body = (body_by_id or {}).get(
            session_id, f"Implement step {session_id} in `demo/module.py`."
        )
        lines.extend([body, ""])
        if session_id in omit_contract_for:
            continue
        check = (check_json_by_id or {}).get(
            session_id,
            {
                "name": f"check-{session_id}",
                "type": "command",
                "argv": ["python3", "-c", "raise SystemExit(0)"],
                "timeout_seconds": 30,
            },
        )
        lines.extend(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                f"- Step {session_id} outcome holds.",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps({"session": session_id, "checks": [check]}),
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _prepare_pipeline_payload(
    codebase_root: str,
    session_ids: tuple[str, ...],
    *,
    corrections_by_id: dict[str, list[dict]] | None = None,
    version: int = prepare_models.LEGACY_PREPARE_VALIDATION_VERSION,
    repositories: tuple[prepare_models.ValidatorRepository, ...] = (),
    session_repositories: tuple[
        tuple[prepare_models.ValidatorRepository, ...], ...
    ] = (),
) -> dict:
    """A schema-valid validator payload covering the given sessions."""
    sessions = []
    for index, session_id in enumerate(session_ids):
        repo = (
            session_repositories[index][0].alias
            if version == prepare_models.PREPARE_VALIDATION_VERSION
            else None
        )
        claim = {
            "claim": "demo/module.py",
            "claim_type": "P",
            "location": "demo/module.py",
            "line": 1,
            "to_be_created": False,
        }
        corrections = [
            dict(correction)
            for correction in (corrections_by_id or {}).get(session_id, [])
        ]
        if repo is not None:
            claim["repo"] = repo
            for correction in corrections:
                correction["repo"] = repo
        sessions.append(
            {
                "session_id": session_id,
                "title": f"Step {session_id}",
                "claims": [claim],
                "issues": [],
                "details": [],
                "patterns": [],
                "dependencies": [],
                "contract_checked": True,
                "contract_problems": [],
                "corrections": corrections,
            }
        )
    payload = {"version": version, "sessions": sessions}
    if version == prepare_models.PREPARE_VALIDATION_VERSION:
        payload["workspace_root"] = codebase_root
        payload["repositories"] = [repo.to_dict() for repo in repositories]
    else:
        payload["codebase_root"] = codebase_root
    return payload


def _recording_validator(
    calls: list,
    *,
    corrections_by_id: dict[str, list[dict]] | None = None,
    payload_override: object | None = None,
):
    """A fake validator that records packets and returns valid payloads."""

    def _validator(packet: prepare_packet.ValidationTaskPacket) -> object:
        calls.append(packet)
        if payload_override is not None:
            return payload_override
        return _prepare_pipeline_payload(
            packet.codebase_root,
            packet.session_ids,
            corrections_by_id=corrections_by_id,
            version=packet.version,
            repositories=packet.repositories,
            session_repositories=packet.session_read_repositories,
        )

    return _validator
