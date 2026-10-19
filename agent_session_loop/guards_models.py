# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
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

from . import artifacts, clock, command_guard, durability

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


# Imported lazily after the rule module can see these dataclasses.
from .guards_rules import _compile_path_pattern
