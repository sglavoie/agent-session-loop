"""Harness adapter boundaries.

Adapters construct argv lists and run harnesses without shell interpolation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from typing import IO, Any, Literal

from . import config
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
        try:
            process.kill()
        except OSError:
            pass


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


@dataclass(frozen=True)
class HarnessInvocation:
    """A concrete subprocess invocation for one harness turn."""

    name: HarnessName
    argv: list[str]
    cwd: Path
    last_message_path: Path | None = None
    model: str | None = None
    effort: str | None = None


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
    interrupted: bool = False
    process_sweep: dict[str, Any] | None = None


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
    """Render Claude stream-json records as readable line-oriented progress."""

    def __init__(self) -> None:
        self._text_buffer = ""
        self._emitted_text = False
        self._assistant_messages: list[str] = []
        self._final_result: str | None = None
        self._final_result_seen = False

    @property
    def final_result_seen(self) -> bool:
        return self._final_result_seen

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
        if record_type == "result":
            self._final_result_seen = True
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


class HarnessAdapter:
    """Base class for harness-specific command construction."""

    name: HarnessName
    skill_prefix: str

    #: Whether this adapter can express the audit read-only profile in argv
    #: alone. An adapter that cannot must leave this false so callers declare
    #: the audit unavailable instead of running it with worker access.
    enforces_audit_read_only: bool = False

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
    ) -> list[str]:
        raise NotImplementedError

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
            ),
            cwd=cwd,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
        )

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
        access: AccessProfile = WORKER_ACCESS,
    ) -> HarnessResult:
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
        if invocation.last_message_path is not None:
            invocation.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            process = subprocess.Popen(
                invocation.argv,
                cwd=invocation.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                # Its own session, so every descendant that inherits the turn's
                # process group can be killed at once on timeout and enumerated
                # by the post-turn sweep. Descendants that start a session of
                # their own are out of reach: see :func:`_kill_process_group`.
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise HarnessError(
                f"{self.name} executable not found: {self.executable}"
            ) from error

        if on_spawn is not None:
            try:
                on_spawn(process.pid)
            except Exception:
                pass

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        reader_errors: list[BaseException] = []
        output_processor = self.output_processor()
        final_result_fired = threading.Event()
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
                            try:
                                on_final_result()
                            except Exception:
                                pass
                        if not processed_chunk:
                            continue
                        chunks.append(processed_chunk)
                        if stream_callback is not None:
                            try:
                                stream_callback(stream_name, processed_chunk)
                            except Exception as error:
                                reader_errors.append(error)
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
        prior_snapshot: tuple[Mapping[str, Any], ...] = ()
        process_sweep: dict[str, Any] | None = None
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
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
            )

        if reader_errors:
            raise HarnessError("stream callback failed") from reader_errors[0]

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if timed_out:
            return HarnessResult(
                name=self.name,
                argv=invocation.argv,
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
            )

        interrupted = process.returncode == -signal.SIGINT
        return HarnessResult(
            name=self.name,
            argv=invocation.argv,
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
        )


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
    ) -> list[str]:
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


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    skill_prefix = "/"
    enforces_audit_read_only = True

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
    ) -> list[str]:
        del cwd, last_message_path
        argv = [
            self.executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
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
        argv.append(prompt)
        return argv


class CopilotAdapter(HarnessAdapter):
    name = "copilot"
    skill_prefix = "/"
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
    ) -> list[str]:
        del last_message_path
        argv = [
            self.executable,
            "-C",
            str(cwd),
        ]
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
    access: AccessProfile = WORKER_ACCESS,
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
        access=access,
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
