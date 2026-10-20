"""Opt-in live acceptance for interruption, continuity, and correction delivery.

Every other test in this repository proves ASL's redirect machinery against
fakes. That is the right default — a suite that spent provider quota could not
run on every commit — but it leaves one claim unproven: that the *real* Codex,
Claude, and Copilot CLIs behave the way the fakes were written to behave. This
module is where that claim is checked, and it is deliberately the only place in
the tree that can spend a token.

It is therefore **explicitly opt-in and off by default**. Without ``--live`` or
``ASL_LIVE_ACCEPTANCE=1`` it launches no harness turn at all: it probes
versions (``<exe> --version``, the same model-free probe
:mod:`agent_session_loop.harness_compatibility` uses), reports every scenario as
``skipped``, and exits 0. That is what lets the scheduled compatibility lane and
this session's own gate run it without model usage, and it is why the module
lives outside ``tests/run_suite.py``'s shard lists and is named so that
``unittest discover`` never picks it up.

The acceptance matrix, one row per property the shared contract in the plan's
``context.md`` states, each proved against every installed harness:

``earlier-context-survives``
    A resumed turn recalls a passphrase that exists only in the previous turn's
    conversation and was never written to disk. Reading it back off the
    filesystem is explicitly ruled out by the prompt, so a pass cannot be
    manufactured by a worker that simply looked around.

``existing-edits-remain``
    A file the interrupted turn wrote before it was killed is still there
    afterwards, with its contents intact.

``active-work-stops``
    Arming the turn's :class:`~agent_session_loop.harnesses.AbortSignal` while a
    long-running shell tool is in flight ends the turn in the ``execution``
    window with returncode 137.

``native-cancellation``
    The harness's *own* interruption — ``turn/interrupt``, the Claude control
    request, ``session/cancel`` — ends the turn without a kill. Driven straight
    through :meth:`HarnessTransport.interrupt`, with the forced kill held back
    as a safety net so a harness that ignores its own protocol is reported as a
    failure rather than hidden by the kill that follows it in production.

``forced-termination``
    After the deadline fires, the worker's process group and every descendant
    tracked while it was alive are *confirmed* stopped through
    :func:`agent_session_loop.process_tree.confirm_termination`. "Killed" and
    "collected" are different claims and this row is the second one.

``correction-reaches-continuation``
    A correction is submitted to a real redirect journal, accepted, carried, and
    rendered by the real producer
    (:func:`agent_session_loop.run_prompts._correction_context_body`). The
    resumed worker must echo back the ``--attempt`` token and ``--redirect-id``
    the journal minted, and the echoed token is then fed to the real
    :func:`agent_session_loop.redirect_ack.acknowledge_correction`, which
    refuses anything that does not name the carrying attempt. A hand-written
    imitation of the correction block would prove nothing about the block the
    runner actually sends; this sends that block.

``unavailable-history-requires-user``
    Resuming a conversation the harness has never heard of fails instead of
    silently starting a fresh one. The policy half — that this pauses for user
    input rather than retrying — is deterministic and lives in
    ``tests/test_asl_harness_compatibility.py``; what can only be learned from
    the real binary is that it refuses at all.

Usage::

    python3 -m tests.asl_live_redirect_acceptance --all-harnesses
    python3 -m tests.asl_live_redirect_acceptance --all-harnesses --live
    python3 -m tests.asl_live_redirect_acceptance --harness codex --live --json

Exit codes: 0 when nothing failed (including a run that was never opted in),
1 when any scenario failed, 2 for a usage error.

The report records the exact versions that were exercised and says, in the
document itself, that no other version is claimed verified. The pinned window in
:mod:`agent_session_loop.harness_compatibility` is a support *policy*; this
report is the only *evidence*, and the two must not be read as the same thing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_session_loop import (
    harness_compatibility,
    harnesses,
    process_tree,
    redirect_ack,
    redirect_ack_attempt,
    redirect_ack_carry,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
    run_prompts,
)
from agent_session_loop.harnesses import HARNESS_NAMES, HarnessName

#: Schema version of :func:`build_report`'s document.
SCHEMA_VERSION = 1

#: The environment switch that permits live harness turns. ``--live`` is the
#: other half; either one is enough, and without both nothing is launched.
OPT_IN_ENV_VAR = "ASL_LIVE_ACCEPTANCE"
_OPT_IN_TRUE = frozenset({"1", "true", "yes", "on"})

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

SCENARIO_CONTEXT = "earlier-context-survives"
SCENARIO_EDITS = "existing-edits-remain"
SCENARIO_STOPS = "active-work-stops"
SCENARIO_NATIVE_CANCEL = "native-cancellation"
SCENARIO_FORCED = "forced-termination"
SCENARIO_CORRECTION = "correction-reaches-continuation"
SCENARIO_HISTORY = "unavailable-history-requires-user"


@dataclass(frozen=True)
class Scenario:
    """One row of the acceptance matrix and the claim it settles."""

    key: str
    proves: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        SCENARIO_CONTEXT,
        "a resumed turn recalls what only the prior turn's conversation holds",
    ),
    Scenario(
        SCENARIO_EDITS,
        "files written before the interruption survive it unchanged",
    ),
    Scenario(
        SCENARIO_STOPS,
        "arming the abort ends an in-flight turn in the execution window",
    ),
    Scenario(
        SCENARIO_NATIVE_CANCEL,
        "the harness's own interruption ends the turn without a forced kill",
    ),
    Scenario(
        SCENARIO_FORCED,
        "the worker group and its tracked descendants are confirmed stopped",
    ),
    Scenario(
        SCENARIO_CORRECTION,
        "the journal's correction block reaches the resumed worker verbatim",
    ),
    Scenario(
        SCENARIO_HISTORY,
        "an unknown conversation is refused rather than silently replaced",
    ),
)

SCENARIO_KEYS: tuple[str, ...] = tuple(scenario.key for scenario in SCENARIOS)

#: Wall-clock ceiling for one live harness turn. Generous: a cold provider
#: start on a loaded machine is slow, and a turn that times out is reported as
#: a failed scenario rather than as a hang.
TURN_TIMEOUT_SECONDS = 300

#: How long the synthetic "long-running tool" sleeps. Long enough that no
#: harness can finish it before the interruption lands, and irrelevant
#: afterwards because the process group is killed.
LONG_TOOL_SECONDS = 600

#: How long to wait for the worker to actually start that tool before giving
#: up. This is the slow part of a live turn — the model has to decide to run it.
TOOL_START_TIMEOUT_SECONDS = 240.0
TOOL_START_POLL_SECONDS = 0.5

#: Breathing room between seeing the tool's marker file and interrupting, so
#: the interruption lands inside the tool rather than racing its first syscall.
INTERRUPT_SETTLE_SECONDS = 2.0

#: How long a native interruption is given to end the turn on its own before
#: the forced kill is used as a safety net and the scenario is failed.
NATIVE_CANCEL_WAIT_SECONDS = 90.0

#: How long to wait for the abort to report the turn fully released.
TERMINATION_WAIT_SECONDS = 60.0

#: The redirect TTL for the acceptance correction. The default 120 seconds is
#: shorter than a live turn, which would expire the correction before the
#: interrupted turn even ended.
CORRECTION_TTL_SECONDS = redirect_store_vocab.REDIRECT_MAX_TTL_SECONDS

NOTES_FILE = "notes.txt"
RUNNING_FILE = "running.txt"
RESUMED_FILE = "resumed.txt"
FORBIDDEN_FILE = "should-not-exist.txt"

ABORT_REASON = "live-acceptance-redirect"

#: The trivial turn that establishes a harness can do any work at all.
READY_TOKEN = "READY"
READY_TIMEOUT_SECONDS = 120

#: Phrases that mean the provider refused the turn rather than the harness
#: mishandling it. Copilot answers an exhausted monthly quota with exit status
#: 0 and the refusal in its *final message*, which is indistinguishable from a
#: successful turn by every field a runner normally reads -- so without this
#: scan an exhausted account is reported as six confident findings about a
#: harness that never got to run.
PROVIDER_UNAVAILABLE_MARKERS = (
    "exceeded your monthly quota",
    "quota",
    "rate limit",
    "usage limit",
    "insufficient_quota",
    "not logged in",
    "please log in",
    "please sign in",
    "unauthorized",
    "authentication failed",
    "credit balance",
)

#: How much of a worker's final message to carry into the report, so a failure
#: is diagnosable from the evidence rather than from a rerun.
FINAL_MESSAGE_EXCERPT_CHARS = 400

VERIFICATION_NOTE = (
    "Only the exact harness versions listed under verified_versions were "
    "exercised live by this run. The pinned window in "
    "agent_session_loop/harness_compatibility.py is a support policy, not "
    "evidence: no other version is claimed verified by this report."
)


def live_opt_in(env: Mapping[str, str] | None = None, *, flag: bool = False) -> bool:
    """Whether this process may launch real harness turns.

    Two independent switches, either of which suffices, because the two callers
    are different: a person running the matrix by hand passes ``--live``, and a
    scheduled lane that has been authorized to spend quota sets the environment
    variable once for the job rather than editing every command in it.
    """
    if flag:
        return True
    source = os.environ if env is None else env
    return source.get(OPT_IN_ENV_VAR, "").strip().lower() in _OPT_IN_TRUE


@dataclass
class ScenarioResult:
    """One matrix cell: what was claimed, what happened, and how long it took."""

    key: str
    status: str
    detail: str
    seconds: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "scenario": self.key,
            "status": self.status,
            "detail": self.detail,
            "seconds": round(self.seconds, 3),
            "evidence": self.evidence,
        }


@dataclass
class HarnessAcceptance:
    """Everything one harness's acceptance run learned about it."""

    harness: HarnessName
    executable: str | None
    version: str | None
    compatibility: str
    compatibility_reason: str
    capabilities: dict[str, Any]
    results: list[ScenarioResult]
    limitations: list[str]
    seconds: float

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FAILED)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_PASSED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    def payload(self) -> dict[str, Any]:
        policy = harness_compatibility.POLICY[self.harness]
        return {
            "harness": self.harness,
            "executable": self.executable,
            "version": self.version,
            "compatibility": self.compatibility,
            "compatibility_reason": self.compatibility_reason,
            "policy_window": {
                "latest": policy.latest,
                "previous": policy.previous,
                "minimum": policy.minimum,
            },
            "capabilities": self.capabilities,
            "scenarios": [result.payload() for result in self.results],
            "counts": {
                STATUS_PASSED: self.passed,
                STATUS_FAILED: self.failed,
                STATUS_SKIPPED: self.skipped,
            },
            "limitations": list(self.limitations),
            "seconds": round(self.seconds, 3),
        }


class _Matrix:
    """Collects one harness's scenario results in declaration order.

    Every scenario is reported exactly once, whatever happened: a sequence that
    raises halfway through still has to say what it did not get to, because a
    missing row and a passing row are indistinguishable in a report that only
    lists what succeeded.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, ScenarioResult] = {}

    def record(
        self,
        key: str,
        status: str,
        detail: str,
        *,
        seconds: float = 0.0,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        if key in self._by_key:
            return
        self._by_key[key] = ScenarioResult(
            key=key,
            status=status,
            detail=detail,
            seconds=seconds,
            evidence=dict(evidence or {}),
        )

    def verdict(
        self,
        key: str,
        ok: bool,
        detail: str,
        *,
        seconds: float = 0.0,
        evidence: Mapping[str, Any] | None = None,
    ) -> bool:
        self.record(
            key,
            STATUS_PASSED if ok else STATUS_FAILED,
            detail,
            seconds=seconds,
            evidence=evidence,
        )
        return ok

    def fill(self, status: str, detail: str) -> None:
        """Give every scenario that has no result yet the same outcome."""
        for key in SCENARIO_KEYS:
            self.record(key, status, detail)

    def results(self) -> list[ScenarioResult]:
        return [self._by_key[key] for key in SCENARIO_KEYS if key in self._by_key]


# fmt: off
__all__ = ("argparse", "json", "os", "shutil", "subprocess", "sys", "tempfile", "threading", "time", "uuid", "Callable", "Iterator", "Mapping", "Sequence", "contextmanager", "dataclass", "field", "Path", "Any", "harness_compatibility", "harnesses", "process_tree", "redirect_ack", "redirect_ack_attempt", "redirect_ack_carry", "redirect_store_models", "redirect_store_requests", "redirect_store_vocab", "run_prompts", "HARNESS_NAMES", "HarnessName", "SCHEMA_VERSION", "OPT_IN_ENV_VAR", "_OPT_IN_TRUE", "STATUS_PASSED", "STATUS_FAILED", "STATUS_SKIPPED", "SCENARIO_CONTEXT", "SCENARIO_EDITS", "SCENARIO_STOPS", "SCENARIO_NATIVE_CANCEL", "SCENARIO_FORCED", "SCENARIO_CORRECTION", "SCENARIO_HISTORY", "Scenario", "SCENARIOS", "SCENARIO_KEYS", "TURN_TIMEOUT_SECONDS", "LONG_TOOL_SECONDS", "TOOL_START_TIMEOUT_SECONDS", "TOOL_START_POLL_SECONDS", "INTERRUPT_SETTLE_SECONDS", "NATIVE_CANCEL_WAIT_SECONDS", "TERMINATION_WAIT_SECONDS", "CORRECTION_TTL_SECONDS", "NOTES_FILE", "RUNNING_FILE", "RESUMED_FILE", "FORBIDDEN_FILE", "ABORT_REASON", "READY_TOKEN", "READY_TIMEOUT_SECONDS", "PROVIDER_UNAVAILABLE_MARKERS", "FINAL_MESSAGE_EXCERPT_CHARS", "VERIFICATION_NOTE", "live_opt_in", "ScenarioResult", "HarnessAcceptance", "_Matrix")
# fmt: on
