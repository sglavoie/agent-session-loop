"""Runtime dispatcher behind every run-scoped command-guard shim.

A guard is enforced by a tiny executable shim installed into the run's
``guard-bin`` directory under the name of the executable it guards. That
directory is prepended to the worker turn's ``PATH``, so the harness — and
anything it spawns that resolves an executable by bare name — reaches the shim
first. The shim itself carries no policy: it calls :func:`dispatch` here with
the policy path and the name it was installed as, and this module decides
whether the invocation is denied or delegated to the real executable.

Three properties matter and are structural rather than incidental:

- **Stdlib only, importable by path.** The shim loads this file directly with
  :mod:`importlib`, never as ``agent_session_loop.command_guard``: a worker's
  ``PATH`` says nothing about its ``sys.path``, and a guard that only works
  when the package happens to be importable is not a guard.
- **No recursive resolution.** The original executable was resolved once, at
  activation, and is recorded in the policy. The delegate path prefers that
  recorded target, and its fallback search always excludes the guard-bin
  directory, so a shim can never exec itself.
- **A missing policy delegates.** Removal and run termination both work by
  taking the rule out of the policy (or deleting the policy outright); the
  shim is left behind deliberately. An inert shim that delegates is the
  designed end state, not a failure.

This is an *operational* safeguard. It stops a command a worker meant to run;
it does not stop code that calls the same API directly, invokes an absolute
path, or rewrites its own ``PATH``. :data:`agent_session_loop.guards.
COMMAND_GATE_COVERAGE` states that boundary in the terms status reports it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any

#: Exit code a denied invocation returns. POSIX reserves 126 for "command
#: found but not executable", which is the closest standard meaning to "this
#: command exists and was refused" and is what a shell reports for it.
DENIED_EXIT_CODE = 126

#: Exit code used when delegation finds no original executable at all, which
#: is what the shell would have reported had the shim never existed.
NOT_FOUND_EXIT_CODE = 127

#: Schema version of the derived policy file this dispatcher reads.
POLICY_VERSION = 1


def load_policy(policy_path: str) -> dict[str, Any] | None:
    """Read the derived policy, or ``None`` when there is nothing to enforce.

    Every failure mode collapses to ``None`` — absent file, unreadable file,
    malformed JSON, wrong version. The policy is written with an atomic
    replace, so a torn read is impossible; anything unreadable here is an
    expired run or a hand-edited file, and both mean "no guards apply".
    """
    try:
        with open(policy_path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("version") != POLICY_VERSION:
        return None
    return raw


def matched_guard(
    policy: dict[str, Any],
    name: str,
    argv: list[str],
) -> dict[str, Any] | None:
    """Return the first guard whose rule matches this invocation, if any.

    Matching is exact, case-sensitive, and prefix-only: a rule's first token
    must equal the name the shim was installed as, and its remaining tokens
    must equal the leading arguments one for one. A one-token rule therefore
    matches every invocation of that executable.
    """
    guards = policy.get("guards")
    if not isinstance(guards, list):
        return None
    for guard in guards:
        if not isinstance(guard, dict):
            continue
        command = guard.get("command")
        if not isinstance(command, list) or not command:
            continue
        if command[0] != name:
            continue
        prefix = command[1:]
        if len(argv) < len(prefix):
            continue
        if list(argv[: len(prefix)]) == prefix:
            return guard
    return None


def denial_message(guard_id: str, command: list[str]) -> str:
    """The stable two-line refusal a denied invocation prints to stderr."""
    return (
        f"asl: command denied by run-scoped guard {guard_id}\n"
        f"asl: matched argv prefix: {' '.join(command)}"
    )


def _delegate_target(
    policy: dict[str, Any] | None,
    name: str,
    guard_bin: str | None,
) -> str | None:
    """Where an unmatched invocation should go, without ever finding a shim.

    The recorded target is preferred because it is what activation resolved,
    before this shim existed to shadow it. The fallback search exists only for
    a policy that never recorded one (an expired run, most of all) and filters
    the guard-bin directory out of ``PATH`` so it cannot resolve to the shim
    that is running.
    """
    if policy is not None:
        executables = policy.get("executables")
        if isinstance(executables, dict):
            recorded = executables.get(name)
            if isinstance(recorded, str) and recorded and os.path.exists(recorded):
                return recorded
        if guard_bin is None:
            candidate = policy.get("guard_bin")
            guard_bin = candidate if isinstance(candidate, str) else None

    entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    if guard_bin is not None:
        guard_real = os.path.realpath(guard_bin)
        entries = [
            entry
            for entry in entries
            if entry and os.path.realpath(entry) != guard_real
        ]
    if not entries:
        return None
    return shutil.which(name, path=os.pathsep.join(entries))


def dispatch(
    *,
    policy_path: str,
    name: str,
    argv: list[str],
    guard_bin: str | None = None,
) -> int:
    """Deny or delegate one guarded invocation.

    On the delegate path this replaces the process with :func:`os.execv`, so
    the return value only ever describes a refusal or a delegation that could
    not happen. ``argv`` is the invocation's arguments *without* ``argv[0]``.
    """
    policy = load_policy(policy_path)
    if policy is not None:
        guard = matched_guard(policy, name, argv)
        if guard is not None:
            identifier = guard.get("id")
            command = guard.get("command")
            print(
                denial_message(
                    identifier if isinstance(identifier, str) else "unknown",
                    [str(token) for token in command]
                    if isinstance(command, list)
                    else [name],
                ),
                file=sys.stderr,
                flush=True,
            )
            return DENIED_EXIT_CODE

    target = _delegate_target(policy, name, guard_bin)
    if target is None:
        print(f"asl: {name}: not found", file=sys.stderr, flush=True)
        return NOT_FOUND_EXIT_CODE
    try:
        os.execv(target, [name, *argv])
    except OSError as error:
        print(f"asl: {name}: cannot execute {target}: {error}", file=sys.stderr)
        return NOT_FOUND_EXIT_CODE
    # Unreachable: a successful execv never returns.
    return NOT_FOUND_EXIT_CODE
