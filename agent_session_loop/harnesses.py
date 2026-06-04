"""Harness adapter boundaries.

Adapters construct argv lists and run harnesses without shell interpolation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shutil
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

        def read_stream(
            stream_name: StreamName,
            chunks: list[str],
            pipe: IO[str],
        ) -> None:
            try:
                for chunk in iter(pipe.readline, ""):
                    chunks.append(chunk)
                    if stream_callback is not None:
                        try:
                            stream_callback(stream_name, chunk)
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
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()

        stdout_thread.join()
        stderr_thread.join()

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
                last_message=_read_last_message(invocation.last_message_path),
                timed_out=True,
            )

        return HarnessResult(
            name=self.name,
            argv=invocation.argv,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            last_message=_read_last_message(invocation.last_message_path) or stdout,
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
        del unsafe_bypass, last_message_path
        argv = [
            self.executable,
            "-C",
            str(cwd),
            "--add-dir",
            str(plan_dir),
            "--allow-all-tools",
            "--allow-all-paths",
            "--no-ask-user",
            "--silent",
        ]
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
