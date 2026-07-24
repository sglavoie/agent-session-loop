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
from typing import IO, Literal

from . import config


HarnessName = Literal["codex", "claude", "copilot"]
HARNESS_NAMES: tuple[HarnessName, ...] = ("codex", "claude", "copilot")
StreamName = Literal["stdout", "stderr"]
StreamCallback = Callable[[StreamName, str], None]


class HarnessError(Exception):
    """Raised when a harness adapter cannot build or run a command."""


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


class _OutputProcessor:
    """Normalize harness output before it is logged and persisted."""

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

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        raise NotImplementedError

    def build_invocation(
        self,
        *,
        cwd: Path,
        plan_dir: Path,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> HarnessInvocation:
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
        plan_dir: Path,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> HarnessResult:
        invocation = self.build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
            model=model,
            effort=effort,
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
            )
        except FileNotFoundError as error:
            raise HarnessError(
                f"{self.name} executable not found: {self.executable}"
            ) from error

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        reader_errors: list[BaseException] = []
        output_processor = self.output_processor()

        def read_stream(
            stream_name: StreamName,
            chunks: list[str],
            pipe: IO[str],
        ) -> None:
            try:
                for chunk in iter(pipe.readline, ""):
                    processed_chunk = output_processor.process_chunk(
                        stream_name,
                        chunk,
                    )
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
        try:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()

            stdout_thread.join()
            stderr_thread.join()
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
        )


class CodexAdapter(HarnessAdapter):
    name = "codex"
    skill_prefix = "$"

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        if last_message_path is None:
            raise HarnessError("codex adapter requires a last_message_path")

        argv = [
            self.executable,
            "exec",
            "-C",
            str(cwd),
            "--add-dir",
            str(plan_dir),
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

    def output_processor(self) -> _OutputProcessor:
        return _ClaudeStreamJsonOutputProcessor()

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        del cwd, last_message_path
        permission_mode = "bypassPermissions" if unsafe_bypass else "dontAsk"
        argv = [
            self.executable,
            "--print",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--add-dir",
            str(plan_dir),
            "--permission-mode",
            permission_mode,
        ]
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        argv.append(prompt)
        return argv


class CopilotAdapter(HarnessAdapter):
    name = "copilot"
    skill_prefix = "/"

    def build_argv(
        self,
        *,
        cwd: Path,
        plan_dir: Path,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        del last_message_path
        argv = [
            self.executable,
            "-C",
            str(cwd),
            "--add-dir",
            str(plan_dir),
        ]
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
    plan_dir: Path,
    prompt: str,
    unsafe_bypass: bool = False,
    last_message_path: Path | None = None,
    timeout: int | None = None,
    stream_callback: StreamCallback | None = None,
    model: str | None = None,
    effort: str | None = None,
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
