"""Harness adapter boundaries.

Adapters construct argv lists and run harnesses without shell interpolation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Literal

from . import claude_stream, codex_app_server, config, guards, guidance_delivery
from .process_tree import (
    clear_process_tree,
    descendant_pids,
    last_process_tree,
    sample_process_table,
    sweep_enabled,
    sweep_survivors,
)

HarnessName = Literal["codex", "claude", "copilot"]
HARNESS_NAMES: tuple[HarnessName, ...] = ("codex", "claude", "copilot")
StreamName = Literal["stdout", "stderr"]
StreamCallback = Callable[[StreamName, str], None]

AccessProfileName = Literal["worker", "audit-read-only"]

#: Claude tools that read or search without mutating anything.
CLAUDE_AUDIT_TOOLS = "Read,Glob,Grep"

#: Claude tools denied outright, so an audit turn cannot edit, shell out, reach
#: the network, spawn a subagent, or write session state.
CLAUDE_AUDIT_DENIED_TOOLS = (
    "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
)

#: Copilot tools that read or search. Verified against the installed CLI rather
#: than inferred from its help text, because an unknown name in
#: ``--available-tools`` silently yields an agent with no tools at all.
COPILOT_AUDIT_TOOLS = "view,rg,glob"

#: Copilot tool families denied outright. Denial beats any allow rule, so these
#: hold even if a future default widens ``--available-tools``.
COPILOT_AUDIT_DENIED_TOOLS = ("write", "shell", "url")


# A dead harness process closes its pipes, so the reader threads see EOF at
# once. This bound only exists for a descendant that outlives the harness and
# keeps the inherited pipes open. The post-turn sweep runs after this join and
# kills such a descendant if it is still in the turn's process group, but one
# that started a session of its own outlives both, so this caps what a turn
# pays to walk away from it (mirrors verification._ABANDONED_READER_JOIN_SECONDS).
_ABANDONED_READER_JOIN_SECONDS = 5.0

#: How often a turn with an abort signal attached re-checks its deadlines. The
#: wait is otherwise a single blocking call, so this is the whole cost of being
#: cancellable and the upper bound on how late a kill lands.
_ABORT_POLL_INTERVAL_SECONDS = 0.1


@dataclass
class AbortSignal:
    """A request to kill one in-flight harness turn at a monotonic deadline.

    Armed from another thread; read by the turn's wait loop. It lives in this
    module because the kill has to happen where the turn's ``process`` is in
    scope: signalling a bare PID from outside would bypass the pre-kill
    descendant snapshot and the post-turn sweep, which is how a descendant that
    left the process group is reached at all.
    """

    event: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None
    reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None:
        """Request the kill, giving the harness ``grace_seconds`` to wind down."""
        with self._lock:
            self.deadline = time.monotonic() + max(0.0, grace_seconds)
            self.reason = reason
        self.event.set()

    @property
    def armed(self) -> bool:
        return self.event.is_set()

    def expires_at(self, now: float) -> float | None:
        """When the kill is due, or ``None`` while this signal is unarmed.

        An armed signal carrying no deadline is due immediately, so a caller
        that only sets the event still gets a kill.
        """
        if not self.event.is_set():
            return None
        with self._lock:
            deadline = self.deadline
        return now if deadline is None else deadline


def _wait_for_turn_exit(
    process: subprocess.Popen,
    *,
    timeout: float | None,
    abort: AbortSignal | None,
) -> Literal["timeout", "abort"] | None:
    """Wait for ``process``; name the deadline that fired, or ``None`` if it exited.

    The two deadlines are mutually exclusive: the earlier one is reported and
    the other is never looked at again. Without an abort signal this is the
    single blocking wait it replaced, so an uncancellable turn wakes no more
    often than it used to.
    """
    if abort is None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return "timeout"
        return None

    timeout_deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            return None
        now = time.monotonic()
        abort_deadline = abort.expires_at(now)
        abort_due = abort_deadline is not None and now >= abort_deadline
        timeout_due = timeout_deadline is not None and now >= timeout_deadline
        if abort_due and timeout_due:
            return "timeout" if timeout_deadline <= abort_deadline else "abort"
        if abort_due:
            return "abort"
        if timeout_due:
            return "timeout"
        interval = _ABORT_POLL_INTERVAL_SECONDS
        if abort_deadline is not None:
            interval = min(interval, abort_deadline - now)
        if timeout_deadline is not None:
            interval = min(interval, timeout_deadline - now)
        try:
            process.wait(timeout=max(interval, 0.0))
        except subprocess.TimeoutExpired:
            continue
        return None


def _sweep_process_table() -> dict[int, dict[str, Any]] | None:
    """The table to snapshot descendants from, or ``None`` when sweeping is off."""
    if not sweep_enabled():
        return None
    return sample_process_table()


def _snapshot_descendants(
    root_pid: int, table: Mapping[int, Mapping[str, Any]] | None
) -> tuple[Mapping[str, Any], ...]:
    """Record ``root_pid``'s descendants while it is still alive.

    Killing the group reparents an escapee to pid 1, after which no descendant
    walk can reach it. A snapshot taken beforehand is the sweep's only route to
    one, so this must run before the kill, not after it.
    """
    if table is None:
        return ()
    return tuple(table[pid] for pid in descendant_pids(root_pid, table))


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill ``process`` and every descendant still in its process group.

    Harness turns spawn with ``start_new_session=True``, so the child's process
    group ID is its PID and every descendant inherits that group unless it
    starts a session of its own. Killing only the direct child leaves a harness
    that fanned out into helper processes running, still holding the captured
    pipes open (mirrors :func:`verification._kill_process_group`).
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        # The group is already empty, or the child left it for one of its own.
        with contextlib.suppress(OSError):
            process.kill()


def _sweep_turn_descendants(
    process: subprocess.Popen,
    prior_snapshot: tuple[Mapping[str, Any], ...] = (),
) -> dict[str, Any] | None:
    """Kill whatever outlived the turn, or ``None`` when the sweep is off.

    ``expected_pgid`` is the child's PID because turns spawn with
    ``start_new_session=True``. ``root_pid`` is already reaped by the caller, so
    the sweep excludes it rather than signalling a PID that may be reused.

    Any tree the hang sentinel sampled for this same root is unioned into the
    snapshot: on the paths that take no pre-kill snapshot of their own it is
    the sweep's only route to a descendant that left the process group, and it
    exists exactly for the silent turns where such a descendant is suspected.
    It is consumed here, so a later turn that reuses this pid starts with no
    sample rather than one describing a tree that is gone.
    """
    try:
        if not sweep_enabled():
            return None
        return sweep_survivors(
            root_pid=process.pid,
            expected_pgid=process.pid,
            prior_snapshot=(*prior_snapshot, *last_process_tree(process.pid)),
        )
    finally:
        clear_process_tree(process.pid)


def _join_readers(
    readers: tuple[threading.Thread, ...], *, timeout: float | None
) -> None:
    """Join the pipe readers, optionally under a shared deadline."""
    if timeout is None:
        for reader in readers:
            reader.join()
        return
    deadline = time.monotonic() + timeout
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))


class HarnessError(Exception):
    """Raised when a harness adapter cannot build or run a command."""


@dataclass(frozen=True)
class AccessProfile:
    """What one harness turn is allowed to reach.

    This is deliberately separate from ``unsafe_bypass``: bypass widens a worker
    turn, while the profile decides which access model the turn runs under at
    all. Folding the audit profile into the bypass flag would make "not
    bypassed" mean two different things and let a worker default leak into an
    audit.
    """

    name: AccessProfileName
    read_roots: tuple[Path, ...] = ()

    @property
    def is_audit(self) -> bool:
        return self.name == "audit-read-only"


#: The profile every implementation and review turn runs under.
WORKER_ACCESS = AccessProfile(name="worker")


def audit_read_only_access(read_roots: tuple[Path, ...] | list[Path]) -> AccessProfile:
    """Build the restricted profile an auditor runs under.

    ``read_roots`` are the directories the auditor may read outside its own
    working directory — the evidence bundle and the read-only ASL and skill
    trees. They are never writable roots, and the application workspace is
    never among them.
    """
    return AccessProfile(name="audit-read-only", read_roots=tuple(read_roots))


def audit_read_only_unavailable_reason(name: str) -> str | None:
    """Say why ``name`` cannot run an audit read-only turn, or ``None``.

    A harness that cannot enforce the profile must be declared unavailable
    rather than run under a weaker one, so this returns a reason instead of a
    fallback.
    """
    try:
        adapter = get_adapter(name)
    except HarnessError:
        return "unsupported-harness"
    if not adapter.enforces_audit_read_only:
        return "profile-unenforceable"
    if adapter.resolve_executable() is None:
        return "harness-not-installed"
    return None


#: Transport discriminators. ``argv`` alone stopped describing a turn once a
#: harness could be driven over a protocol instead of a command line, so every
#: invocation and every result now says which of the two it was.
TRANSPORT_EXEC = codex_app_server.TRANSPORT_EXEC
TRANSPORT_APP_SERVER = codex_app_server.TRANSPORT_APP_SERVER
TRANSPORT_STREAM_JSON = claude_stream.TRANSPORT_STREAM_JSON


@dataclass(frozen=True)
class HarnessInvocation:
    """A concrete subprocess invocation for one harness turn.

    ``argv`` remains the invocation's identity for every caller that had one.
    Under a protocol transport it says only which binary is spawned, and
    ``turn_plan`` carries what argv used to: the access profile, the writable
    roots, the model, and the effort. Neither ever carries the prompt.
    """

    name: HarnessName
    argv: list[str]
    cwd: Path
    last_message_path: Path | None = None
    model: str | None = None
    effort: str | None = None
    transport: str = TRANSPORT_EXEC
    turn_plan: codex_app_server.TurnPlan | None = None
    #: The turn's ephemeral harness-configuration directory, when it has one.
    #: It holds settings a turn needs and must not outlive — a hook overlay and
    #: its guidance spool — and is created by ``run`` and deleted by it, so a
    #: turn's private channel cannot be reached from the next turn.
    overlay_dir: Path | None = None


@dataclass(frozen=True)
class HarnessResult:
    """Captured result from a single harness turn."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    last_message: str = ""
    name: HarnessName | None = None
    timed_out: bool = False
    aborted: bool = False
    interrupted: bool = False
    process_sweep: dict[str, Any] | None = None
    observed_model: str | None = None
    stream_io: dict[str, int | float] | None = None
    transport: str = TRANSPORT_EXEC


class _OutputProcessor:
    """Normalize harness output before it is logged and persisted."""

    @property
    def final_result_seen(self) -> bool:
        """Whether a terminal result record has been observed on stdout.

        Only the claude stream-json processor can report one; processors that
        do not parse structured output never do, which correctly disables any
        result-based detection for their harnesses.
        """
        return False

    @property
    def observed_model(self) -> str | None:
        """The model the harness reported it actually executed with.

        Only the claude stream-json processor can report one; processors that
        do not parse structured output never do, which correctly leaves the
        observed model unknown for their harnesses.
        """
        return None

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        del stream_name
        return text

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        del stderr
        return _read_last_message(last_message_path) or stdout


class _ClaudeStreamJsonOutputProcessor(_OutputProcessor):
    """Render Claude stream-json records as readable line-oriented progress.

    Also the turn's only reader of the CLI's protocol acknowledgements: when a
    session is attached, the terminal ``result`` records and the ``user`` echoes
    are reported to it. Rendering and acknowledgement share this one parse
    because there is only ever one reader of the stream.
    """

    def __init__(
        self, session: claude_stream.ClaudeStreamSession | None = None
    ) -> None:
        self._session = session
        self._text_buffer = ""
        self._emitted_text = False
        self._assistant_messages: list[str] = []
        self._final_result: str | None = None
        self._final_result_seen = False
        self._observed_model: str | None = None

    @property
    def final_result_seen(self) -> bool:
        return self._final_result_seen

    @property
    def observed_model(self) -> str | None:
        return self._observed_model

    def _note_observed_model(self, model: object) -> None:
        # First wins: a resumed session's later records can name a different
        # model than the one this turn started under.
        if self._observed_model is None and isinstance(model, str) and model:
            self._observed_model = model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text

        rendered: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                rendered.append(line)
                continue
            if not isinstance(record, dict):
                rendered.append(line)
                continue
            rendered.extend(self._render_record(record))
        return "".join(f"{line}\n" for line in rendered)

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        saved = _read_last_message(last_message_path)
        if saved:
            return saved
        if self._final_result:
            return self._ensure_trailing_newline(self._final_result)
        if self._assistant_messages:
            return self._ensure_trailing_newline("\n".join(self._assistant_messages))
        del stderr
        return stdout

    def _render_record(self, record: dict[str, object]) -> list[str]:
        record_type = record.get("type")
        if record_type == "system":
            return self._render_system(record)
        if record_type == "stream_event":
            event = record.get("event")
            if isinstance(event, dict):
                return self._render_stream_event(event)
            return []
        if record_type == "assistant":
            text = self._assistant_text(record.get("message"))
            if text:
                self._assistant_messages.append(text)
                if not self._emitted_text:
                    return self._format_text_lines(text)
            return []
        if record_type == "user":
            # The CLI echoes a user message back when it takes it up, which is
            # the protocol acknowledgement a stdin write on its own is not.
            # Never rendered: an echo carries the prompt or a guidance body,
            # and neither belongs in a turn log.
            if self._session is not None:
                self._session.note_user_echo(
                    self._assistant_text(record.get("message"))
                )
            return []
        if record_type == "result":
            self._final_result_seen = True
            if self._session is not None:
                self._session.note_result()
            result = record.get("result")
            if isinstance(result, str):
                self._final_result = result
            subtype = record.get("subtype") or "unknown"
            details = self._result_details(record)
            suffix = "" if not details else f" ({', '.join(details)})"
            return [f"claude result: {subtype}{suffix}"]
        if record_type == "error":
            message = record.get("message") or record.get("error") or "unknown"
            return [f"claude error: {message}"]
        return []

    def _render_system(self, record: dict[str, object]) -> list[str]:
        subtype = record.get("subtype")
        if subtype == "init":
            parts = ["claude init"]
            model = record.get("model")
            if model:
                self._note_observed_model(model)
                parts.append(f"model {model}")
            session_id = record.get("session_id")
            if session_id:
                parts.append(f"session {session_id}")
            return ["; ".join(str(part) for part in parts)]
        if subtype == "status":
            status = record.get("status") or "unknown"
            return [f"claude status: {status}"]
        return []

    def _render_stream_event(self, event: dict[str, object]) -> list[str]:
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("model"):
                self._note_observed_model(message["model"])
                return [f"claude response: model {message['model']}"]
            return ["claude response started"]
        if event_type == "content_block_start":
            content_block = event.get("content_block")
            if (
                isinstance(content_block, dict)
                and content_block.get("type") == "tool_use"
            ):
                name = content_block.get("name") or "unknown"
                return [f"claude tool: {name}"]
            return []
        if event_type == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    self._text_buffer += text
                    return self._drain_text_buffer(force=False)
            return []
        if event_type == "content_block_stop":
            return self._drain_text_buffer(force=True)
        return []

    def _drain_text_buffer(self, *, force: bool) -> list[str]:
        lines: list[str] = []
        while "\n" in self._text_buffer:
            line, self._text_buffer = self._text_buffer.split("\n", 1)
            if line.strip():
                lines.append(f"claude text: {line}")
        if force and self._text_buffer.strip():
            lines.append(f"claude text: {self._text_buffer}")
            self._text_buffer = ""
        if lines:
            self._emitted_text = True
        return lines

    def _format_text_lines(self, text: str) -> list[str]:
        lines = [f"claude text: {line}" for line in text.splitlines() if line.strip()]
        if lines:
            self._emitted_text = True
        return lines

    def _assistant_text(self, raw_message: object) -> str:
        if not isinstance(raw_message, dict):
            return ""
        content = raw_message.get("content")
        if isinstance(content, str):
            # User echoes may carry bare text where an assistant message
            # always carries blocks.
            return content.strip()
        if not isinstance(content, list):
            return ""
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
        return "".join(chunks).strip()

    def _result_details(self, record: dict[str, object]) -> list[str]:
        details: list[str] = []
        turns = record.get("num_turns")
        if isinstance(turns, int):
            label = "turn" if turns == 1 else "turns"
            details.append(f"{turns} {label}")
        duration_ms = record.get("duration_ms")
        if isinstance(duration_ms, int | float):
            details.append(f"{duration_ms / 1000:.1f}s")
        return details

    def _ensure_trailing_newline(self, text: str) -> str:
        return text if text.endswith("\n") else f"{text}\n"


class HarnessTransport:
    """The in-turn conversation an adapter has with the process it spawned.

    A one-shot harness has none: argv said everything, and stdout is a report
    rather than a dialogue. A protocol harness has exactly one, opened right
    after the process exists and released once it is gone, and it is the only
    thing in the turn that may write to that process's stdin.

    The three methods are what the generic turn loop needs to know about a
    conversation it otherwise ignores: how to read the process's output, how
    (or whether) to inject guidance into it, and how to let go at the end.
    """

    def output_processor(self) -> _OutputProcessor:
        """The processor that turns this transport's stdout into log text."""
        raise NotImplementedError

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        """The live-delivery channel, or ``None`` when there is none."""
        return None

    def shutdown(self) -> None:
        """Release the conversation. Called once, on every path out of a turn."""


class HarnessAdapter:
    """Base class for harness-specific command construction."""

    name: HarnessName
    skill_prefix: str

    #: Whether this adapter can express the audit read-only profile in argv
    #: alone. An adapter that cannot must leave this false so callers declare
    #: the audit unavailable instead of running it with worker access.
    enforces_audit_read_only: bool = False

    #: Whether guidance reaches this harness by riding the next turn's prompt
    #: instead of by a same-turn steer. True only for a harness whose protocol
    #: has no in-turn channel at all, so the prompt genuinely is the delivery
    #: and can be recorded as one. A harness that *can* be steered must leave
    #: this false even when a particular attempt defers: there, the prompt is
    #: a fallback and the ledger already has the live attempt's own verdict.
    delivers_guidance_next_turn: bool = False

    #: Whether this adapter arms an ASL-owned pre-tool hook that reads the
    #: run's guard policy and can refuse a call before it executes. False is
    #: the honest default: a harness that exposes no hook ASL can arm without
    #: touching the operator's own configuration has no pre-tool lane, and
    #: coverage must say so rather than imply the guard is enforced twice.
    enforces_guard_pretool: bool = False

    def __init__(self, executable: str | None = None) -> None:
        try:
            self.executable = executable or config.configured_harness_executable(
                self.name
            )
        except config.ConfigError as error:
            raise HarnessError(str(error)) from error

    def resolve_executable(self) -> str | None:
        """Return the resolved executable path if it is on PATH."""
        return shutil.which(self.executable)

    def output_processor(self) -> _OutputProcessor:
        return _OutputProcessor()

    def _worker_plan_dir(self, plan_dir: Path | None) -> str:
        """The worker's writable grant, refusing to stringify a missing one.

        Every worker branch renders ``plan_dir`` through here so a ``None``
        cannot reach argv as the literal ``"None"`` — a path the harness would
        create rather than reject.
        """
        if plan_dir is None:
            raise HarnessError(f"{self.name} worker turns require a plan_dir")
        return str(plan_dir)

    def check_access(
        self,
        access: AccessProfile,
        *,
        plan_dir: Path | None,
        unsafe_bypass: bool,
    ) -> None:
        """Reject a turn whose access profile this adapter will not honor.

        Raising here rather than dropping a flag is the point: a misconfigured
        audit must fail loudly instead of quietly running with worker access.
        """
        if not access.is_audit:
            self._worker_plan_dir(plan_dir)
            return
        if unsafe_bypass:
            raise HarnessError(
                f"{self.name} audit turns cannot inherit unsafe bypass access"
            )
        if plan_dir is not None:
            raise HarnessError(
                f"{self.name} audit turns take read roots, not a writable plan_dir"
            )
        if not self.enforces_audit_read_only:
            raise HarnessError(
                f"{self.name} cannot enforce the audit read-only access profile"
            )

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def _guard_policy(self, env: Mapping[str, str] | None) -> dict[str, Any] | None:
        """The run's derived guard policy, as the turn environment points at it.

        The environment is the only place argv construction can learn about
        guards: the controller writes the policy atomically and exports its
        path, so an adapter that builds a command line reads the same document
        the ``PATH`` shim and the pre-tool hook read. ``None`` means there is
        nothing to compile — no guards, no policy, or a policy this process
        cannot read — and callers must degrade to the postflight lane rather
        than refuse the turn.
        """
        if not env:
            return None
        policy_path = env.get(guards.GUARD_POLICY_ENV_VAR)
        if not policy_path:
            return None
        try:
            document = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        except OSError, ValueError:
            return None
        return document if isinstance(document, dict) else None

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        return HarnessInvocation(
            name=self.name,
            argv=self.build_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                env=env,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
        )

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
    ) -> HarnessTransport | None:
        """Open this turn's protocol conversation, if the transport has one.

        ``None`` — the base implementation — is the one-shot case: argv said
        everything, and the turn is a process to watch rather than a dialogue
        to hold. Called once, immediately after the process exists and before
        the output readers start, so a conversation can be underway by the
        time the first byte arrives.
        """
        del process, invocation, prompt
        return None

    def live_delivery_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        transport: HarnessTransport | None = None,
    ) -> guidance_delivery.GuidanceTransport | None:
        """Return the transport that injects guidance into this live turn.

        A protocol harness answers from its open conversation; a one-shot
        harness has no live channel at all, so every attempt defers with
        ``harness-unsupported`` rather than claiming a delivery it cannot
        make.
        """
        del process, invocation
        if transport is None:
            return None
        return transport.guidance_transport()

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> HarnessResult:
        if invocation is None:
            invocation = self.build_invocation(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                # The turn environment is where argv learns about guards: an
                # adapter whose only pre-execution lane is its command line
                # reads the policy this env points at. Built here rather than
                # by the caller so the argv that runs is the argv the guards
                # were compiled into.
                env=env,
            )
        if invocation.last_message_path is not None:
            invocation.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            process = subprocess.Popen(
                invocation.argv,
                cwd=invocation.cwd,
                # A protocol transport talks back to the process it spawned, so
                # it needs a pipe of its own. A one-shot turn leaves stdin
                # inherited, exactly as it always was.
                stdin=(
                    subprocess.PIPE if invocation.transport != TRANSPORT_EXEC else None
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                # Its own session, so every descendant that inherits the turn's
                # process group can be killed at once on timeout and enumerated
                # by the post-turn sweep. Descendants that start a session of
                # their own are out of reach: see :func:`_kill_process_group`.
                start_new_session=True,
                # ``None`` inherits this process's environment, which is what
                # every turn did before command guards existed and what every
                # turn still does when none are active. A supplied mapping is
                # the complete child environment, already derived from the
                # parent's, so a guard's ``PATH`` prefix reaches the harness and
                # everything it spawns.
                env=None if env is None else dict(env),
            )
        except FileNotFoundError as error:
            raise HarnessError(
                f"{self.name} executable not found: {self.executable}"
            ) from error

        if on_spawn is not None:
            with contextlib.suppress(Exception):
                on_spawn(process.pid)

        # The conversation and the live-delivery endpoint are both opened here,
        # inside the boundary that owns the process, and both are closed in the
        # ``finally`` below on every path out of the turn. A delivery can
        # therefore only ever reach this exact live process: once the turn is
        # over the endpoint answers ``stale-turn`` instead of injecting into a
        # dead PID or into whatever turn started next.
        transport = self.open_transport(
            process=process,
            invocation=invocation,
            prompt=prompt,
        )
        if on_transport is not None:
            on_transport(transport)
        if live_delivery is not None:
            live_delivery.open(
                self.live_delivery_transport(
                    process=process,
                    invocation=invocation,
                    transport=transport,
                )
            )
        try:
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            stream_io: dict[str, int | float] = {
                "stdout_chunks": 0,
                "stderr_chunks": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "callback_seconds": 0.0,
            }
            reader_errors: list[BaseException] = []
            output_processor = (
                self.output_processor()
                if transport is None
                else transport.output_processor()
            )
            final_result_fired = threading.Event()
            observed_model_fired = threading.Event()
            # Quiescence guard for abandoned readers: a reader that outlives the
            # bounded join still holds the stream callback, hooks, and output
            # state, so a late write from a pipe-holding descendant could fire
            # them after this turn has finished. Every per-chunk side effect runs
            # under this lock behind an abandonment check, and abandonment is
            # flipped under the same lock, so once ``abandon_readers`` returns no
            # reader can start a new side effect and any in-flight one is done.
            side_effect_lock = threading.Lock()
            abandoned = threading.Event()

            def abandon_readers() -> None:
                with side_effect_lock:
                    abandoned.set()

            def read_stream(
                stream_name: StreamName,
                chunks: list[str],
                pipe: IO[str],
            ) -> None:
                try:
                    for chunk in iter(pipe.readline, ""):
                        with side_effect_lock:
                            if abandoned.is_set():
                                break
                            processed_chunk = output_processor.process_chunk(
                                stream_name,
                                chunk,
                            )
                            # Only stdout parsing can flip final_result_seen, so
                            # this branch runs on a single thread and the Event
                            # guard makes the hook fire exactly once.
                            if (
                                stream_name == "stdout"
                                and on_final_result is not None
                                and not final_result_fired.is_set()
                                and output_processor.final_result_seen
                            ):
                                final_result_fired.set()
                                with contextlib.suppress(Exception):
                                    on_final_result()
                            if (
                                stream_name == "stdout"
                                and on_observed_model is not None
                                and not observed_model_fired.is_set()
                            ):
                                observed = output_processor.observed_model
                                if observed is not None:
                                    observed_model_fired.set()
                                    with contextlib.suppress(Exception):
                                        on_observed_model(observed)
                            if not processed_chunk:
                                continue
                            chunks.append(processed_chunk)
                            chunks_key = f"{stream_name}_chunks"
                            chars_key = f"{stream_name}_chars"
                            stream_io[chunks_key] = int(stream_io[chunks_key]) + 1
                            stream_io[chars_key] = int(stream_io[chars_key]) + len(
                                processed_chunk
                            )
                            if stream_callback is not None:
                                callback_started = time.monotonic()
                                try:
                                    stream_callback(stream_name, processed_chunk)
                                except Exception as error:
                                    reader_errors.append(error)
                                finally:
                                    stream_io["callback_seconds"] = float(
                                        stream_io["callback_seconds"]
                                    ) + (time.monotonic() - callback_started)
                except Exception as error:
                    reader_errors.append(error)
                finally:
                    pipe.close()

            if process.stdout is None or process.stderr is None:
                raise HarnessError(f"{self.name} did not expose stdout/stderr pipes")

            stdout_thread = threading.Thread(
                target=read_stream,
                args=("stdout", stdout_chunks, process.stdout),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=read_stream,
                args=("stderr", stderr_chunks, process.stderr),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            timed_out = False
            aborted = False
            prior_snapshot: tuple[Mapping[str, Any], ...] = ()
            process_sweep: dict[str, Any] | None = None
            try:
                expired = _wait_for_turn_exit(process, timeout=timeout, abort=abort)
                if expired is not None:
                    timed_out = expired == "timeout"
                    aborted = expired == "abort"
                    prior_snapshot = _snapshot_descendants(
                        process.pid, _sweep_process_table()
                    )
                    _kill_process_group(process)
                    process.wait()

                _join_readers(
                    (stdout_thread, stderr_thread),
                    timeout=_ABANDONED_READER_JOIN_SECONDS,
                )
                abandon_readers()
                # After the join, so an in-group straggler gets to flush what it
                # was writing before the sweep takes it down.
                process_sweep = _sweep_turn_descendants(process, prior_snapshot)
            except KeyboardInterrupt:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                stdout_thread.join(timeout=1)
                stderr_thread.join(timeout=1)
                abandon_readers()
                # This branch returns early, so it needs its own sweep: the one on
                # the normal path is never reached from here. Explicit signalling
                # above is unchanged; the sweep only reaches what it left behind.
                interrupt_sweep = _sweep_turn_descendants(process)
                return HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    returncode=130,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
                    last_message=output_processor.last_message(
                        stdout="".join(stdout_chunks),
                        stderr="".join(stderr_chunks),
                        last_message_path=invocation.last_message_path,
                    ),
                    interrupted=True,
                    process_sweep=interrupt_sweep,
                    observed_model=output_processor.observed_model,
                    stream_io=dict(stream_io),
                )

            if reader_errors:
                raise HarnessError("stream callback failed") from reader_errors[0]

            stdout = "".join(stdout_chunks)
            stderr = "".join(stderr_chunks)
            if aborted:
                return HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    # 128 + SIGKILL, not the timeout path's 124: a caller reading
                    # only the returncode still has to tell the two apart.
                    returncode=137,
                    stdout=stdout,
                    stderr=stderr,
                    last_message=output_processor.last_message(
                        stdout=stdout,
                        stderr=stderr,
                        last_message_path=invocation.last_message_path,
                    ),
                    aborted=True,
                    process_sweep=process_sweep,
                    observed_model=output_processor.observed_model,
                    stream_io=dict(stream_io),
                )
            if timed_out:
                return HarnessResult(
                    name=self.name,
                    argv=invocation.argv,
                    transport=invocation.transport,
                    returncode=124,
                    stdout=stdout,
                    stderr=stderr,
                    last_message=output_processor.last_message(
                        stdout=stdout,
                        stderr=stderr,
                        last_message_path=invocation.last_message_path,
                    ),
                    timed_out=True,
                    process_sweep=process_sweep,
                    observed_model=output_processor.observed_model,
                    stream_io=dict(stream_io),
                )

            interrupted = process.returncode == -signal.SIGINT
            return HarnessResult(
                name=self.name,
                argv=invocation.argv,
                transport=invocation.transport,
                returncode=130 if interrupted else process.returncode,
                stdout=stdout,
                stderr=stderr,
                last_message=output_processor.last_message(
                    stdout=stdout,
                    stderr=stderr,
                    last_message_path=invocation.last_message_path,
                ),
                interrupted=interrupted,
                process_sweep=process_sweep,
                observed_model=output_processor.observed_model,
                stream_io=dict(stream_io),
            )
        finally:
            # Endpoint first: closing it waits out any delivery already in
            # flight, so no steer can still be using the conversation the
            # transport shutdown is about to tear down.
            if live_delivery is not None:
                live_delivery.close()
            if transport is not None:
                transport.shutdown()


class _CodexAppServerOutputProcessor(_OutputProcessor):
    """Render one app-server conversation as the turn's readable stdout.

    Thin by design: every decision about the protocol lives in the session, and
    this only routes stdout into it and reports what it learned. stderr is left
    alone, because the app-server writes its own diagnostics there in prose.
    """

    def __init__(self, session: codex_app_server.CodexAppServerSession) -> None:
        self._session = session

    @property
    def final_result_seen(self) -> bool:
        # ``turn/completed`` is the app-server's terminal record, and it is as
        # authoritative as claude's ``result`` record: the turn is over even if
        # the process has not exited yet.
        return self._session.turn_status is not None

    @property
    def observed_model(self) -> str | None:
        return self._session.observed_model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text
        return self._session.feed(text)

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        # The protocol reports the final message directly, so it is preferred
        # over the file the legacy transport had to be told to write.
        final = self._session.final_message
        if final:
            return final if final.endswith("\n") else f"{final}\n"
        del stderr
        return _read_last_message(last_message_path) or stdout


class _FallbackWindowStream:
    """Hold an app-server turn's output back while a fallback is still possible.

    Anything printed before ``turn/start`` is accepted is handshake noise, and
    if the conversation fails there the legacy transport runs the same turn for
    real. Streaming both would show an operator two turns' output for one turn
    and would leave the discarded attempt's bytes in the persisted log, so the
    early chunks are buffered until acceptance forecloses the fallback -- after
    which every chunk passes straight through, live, as it always did.

    Called only from the reader threads, which serialize on the harness's
    side-effect lock, and released or discarded from the turn's own thread once
    the readers are done with it.
    """

    def __init__(self, downstream: StreamCallback | None) -> None:
        self._downstream = downstream
        self._session: codex_app_server.CodexAppServerSession | None = None
        self._buffer: list[tuple[StreamName, str]] = []
        self._passing = False

    def attach(self, session: codex_app_server.CodexAppServerSession) -> None:
        self._session = session

    def __call__(self, stream_name: StreamName, text: str) -> None:
        if not self._passing:
            session = self._session
            if session is None or not (session.turn_accepted or session.work_observed):
                self._buffer.append((stream_name, text))
                return
            self.release()
        if self._downstream is not None:
            self._downstream(stream_name, text)

    def release(self) -> None:
        """Emit what was withheld: this turn is the one that really ran."""
        self._passing = True
        buffered, self._buffer = self._buffer, []
        if self._downstream is None:
            return
        for stream_name, text in buffered:
            self._downstream(stream_name, text)

    def discard(self) -> None:
        """Drop what was withheld: another run of this turn will be streamed."""
        self._passing = True
        self._buffer.clear()


class _CodexAppServerTransport(HarnessTransport):
    """The adapter's handle on one live app-server conversation."""

    def __init__(self, session: codex_app_server.CodexAppServerSession) -> None:
        self.session = session
        self._processor = _CodexAppServerOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self.session.steer

    def shutdown(self) -> None:
        self.session.shutdown()


class CodexAdapter(HarnessAdapter):
    name = "codex"
    skill_prefix = "$"
    enforces_audit_read_only = True

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """The app-server argv, which deliberately says almost nothing.

        Everything argv used to carry -- cwd, sandbox, writable roots, model,
        effort, and the prompt itself -- moves into the turn plan and the
        protocol. The prompt in particular is no longer a command-line
        argument, so it no longer appears in any process listing.
        """
        del cwd, plan_dir, prompt, unsafe_bypass, model, effort, access, env
        if last_message_path is None:
            raise HarnessError("codex adapter requires a last_message_path")
        return [self.executable, "app-server"]

    def build_exec_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
    ) -> list[str]:
        """The legacy one-shot argv, kept for the pre-start fallback only.

        This is the transport codex ran under before the app-server, unchanged.
        It is reachable only when the app-server conversation failed before the
        turn was accepted and before any work was emitted.
        """
        if last_message_path is None:
            raise HarnessError("codex adapter requires a last_message_path")

        if access.is_audit:
            # No ``--add-dir``: for codex it grants *write* access, and the
            # read-only sandbox already permits reading outside the cwd, so the
            # read roots need no flag at all. ``--skip-git-repo-check`` lets the
            # audit run from a dedicated directory that is not a repository.
            argv = [
                self.executable,
                "exec",
                "-C",
                str(cwd),
                "-s",
                "read-only",
                "--skip-git-repo-check",
            ]
        else:
            argv = [
                self.executable,
                "exec",
                "-C",
                str(cwd),
                "--add-dir",
                self._worker_plan_dir(plan_dir),
            ]
            if unsafe_bypass:
                argv.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                argv.extend(["-s", "workspace-write"])
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["-c", f'model_reasoning_effort="{effort}"'])
        argv.extend(["-o", str(last_message_path), prompt])
        return argv

    def build_turn_plan(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        unsafe_bypass: bool = False,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
    ) -> codex_app_server.TurnPlan:
        """Restate the access profile as app-server thread and turn parameters.

        Each branch is the protocol spelling of the legacy flag it replaces:
        ``-s read-only`` and ``-s workspace-write`` become the ``sandbox``
        parameter, ``--dangerously-bypass-approvals-and-sandbox`` becomes
        ``danger-full-access`` with approvals off, and ``--add-dir`` becomes the
        one writable root under ``sandbox_workspace_write``. ``expected_sandbox``
        is what the session then demands the app-server confirm it applied, so
        the profile is verified rather than assumed.
        """
        thread_params: dict[str, Any] = {
            "cwd": str(cwd),
            # The turn is unattended on both transports; the legacy argv said
            # the same thing by never offering an approval path at all.
            "approvalPolicy": "never",
        }
        if access.is_audit:
            # No writable root: the read-only sandbox already permits reading
            # outside the cwd, which is what the audit's read roots need, and
            # the app-server needs no equivalent of --skip-git-repo-check.
            thread_params["sandbox"] = "read-only"
            expected_sandbox = codex_app_server.SANDBOX_READ_ONLY
        elif unsafe_bypass:
            thread_params["sandbox"] = "danger-full-access"
            expected_sandbox = codex_app_server.SANDBOX_DANGER_FULL_ACCESS
        else:
            thread_params["sandbox"] = "workspace-write"
            thread_params["config"] = {
                "sandbox_workspace_write": {
                    "writable_roots": [self._worker_plan_dir(plan_dir)]
                }
            }
            expected_sandbox = codex_app_server.SANDBOX_WORKSPACE_WRITE
        if model is not None:
            thread_params["model"] = model
        turn_params: dict[str, Any] = {}
        if effort is not None:
            turn_params["effort"] = effort
        return codex_app_server.TurnPlan(
            thread_params=thread_params,
            turn_params=turn_params,
            expected_sandbox=expected_sandbox,
        )

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        return HarnessInvocation(
            name=self.name,
            argv=self.build_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
                env=env,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            transport=TRANSPORT_APP_SERVER,
            turn_plan=self.build_turn_plan(
                cwd=cwd,
                plan_dir=plan_dir,
                unsafe_bypass=unsafe_bypass,
                model=model,
                effort=effort,
                access=access,
            ),
        )

    def build_exec_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        return HarnessInvocation(
            name=self.name,
            argv=self.build_exec_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            transport=TRANSPORT_EXEC,
        )

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_APP_SERVER:
            return None
        if invocation.turn_plan is None:
            raise HarnessError("codex app-server turns require a turn plan")
        session = codex_app_server.CodexAppServerSession(
            process=process,
            plan=invocation.turn_plan,
            prompt=prompt,
        )
        session.start()
        return _CodexAppServerTransport(session)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> HarnessResult:
        """Run the turn on the app-server, falling back only before it starts.

        The fallback window is narrow on purpose. ``session.fallback_reason``
        is non-``None`` only while the conversation failed before ``turn/start``
        was accepted *and* before the app-server emitted anything the turn did,
        which is the only state in which re-running the prompt cannot execute
        it twice. Once the turn is accepted, a protocol failure is reported as
        a failed turn -- never retried.
        """
        if invocation is not None and invocation.transport == TRANSPORT_EXEC:
            # An explicitly legacy invocation (a fallback re-entry, or a caller
            # that built one deliberately) is run as-is.
            return super().run(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                timeout=timeout,
                stream_callback=stream_callback,
                model=model,
                effort=effort,
                on_spawn=on_spawn,
                on_final_result=on_final_result,
                on_observed_model=on_observed_model,
                access=access,
                abort=abort,
                live_delivery=live_delivery,
                env=env,
                invocation=invocation,
                on_transport=on_transport,
            )

        if invocation is None:
            invocation = self.build_invocation(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
            )

        # Captured rather than stored on ``self``: one adapter instance serves
        # every turn of a run, and the session belongs to exactly one of them.
        opened: list[codex_app_server.CodexAppServerSession] = []
        window = _FallbackWindowStream(stream_callback)

        def capture(transport: HarnessTransport | None) -> None:
            if isinstance(transport, _CodexAppServerTransport):
                opened.append(transport.session)
                window.attach(transport.session)
            if on_transport is not None:
                on_transport(transport)

        result = super().run(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            timeout=timeout,
            stream_callback=window,
            model=model,
            effort=effort,
            on_spawn=on_spawn,
            on_final_result=on_final_result,
            on_observed_model=on_observed_model,
            access=access,
            abort=abort,
            live_delivery=live_delivery,
            env=env,
            invocation=invocation,
            on_transport=capture,
        )
        if not opened:
            window.release()
            return result
        session = opened[0]

        if result.timed_out or result.aborted or result.interrupted:
            # The turn was stopped rather than failed. Re-running it would
            # spend the same wall clock again, and the operator asked for it to
            # stop, so these codes stand exactly as the base run reported them.
            window.release()
            return self._app_server_result(result, session)

        fallback = session.fallback_reason
        if fallback is None:
            window.release()
            return self._app_server_result(result, session)

        window.discard()
        legacy = self.build_exec_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
        )
        fallback_result = super().run(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            timeout=timeout,
            stream_callback=stream_callback,
            model=model,
            effort=effort,
            on_spawn=on_spawn,
            on_final_result=on_final_result,
            on_observed_model=on_observed_model,
            access=access,
            abort=abort,
            live_delivery=live_delivery,
            env=env,
            invocation=legacy,
            on_transport=on_transport,
        )
        return self._with_stderr_note(
            fallback_result,
            f"codex app-server unavailable, ran codex exec instead: {fallback}",
        )

    def _app_server_result(
        self,
        result: HarnessResult,
        session: codex_app_server.CodexAppServerSession,
    ) -> HarnessResult:
        """Restate the turn's outcome from the protocol rather than the exit code.

        An app-server exits 0 whether the turn it ran succeeded or failed, and
        exits by our own ``terminate`` on the shutdown path, so its return code
        carries no verdict. The turn's own terminal status does.
        """
        error = session.protocol_error
        if error is not None:
            result = self._with_stderr_note(result, f"codex app-server: {error}")
        if result.timed_out or result.aborted or result.interrupted:
            return result
        if (
            session.turn_status == codex_app_server.TURN_STATUS_COMPLETED
            and error is None
        ):
            returncode = 0
        elif result.returncode > 0:
            returncode = result.returncode
        else:
            # 0 (a clean app-server exit after an unfinished turn) and any
            # negative signal code from our own shutdown both mean "no verdict";
            # neither may be reported as success.
            returncode = 1
        return dataclasses.replace(result, returncode=returncode)

    @staticmethod
    def _with_stderr_note(result: HarnessResult, note: str) -> HarnessResult:
        """Append one adapter-authored line to the turn's stderr.

        The stderr artifact is written from ``result.stderr`` while
        ``stream_io`` counts what the readers saw, and the two are required to
        agree byte for byte. A note added here is stderr the turn really has,
        so it is accounted for as stderr rather than smuggled in beside it.
        """
        text = f"{note}\n"
        stream_io = dict(result.stream_io or {})
        if stream_io:
            stream_io["stderr_chars"] = int(stream_io.get("stderr_chars", 0)) + len(
                text
            )
            stream_io["stderr_chunks"] = int(stream_io.get("stderr_chunks", 0)) + 1
        return dataclasses.replace(
            result,
            stderr=result.stderr + text,
            stream_io=stream_io or result.stream_io,
        )


class _ClaudeStreamTransport(HarnessTransport):
    """The adapter's handle on one live claude stream-JSON conversation."""

    def __init__(self, session: claude_stream.ClaudeStreamSession) -> None:
        self.session = session
        self._processor = _ClaudeStreamJsonOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self.session.steer

    def shutdown(self) -> None:
        self.session.shutdown()


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    skill_prefix = "/"
    enforces_audit_read_only = True
    enforces_guard_pretool = True

    def output_processor(self) -> _OutputProcessor:
        return _ClaudeStreamJsonOutputProcessor()

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        # Claude's guard lane is the settings overlay written at spawn time,
        # not argv, so the turn environment says nothing this command line
        # needs.
        del cwd, last_message_path, env
        argv = [
            self.executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # Input is a stream too, which is what makes a turn a conversation
            # rather than a one-shot: the prompt below is written to stdin as
            # the first message, and guidance can follow it while the turn runs.
            "--input-format",
            "stream-json",
        ]
        if access.is_audit:
            for root in access.read_roots:
                argv.extend(["--add-dir", str(root)])
            argv.extend(
                [
                    "--permission-mode",
                    "plan",
                    "--tools",
                    CLAUDE_AUDIT_TOOLS,
                    "--allowed-tools",
                    CLAUDE_AUDIT_TOOLS,
                    "--disallowed-tools",
                    CLAUDE_AUDIT_DENIED_TOOLS,
                    "--strict-mcp-config",
                    "--no-session-persistence",
                    "--disable-slash-commands",
                ]
            )
        else:
            argv.extend(
                [
                    "--add-dir",
                    self._worker_plan_dir(plan_dir),
                    "--permission-mode",
                    "bypassPermissions" if unsafe_bypass else "dontAsk",
                ]
            )
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        # The prompt is deliberately absent from argv: under streaming input it
        # is the conversation's first stdin message, so it never reaches a
        # process listing, a crash report, or a printed invocation.
        del prompt
        return argv

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> HarnessInvocation:
        invocation = super().build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
            access=access,
            env=env,
        )
        return dataclasses.replace(invocation, transport=TRANSPORT_STREAM_JSON)

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_STREAM_JSON:
            return None
        spool = None
        if invocation.overlay_dir is not None:
            spool = claude_stream.ClaudeGuidanceSpool(invocation.overlay_dir / "spool")
        session = claude_stream.ClaudeStreamSession(
            process=process,
            prompt=prompt,
            spool=spool,
        )
        # The prompt goes out before the readers start, so the CLI has work to
        # do from its first byte. A write failure here is not raised: the
        # process exists and has to be collected like any other failed turn,
        # with ``prompt_error`` explaining the empty output.
        session.send_prompt()
        return _ClaudeStreamTransport(session)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: AccessProfile = WORKER_ACCESS,
        abort: AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: HarnessInvocation | None = None,
        on_transport: Callable[[HarnessTransport | None], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> HarnessResult:
        """Run the turn, arming its guidance hook for the turn's lifetime only.

        The overlay is the one piece of a claude turn that exists on disk and
        must not survive it. It is created here, named on argv with
        ``--settings``, and deleted in the ``finally`` — so the hook the CLI was
        told to run has nothing left to inject the moment the turn is over, and
        the operator's own configuration is never touched at any point.
        """
        if invocation is None:
            invocation = self.build_invocation(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                model=model,
                effort=effort,
                access=access,
            )
        overlay_dir: Path | None = None
        if invocation.transport == TRANSPORT_STREAM_JSON and not access.is_audit:
            # Audit turns get no overlay: an audit observes, and a hook that
            # injects operator guidance into one would make it something else.
            overlay_dir = Path(tempfile.mkdtemp(prefix="asl-claude-turn-"))
            spool = claude_stream.ClaudeGuidanceSpool(overlay_dir / "spool")
            # The policy path arrives in the turn environment the guard
            # controller prepared, so the hook reads whatever is denied *now*
            # rather than a copy taken when the overlay was written: a guard
            # added mid-turn is enforced by the next tool call, not the next
            # turn.
            settings_path = claude_stream.write_settings_overlay(
                overlay_dir,
                spool,
                guard_policy_path=(env or {}).get(guards.GUARD_POLICY_ENV_VAR),
            )
            invocation = dataclasses.replace(
                invocation,
                argv=[*invocation.argv, "--settings", str(settings_path)],
                overlay_dir=overlay_dir,
            )

        opened: list[HarnessTransport | None] = []

        def capture(transport: HarnessTransport | None) -> None:
            opened.append(transport)
            if on_transport is not None:
                on_transport(transport)

        try:
            result = super().run(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
                timeout=timeout,
                stream_callback=stream_callback,
                model=model,
                effort=effort,
                on_spawn=on_spawn,
                on_final_result=on_final_result,
                on_observed_model=on_observed_model,
                access=access,
                abort=abort,
                live_delivery=live_delivery,
                env=env,
                invocation=invocation,
                on_transport=capture,
            )
        finally:
            claude_stream.remove_overlay(overlay_dir)

        # A prompt that never reached the CLI produces a turn with no output
        # and no explanation. Saying so on stderr is the difference between a
        # diagnosable failure and a mysteriously empty one.
        transport = opened[0] if opened else None
        if isinstance(transport, _ClaudeStreamTransport):
            prompt_error = transport.session.prompt_error
            if prompt_error:
                return dataclasses.replace(
                    result,
                    stderr=(
                        f"{result.stderr}claude prompt was not delivered: "
                        f"{prompt_error}\n"
                    ),
                )
        return result


class _CopilotOneShotTransport(HarnessTransport):
    """Copilot's answer to a live steer, which is a considered "no".

    Copilot's CLI runs one prompt per process and exposes no same-turn steer:
    the only in-turn control its protocol offers is cancellation, and cancelling
    a turn to say something to it would destroy the work the guidance was meant
    to redirect. So this transport exists precisely to decline — attaching it
    turns a silent ``harness-unsupported`` into ``unsupported-active-steer``,
    which says the harness was asked and the answer is next turn.

    The guidance stays deliverable, and the next turn's prompt carries it.
    """

    def output_processor(self) -> _OutputProcessor:
        return _OutputProcessor()

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self._decline

    def _decline(
        self, request: guidance_delivery.GuidanceRequest
    ) -> guidance_delivery.GuidanceOutcome:
        del request
        return guidance_delivery.deferred(
            guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER
        )


class CopilotAdapter(HarnessAdapter):
    name = "copilot"
    skill_prefix = "/"
    enforces_audit_read_only = True
    delivers_guidance_next_turn = True

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
        access: AccessProfile = WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        del last_message_path
        argv = [
            self.executable,
            "-C",
            str(cwd),
        ]
        # Copilot has no hook ASL can arm, so its only pre-execution lane is
        # the command line it is spawned with. That fixes the rules at process
        # start: a guard added mid-turn cannot reach this argv, and a
        # protected-path rule has no `--deny-tool` form at all. Both gaps are
        # reported as such by `guards.guard_coverage` rather than papered over
        # -- this is a narrowing of the postflight guarantee, not a second one.
        #
        # The ``=`` form is load-bearing here for the same reason as in the
        # audit profile below: ``--deny-tool`` takes a variadic value, so the
        # space form swallows the next argument.
        for denied in guards.copilot_deny_tool_rules_from_policy(
            self._guard_policy(env)
        ):
            argv.append(f"--deny-tool={denied}")
        if access.is_audit:
            for root in access.read_roots:
                argv.extend(["--add-dir", str(root)])
            argv.append(f"--available-tools={COPILOT_AUDIT_TOOLS}")
            # The ``=`` form is load-bearing: ``--deny-tool`` takes a variadic
            # value, so the space form can swallow the next argument.
            argv.extend(
                f"--deny-tool={denied}" for denied in COPILOT_AUDIT_DENIED_TOOLS
            )
            argv.extend(
                [
                    "--disable-builtin-mcps",
                    "--no-custom-instructions",
                    "--no-remote",
                    "--no-remote-export",
                    "--disallow-temp-dir",
                ]
            )
        else:
            argv.extend(["--add-dir", self._worker_plan_dir(plan_dir)])
            if unsafe_bypass:
                argv.extend(["--allow-all-tools", "--allow-all-paths"])
        argv.extend(["--no-ask-user", "--silent"])
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        argv.extend(["-p", prompt])
        return argv

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
    ) -> HarnessTransport | None:
        del process, invocation, prompt
        return _CopilotOneShotTransport()


def _read_last_message(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def get_adapter(name: str) -> HarnessAdapter:
    """Return the adapter for a supported harness name."""
    if name == "codex":
        return CodexAdapter()
    if name == "claude":
        return ClaudeAdapter()
    if name == "copilot":
        return CopilotAdapter()
    raise HarnessError(f"unsupported harness: {name}")


def delivers_guidance_next_turn(name: str) -> bool:
    """Whether ``name``'s guidance arrives with the next turn's prompt.

    Asked by the delivery spine rather than answered there, so the fact lives
    with the adapter that knows its harness's protocol.
    """
    try:
        return get_adapter(name).delivers_guidance_next_turn
    except HarnessError:
        return False


def run_harness(
    name: str,
    *,
    cwd: Path,
    plan_dir: Path | None,
    prompt: str,
    unsafe_bypass: bool = False,
    last_message_path: Path | None = None,
    timeout: int | None = None,
    stream_callback: StreamCallback | None = None,
    model: str | None = None,
    effort: str | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_final_result: Callable[[], None] | None = None,
    on_observed_model: Callable[[str], None] | None = None,
    access: AccessProfile = WORKER_ACCESS,
    abort: AbortSignal | None = None,
    live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
    env: Mapping[str, str] | None = None,
) -> HarnessResult:
    """Run one harness turn through the selected adapter."""
    return get_adapter(name).run(
        cwd=cwd,
        plan_dir=plan_dir,
        prompt=prompt,
        unsafe_bypass=unsafe_bypass,
        last_message_path=last_message_path,
        timeout=timeout,
        stream_callback=stream_callback,
        model=model,
        effort=effort,
        on_spawn=on_spawn,
        on_final_result=on_final_result,
        on_observed_model=on_observed_model,
        access=access,
        abort=abort,
        live_delivery=live_delivery,
        env=env,
    )


# Single source for the skill-invocation prefix rule: derived from the adapters
# themselves so the mapping cannot drift from what each harness actually parses.
_SKILL_PREFIX_ADAPTERS: tuple[type[HarnessAdapter], ...] = (
    CodexAdapter,
    ClaudeAdapter,
    CopilotAdapter,
)
SKILL_PREFIXES: dict[HarnessName, str] = {
    adapter.name: adapter.skill_prefix for adapter in _SKILL_PREFIX_ADAPTERS
}


def detect_invoking_harness(
    env: Mapping[str, str] | None = None,
) -> HarnessName | None:
    """Best-effort detection of the harness that launched this process.

    Returns the single harness whose environment markers are present, or
    ``None`` when zero or more than one match (ambiguous). Detection only
    orders human-facing output — it never gates behavior — so unknown or
    conflicting environments fall back to ``None`` and the default ordering.
    """
    source = os.environ if env is None else env
    detected: list[HarnessName] = []
    if source.get("CLAUDECODE"):
        detected.append("claude")
    if any(key.startswith("CODEX_") for key in source):
        detected.append("codex")
    if any(
        key.startswith("COPILOT_") or key.startswith("GITHUB_COPILOT_")
        for key in source
    ):
        detected.append("copilot")
    if len(detected) == 1:
        return detected[0]
    return None


def _ordered_harness_names(
    env: Mapping[str, str] | None = None,
) -> list[HarnessName]:
    """Harness names with the detected invoking harness (if any) first."""
    detected = detect_invoking_harness(env)
    if detected is None:
        return list(HARNESS_NAMES)
    return [detected, *(name for name in HARNESS_NAMES if name != detected)]


def skill_invocations(
    skill: str,
    argument: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[HarnessName, str]:
    """Return ready-to-paste ``<prefix><skill>[ <argument>]`` per harness.

    The mapping is ordered with the best-effort invoking harness first (when it
    is unambiguously detected) so callers can surface the most likely command at
    the top; ordering never changes which harnesses appear.
    """
    suffix = f" {argument}" if argument else ""
    return {
        name: f"{SKILL_PREFIXES[name]}{skill}{suffix}"
        for name in _ordered_harness_names(env)
    }
