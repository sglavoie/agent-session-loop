"""Shared fixtures and helpers for the agent_session_loop test suite.

Split out of tests/test_agent_session_loop.py; imported by the
tests/test_asl_*.py subsystem modules.
"""

from __future__ import annotations

import argparse
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
    authoring_render,
    config,
    control_signal,
    events,
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    plans_session_ids,
    prepare_models,
    prepare_packet,
    process_tree,
    review,
    review_session_body,
    run_control,
    state,
    workbench_registry,
    workbench_registry_models,
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


def isolated_home(case: unittest.TestCase) -> Path:
    """Point ``HOME`` at a fresh temporary directory for the rest of ``case``.

    ``Path.home()`` and ``~`` expansion both read ``HOME``, so a test that
    reasons about the home directory gets one it owns and never touches the
    developer's real one.
    """
    home = Path(tempfile.mkdtemp(prefix="asl-test-home-"))
    case.addCleanup(shutil.rmtree, home, ignore_errors=True)
    patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
    patcher.start()
    case.addCleanup(patcher.stop)
    return home


def load_section_tests(
    loader: unittest.TestLoader, *modules: ModuleType | str
) -> unittest.TestSuite:
    """The test cases each of ``modules`` defines itself, in class-name order.

    The ``load_tests`` body every focused section and its facade share. A
    section star-imports its shared fixtures and a facade copies every
    section's names into each of them, so a module's namespace holds test
    classes it does not own; only the ones whose ``__module__`` is the module
    itself are loaded, so each test runs under the section that defines it.
    """
    suite = unittest.TestSuite()
    for module in modules:
        namespace = sys.modules[module] if isinstance(module, str) else module
        cases = sorted(
            (
                value
                for value in vars(namespace).values()
                if isinstance(value, type)
                and issubclass(value, unittest.TestCase)
                and value.__module__ == namespace.__name__
            ),
            key=lambda case: case.__name__,
        )
        for case in cases:
            suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def force_pid_alive(pid: int) -> bool:
    """Treat only the current process as alive so dead PIDs are deterministic."""
    return pid == os.getpid()


def parse_cli_args(tokens: list[str]) -> argparse.Namespace:
    """Parse ``tokens`` with the real ``asl`` argument parser."""
    from agent_session_loop import cli

    return cli.build_parser().parse_args(tokens)


def call_receivers(source: str, *attributes: str) -> set[str]:
    """Every receiver of a call to one of ``attributes``, as source text."""
    receivers: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in attributes:
            continue
        receivers.add(ast.unparse(node.func.value))
    return receivers


def durable_write_receivers(source: str) -> set[str]:
    """Every receiver of a ``write_state`` or ``append_event`` call, as source."""
    return call_receivers(source, "write_state", "append_event")


def stderr_owners(source: str) -> set[str]:
    """The top-level definitions in ``source`` that name ``sys.stderr``."""
    owners: set[str] = set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "stderr"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "sys"
            ):
                owners.add(node.name)
                break
    return owners


class RaisingStream(io.StringIO):
    """A sink that behaves like a pipe whose reader has gone away."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    def write(self, text: str) -> int:
        raise self._error

    def flush(self) -> None:
        raise self._error


class ManualClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self._lock = threading.Lock()
        self._value = start

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, delta: float) -> None:
        with self._lock:
            self._value += delta
