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


@dataclass(frozen=True)
class Workspace:
    """A throwaway repository plus the plan and state directories beside it."""

    root: Path
    repo: Path
    plan_dir: Path
    state_dir: Path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@contextmanager
def disposable_workspace(label: str, *, keep: bool = False) -> Iterator[Workspace]:
    """Create a throwaway git repository and tear it down afterwards.

    A real repository rather than a bare directory: every harness ASL launches
    behaves differently inside version control (Codex reports the diff, Claude
    reads the status, Copilot scopes its writes), so a check that ran outside
    one would be exercising a configuration no run ever uses.
    """
    root = Path(tempfile.mkdtemp(prefix=f"asl-live-{label}-")).resolve()
    try:
        repo = root / "repo"
        plan_dir = root / "plan"
        state_dir = root / "state"
        for path in (repo, plan_dir, state_dir):
            path.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "acceptance@example.invalid")
        _git(repo, "config", "user.name", "ASL Live Acceptance")
        (repo / "README.md").write_text(
            "Disposable repository for an ASL live acceptance check.\n",
            encoding="utf-8",
        )
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-q", "-m", "seed")
        yield Workspace(root=root, repo=repo, plan_dir=plan_dir, state_dir=state_dir)
    finally:
        if keep:
            print(f"  workspace retained: {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


@dataclass
class TurnRun:
    """One live harness turn and the observations taken around it."""

    result: harnesses.HarnessResult
    seconds: float
    spawn_pid: int | None
    conversation_id: str | None
    native_interrupt: bool | None
    descendant_evidence: dict[str, Any] | None
    released: bool

    @property
    def final_message(self) -> str:
        """What the worker said, bounded, for the report's evidence."""
        return _excerpt(self.result.last_message)

    def summary(self) -> dict[str, Any]:
        return {
            "returncode": self.result.returncode,
            "aborted": self.result.aborted,
            "timed_out": self.result.timed_out,
            "cancel_stage": self.result.cancel_stage,
            "transport": self.result.transport,
            "conversation_id": self.conversation_id,
            "seconds": round(self.seconds, 3),
        }


class _LiveTurn:
    """Drives one real harness turn with a watcher thread alongside it.

    The watcher exists because every interesting property here is about timing:
    a turn has to be interrupted *while* it is doing something, which means
    something outside the turn has to notice that the work started. It watches
    for a marker file the prompt tells the worker to create as part of the
    long-running command itself, so "the tool is running" is observed rather
    than assumed from elapsed time.
    """

    def __init__(self, harness: HarnessName, workspace: Workspace) -> None:
        self.harness = harness
        self.workspace = workspace
        self.abort = harnesses.AbortSignal()
        self.spawned = threading.Event()
        self.pid: int | None = None
        self.transport: harnesses.HarnessTransport | None = None
        self.conversation_id: str | None = None
        self.native_interrupt: bool | None = None
        self.descendant_evidence: dict[str, Any] | None = None

    def _on_spawn(self, pid: int) -> None:
        self.pid = pid
        self.spawned.set()

    def _on_transport(self, transport: harnesses.HarnessTransport | None) -> None:
        self.transport = transport

    def _on_conversation(self, native_id: str) -> None:
        self.conversation_id = native_id

    def wait_for_file(self, name: str, timeout: float) -> bool:
        """Poll for a marker the worker creates, bounded by ``timeout``."""
        target = self.workspace.repo / name
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if target.exists():
                return True
            time.sleep(TOOL_START_POLL_SECONDS)
        return target.exists()

    def collect_evidence(self) -> dict[str, Any]:
        """Record the worker's descendants while it is still alive.

        Taken before any kill on purpose: killing the group reparents an
        escapee to pid 1, after which no descendant walk can reach it, so the
        walk that makes :func:`process_tree.confirm_termination` meaningful has
        to happen first.
        """
        pid = self.pid
        evidence: dict[str, Any] = (
            {"collected": False, "root_pid": None, "entries": []}
            if pid is None
            else process_tree.collect_descendant_evidence(pid)
        )
        self.descendant_evidence = evidence
        return evidence

    def request_native_interrupt(self) -> bool:
        """Ask the harness's own protocol to stop the work it has in flight."""
        transport = self.transport
        if transport is None:
            self.native_interrupt = False
            return False
        try:
            answered = bool(transport.interrupt())
        except Exception:  # noqa: BLE001 - a refused interrupt is an observation
            answered = False
        self.native_interrupt = answered
        return answered

    def run(
        self,
        prompt: str,
        *,
        watch: Callable[[_LiveTurn], None] | None,
        resume_conversation_id: str | None = None,
        timeout: int = TURN_TIMEOUT_SECONDS,
    ) -> TurnRun:
        """Run one turn. ``watch`` has no default deliberately.

        The first live run of this module failed three scenarios because a
        watcher was defined and never passed: the turn simply ran to its timeout
        and every interruption row reported "not stopped". Making the parameter
        required means a caller has to say ``watch=None`` to mean it.
        """
        adapter = harnesses.get_adapter(self.harness)
        # Outside the repository on purpose: the last-message sink is runner
        # bookkeeping, and a file appearing in the worktree would muddy the
        # "existing edits remain" row it sits next to.
        last_message_path = self.workspace.root / f"last-message-{uuid.uuid4().hex}.txt"
        watcher: threading.Thread | None = None
        if watch is not None:

            def observe() -> None:
                try:
                    watch(self)
                except Exception as error:  # noqa: BLE001 - never wedge the turn
                    print(f"  watcher failed: {type(error).__name__}: {error}")

            watcher = threading.Thread(
                target=observe, name="live-acceptance-watch", daemon=True
            )
        started = time.monotonic()
        if watcher is not None:
            watcher.start()
        try:
            result = adapter.run(
                cwd=self.workspace.repo,
                plan_dir=self.workspace.plan_dir,
                prompt=prompt,
                unsafe_bypass=True,
                last_message_path=last_message_path,
                timeout=timeout,
                abort=self.abort,
                on_spawn=self._on_spawn,
                on_transport=self._on_transport,
                on_conversation=self._on_conversation,
                resume_conversation_id=resume_conversation_id,
            )
        finally:
            if watcher is not None:
                watcher.join(timeout=5.0)
        seconds = time.monotonic() - started
        released = (
            True
            if not self.abort.armed
            else self.abort.wait_completed(TERMINATION_WAIT_SECONDS)
        )
        return TurnRun(
            result=result,
            seconds=seconds,
            spawn_pid=self.pid,
            conversation_id=self.conversation_id or result.conversation_id,
            native_interrupt=self.native_interrupt,
            descendant_evidence=self.descendant_evidence,
            released=bool(released),
        )


def first_turn_prompt(secret: str, nonce: str) -> str:
    """The synthetic prompt for the turn that will be interrupted."""
    return "\n".join(
        [
            "You are the subject of an automated acceptance check. You are "
            "running inside a disposable throwaway git repository created for "
            "this check and deleted immediately afterwards. Nothing here is "
            "real work and nothing outside this directory is in scope.",
            "",
            f"Remember this passphrase for the rest of this conversation: {secret}",
            "",
            "Do not write the passphrase into any file, and do not repeat it in "
            "your final message. A later turn will ask you to recall it.",
            "",
            "Now do exactly these two steps, in order, and nothing else:",
            "",
            f"1. Create a file named `{NOTES_FILE}` in the current directory "
            f"whose only line is exactly: NONCE={nonce}",
            "2. Run exactly this shell command in the foreground and wait for "
            f"it to finish: touch {RUNNING_FILE} && sleep {LONG_TOOL_SECONDS}",
            "",
            "This turn is expected to be interrupted while step 2 is still "
            "running. The interruption is what the check measures, so do not "
            "work around it: do not shorten the sleep, do not background it, "
            "and do not replace it with anything else.",
        ]
    )


def correction_text() -> str:
    """The operator feedback the redirect journal carries for the resumed turn.

    Written as an operator would write it — it corrects the interrupted work
    rather than describing the test — because it is delivered through the real
    correction block, where prompt-shaped text is explicitly treated as
    correction content and not as runner-owned structure.
    """
    return "\n".join(
        [
            "Stop waiting on the `sleep` command from the previous turn. It was "
            "interrupted on purpose and must not be restarted.",
            "",
            "This is an offline acceptance check with no live run behind it, so "
            "do NOT run the acknowledge command printed below this correction: "
            "there is nothing for it to report to and it will fail. Report your "
            "receipt in a file instead.",
            "",
            f"Create a file named `{RESUMED_FILE}` in the current directory "
            "containing exactly these three lines and nothing else:",
            "",
            "SECRET=<the passphrase you were asked to remember earlier in this "
            "conversation>",
            "ATTEMPT=<the exact value of the --attempt argument in the "
            "acknowledge command printed below this correction>",
            "OPERATION=<the exact value of the --redirect-id argument in that "
            "same command>",
            "",
            "If you have no memory of a passphrase from an earlier turn in this "
            "conversation, write SECRET=UNKNOWN. Do not guess it and do not "
            "search the filesystem for it: it was never written to disk.",
            "",
            "Then reply with the single word ACKNOWLEDGED.",
        ]
    )


def resume_turn_prompt(correction_block: Sequence[str]) -> str:
    """Wrap the real correction block as the resumed turn's whole prompt."""
    return "\n".join(
        [
            "## Operator correction",
            "",
            *correction_block,
            "",
            "---",
            "",
            "The correction above is your entire remaining task. Apply it and stop.",
        ]
    )


def native_cancel_prompt() -> str:
    """A turn whose only job is to sit inside a long-running shell tool."""
    return "\n".join(
        [
            "You are the subject of an automated acceptance check inside a "
            "disposable throwaway git repository.",
            "",
            "Run exactly this shell command in the foreground and wait for it to "
            f"finish: touch {RUNNING_FILE} && sleep {LONG_TOOL_SECONDS}",
            "",
            "Then reply with the single word DONE. Do not background the "
            "command, do not shorten it, and do nothing else.",
        ]
    )


def unknown_history_prompt() -> str:
    """A turn that must never run, because the conversation it names is absent."""
    return (
        "Create a file named "
        f"`{FORBIDDEN_FILE}` in the current directory containing the single "
        "word RAN, then reply DONE."
    )


#: Words that make a refusal attributable to the missing conversation rather
#: than to some unrelated failure that happened to stop the same turn.
_HISTORY_WORDS = ("conversation", "session", "resume", "rollout", "thread")


def _mentions_conversation(text: str, conversation_id: str) -> bool:
    """Whether ``text`` blames the conversation for the failure it reports."""
    lowered = text.lower()
    if conversation_id.lower() in lowered:
        return True
    return any(word in lowered for word in _HISTORY_WORDS)


def provider_refusal(text: str) -> str | None:
    """The phrase that marks ``text`` as a provider refusal, or ``None``.

    Scanned against a deliberately tiny reply, so a marker appearing here means
    the provider is talking rather than the worker.
    """
    lowered = text.lower()
    for marker in PROVIDER_UNAVAILABLE_MARKERS:
        if marker in lowered:
            return marker
    return None


def _excerpt(text: str | None) -> str:
    """A bounded, single-paragraph view of a worker's final message."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= FINAL_MESSAGE_EXCERPT_CHARS:
        return collapsed
    return collapsed[:FINAL_MESSAGE_EXCERPT_CHARS] + "..."


def ready_prompt() -> str:
    """The cheapest turn that still proves the harness can answer at all."""
    return (
        f"Reply with the single word {READY_TOKEN} and do nothing else. "
        "Do not use any tools and do not write any files."
    )


def check_reachable(harness: HarnessName, *, keep: bool = False) -> tuple[bool, str]:
    """Whether ``harness`` can complete a trivial turn, and why not if it cannot.

    This gate exists because "the provider would not serve this turn" and "the
    harness mishandled the turn" are different findings with different owners,
    and only one of them is this module's business. Without it an exhausted
    quota or a signed-out CLI is reported as a pile of protocol defects in a
    harness that never ran a single tool.
    """
    with disposable_workspace(f"{harness}-ready", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)
        try:
            run = turn.run(ready_prompt(), watch=None, timeout=READY_TIMEOUT_SECONDS)
        except harnesses.HarnessError as error:
            return False, f"the harness refused a trivial turn: {error}"
        message = run.result.last_message or ""
        combined = f"{message}\n{run.result.stderr or ''}"
        if run.result.timed_out:
            return False, (
                f"a trivial turn did not finish within {READY_TIMEOUT_SECONDS}s"
            )
        if run.result.returncode != 0:
            return False, (
                f"a trivial turn exited {run.result.returncode}: {_excerpt(combined)}"
            )
        marker = provider_refusal(combined)
        if marker is not None:
            return False, (
                f"the provider refused the turn ({marker}): {_excerpt(message)}"
            )
        if READY_TOKEN.lower() not in combined.lower():
            return False, (
                f"a trivial turn succeeded without answering it: {_excerpt(message)}"
            )
        return True, ""


def _read_lines(path: Path) -> dict[str, str]:
    """Parse the resumed worker's ``KEY=value`` receipt file."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip().upper()] = value.strip().strip("`\"'")
    return values


def _seed_correction(
    workspace: Workspace,
    *,
    harness: HarnessName,
    run_id: str,
) -> tuple[
    redirect_store_models.RedirectTarget,
    redirect_store_models.RedirectTarget,
    str,
    list[str],
]:
    """Submit, accept, and carry one correction through the real journal.

    Returns the interrupted turn's target, the continuation attempt, the
    operation id, and the correction block exactly as the runner would render
    it into a resumed prompt.
    """
    target = redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id="live-acceptance",
        turn_index=1,
        harness=harness,
    )
    continuation = redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id="live-acceptance",
        turn_index=2,
        harness=harness,
    )
    request = redirect_store_requests.submit_request(
        workspace.plan_dir,
        target=target,
        feedback=correction_text(),
        state_dir=workspace.state_dir,
        ttl_seconds=CORRECTION_TTL_SECONDS,
    )
    redirect_store_requests.accept_request(
        workspace.plan_dir,
        request.operation_id,
        target=target,
        state_dir=workspace.state_dir,
    )
    carried = redirect_ack.carry_corrections(
        workspace.plan_dir,
        continuation=continuation,
        accepted_operation_ids=(request.operation_id,),
        state_dir=workspace.state_dir,
    )
    block = run_prompts._correction_context_body(
        carried,
        plan_dir=workspace.plan_dir,
        state_dir=workspace.state_dir,
    )
    return target, continuation, request.operation_id, list(block)


def _interrupt_and_continue(
    harness: HarnessName, matrix: _Matrix, *, keep: bool
) -> dict[str, Any]:
    """Interrupt a working turn, then resume it carrying a real correction.

    One workspace and one conversation across two turns, because that is the
    only shape in which "earlier context survived" and "the edits are still
    there" are answerable at all.
    """
    observed: dict[str, Any] = {}
    secret = f"PASSPHRASE-{uuid.uuid4().hex[:12].upper()}"
    nonce = uuid.uuid4().hex[:12].upper()
    run_id = f"live-{uuid.uuid4().hex[:12]}"
    with disposable_workspace(f"{harness}-continue", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)

        def watch(live: _LiveTurn) -> None:
            if not live.wait_for_file(RUNNING_FILE, TOOL_START_TIMEOUT_SECONDS):
                live.abort.arm("live-acceptance-tool-never-started")
                return
            time.sleep(INTERRUPT_SETTLE_SECONDS)
            live.collect_evidence()
            live.abort.arm(ABORT_REASON)

        first = turn.run(first_turn_prompt(secret, nonce), watch=watch)
        observed["first_turn"] = first.summary()
        tool_started = (workspace.repo / RUNNING_FILE).exists()

        matrix.verdict(
            SCENARIO_STOPS,
            bool(
                tool_started
                and first.result.aborted
                and first.result.returncode == 137
                and first.result.cancel_stage == harnesses.CANCEL_STAGE_EXECUTION
            ),
            (
                "the abort ended the turn in the execution window"
                if first.result.aborted
                else "the turn was not stopped by the abort"
            ),
            seconds=first.seconds,
            evidence={
                "tool_started": tool_started,
                "returncode": first.result.returncode,
                "cancel_stage": first.result.cancel_stage,
                "aborted": first.result.aborted,
                "released": first.released,
                # Which request was made, so "nothing armed the abort" is never
                # read as "the harness ignored it".
                "abort_reason": turn.abort.reason or None,
                "timed_out": first.result.timed_out,
                "final_message": first.final_message,
            },
        )

        collected = first.descendant_evidence or {}
        # The walk records the group it actually saw; the spawn pid is only a
        # fallback for the case where the worker exited before it was sampled.
        pgid = collected.get("root_pgid") or first.spawn_pid or 0
        confirmation = process_tree.confirm_termination(
            pgid=int(pgid), evidence=collected
        )
        evidence_collected = bool(collected.get("collected"))
        matrix.verdict(
            SCENARIO_FORCED,
            bool(first.released and evidence_collected and confirmation["complete"]),
            (
                "the worker group and its tracked descendants are confirmed stopped"
                if confirmation["complete"]
                else "termination could not be confirmed: "
                f"{confirmation['incomplete_reason']}"
            ),
            seconds=first.seconds,
            evidence={
                "evidence_collected": evidence_collected,
                "tracked_descendants": len(collected.get("entries") or ()),
                "escaped_pids": list(collected.get("escaped_pids") or ()),
                "confirmation": confirmation,
                "released": first.released,
            },
        )

        notes = workspace.repo / NOTES_FILE
        notes_text = notes.read_text(encoding="utf-8") if notes.exists() else ""
        matrix.verdict(
            SCENARIO_EDITS,
            f"NONCE={nonce}" in notes_text,
            (
                f"{NOTES_FILE} written before the interruption survived it"
                if f"NONCE={nonce}" in notes_text
                else f"{NOTES_FILE} did not survive with its recorded nonce"
            ),
            evidence={
                "file": NOTES_FILE,
                "present": notes.exists(),
                "final_message": first.final_message,
            },
        )

        conversation_id = first.conversation_id
        observed["conversation_id"] = conversation_id
        if not conversation_id:
            matrix.record(
                SCENARIO_CONTEXT,
                STATUS_FAILED,
                "the interrupted turn named no conversation to continue",
            )
            matrix.record(
                SCENARIO_CORRECTION,
                STATUS_FAILED,
                "no conversation was captured, so nothing could be continued",
            )
            return observed

        _target, continuation, operation_id, block = _seed_correction(
            workspace, harness=harness, run_id=run_id
        )
        resumed_turn = _LiveTurn(harness, workspace)
        second = resumed_turn.run(
            resume_turn_prompt(block),
            watch=None,
            resume_conversation_id=conversation_id,
        )
        observed["resumed_turn"] = second.summary()
        receipt = _read_lines(workspace.repo / RESUMED_FILE)

        matrix.verdict(
            SCENARIO_CONTEXT,
            receipt.get("SECRET") == secret,
            (
                "the resumed turn recalled a passphrase that was never on disk"
                if receipt.get("SECRET") == secret
                else "the resumed turn did not recall the prior turn's passphrase"
            ),
            seconds=second.seconds,
            evidence={
                "resumed_conversation": second.conversation_id,
                "requested_conversation": conversation_id,
                "secret_recalled": receipt.get("SECRET") == secret,
                "secret_reported_unknown": receipt.get("SECRET") == "UNKNOWN",
                "returncode": second.result.returncode,
                "final_message": second.final_message,
            },
        )

        echoed_attempt = receipt.get("ATTEMPT", "")
        echoed_operation = receipt.get("OPERATION", "")
        acknowledged = False
        ack_detail = "the worker echoed no attempt token"
        if echoed_attempt and echoed_operation == operation_id:
            try:
                redirect_ack.acknowledge_correction(
                    workspace.plan_dir,
                    operation_id,
                    token=echoed_attempt,
                    live_attempt=continuation,
                    state_dir=workspace.state_dir,
                )
            except redirect_ack.RedirectAckError as error:
                ack_detail = f"the journal refused the receipt: {error}"
            else:
                acknowledged = True
                ack_detail = (
                    "the correction reached the resumed worker and the journal "
                    "accepted its receipt"
                )
        elif echoed_operation and echoed_operation != operation_id:
            ack_detail = "the worker echoed a different operation id"

        matrix.verdict(
            SCENARIO_CORRECTION,
            acknowledged,
            ack_detail,
            seconds=second.seconds,
            evidence={
                "operation_id": operation_id,
                "attempt": redirect_ack.attempt_token(continuation),
                "echoed_attempt": echoed_attempt,
                "echoed_operation": echoed_operation,
                "acknowledged": acknowledged,
                "final_message": second.final_message,
            },
        )
        return observed


def _native_cancellation(
    harness: HarnessName, matrix: _Matrix, *, keep: bool
) -> dict[str, Any]:
    """Stop a turn through the harness's own protocol, with no forced kill.

    The kill is held back rather than removed: a harness that ignores its own
    interruption would otherwise hang this check for the whole turn timeout,
    and the point is to learn *which* of the two stopped it.
    """
    observed: dict[str, Any] = {}
    with disposable_workspace(f"{harness}-cancel", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)
        answered: list[bool] = []
        ended_natively: list[bool] = []

        def watch(live: _LiveTurn) -> None:
            if not live.wait_for_file(RUNNING_FILE, TOOL_START_TIMEOUT_SECONDS):
                live.abort.arm("live-acceptance-tool-never-started")
                return
            time.sleep(INTERRUPT_SETTLE_SECONDS)
            answered.append(live.request_native_interrupt())
            # The safety net: if the harness's own cancellation does not end
            # the turn, arm the abort so the check reports a failure instead of
            # waiting out the turn timeout.
            if live.abort.wait_completed(NATIVE_CANCEL_WAIT_SECONDS):
                ended_natively.append(True)
                return
            live.abort.arm("live-acceptance-native-cancel-safety-net")

        run = turn.run(native_cancel_prompt(), watch=watch)
        observed["turn"] = run.summary()
        tool_started = (workspace.repo / RUNNING_FILE).exists()
        interrupt_answered = bool(answered and answered[0])
        # A turn that merely ran out its timeout also carries `aborted=False`,
        # so "no kill was needed" has to exclude it or the laziest possible
        # outcome would score as the harness cancelling cleanly.
        stopped_without_kill = not run.result.aborted and not run.result.timed_out
        matrix.verdict(
            SCENARIO_NATIVE_CANCEL,
            bool(tool_started and interrupt_answered and stopped_without_kill),
            (
                "the harness's own interruption ended the turn without a kill"
                if stopped_without_kill and interrupt_answered
                else "native cancellation did not end the turn on its own"
            ),
            seconds=run.seconds,
            evidence={
                "tool_started": tool_started,
                "transport": run.result.transport,
                "interrupt_answered": interrupt_answered,
                "forced_kill_needed": run.result.aborted,
                "returncode": run.result.returncode,
                "cancel_stage": run.result.cancel_stage,
                "abort_reason": turn.abort.reason or None,
                "timed_out": run.result.timed_out,
                "final_message": run.final_message,
            },
        )
        return observed


def _unavailable_history(
    harness: HarnessName, matrix: _Matrix, *, keep: bool
) -> dict[str, Any]:
    """Resume a conversation the harness has never heard of."""
    observed: dict[str, Any] = {}
    unknown = str(uuid.uuid4())
    with disposable_workspace(f"{harness}-absent", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)
        try:
            run = turn.run(
                unknown_history_prompt(),
                watch=None,
                resume_conversation_id=unknown,
                timeout=120,
            )
        except harnesses.HarnessError as error:
            # A refusal only counts when it is *about the conversation*. A
            # setup error — a missing plan directory, an unusable workspace —
            # raises the same exception type and would otherwise be scored as a
            # pass, which would make this the one row in the matrix that gets
            # easier to satisfy the more broken the environment is.
            about_history = _mentions_conversation(str(error), unknown)
            matrix.verdict(
                SCENARIO_HISTORY,
                about_history,
                (
                    f"the adapter refused the unknown conversation: {error}"
                    if about_history
                    else f"the turn could not be set up, so nothing was "
                    f"learned about missing history: {error}"
                ),
                evidence={
                    "refused_by": "adapter" if about_history else None,
                    "conversation_id": unknown,
                    "error": str(error),
                },
            )
            return observed
        observed["turn"] = run.summary()
        ran = (workspace.repo / FORBIDDEN_FILE).exists()
        # Same rule as the exception path: the run has to have stopped *and*
        # said the conversation is why.
        blamed = bool(run.result.conversation_error) or _mentions_conversation(
            f"{run.result.stderr}\n{run.result.stdout}", unknown
        )
        refused = run.result.returncode != 0 and blamed
        matrix.verdict(
            SCENARIO_HISTORY,
            bool(refused and not ran),
            (
                "an unknown conversation was refused rather than replaced"
                if refused and not ran
                else "the turn did not stop and blame the missing conversation"
            ),
            seconds=run.seconds,
            evidence={
                "requested_conversation": unknown,
                "returncode": run.result.returncode,
                "conversation_error": run.result.conversation_error,
                "continuity_unavailable": run.result.continuity_unavailable,
                "blamed_the_conversation": blamed,
                "work_performed": ran,
                "policy_note": (
                    "the pause-for-user-input half of this rule is deterministic "
                    "and is checked by tests.test_asl_harness_compatibility"
                ),
                "final_message": run.final_message,
            },
        )
        return observed


def run_harness_acceptance(
    harness: HarnessName,
    *,
    opted_in: bool,
    keep: bool = False,
) -> HarnessAcceptance:
    """Probe one harness and, when opted in, run the full matrix against it."""
    started = time.monotonic()
    matrix = _Matrix()
    limitations: list[str] = []
    compat = harness_compatibility.check_harness(harness)
    adapter = harnesses.get_adapter(harness)
    capabilities: dict[str, Any] = {
        "captures_conversations": bool(adapter.captures_conversations),
        "resumes_conversations": bool(adapter.resumes_conversations),
        "continues_conversations": harnesses.continues_conversations(harness),
        "native_interrupt": None,
        "transport": None,
        # None means "never asked", which is different from "asked and no".
        "reachable": None,
    }

    def finish() -> HarnessAcceptance:
        return HarnessAcceptance(
            harness=harness,
            executable=compat.executable,
            version=compat.version,
            compatibility=compat.status,
            compatibility_reason=compat.reason,
            capabilities=capabilities,
            results=matrix.results(),
            limitations=limitations,
            seconds=time.monotonic() - started,
        )

    if not opted_in:
        matrix.fill(
            STATUS_SKIPPED,
            f"not opted in; pass --live or set {OPT_IN_ENV_VAR}=1",
        )
        limitations.append(
            f"{harness}: no live turn was run, so nothing in this report is "
            "acceptance evidence for it"
        )
        return finish()
    if compat.executable is None:
        matrix.fill(STATUS_SKIPPED, f"{harness} is not installed on PATH")
        limitations.append(
            f"{harness}: not installed, so no version of it has been verified"
        )
        return finish()
    if compat.blocks:
        matrix.fill(
            STATUS_SKIPPED,
            f"{harness} {compat.version} is {compat.status}: {compat.detail}",
        )
        limitations.append(
            f"{harness}: installed version {compat.version} is known "
            "incompatible and was not exercised"
        )
        return finish()

    print(f"== {harness} {compat.version or 'unknown version'} ({compat.executable})")
    print(f"-- {harness}: reachability")
    reachable, why = check_reachable(harness, keep=keep)
    if not reachable:
        # Skipped, not failed. Nothing was learned about the harness, and a
        # gate that went red here would be reporting the state of an account.
        # Short per row, complete once: repeating a provider's error on all
        # seven rows buries the six other harnesses' results under it.
        matrix.fill(
            STATUS_SKIPPED, f"{harness} could not be exercised (see limitations)"
        )
        limitations.append(f"{harness}: {why}")
        capabilities["reachable"] = False
        print(f"   unreachable: {why}")
        return finish()
    capabilities["reachable"] = True
    sequences: tuple[
        tuple[str, Callable[[HarnessName, _Matrix], dict[str, Any]]], ...
    ] = (
        (
            "interrupt-and-continue",
            lambda name, m: _interrupt_and_continue(name, m, keep=keep),
        ),
        (
            "native-cancellation",
            lambda name, m: _native_cancellation(name, m, keep=keep),
        ),
        (
            "unavailable-history",
            lambda name, m: _unavailable_history(name, m, keep=keep),
        ),
    )
    for label, sequence in sequences:
        print(f"-- {harness}: {label}")
        try:
            observed = sequence(harness, matrix)
        except Exception as error:  # noqa: BLE001 - one sequence must not end the run
            limitations.append(
                f"{harness}: the {label} sequence raised "
                f"{type(error).__name__}: {error}"
            )
            print(f"   sequence failed: {type(error).__name__}: {error}")
            continue
        transport = (observed.get("turn") or observed.get("first_turn") or {}).get(
            "transport"
        )
        if transport and capabilities["transport"] is None:
            capabilities["transport"] = transport
    for result in matrix.results():
        if result.key == SCENARIO_NATIVE_CANCEL:
            capabilities["native_interrupt"] = result.evidence.get("interrupt_answered")
    matrix.fill(STATUS_FAILED, "the sequence that owns this scenario did not report")
    return finish()


def build_report(
    acceptances: Sequence[HarnessAcceptance],
    *,
    opted_in: bool,
    requested: Sequence[str],
    seconds: float,
) -> dict[str, Any]:
    """The stable JSON document this runner exists to produce."""
    counts = {STATUS_PASSED: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
    limitations: list[str] = []
    verified: dict[str, str | None] = {}
    for acceptance in acceptances:
        counts[STATUS_PASSED] += acceptance.passed
        counts[STATUS_FAILED] += acceptance.failed
        counts[STATUS_SKIPPED] += acceptance.skipped
        limitations.extend(acceptance.limitations)
        # A version is only "verified" when a live scenario actually passed
        # against it. An installed version nothing exercised is inventory.
        verified[acceptance.harness] = acceptance.version if acceptance.passed else None
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": counts[STATUS_FAILED] == 0,
        "opted_in": opted_in,
        "model_use": opted_in,
        "opt_in": {"env_var": OPT_IN_ENV_VAR, "flag": "--live"},
        "requested_harnesses": list(requested),
        "scenarios": [
            {"scenario": scenario.key, "proves": scenario.proves}
            for scenario in SCENARIOS
        ],
        "harnesses": [acceptance.payload() for acceptance in acceptances],
        "counts": counts,
        "verified_versions": verified,
        "verification_note": VERIFICATION_NOTE,
        "limitations": limitations,
        "seconds": round(seconds, 3),
    }


def format_report(report: Mapping[str, Any]) -> str:
    """A terminal rendering that names the consequence of every row."""
    marks = {STATUS_PASSED: "ok", STATUS_FAILED: "FAIL", STATUS_SKIPPED: "skip"}
    lines: list[str] = []
    if not report["opted_in"]:
        lines.append(
            "live acceptance is opt-in and was not enabled: no harness turn ran "
            f"and no model was used (pass --live or set {OPT_IN_ENV_VAR}=1)"
        )
        lines.append("")
    for entry in report["harnesses"]:
        version = entry["version"] or "unknown version"
        lines.append(
            f"{entry['harness']} {version} "
            f"[{entry['compatibility']}: {entry['compatibility_reason']}]"
        )
        for scenario in entry["scenarios"]:
            lines.append(
                f"  {marks[scenario['status']]:>4}  "
                f"{scenario['scenario']:<34} {scenario['seconds']:>7.1f}s  "
                f"{scenario['detail']}"
            )
        lines.append("")
    counts = report["counts"]
    lines.append(
        f"{counts[STATUS_PASSED]} passed, {counts[STATUS_FAILED]} failed, "
        f"{counts[STATUS_SKIPPED]} skipped in {report['seconds']:.1f}s"
    )
    verified = {k: v for k, v in report["verified_versions"].items() if v}
    lines.append(
        "verified versions: "
        + (
            ", ".join(f"{name} {version}" for name, version in sorted(verified.items()))
            if verified
            else "none"
        )
    )
    lines.append(report["verification_note"])
    for limitation in report["limitations"]:
        lines.append(f"  limitation: {limitation}")
    return "\n".join(lines)


def _harness_name(value: str) -> HarnessName:
    for name in HARNESS_NAMES:
        if name == value:
            return name
    raise argparse.ArgumentTypeError(
        f"unknown harness {value!r}; choose from {', '.join(HARNESS_NAMES)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m tests.asl_live_redirect_acceptance",
        description=(
            "Opt-in live acceptance for ASL interruption, conversation "
            "continuity, and urgent-correction delivery against the real "
            "harness CLIs."
        ),
    )
    parser.add_argument(
        "--harness",
        action="append",
        type=_harness_name,
        metavar="NAME",
        help="exercise one harness; repeatable",
    )
    parser.add_argument(
        "--all-harnesses",
        action="store_true",
        help="exercise every supported harness",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "actually launch harness turns. This spends provider quota. "
            f"{OPT_IN_ENV_VAR}=1 is the equivalent environment switch."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print the JSON report")
    parser.add_argument(
        "--report", metavar="PATH", help="write the JSON report to PATH"
    )
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="leave the disposable repositories on disk for inspection",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    requested: tuple[HarnessName, ...]
    if args.all_harnesses:
        requested = tuple(HARNESS_NAMES)
    elif args.harness:
        requested = tuple(dict.fromkeys(args.harness))
    else:
        parser.error("choose --all-harnesses or at least one --harness")
        return 2
    opted_in = live_opt_in(flag=bool(args.live))
    started = time.monotonic()
    acceptances = [
        run_harness_acceptance(
            harness, opted_in=opted_in, keep=bool(args.keep_workspaces)
        )
        for harness in requested
    ]
    report = build_report(
        acceptances,
        opted_in=opted_in,
        requested=requested,
        seconds=time.monotonic() - started,
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
