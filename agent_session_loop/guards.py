"""Run-scoped guards: rules, active state, the command gate, and path guards.

A guard is an operator's standing refusal, attached to one live run: "while
this run lasts, do not let the worker run ``git push``", or "while this run
lasts, ``docs/api.md`` must end the turn exactly as it started". It is enforced
by the process boundary rather than by the worker's cooperation, which is the
whole point — guidance a worker never reads changes nothing, and a guard has to
hold anyway.

There are two rule kinds and they are enforced by different machinery, which is
why :data:`GUARD_KINDS` is a first-class distinction rather than an
implementation detail:

- A **command rule** is denied *before* it runs, by a ``PATH`` shim.
- A **protected-path rule** is convicted *after* the turn, by comparing the
  workspace against the snapshot captured before the turn started. A pre-tool
  hook denies the direct writes it can see, but the postflight comparison is
  the part that actually holds, because no hook can predict what an arbitrary
  shell script will touch.

The command mechanism has four moving parts and they are deliberately small:

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
reports it, so nobody has to infer the boundary from the implementation, and
:func:`guard_coverage` says the same thing per guard and per enforcement lane
so an operator can see which half of a guard is actually armed.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import json
import os
import re
import shlex
import shutil
import stat
import sys
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import artifacts, clock, command_guard

#: Directory, under the per-run state directory, that shadows ``PATH``.
GUARD_BIN_DIR_NAME = "guard-bin"

#: The derived policy every shim reads, beside the guard-bin it shadows with.
GUARD_POLICY_FILE_NAME = "guard-policy.json"

#: How a pre-tool hook finds the policy. The shims have the path baked in at
#: install time; a hook is a single script shared by every run, so it is told
#: through the turn environment instead of guessing from a working directory.
GUARD_POLICY_ENV_VAR = "ASL_GUARD_POLICY"

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
REJECT_EMPTY_GUARD_RULE = "empty-guard-rule"

# Protected-path rejections. A path rule that cannot be matched against the
# workspace comparison output is refused at parse time rather than accepted as
# protection that silently never fires.
REJECT_EMPTY_PATH_RULE = "empty-path-rule"
REJECT_PATH_MISSING_QUALIFIER = "path-missing-qualifier"
REJECT_PATH_UNKNOWN_ALIAS = "path-unknown-alias"
REJECT_PATH_ABSOLUTE = "absolute-path-in-rule"
REJECT_PATH_BACKSLASH = "backslash-in-path-rule"
REJECT_PATH_TRAVERSAL = "parent-traversal-in-path-rule"
REJECT_PATH_EMPTY_SEGMENT = "empty-segment-in-path-rule"
REJECT_PATH_PATTERN = "unsupported-path-pattern"
REJECT_PROTECTED_PATH_EVIDENCE = "protected-path-evidence-unavailable"

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


#: What a protected-path guard reaches, and what it does not — the path-rule
#: counterpart to :data:`COMMAND_GATE_COVERAGE`, reported verbatim by status.
PROTECTED_PATH_COVERAGE: dict[str, Any] = {
    "mechanism": "turn-start workspace snapshot compared at finalization",
    "matching": (
        "case-sensitive POSIX qualified paths; `*` and `?` within one segment, "
        "`**` across segments"
    ),
    "baseline": "the snapshot captured before the turn started, not before the "
    "guard was added",
    "covers": (
        "any change to a matched path present at finalization, however it was made",
        "a change made earlier in the turn than the guard itself",
        "a change made by a process the harness never saw",
    ),
    "does_not_cover": (
        "a matched path changed and restored to its exact turn-start content",
        "a change made after the run terminates",
        "predicting what an arbitrary shell script will write before it runs",
    ),
    "on_violation": "the turn is rejected, its filesystem changes are retained, "
    "and the session returns to pending",
    "guarantee": "operational safeguard, not a sandbox against hostile code",
}

#: The two rule kinds. A guard is exactly one of them.
GUARD_KIND_COMMAND = "command"
GUARD_KIND_PROTECTED_PATH = "protected-path"
GUARD_KINDS: tuple[str, ...] = (GUARD_KIND_COMMAND, GUARD_KIND_PROTECTED_PATH)

#: The qualifier a legacy (single-repository) plan must use. Registered plans
#: use their declared aliases instead; there is no wildcard qualifier, because
#: "every repository" written by hand is indistinguishable from a typo.
LEGACY_PATH_QUALIFIER = "."

#: Enforcement lanes reported per guard. They are separate because a guard can
#: be armed in one and blind in another, and collapsing them into a single
#: "protected: yes" would be the most misleading thing status could say.
LANE_COMMAND_GATE = "command-gate"
LANE_PRETOOL = "pretool"
LANE_POSTFLIGHT = "postflight"
COVERAGE_LANES: tuple[str, ...] = (LANE_COMMAND_GATE, LANE_PRETOOL, LANE_POSTFLIGHT)

#: Per-lane verdicts. ``degraded`` is not a hedge: it means the lane enforces
#: something strictly narrower than the rule, and the narrowing is named.
COVERAGE_ACTIVE = "active"
COVERAGE_DEGRADED = "degraded"
COVERAGE_UNSUPPORTED = "unsupported"

#: Stable per-lane reasons. Clients branch on these, so they are spelled once.
COVERAGE_REASON_SHIM_ARMED = "path-shim-armed"
COVERAGE_REASON_SHIM_UNAVAILABLE = "path-shim-not-armed"
COVERAGE_REASON_OVERLAY_UNARMED = "guard-overlay-not-armed"
COVERAGE_REASON_NOT_A_COMMAND_RULE = "not-a-command-rule"
COVERAGE_REASON_NOT_A_PATH_RULE = "not-a-path-rule"
COVERAGE_REASON_HOOK_DENIES = "hook-denies-before-execution"
COVERAGE_REASON_HOOK_UNAVAILABLE = "harness-exposes-no-pre-tool-hook"
COVERAGE_REASON_CODEX_HOOK_TRUST = "codex-hook-trust-requires-user-config-mutation"
COVERAGE_REASON_COPILOT_PROCESS_START = "copilot-deny-tool-fixed-at-process-start"
COVERAGE_REASON_COPILOT_NO_PATH_RULE = "copilot-deny-tool-cannot-express-paths"
COVERAGE_REASON_COPILOT_UNEXPRESSIBLE = "copilot-deny-tool-cannot-express-rule"
COVERAGE_REASON_ADDED_AFTER_PROCESS_START = "guard-added-after-process-start"
COVERAGE_REASON_SNAPSHOT_COMPARISON = "turn-start-snapshot-comparison"
COVERAGE_REASON_NOT_OBSERVABLE = "command-execution-leaves-no-postflight-evidence"


class GuardRuleError(ValueError):
    """A rule that cannot be turned into an argv prefix or a path matcher."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class ProtectedPath:
    """One qualified path pattern: which repository, and which paths in it.

    The qualifier is an alias from the plan's ``## Repositories`` registry (or
    ``.`` for a legacy single-repository plan) and matches the ``alias:path``
    vocabulary the workspace write guard already produces, so a rule is
    compared against evidence that was built independently of it.
    """

    qualifier: str
    pattern: str

    @property
    def rendered(self) -> str:
        return f"{self.qualifier}:{self.pattern}"

    def matches(self, qualifier: str, path: str) -> bool:
        return qualifier == self.qualifier and bool(
            _compile_path_pattern(self.pattern).match(path)
        )

    def matches_qualified(self, qualified: str) -> bool:
        qualifier, separator, path = qualified.partition(":")
        if not separator:
            return False
        return self.matches(qualifier, path)

    def to_payload(self) -> dict[str, Any]:
        return {"qualifier": self.qualifier, "pattern": self.pattern}


@dataclass(frozen=True)
class GuardRule:
    """One active guard: what it forbids, its identity, and why it exists.

    Exactly one of ``command`` and ``path`` is populated. They are separate
    fields rather than a tagged union because every consumer branches on the
    kind anyway, and an empty tuple is a less ambiguous "not this kind" than a
    sentinel would be.
    """

    id: str
    reason: str | None
    created_at: str
    command: tuple[str, ...] = ()
    path: ProtectedPath | None = None

    @property
    def kind(self) -> str:
        return GUARD_KIND_COMMAND if self.command else GUARD_KIND_PROTECTED_PATH

    @property
    def executable(self) -> str | None:
        return self.command[0] if self.command else None

    @property
    def rendered_command(self) -> str | None:
        return " ".join(self.command) if self.command else None

    @property
    def rendered(self) -> str:
        """One line naming what this guard forbids, whichever kind it is."""
        if self.command:
            return " ".join(self.command)
        assert self.path is not None
        return self.path.rendered

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "command": list(self.command),
            "rendered_command": self.rendered_command,
            "executable": self.executable,
            "protected_path": self.path.to_payload() if self.path else None,
            "rendered": self.rendered,
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


#: Pattern metacharacters this matcher implements. Anything else that a glob
#: dialect might mean — character classes, brace expansion, negation — is
#: refused, because a rule that quietly matched nothing would read as
#: protection right up until it failed to fire.
_PATH_WILDCARD_CHARS = frozenset("*?")
_UNSUPPORTED_PATTERN_CHARS = frozenset("[]{}!\0")


@functools.lru_cache(maxsize=512)
def _compile_path_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a validated path pattern into a whole-string, case-sensitive regex.

    ``**`` is a whole segment and spans zero or more segments; ``*`` and ``?``
    never cross a separator. Matching is case-sensitive even on a
    case-insensitive filesystem, because the qualified paths this is compared
    against come from Git, which is case-sensitive about what it records.
    """
    segments = pattern.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            # Trailing ``**`` must still require something below it: ``a/**``
            # protects the contents of ``a``, and ``a`` itself is the literal
            # rule ``a``.
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
    return re.compile("".join(parts) + r"\Z")


def parse_protected_path_rule(
    raw: str,
    *,
    aliases: Sequence[str] | None = None,
) -> ProtectedPath:
    """Turn one operator-written ``Alias:glob`` rule into a path matcher.

    The qualifier is mandatory and is validated against the plan's own
    vocabulary: a registered plan's declared aliases, or ``.`` alone for a
    legacy plan. Guessing a default repository would be the one failure mode
    with no recovery — the operator would be told the guard is armed while it
    watched a repository they did not name.

    ``aliases`` of ``None`` means "no plan context available", which validates
    the syntax and defers the alias check to the caller that has one.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise GuardRuleError(
            REJECT_EMPTY_PATH_RULE,
            "a protected-path rule must name a repository and a path",
        )
    candidate = raw.strip()
    if "\\" in candidate:
        raise GuardRuleError(
            REJECT_PATH_BACKSLASH,
            f"protected-path rule {candidate!r} contains a backslash; paths are "
            "compared as POSIX paths, so `/` is the only separator",
        )
    qualifier, separator, pattern = candidate.partition(":")
    if not separator:
        raise GuardRuleError(
            REJECT_PATH_MISSING_QUALIFIER,
            f"protected-path rule {candidate!r} names no repository; write "
            "`Alias:path` (or `.:path` for a single-repository plan)",
        )
    if not qualifier or not pattern:
        raise GuardRuleError(
            REJECT_EMPTY_PATH_RULE,
            f"protected-path rule {candidate!r} has an empty repository or path",
        )
    if pattern.startswith("/"):
        raise GuardRuleError(
            REJECT_PATH_ABSOLUTE,
            f"protected-path rule {candidate!r} is absolute; a path is relative "
            "to the repository its qualifier names, so an absolute path could "
            "never match the evidence the guard compares against",
        )
    if _UNSUPPORTED_PATTERN_CHARS & set(pattern):
        raise GuardRuleError(
            REJECT_PATH_PATTERN,
            f"protected-path rule {candidate!r} uses pattern syntax this "
            "matcher does not implement; only literals, `*`, `**`, and `?` are "
            "supported",
        )
    segments = pattern.split("/")
    if segments[0] == ".":
        # A leading `./` is how people type "in this repository"; it is the one
        # `.` segment that carries an intention rather than a mistake.
        segments = segments[1:]
    for segment in segments:
        if not segment:
            raise GuardRuleError(
                REJECT_PATH_EMPTY_SEGMENT,
                f"protected-path rule {candidate!r} has an empty path segment; "
                "a trailing or doubled `/` is almost never what was meant",
            )
        if segment == os.pardir:
            raise GuardRuleError(
                REJECT_PATH_TRAVERSAL,
                f"protected-path rule {candidate!r} escapes its repository with "
                "`..`; name the repository that owns the path instead",
            )
        if segment == os.curdir:
            raise GuardRuleError(
                REJECT_PATH_PATTERN,
                f"protected-path rule {candidate!r} contains a `.` segment; "
                "write the path as it appears in the repository",
            )
        if "**" in segment and segment != "**":
            raise GuardRuleError(
                REJECT_PATH_PATTERN,
                f"protected-path rule {candidate!r} mixes `**` with other "
                "characters in one segment; `**` spans whole segments, so it "
                "must stand alone between separators",
            )
    if not segments:
        raise GuardRuleError(
            REJECT_EMPTY_PATH_RULE,
            f"protected-path rule {candidate!r} names no path inside the repository",
        )
    normalized = "/".join(segments)
    if aliases is not None and qualifier not in tuple(aliases):
        known = ", ".join(sorted(aliases)) or "none"
        raise GuardRuleError(
            REJECT_PATH_UNKNOWN_ALIAS,
            f"protected-path rule {candidate!r} names repository {qualifier!r}, "
            f"which this plan does not declare; known qualifiers: {known}",
        )
    return ProtectedPath(qualifier=qualifier, pattern=normalized)


def match_protected_paths(
    guards: Sequence[GuardRule],
    qualified_paths: Iterable[str],
) -> OrderedDict[str, tuple[str, ...]]:
    """Map each violated guard id to the qualified paths that violated it.

    Guards with no match are absent rather than present-and-empty, so a caller
    can treat a non-empty result as "the turn is convicted" without filtering.
    """
    observed = tuple(dict.fromkeys(qualified_paths))
    matched: OrderedDict[str, tuple[str, ...]] = OrderedDict()
    for guard in guards:
        if guard.path is None:
            continue
        hits = tuple(
            qualified
            for qualified in observed
            if guard.path.matches_qualified(qualified)
        )
        if hits:
            matched[guard.id] = hits
    return matched


def guard_coverage(
    guard: GuardRule,
    *,
    harness: str | None,
    gate_armed: bool = True,
    overlay_armed: bool = True,
    pretool_armed: bool = False,
    process_start: bool = False,
) -> dict[str, dict[str, str]]:
    """Say, per enforcement lane, how much of ``guard`` is actually armed.

    The three lanes fail independently and an operator's next move differs by
    which one is blind, so each carries its own verdict and its own stable
    reason rather than being folded into one number.

    ``pretool_armed`` reports whether the harness adapter installed this run's
    hook overlay; ``process_start`` reports whether the guard already existed
    when the worker process was spawned, which is the only thing a fixed
    command-line deny rule can ever cover.

    ``overlay_armed`` reports whether the turn in flight actually received the
    overlay: :meth:`GuardController.prepare_turn` fails open, so a worker can be
    running with an untouched ``PATH`` and no policy path in its environment
    while the guard is still perfectly present in the active set. That is the
    one case where both pre-execution lanes are blind for a reason no rule and
    no harness explains, so it is reported rather than inferred.
    """
    lanes: dict[str, dict[str, str]] = {}
    if guard.kind == GUARD_KIND_COMMAND:
        if not gate_armed:
            lanes[LANE_COMMAND_GATE] = _lane(
                COVERAGE_UNSUPPORTED, COVERAGE_REASON_SHIM_UNAVAILABLE
            )
        elif not overlay_armed:
            # The shim may well exist on disk; what is missing is the ``PATH``
            # entry that would make the worker find it, so the gate is not a
            # narrower enforcement but no enforcement at all.
            lanes[LANE_COMMAND_GATE] = _lane(
                COVERAGE_UNSUPPORTED, COVERAGE_REASON_OVERLAY_UNARMED
            )
        else:
            lanes[LANE_COMMAND_GATE] = _lane(
                COVERAGE_ACTIVE, COVERAGE_REASON_SHIM_ARMED
            )
        lanes[LANE_POSTFLIGHT] = _lane(
            COVERAGE_UNSUPPORTED, COVERAGE_REASON_NOT_OBSERVABLE
        )
    else:
        lanes[LANE_COMMAND_GATE] = _lane(
            COVERAGE_UNSUPPORTED, COVERAGE_REASON_NOT_A_COMMAND_RULE
        )
        lanes[LANE_POSTFLIGHT] = _lane(
            COVERAGE_ACTIVE, COVERAGE_REASON_SNAPSHOT_COMPARISON
        )
    pretool = _pretool_lane(
        guard,
        harness=harness,
        pretool_armed=pretool_armed,
        process_start=process_start,
    )
    if not overlay_armed and pretool["status"] != COVERAGE_UNSUPPORTED:
        # Every pretool lane reads the policy through the turn environment: the
        # Claude hook resolves `ASL_GUARD_POLICY`, and Copilot compiles
        # `--deny-tool` from the same document. Without the injection there is
        # nothing for either to read, so a lane that would otherwise be armed
        # is downgraded. A lane that is already unsupported keeps its own
        # reason, which is the more specific and more durable of the two.
        pretool = _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_OVERLAY_UNARMED)
    lanes[LANE_PRETOOL] = pretool
    return lanes


def _lane(status: str, reason: str) -> dict[str, str]:
    return {"status": status, "reason": reason}


def _pretool_lane(
    guard: GuardRule,
    *,
    harness: str | None,
    pretool_armed: bool,
    process_start: bool,
) -> dict[str, str]:
    """The pre-execution verdict, which is the lane harnesses differ on.

    Claude accepts a per-turn hook overlay, so both rule kinds are refused
    before the tool call runs. Codex reads hooks only from a trusted
    ``CODEX_HOME`` file, and arming that trust means writing a hash into the
    operator's own configuration, which ASL will not do. Copilot has no hook at
    all: its ``--deny-tool`` rules are fixed when the process is spawned, so
    they cover a command rule that already existed and nothing else.
    """
    if harness == "claude":
        if not pretool_armed:
            return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_HOOK_UNAVAILABLE)
        return _lane(COVERAGE_ACTIVE, COVERAGE_REASON_HOOK_DENIES)
    if harness == "codex":
        return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_CODEX_HOOK_TRUST)
    if harness == "copilot":
        if guard.kind != GUARD_KIND_COMMAND:
            return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_COPILOT_NO_PATH_RULE)
        if copilot_deny_tool_rule(guard.command) is None:
            return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_COPILOT_UNEXPRESSIBLE)
        if not process_start:
            return _lane(
                COVERAGE_UNSUPPORTED, COVERAGE_REASON_ADDED_AFTER_PROCESS_START
            )
        return _lane(COVERAGE_DEGRADED, COVERAGE_REASON_COPILOT_PROCESS_START)
    return _lane(COVERAGE_UNSUPPORTED, COVERAGE_REASON_HOOK_UNAVAILABLE)


def copilot_deny_tool_rule(command: Sequence[str]) -> str | None:
    """The ``--deny-tool`` value for one command rule, or ``None`` if there is none.

    ``--deny-tool='shell(git push)'`` matches on the command *text*, so a rule
    is only expressible when writing its tokens back out with single spaces
    reproduces the invocation it was parsed from. A token carrying whitespace
    or a parenthesis does not survive that round trip -- it would either split
    into two tokens or close the ``shell(...)`` wrapper early -- so it compiles
    to nothing here and its ``pretool`` lane says so rather than shipping a
    rule that quietly matches something else.
    """
    tokens = [str(token) for token in command]
    if not tokens:
        return None
    if any(not token or set(token) & _COPILOT_UNEXPRESSIBLE for token in tokens):
        return None
    return f"shell({' '.join(tokens)})"


#: Characters that make a command rule impossible to state as a ``shell(...)``
#: deny value: whitespace is the token separator on the other side, and the
#: parentheses delimit the wrapper itself.
_COPILOT_UNEXPRESSIBLE = frozenset(" \t\n()")


def copilot_deny_tool_rules(guards: Sequence[GuardRule]) -> tuple[str, ...]:
    """Compile the guards Copilot's ``--deny-tool`` can express, in order.

    Only command rules compile: the CLI has no path-aware equivalent, so a
    protected-path guard contributes nothing here and is reported as
    postflight-only rather than silently dropped.
    """
    return _dedupe(copilot_deny_tool_rule(guard.command) for guard in guards)


def copilot_deny_tool_rules_from_policy(
    policy: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """The same compilation, driven by a policy document instead of objects.

    Argv is built from the derived policy the turn environment points at, not
    from the live controller: the two are the same set by construction, and
    reading the file is what makes "guards present at process start" a fact
    about the document the worker's other lanes also read. Anything malformed
    is skipped rather than raised -- a policy this side cannot parse must
    degrade to the lanes that do not depend on argv, never fail the turn.
    """
    if not policy:
        return ()
    entries = policy.get("guards")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return ()
    commands = [
        entry.get("command")
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("command"), Sequence)
    ]
    return _dedupe(
        copilot_deny_tool_rule(command)
        for command in commands
        if not isinstance(command, (str, bytes))
    )


def _dedupe(values: Iterable[str | None]) -> tuple[str, ...]:
    """First-wins deduplication that drops the ``None``s, preserving order."""
    seen: list[str] = []
    for value in values:
        if value is not None and value not in seen:
            seen.append(value)
    return tuple(seen)


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
    workspace_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The exact JSON a shim or a pre-tool hook reads, derived from the active set.

    Two readers, one file. The shim only looks at ``guards[].command`` and
    ``executables``; the hook additionally needs ``protected_paths`` and
    ``workspace.aliases`` to turn the absolute path a tool call carries back
    into the ``alias:path`` vocabulary the rules are written in. Keeping them in
    one atomically-replaced document is what stops the two enforcement lanes
    from disagreeing about what is currently denied.
    """
    return {
        "version": POLICY_VERSION,
        "run_id": run_id,
        "guard_bin": str(guard_bin),
        "guards": [
            {
                "id": guard.id,
                "kind": guard.kind,
                "command": list(guard.command),
                "protected_path": (
                    guard.path.to_payload() if guard.path is not None else None
                ),
                "reason": guard.reason,
            }
            for guard in guards
        ],
        "executables": dict(executables),
        "workspace": {"aliases": dict(workspace_aliases or {})},
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
        workspace_aliases: Mapping[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self._path_value = path_value
        self._lock = threading.Lock()
        self._guards: dict[str, GuardRule] = {}
        self._executables: dict[str, str] = {}
        self._workspace_aliases: dict[str, str] = dict(workspace_aliases or {})
        self._expired = False
        # Coverage context for the turn now in flight. It is per-turn rather
        # than per-run because the harness can be switched mid-run, and a guard
        # that was pre-tool-enforced under Claude is not enforced the same way
        # after `control select-harness` moves the run to Copilot.
        self._harness: str | None = None
        self._pretool_armed = False
        self._process_start_ids: frozenset[str] = frozenset()
        # Tri-state, and the third state is the point: ``None`` means no turn
        # is in flight, ``True`` means one is and its baseline was captured,
        # ``False`` means one is and it was not. Only the last refuses a path
        # guard, because it is the only case where arming one would promise a
        # comparison that can never be made.
        self._turn_snapshot_available: bool | None = None
        # Whether the last `prepare_turn` both materialized the overlay and
        # injected it into the environment it handed back. It starts True
        # because a controller that has prepared no turn yet has told nobody
        # anything, and it is rewritten by every `prepare_turn` — so a failure
        # is reported for exactly as long as it is the truth: until the next
        # turn is prepared successfully.
        self._overlay_armed = True

    # -- read-only projections ---------------------------------------------

    @property
    def overlay_armed(self) -> bool:
        """Whether the environment the last prepared turn received is guarded."""
        with self._lock:
            return self._overlay_armed

    @property
    def guard_bin(self) -> Path:
        return guard_bin_dir(self.run_dir)

    @property
    def policy_file(self) -> Path:
        return policy_path(self.run_dir)

    def active(self) -> tuple[GuardRule, ...]:
        with self._lock:
            return tuple(self._guards.values())

    def active_paths(self) -> tuple[GuardRule, ...]:
        """The protected-path subset, which is what finalization enforces."""
        return tuple(
            guard for guard in self.active() if guard.kind == GUARD_KIND_PROTECTED_PATH
        )

    def workspace_aliases(self) -> dict[str, str]:
        with self._lock:
            return dict(self._workspace_aliases)

    def mark_turn_snapshot(self, available: bool | None) -> None:
        """Record whether the turn in flight has a baseline to compare against.

        Called with a boolean when a turn starts and with ``None`` when it
        ends. A path guard added between turns is always accepted: the next
        turn captures its own baseline, so there is nothing missing yet.
        """
        with self._lock:
            self._turn_snapshot_available = available

    def set_workspace_aliases(self, aliases: Mapping[str, str]) -> None:
        """Bind alias names to absolute roots for the pre-tool hook.

        Resolved once the run knows its workspace, which is after the
        controller exists: the controller is created with the run directory, and
        the repository registry is derived later. Rewriting the policy here
        keeps the hook from having to re-derive a binding the runner already
        made.
        """
        with self._lock:
            self._workspace_aliases = dict(aliases)
            if self._guards and not self._expired:
                self._restore_locked()

    def to_payload(self) -> list[dict[str, Any]]:
        """The active summaries carried by runner state and read by status.

        Each entry carries its own per-lane coverage, because two guards on the
        same run can be enforced differently: one added before the worker
        process started can reach a fixed command-line deny rule that one added
        mid-turn never will.
        """
        with self._lock:
            harness = self._harness
            pretool_armed = self._pretool_armed
            process_start_ids = self._process_start_ids
            gate_armed = not self._expired
            overlay_armed = self._overlay_armed
            active = tuple(self._guards.values())
        return [
            {
                **guard.to_payload(),
                "coverage": guard_coverage(
                    guard,
                    harness=harness,
                    gate_armed=gate_armed,
                    overlay_armed=overlay_armed,
                    pretool_armed=pretool_armed,
                    process_start=guard.id in process_start_ids,
                ),
            }
            for guard in active
        ]

    # -- mutations ---------------------------------------------------------

    def add(
        self,
        rules: Iterable[Sequence[str]] = (),
        *,
        paths: Iterable[ProtectedPath] = (),
        reason: str | None = None,
        created_at: str | None = None,
    ) -> GuardOutcome:
        """Activate one or more rules of either kind, all-or-nothing.

        A queued command carrying several ``--deny-command`` and
        ``--protect-path`` rules is one operator intention, so a partial
        activation would be the worst outcome: the operator would be told some
        subset is armed without being told which. Every rule is staged, and the
        set only changes once every shim is installed.

        A path rule needs no shim and cannot fail to resolve, so mixing the
        kinds costs nothing: the all-or-nothing boundary is the shim
        installation, and a path rule simply joins the set alongside it.
        """
        staged = [tuple(rule) for rule in rules]
        staged_paths = list(paths)
        if not staged and not staged_paths:
            return GuardOutcome(
                action=ACTION_REJECTED,
                reason=REJECT_EMPTY_GUARD_RULE,
                detail="guard add requires at least one command or path rule",
            )
        stamp = created_at or clock.utc_iso_now()
        with self._lock:
            if self._expired:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_NO_ACTIVE_RUN,
                    detail="the run this guard names has already terminated",
                )
            if staged_paths and self._turn_snapshot_available is False:
                return GuardOutcome(
                    action=ACTION_REJECTED,
                    reason=REJECT_PROTECTED_PATH_EVIDENCE,
                    detail=(
                        "the turn in flight has no captured baseline, so a "
                        "protected-path guard could never be compared against "
                        "one; re-add it once this turn ends"
                    ),
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
            ] + [
                GuardRule(
                    id=new_guard_id(),
                    path=protected,
                    reason=reason,
                    created_at=stamp,
                )
                for protected in staged_paths
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

    def prepare_turn(
        self,
        env: Mapping[str, str] | None = None,
        *,
        harness: str | None = None,
        pretool_armed: bool = False,
    ) -> dict[str, str]:
        """Arm the overlay for the turn about to start and return its environment.

        Called before every worker turn. The directory is created even when no
        guard is active, so a guard added mid-turn installs into a ``PATH``
        entry the running process already has; the policy only exists while
        something is denied, which is what makes an inherited shim from an
        earlier turn inert rather than stale.

        The returned mapping is the parent environment with ``PATH`` rewritten
        and ``ASL_GUARD_POLICY`` added, so a pre-tool hook reads the same
        derived policy the shims do instead of re-deriving it from the run
        directory it would otherwise have to guess.

        The guard set is snapshotted here as this turn's "existed at process
        start" cohort: it is the only cohort a fixed command-line deny rule can
        ever cover, and coverage that claimed otherwise for a mid-turn addition
        would be the exact lie an operator would act on.

        Whether the overlay was actually armed is recorded before the
        environment is returned, because this method fails open: a turn that
        could not be guarded still runs, and the only thing that keeps that
        from becoming a false claim of enforcement is that coverage reads the
        same verdict this call wrote.
        """
        base = dict(os.environ if env is None else env)
        with self._lock:
            self._harness = harness
            self._pretool_armed = pretool_armed
            self._process_start_ids = frozenset(self._guards)
            guard_bin = self.guard_bin
            try:
                guard_bin.mkdir(parents=True, exist_ok=True)
                if self._guards and not self._expired:
                    self._materialize_locked(self._guards, self._executables)
                else:
                    self.policy_file.unlink(missing_ok=True)
                overlay_path = child_path(base.get("PATH"), guard_bin)
                policy_file = str(self.policy_file)
            except OSError:
                # An overlay that cannot be armed must not cost the turn. The
                # guards stay in the active set and the next turn tries again;
                # what is lost is enforcement, and every lane that depended on
                # this overlay now says so instead of reporting a gate the
                # worker's environment does not contain.
                self._overlay_armed = False
                return base
            self._overlay_armed = True
        base["PATH"] = overlay_path
        base[GUARD_POLICY_ENV_VAR] = policy_file
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
                workspace_aliases=self._workspace_aliases,
            ),
        )
        for name in sorted(
            {rule.executable for rule in guards.values() if rule.executable is not None}
        ):
            install_shim(
                guard_bin=self.guard_bin,
                name=name,
                policy_file=self.policy_file,
            )

    def _restore_locked(self) -> None:
        """Best-effort return of the policy file to the authoritative set."""
        with contextlib.suppress(OSError):
            self._materialize_locked(self._guards, self._executables)
