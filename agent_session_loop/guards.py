"""Run-scoped command guards: rules, active state, and the command gate.

A guard is an operator's standing refusal, attached to one live run: "while
this run lasts, do not let the worker run ``git push``". It is enforced by the
process boundary rather than by the worker's cooperation, which is the whole
point — guidance a worker never reads changes nothing, and a guard has to hold
anyway.

The mechanism has four moving parts and they are deliberately small:

- **A rule** is a POSIX-tokenized argv prefix (:func:`parse_command_rule`).
  The first token names a bare executable; the rest must match the leading
  arguments exactly and case-sensitively. A one-token rule blocks every
  invocation of that executable.
- **A policy** is the derived file the runtime shims read
  (:func:`write_policy`). It is the single source of truth for what is
  currently denied, and it is replaced atomically, so a shim never reads a
  half-written rule set.
- **A shim** is a tiny executable installed into the run's ``guard-bin``
  directory under the guarded executable's name
  (:func:`install_shim`). ``guard-bin`` is prepended to every worker turn's
  ``PATH``, so a bare-name resolution finds it before the real binary.
- **A controller** (:class:`GuardController`) owns the active set for one run
  and is the only thing that writes either file.

Expiration is the absence of the policy, not the absence of the shims: run
termination deletes the policy and every shim left on disk becomes an inert
delegator. That is safe because ``guard-bin`` lives under the per-run state
directory and no later worker ever has it on ``PATH``.

The guarantee is operational, not adversarial. :data:`COMMAND_GATE_COVERAGE`
states what the gate does and does not reach, in the same words status
reports it, so nobody has to infer the boundary from the implementation.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shlex
import shutil
import stat
import sys
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artifacts, clock, command_guard

#: Directory, under the per-run state directory, that shadows ``PATH``.
GUARD_BIN_DIR_NAME = "guard-bin"

#: The derived policy every shim reads, beside the guard-bin it shadows with.
GUARD_POLICY_FILE_NAME = "guard-policy.json"

#: Schema version of the policy file. Shared with the dispatcher so the two
#: cannot drift: a shim that does not recognize the version enforces nothing.
POLICY_VERSION = command_guard.POLICY_VERSION

#: Exit code a denied command returns, restated here so callers and tests can
#: reach it without importing the runtime dispatcher.
DENIED_EXIT_CODE = command_guard.DENIED_EXIT_CODE

# --------------------------------------------------------------------------
# Rejection reasons
#
# Stable strings shared by the queue, the runner's `guard-rejected` event, and
# the CLI's error payloads, so a client branches on one vocabulary.
# --------------------------------------------------------------------------

REJECT_EMPTY_COMMAND_RULE = "empty-command-rule"
REJECT_UNPARSABLE_COMMAND_RULE = "unparsable-command-rule"
REJECT_EMPTY_TOKEN = "empty-token"
REJECT_EXECUTABLE_NOT_BARE_NAME = "executable-not-bare-name"
REJECT_SHELL_OPERATOR = "shell-operator-in-rule"
REJECT_GLOB = "glob-in-rule"
REJECT_ENVIRONMENT_ASSIGNMENT = "environment-assignment-in-rule"
REJECT_NO_ACTIVE_RUN = "no-active-run"
REJECT_RUN_CHANGED = "run-changed"
REJECT_UNKNOWN_GUARD = "unknown-guard"
REJECT_EXECUTABLE_NOT_FOUND = "executable-not-found"
REJECT_ACTIVATION_FAILED = "activation-failed"

#: Characters that only mean something to a shell. A rule is an argv prefix,
#: never a shell fragment, so one of these inside a token means the operator
#: wrote a command line and expected it to be interpreted.
_SHELL_OPERATOR_CHARS = frozenset("|&;<>()$`\n\r\t")

#: Glob metacharacters. The gate matches literal argv, so a pattern here would
#: read as protection that silently never fires.
_GLOB_CHARS = frozenset("*?[]")


#: What the command gate reaches, and what it does not. Reported verbatim by
#: `asl status` so the boundary is an operational fact rather than folklore.
COMMAND_GATE_COVERAGE: dict[str, Any] = {
    "mechanism": "per-run PATH shim",
    "matching": "POSIX tokenization, case-sensitive exact argv prefix",
    "denied_exit_code": DENIED_EXIT_CODE,
    "covers": (
        "a bare executable name run directly by the harness",
        "a bare name run through `env NAME=VALUE cmd ...`",
        "a bare name run through `sh -c` or any other shell",
        "a bare name a child process resolves through PATH, including "
        "Python's subprocess",
    ),
    "does_not_cover": (
        "an absolute or relative path to the real executable",
        "an equivalent library or network call that never spawns the command",
        "a process that clears or rewrites PATH before spawning",
        "a command that was already executing when the guard was applied",
    ),
    "guarantee": "operational safeguard, not a sandbox against hostile code",
}


class GuardRuleError(ValueError):
    """A command rule that cannot be turned into an argv prefix."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class GuardRule:
    """One active guard: an argv prefix, its identity, and why it exists."""

    id: str
    command: tuple[str, ...]
    reason: str | None
    created_at: str

    @property
    def executable(self) -> str:
        return self.command[0]

    @property
    def rendered_command(self) -> str:
        return " ".join(self.command)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "command": list(self.command),
            "rendered_command": self.rendered_command,
            "executable": self.executable,
            "reason": self.reason,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class GuardOutcome:
    """The verdict on one queued guard mutation.

    ``applied`` and ``removed`` are the two successes; ``rejected`` carries a
    stable ``reason``. Exactly one of these is what a ``--wait`` client is
    waiting for, which is why a failed activation must produce one too rather
    than leaving the wait to time out.
    """

    action: str
    guard_ids: tuple[str, ...] = ()
    reason: str | None = None
    detail: str | None = None
    guards: tuple[GuardRule, ...] = ()

    @property
    def ok(self) -> bool:
        return self.action in (ACTION_APPLIED, ACTION_REMOVED)

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "guard_ids": list(self.guard_ids),
            "reason": self.reason,
            "detail": self.detail,
            "guards": [guard.to_payload() for guard in self.guards],
        }


ACTION_APPLIED = "applied"
ACTION_REMOVED = "removed"
ACTION_REJECTED = "rejected"


def new_guard_id() -> str:
    """Mint an identifier short enough to type and wide enough not to collide."""
    return f"g-{uuid.uuid4().hex[:12]}"


def parse_command_rule(raw: str) -> tuple[str, ...]:
    """Turn one operator-written rule into the argv prefix it must match.

    POSIX tokenization is what makes ``--deny-command "git push"`` and
    ``--deny-command 'git push origin main'`` mean the obvious thing while
    quoting still works for an argument that contains a space. Everything a
    shell would *interpret* rather than pass through is refused outright: a
    rule is compared against literal argv, so a pattern or an operator here
    would be protection that silently never matches.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GuardRuleError(
            REJECT_EMPTY_COMMAND_RULE,
            "a command rule must contain at least one token",
        )
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as error:
        raise GuardRuleError(
            REJECT_UNPARSABLE_COMMAND_RULE,
            f"command rule is not POSIX-tokenizable: {error}",
        ) from error
    if not tokens:
        raise GuardRuleError(
            REJECT_EMPTY_COMMAND_RULE,
            "a command rule must contain at least one token",
        )
    for token in tokens:
        if not token:
            raise GuardRuleError(
                REJECT_EMPTY_TOKEN,
                "a command rule may not contain an empty token",
            )
        if _SHELL_OPERATOR_CHARS & set(token):
            raise GuardRuleError(
                REJECT_SHELL_OPERATOR,
                f"command rule token {token!r} contains a shell operator; a "
                "rule is an argv prefix, not a shell command line",
            )
        if _GLOB_CHARS & set(token):
            raise GuardRuleError(
                REJECT_GLOB,
                f"command rule token {token!r} contains a glob; rules match "
                "literal arguments only",
            )
    executable = tokens[0]
    if "=" in executable:
        raise GuardRuleError(
            REJECT_ENVIRONMENT_ASSIGNMENT,
            f"command rule starts with the environment assignment "
            f"{executable!r}; name the executable the assignment precedes",
        )
    if (
        "/" in executable
        or os.sep in executable
        or (os.altsep is not None and os.altsep in executable)
        or executable in (os.curdir, os.pardir)
    ):
        raise GuardRuleError(
            REJECT_EXECUTABLE_NOT_BARE_NAME,
            f"command rule must start with a bare executable name, not "
            f"{executable!r}; the gate matches how PATH resolves a name and "
            "cannot see a path that bypasses it",
        )
    return tuple(tokens)


def guard_bin_dir(run_dir: str | Path) -> Path:
    """The per-run directory prepended to a worker turn's ``PATH``."""
    return Path(run_dir) / GUARD_BIN_DIR_NAME


def policy_path(run_dir: str | Path) -> Path:
    """The derived policy every shim for this run reads."""
    return Path(run_dir) / GUARD_POLICY_FILE_NAME


def resolve_original(
    name: str,
    *,
    guard_bin: Path,
    path_value: str | None = None,
) -> str | None:
    """Resolve ``name`` the way the worker would, minus the guard-bin overlay.

    Done once, at activation, and recorded in the policy: a shim that resolved
    its own target at dispatch time would have to reason about a ``PATH`` that
    already contains itself. Filtering guard-bin here is what makes "no
    recursive executable resolution" a property of the design rather than of
    the shim's luck.
    """
    entries = (
        os.environ.get("PATH", os.defpath) if path_value is None else path_value
    ).split(os.pathsep)
    guard_real = os.path.realpath(guard_bin)
    filtered = [
        entry for entry in entries if entry and os.path.realpath(entry) != guard_real
    ]
    if not filtered:
        return None
    return shutil.which(name, path=os.pathsep.join(filtered))


def policy_document(
    *,
    run_id: str,
    guard_bin: Path,
    guards: Sequence[GuardRule],
    executables: Mapping[str, str],
) -> dict[str, Any]:
    """The exact JSON a shim reads, derived from the active set."""
    return {
        "version": POLICY_VERSION,
        "run_id": run_id,
        "guard_bin": str(guard_bin),
        "guards": [
            {
                "id": guard.id,
                "command": list(guard.command),
                "reason": guard.reason,
            }
            for guard in guards
        ],
        "executables": dict(executables),
    }


def write_policy(path: Path, document: Mapping[str, Any]) -> None:
    """Replace the policy atomically, so no shim ever reads a partial rule set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(dict(document), sort_keys=True, indent=2) + "\n"
    artifacts.atomic_write_bytes(path, encoded.encode("utf-8"))


def shim_source(*, module_path: Path, policy_file: Path, name: str) -> str:
    """The text of one installed shim.

    It loads :mod:`agent_session_loop.command_guard` *by path* rather than by
    package name. A worker's ``PATH`` says nothing about its ``sys.path``, and
    a guard that only holds when the package happens to be importable is not a
    guard. The interpreter is pinned to the one running the runner for the
    same reason: the shim must not depend on what ``python3`` resolves to
    inside the worker's environment.
    """
    return (
        f"#!{sys.executable}\n"
        "# Generated by agent-session-loop. Inert once the run's policy is gone.\n"
        "import importlib.util\n"
        "import sys\n"
        f"_MODULE = {str(module_path)!r}\n"
        f"_POLICY = {str(policy_file)!r}\n"
        f"_NAME = {name!r}\n"
        f"_GUARD_BIN = {str(policy_file.parent / GUARD_BIN_DIR_NAME)!r}\n"
        "_spec = importlib.util.spec_from_file_location("
        '"_asl_command_guard", _MODULE)\n'
        "if _spec is None or _spec.loader is None:\n"
        "    raise SystemExit(127)\n"
        "_module = importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_module)\n"
        "raise SystemExit(\n"
        "    _module.dispatch(\n"
        "        policy_path=_POLICY,\n"
        "        name=_NAME,\n"
        "        argv=sys.argv[1:],\n"
        "        guard_bin=_GUARD_BIN,\n"
        "    )\n"
        ")\n"
    )


def install_shim(*, guard_bin: Path, name: str, policy_file: Path) -> Path:
    """Install (or refresh) one executable shim atomically.

    The temp file is written and made executable *before* the replace, so the
    name never exists as a non-executable file: a worker resolving it mid-
    install finds either the previous shim, the real executable, or the new
    shim, and never a broken third thing.
    """
    guard_bin.mkdir(parents=True, exist_ok=True)
    module_path = Path(command_guard.__file__).resolve()
    source = shim_source(
        module_path=module_path,
        policy_file=policy_file,
        name=name,
    )
    destination = guard_bin / name
    temporary = guard_bin / f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(source, encoding="utf-8")
        temporary.chmod(
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def child_path(env_path: str | None, guard_bin: Path) -> str:
    """Prepend ``guard_bin`` to a ``PATH`` value, without duplicating it."""
    current = env_path if env_path is not None else os.defpath
    entries = [entry for entry in current.split(os.pathsep) if entry]
    guard_entry = str(guard_bin)
    entries = [entry for entry in entries if entry != guard_entry]
    return os.pathsep.join([guard_entry, *entries])


class GuardController:
    """The active guard set for one run, and the only writer of its files.

    Every mutation is serialized on one lock because the two callers run on
    different threads: the mailbox/control poll thread drains queued guard
    commands mid-turn, while the run loop prepares the overlay between turns.

    The activation order is the contract, and it is why a failure cannot leave
    a half-armed guard: the executable is resolved, the policy is written, the
    shim is installed, and only then does the rule join the active set the
    caller is told about. A failure at any step raises before the set changes,
    and the policy is rewritten from the unchanged set on the way out.
    """

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: str | Path,
        path_value: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self._path_value = path_value
        self._lock = threading.Lock()
        self._guards: dict[str, GuardRule] = {}
        self._executables: dict[str, str] = {}
        self._expired = False

    # -- read-only projections ---------------------------------------------

    @property
    def guard_bin(self) -> Path:
        return guard_bin_dir(self.run_dir)

    @property
    def policy_file(self) -> Path:
        return policy_path(self.run_dir)

    def active(self) -> tuple[GuardRule, ...]:
        with self._lock:
            return tuple(self._guards.values())

    def to_payload(self) -> list[dict[str, Any]]:
        """The active summaries carried by runner state and read by status."""
        return [guard.to_payload() for guard in self.active()]

    # -- mutations ---------------------------------------------------------

    def add(
        self,
        rules: Iterable[Sequence[str]],
        *,
        reason: str | None = None,
        created_at: str | None = None,
    ) -> GuardOutcome:
        """Activate one or more command rules, all-or-nothing.

        A queued command carrying several ``--deny-command`` rules is one
        operator intention, so a partial activation would be the worst
        outcome: the operator would be told some subset is armed without
        being told which. Every rule is staged, and the set only changes once
        every shim is installed.
        """
        staged = [tuple(rule) for rule in rules]
        if not staged:
            return GuardOutcome(
                action=ACTION_REJECTED,
                reason=REJECT_EMPTY_COMMAND_RULE,
                detail="guard add requires at least one command rule",
            )
        stamp = created_at or clock.utc_iso_now()
        with self._lock:
            if self._expired:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_NO_ACTIVE_RUN,
                    detail="the run this guard names has already terminated",
                )
            resolved: dict[str, str] = {}
            for command in staged:
                name = command[0]
                if name in self._executables:
                    continue
                target = resolve_original(
                    name,
                    guard_bin=self.guard_bin,
                    path_value=self._path_value,
                )
                if target is None:
                    return GuardOutcome(
                        action=ACTION_REJECTED,
                        reason=REJECT_EXECUTABLE_NOT_FOUND,
                        detail=(
                            f"{name!r} does not resolve on PATH, so a shim for "
                            "it could shadow nothing and would break the "
                            "worker's own resolution"
                        ),
                    )
                resolved[name] = target

            new_rules = [
                GuardRule(
                    id=new_guard_id(),
                    command=command,
                    reason=reason,
                    created_at=stamp,
                )
                for command in staged
            ]
            merged = dict(self._guards)
            for rule in new_rules:
                merged[rule.id] = rule
            executables = {**self._executables, **resolved}
            try:
                self._materialize_locked(merged, executables)
            except OSError as error:
                # The active set has not changed, but the policy on disk may
                # have; restore it from the set that is still authoritative so
                # the file and the state cannot disagree.
                self._restore_locked()
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_ACTIVATION_FAILED,
                    detail=f"could not arm the command gate: {error}",
                )
            self._guards = merged
            self._executables = executables
            return GuardOutcome(
                action=ACTION_APPLIED,
                guard_ids=tuple(rule.id for rule in new_rules),
                guards=tuple(new_rules),
            )

    def remove(self, guard_id: str) -> GuardOutcome:
        """Deactivate one guard, updating the policy before reporting success.

        The shim is deliberately left in place: it delegates once its rule is
        gone, and deleting it would race a worker that is resolving the name
        right now.
        """
        with self._lock:
            if self._expired:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_NO_ACTIVE_RUN,
                    detail="the run this guard names has already terminated",
                )
            existing = self._guards.get(guard_id)
            if existing is None:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_UNKNOWN_GUARD,
                    detail=f"no active guard {guard_id!r} on this run",
                )
            remaining = {
                identifier: rule
                for identifier, rule in self._guards.items()
                if identifier != guard_id
            }
            try:
                self._materialize_locked(remaining, self._executables)
            except OSError as error:
                self._restore_locked()
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_ACTIVATION_FAILED,
                    detail=f"could not update the command gate: {error}",
                )
            self._guards = remaining
            return GuardOutcome(
                action=ACTION_REMOVED,
                guard_ids=(guard_id,),
                guards=(existing,),
            )

    def prepare_turn(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Arm the overlay for the turn about to start and return its environment.

        Called before every worker turn. The directory is created even when no
        guard is active, so a guard added mid-turn installs into a ``PATH``
        entry the running process already has; the policy only exists while
        something is denied, which is what makes an inherited shim from an
        earlier turn inert rather than stale.

        The returned mapping is the parent environment with ``PATH`` rewritten
        and nothing else touched, so every harness keeps the environment it
        was going to get.
        """
        base = dict(os.environ if env is None else env)
        with self._lock:
            guard_bin = self.guard_bin
            try:
                guard_bin.mkdir(parents=True, exist_ok=True)
                if self._guards and not self._expired:
                    self._materialize_locked(self._guards, self._executables)
                else:
                    self.policy_file.unlink(missing_ok=True)
            except OSError:
                # An overlay that cannot be armed must not cost the turn. The
                # guards stay in the active set and the next turn tries again;
                # what is lost is enforcement, and the run reports the active
                # set either way rather than claiming a gate it does not have.
                return base
        base["PATH"] = child_path(base.get("PATH"), guard_bin)
        return base

    def expire(self) -> tuple[GuardRule, ...]:
        """Terminate every guard with the run, and report what was active.

        Deleting the policy is the whole mechanism: the shims left in
        ``guard-bin`` become delegators, and no later worker inherits a
        ``PATH`` that contains this run's directory anyway. They are retained
        as inert evidence of what the run enforced.
        """
        with self._lock:
            expiring = tuple(self._guards.values())
            self._guards = {}
            self._expired = True
            try:
                self.policy_file.unlink(missing_ok=True)
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ENOTDIR):
                    raise
            return expiring

    # -- internals ---------------------------------------------------------

    def _materialize_locked(
        self,
        guards: Mapping[str, GuardRule],
        executables: Mapping[str, str],
    ) -> None:
        """Write the policy, then install every shim it needs. Caller holds the lock.

        Policy first: a shim that exists before its rule does would deny
        nothing, while a rule that exists before its shim is simply not yet
        reachable. Neither ordering can deny something the operator did not
        ask for, and this one can never leave a shim shadowing a binary for a
        run whose policy write failed.
        """
        write_policy(
            self.policy_file,
            policy_document(
                run_id=self.run_id,
                guard_bin=self.guard_bin,
                guards=list(guards.values()),
                executables=executables,
            ),
        )
        for name in sorted({rule.executable for rule in guards.values()}):
            install_shim(
                guard_bin=self.guard_bin,
                name=name,
                policy_file=self.policy_file,
            )

    def _restore_locked(self) -> None:
        """Best-effort return of the policy file to the authoritative set."""
        with contextlib.suppress(OSError):
            self._materialize_locked(self._guards, self._executables)
