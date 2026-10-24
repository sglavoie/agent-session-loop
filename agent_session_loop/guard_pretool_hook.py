"""Pre-tool guard hook: refuse a guarded tool call before the harness runs it.

Armed for one turn through the harness's own hook overlay, this script reads
the run's derived guard policy and answers one question per tool call: is this
call something the operator has already forbidden? A denial here is strictly
better than the postflight comparison that backs it up, because the worker is
told *why* and can do something else, instead of finishing a turn that is then
thrown away.

It is a *narrowing* of the enforcement, never a replacement for it:

- A command rule is checked against a tool call whose command is one
  unambiguous simple command. Anything with a shell operator in it is passed
  through untouched — the ``PATH`` shim is what catches those, and pretending
  to parse a shell script here would produce confident wrong answers.
- A path rule is checked against a tool call that names the file it writes.
  A shell script that writes the same file is not predicted, and is caught
  after the turn instead.

Every failure path exits 0 with no output, which allows the call. That is the
correct bias for this layer specifically: the hook is an early, partial
convenience, and the guarantees live in the ``PATH`` shim and the turn-start
snapshot comparison, neither of which can be affected by anything that goes
wrong in here. A hook that blocked the worker whenever it failed to read a file
would convert its own bugs into stalled runs.

Stdlib only, and imported by path rather than as part of its package: the
harness runs this with whatever interpreter and working directory it likes, and
a hook that only worked when ``agent_session_loop`` happened to be importable
would be armed exactly when it was not needed.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import sys
from typing import Any

#: Tool-call inputs that name a single file the tool acts on. The same keys
#: name the file a read tool would open, so the tool *name* is what decides
#: whether the call is a write -- see :data:`_WRITE_TOOLS`.
_WRITE_PATH_KEYS = ("file_path", "path", "notebook_path")

#: Tool names whose ``file_path`` this hook treats as a write. A guard freezes
#: content, not attention: denying a read of a protected file would block the
#: worker from even looking at what it must not change, which is neither what
#: the operator asked for nor something the postflight comparison would ever
#: convict. Anything not named here is left to the substring test below and,
#: failing that, to the turn-start snapshot.
_WRITE_TOOLS = frozenset(
    {
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "create_file",
        "edit_file",
        "str_replace_editor",
        "apply_patch",
    }
)

#: Substrings that make an unrecognized tool name a write. Harnesses add tools
#: faster than this file is edited, so a name that says it writes is taken at
#: its word; a name that does not is simply not covered by this lane.
_WRITE_TOOL_HINTS = ("write", "edit", "patch", "replace")

#: Tool names whose ``command`` this hook is willing to inspect. Anything else
#: carrying a ``command`` key is left to the PATH shim.
_COMMAND_TOOLS = frozenset({"Bash", "shell", "Shell", "BashOutput", "exec"})

#: A command string containing any of these is not one unambiguous invocation,
#: so this hook declines to reason about it rather than guessing which of the
#: several commands inside it would actually run.
_SHELL_METACHARACTERS = frozenset("|&;<>()$`\n")

_DENY_PREFIX = "asl: refused by run-scoped guard"


def _load_policy(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError, ValueError:
        return None
    if not isinstance(document, dict):
        return None
    return document


def _compile_pattern(pattern: str) -> re.Pattern[str] | None:
    """Mirror of the matcher in ``agent_session_loop.guards``.

    Duplicated rather than imported for the reason in the module docstring: the
    package may not be importable here. The policy file records the *validated*
    pattern, so this side only has to translate, never to re-validate.
    """
    parts: list[str] = []
    segments = pattern.split("/")
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append("[^/]+(?:/[^/]+)*" if last else "(?:[^/]+/)*")
            continue
        for character in segment:
            if character == "*":
                parts.append("[^/]*")
            elif character == "?":
                parts.append("[^/]")
            else:
                parts.append(re.escape(character))
        if not last:
            parts.append("/")
    try:
        return re.compile("".join(parts) + r"\Z")
    except re.error:
        return None


def _qualify(absolute: str, aliases: dict[str, Any]) -> list[tuple[str, str]]:
    """Every ``(alias, relative)`` pair whose root contains ``absolute``.

    A list rather than one answer because nested repositories are legal: a path
    inside a submodule is inside its parent too, and a rule written against
    either qualifier is a rule the operator meant.
    """
    matches: list[tuple[str, str]] = []
    for alias, root in aliases.items():
        if not isinstance(alias, str) or not isinstance(root, str) or not root:
            continue
        try:
            relative = os.path.relpath(absolute, os.path.realpath(root))
        except OSError, ValueError:
            continue
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            continue
        if os.path.isabs(relative):
            continue
        matches.append((alias, relative.replace(os.sep, "/")))
    return matches


def _is_write_tool(tool_name: Any) -> bool:
    if not isinstance(tool_name, str) or not tool_name:
        return False
    if tool_name in _WRITE_TOOLS:
        return True
    lowered = tool_name.lower()
    return any(hint in lowered for hint in _WRITE_TOOL_HINTS)


def _qualified_write_target(
    tool_input: dict[str, Any],
    policy: dict[str, Any],
) -> list[tuple[str, str]]:
    """Every ``(alias, relative)`` naming the file a write tool call targets."""
    workspace = policy.get("workspace")
    aliases = workspace.get("aliases") if isinstance(workspace, dict) else None
    if not isinstance(aliases, dict) or not aliases:
        return []
    raw_path: Any = None
    for key in _WRITE_PATH_KEYS:
        candidate = tool_input.get(key)
        if isinstance(candidate, str) and candidate:
            raw_path = candidate
            break
    if not isinstance(raw_path, str):
        return []
    try:
        absolute = os.path.realpath(os.path.abspath(raw_path))
    except OSError, ValueError:
        return []
    return _qualify(absolute, aliases)


def _matched_external_read_only_alias(
    tool_input: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[str, str] | None:
    """The ``(alias, relative)`` of a write into an external read-only alias."""
    raw = policy.get("external_read_only_aliases")
    if not isinstance(raw, list) or not raw:
        return None
    read_only = {alias for alias in raw if isinstance(alias, str)}
    for alias, relative in _qualified_write_target(tool_input, policy):
        if alias in read_only:
            return alias, relative
    return None


def _matched_path_guard(
    tool_input: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any] | None:
    qualified = _qualified_write_target(tool_input, policy)
    if not qualified:
        return None
    for guard in policy.get("guards") or ():
        if not isinstance(guard, dict):
            continue
        protected = guard.get("protected_path")
        if not isinstance(protected, dict):
            continue
        qualifier = protected.get("qualifier")
        pattern = protected.get("pattern")
        if not isinstance(qualifier, str) or not isinstance(pattern, str):
            continue
        compiled = _compile_pattern(pattern)
        if compiled is None:
            continue
        for alias, relative in qualified:
            if alias == qualifier and compiled.match(posixpath.normpath(relative)):
                return guard
    return None


def _matched_command_guard(
    tool_input: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any] | None:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if _SHELL_METACHARACTERS & set(command):
        return None
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    # The absolute-path bypass, caught where it is cheap to catch: `/usr/bin/git
    # push` never resolves through PATH, so the shim cannot see it, but the
    # basename is right here in the argv the tool call carries.
    names = {argv[0], os.path.basename(argv[0])}
    for guard in policy.get("guards") or ():
        if not isinstance(guard, dict):
            continue
        rule = guard.get("command")
        if not isinstance(rule, list) or not rule:
            continue
        if str(rule[0]) not in names:
            continue
        tail = [str(token) for token in rule[1:]]
        if argv[1 : 1 + len(tail)] == tail:
            return guard
    return None


def _deny(guard: dict[str, Any], subject: str) -> None:
    guard_id = guard.get("id")
    reason = guard.get("reason")
    message = f"{_DENY_PREFIX} {guard_id}: {subject}"
    if isinstance(reason, str) and reason:
        message += f" ({reason})"
    message += (
        ". This is an operator instruction for this run; do not work around "
        "it, and say what you needed it for instead."
    )
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": message,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def main(argv: list[str]) -> int:
    policy_path = argv[1] if len(argv) > 1 else os.environ.get("ASL_GUARD_POLICY", "")
    if not policy_path:
        return 0
    policy = _load_policy(policy_path)
    if policy is None:
        return 0
    try:
        event = json.load(sys.stdin)
    except ValueError, OSError:
        return 0
    if not isinstance(event, dict):
        return 0
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    tool_name = event.get("tool_name")

    guard = (
        _matched_path_guard(tool_input, policy) if _is_write_tool(tool_name) else None
    )
    if guard is not None:
        protected = guard.get("protected_path") or {}
        _deny(
            guard,
            f"{protected.get('qualifier')}:{protected.get('pattern')} must end "
            "this turn exactly as it started",
        )
        return 0
    external = (
        _matched_external_read_only_alias(tool_input, policy)
        if _is_write_tool(tool_name)
        else None
    )
    if external is not None:
        alias, relative = external
        _deny(
            {"id": "external-read-only"},
            f"{alias}:{relative} is in a repository this session may only read "
            "(others are changing it during this run)",
        )
        return 0
    if isinstance(tool_name, str) and tool_name in _COMMAND_TOOLS:
        guard = _matched_command_guard(tool_input, policy)
        if guard is not None:
            rendered = " ".join(str(token) for token in guard.get("command") or ())
            _deny(guard, f"the command `{rendered}` is denied for this run")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    try:
        raise SystemExit(main(sys.argv))
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        # Never let an unexpected failure in this layer stall the worker; the
        # shim and the postflight comparison are unaffected by anything here.
        raise SystemExit(0) from None
