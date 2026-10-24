"""Privacy-safe lifecycle tracing for real ASL CLI processes.

A trace record carries only vocabulary the parser already owns — subcommand
names and option names — plus process facts (pid, timestamps, exit code). Raw
argv, option values, positional values, unknown tokens, paths, environment
values, message bodies, captured output, and exception messages are never
assembled into a record, so the trace cannot leak content even if the state
root is shared or archived.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config, harnesses

SCHEMA_VERSION = 1

TRACE_ENV_VAR = "ASL_CLI_TRACE"
DISABLED_VALUES = frozenset({"0", "false", "no", "off"})

ORIGIN_ENV_VAR = "ASL_INVOCATION_ORIGIN"
ORIGIN_DIRECT = "direct"
ORIGIN_AUTOPILOT = "autopilot"
ORIGIN_WORKBENCH = "workbench"
ORIGIN_INSTALL_VERIFY = "install-verify"
ORIGIN_OTHER = "other"
ORIGIN_NAMES: tuple[str, ...] = (
    ORIGIN_DIRECT,
    ORIGIN_AUTOPILOT,
    ORIGIN_WORKBENCH,
    ORIGIN_INSTALL_VERIFY,
)

EVENT_START = "start"
EVENT_END = "end"

OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_HELP = "help"
OUTCOME_USAGE = "usage"
OUTCOME_INTERRUPTED = "interrupted"
OUTCOME_EXCEPTION = "exception"

HELP_ROOT = "root"
HELP_GROUP = "group"
HELP_LEAF = "leaf"

FILE_PREFIX = "cli-invocations-"
FILE_SUFFIX = ".jsonl"
LOCK_FILE_NAME = ".cli-invocations.lock"
RETENTION_DAYS = 30


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return whether this process should write invocation traces."""
    source = os.environ if env is None else env
    return source.get(TRACE_ENV_VAR, "").strip().lower() not in DISABLED_VALUES


def origin(env: Mapping[str, str] | None = None) -> str:
    """Return the allowlisted invocation origin, mapping anything else to other."""
    source = os.environ if env is None else env
    raw = source.get(ORIGIN_ENV_VAR)
    if raw is None or raw == "":
        return ORIGIN_DIRECT
    return raw if raw in ORIGIN_NAMES else ORIGIN_OTHER


def child_env(base: Mapping[str, str], origin_name: str) -> dict[str, str]:
    """Return ``base`` with the launcher's own origin marker applied.

    The marker is applied last: a launcher attributing its own children may not
    be overridden by an inherited or caller-supplied value, or every child of a
    traced parent would inherit the parent's origin.
    """
    env = dict(base)
    env[ORIGIN_ENV_VAR] = origin_name
    return env


def normalize_exit_code(code: object) -> int:
    """Return the integer status a ``SystemExit`` code produces at the shell."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    return 1


def outcome_for_exit_code(code: int) -> str:
    return OUTCOME_OK if code == 0 else OUTCOME_FAILED


# ---------------------------------------------------------------------------
# Parser-derived classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandShape:
    """The recognized, content-free shape of one command line."""

    command_path: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    help_depth: str | None = None


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _value_token_count(action: argparse.Action, token: str) -> int:
    if "=" in token or action.nargs == 0:
        return 0
    if isinstance(action.nargs, int):
        return action.nargs
    return 1


def classify(parser: argparse.ArgumentParser, argv: Sequence[str]) -> CommandShape:
    """Reduce ``argv`` to the subcommand and option names ``parser`` recognizes.

    Option values are counted and skipped rather than inspected, so a value that
    happens to spell a subcommand name cannot be promoted into the command path.
    """
    command_path: list[str] = []
    options: list[str] = []
    help_requested = False
    current = parser
    skip = 0
    for token in argv:
        if skip:
            skip -= 1
            continue
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            action = current._option_string_actions.get(name)
            if action is None:
                continue
            options.append(name)
            if isinstance(action, argparse._HelpAction):
                help_requested = True
            skip = _value_token_count(action, token)
            continue
        subparsers = _subparsers_action(current)
        if subparsers is not None and token in subparsers.choices:
            command_path.append(token)
            current = subparsers.choices[token]

    help_depth = None
    if help_requested:
        if not command_path:
            help_depth = HELP_ROOT
        elif _subparsers_action(current) is not None:
            help_depth = HELP_GROUP
        else:
            help_depth = HELP_LEAF

    return CommandShape(
        command_path=tuple(command_path),
        options=tuple(sorted(set(options))),
        help_depth=help_depth,
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def trace_root(state_dir: str | Path | None = None) -> Path:
    """Return the state root a trace for ``state_dir`` belongs under."""
    if state_dir is None:
        return config.default_state_root()
    from . import state

    return state.state_root(state_dir)


def trace_file_name(day: date) -> str:
    return f"{FILE_PREFIX}{day.isoformat()}{FILE_SUFFIX}"


def _file_day(name: str) -> date | None:
    stamp = name[len(FILE_PREFIX) : -len(FILE_SUFFIX)]
    try:
        return date.fromisoformat(stamp)
    except ValueError:
        return None


@contextmanager
def _trace_lock(root: Path) -> Iterator[None]:
    fd = os.open(root / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _append(path: Path, blob: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, blob)
    finally:
        os.close(fd)


def _prune(root: Path, today: date) -> None:
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for path in root.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"):
        day = _file_day(path.name)
        if day is not None and day < cutoff:
            with suppress(OSError):
                path.unlink()


def _write(root: Path, record: dict[str, Any], *, prune: bool = False) -> None:
    # A trace is observability, never a precondition: a read-only state root, a
    # full disk, or a lock we cannot take degrades to no record rather than to a
    # failed command.
    with suppress(Exception):
        root.mkdir(parents=True, exist_ok=True)
        blob = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        today = datetime.now(UTC).date()
        with _trace_lock(root):
            _append(root / trace_file_name(today), blob + b"\n")
            if prune:
                _prune(root, today)


@dataclass(frozen=True)
class TraceCorpus:
    """Well-formed trace records plus the count of lines that were not usable.

    ``malformed_lines`` covers every non-blank line that did not parse as a JSON
    object, so a consumer can report trace corruption instead of silently
    summarizing a smaller corpus.
    """

    records: list[dict[str, Any]]
    malformed_lines: int


def read_corpus(root: str | Path) -> TraceCorpus:
    """Return the trace corpus under ``root``, oldest file first.

    Reading is tolerant and read-only: an unreadable file is skipped whole, and a
    malformed or non-object line is counted rather than returned, so a truncated
    or hand-edited trace file degrades the sample rather than failing the reader.
    """
    records: list[dict[str, Any]] = []
    malformed_lines = 0
    for path in sorted(Path(root).glob(f"{FILE_PREFIX}*{FILE_SUFFIX}")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                malformed_lines += 1
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                malformed_lines += 1
    return TraceCorpus(records=records, malformed_lines=malformed_lines)


# ---------------------------------------------------------------------------
# Invocation lifecycle
# ---------------------------------------------------------------------------


_current_invocation_id: str | None = None


def current_invocation_id() -> str | None:
    """Return this process's own invocation id, or None when untraced.

    A command that reads the trace corpus (``asl metrics``) has already written
    its own start record by the time its handler runs, so it needs to recognize
    and drop that record rather than report itself as an unmatched start.
    """
    return _current_invocation_id


@dataclass(frozen=True)
class Invocation:
    """Handle for one traced process; ``root`` is None when tracing is off."""

    root: Path | None = None
    invocation_id: str = ""
    started_at: float = 0.0
    base: dict[str, Any] = field(default_factory=dict)

    def finish(
        self,
        *,
        exit_code: int | None,
        outcome: str,
        exception: BaseException | None = None,
    ) -> None:
        if self.root is None:
            return
        record = dict(self.base)
        record.update(
            {
                "event": EVENT_END,
                "timestamp": datetime.now(UTC).isoformat(),
                "exit_code": exit_code,
                "outcome": outcome,
                "duration_ms": round((time.monotonic() - self.started_at) * 1000, 3),
                "exception_type": (
                    None if exception is None else type(exception).__name__
                ),
            }
        )
        _write(self.root, record, prune=True)


def start(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    *,
    state_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Invocation:
    """Record a start event and return the handle that closes the pair."""
    global _current_invocation_id
    try:
        source = os.environ if env is None else env
        if not enabled(source):
            return Invocation()
        shape = classify(parser, argv)
        base: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "invocation_id": uuid.uuid4().hex,
            "pid": os.getpid(),
            "command_path": list(shape.command_path),
            "options": list(shape.options),
            "help_depth": shape.help_depth,
            "harness": harnesses.detect_invoking_harness(source),
            "origin": origin(source),
        }
        root = trace_root(state_dir)
    except Exception:  # noqa: BLE001
        # Same fail-open contract as `_write`: never let tracing setup decide
        # whether a command runs.
        return Invocation()
    started_at = time.monotonic()
    _current_invocation_id = base["invocation_id"]
    _write(
        root,
        {**base, "event": EVENT_START, "timestamp": datetime.now(UTC).isoformat()},
    )
    return Invocation(
        root=root,
        invocation_id=base["invocation_id"],
        started_at=started_at,
        base=base,
    )


def record_parser_exit(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    code: object,
    *,
    env: Mapping[str, str] | None = None,
) -> None:
    """Emit a paired start/end record for a help or usage exit during parsing.

    Parsing terminated before a namespace existed, so there is no effective
    ``--state-dir`` to honor and the pair lands under the default state root.
    """
    exit_code = normalize_exit_code(code)
    invocation = start(parser, argv, env=env)
    invocation.finish(
        exit_code=exit_code,
        outcome=OUTCOME_HELP if exit_code == 0 else OUTCOME_USAGE,
    )
