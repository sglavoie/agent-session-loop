"""Harness adapter boundaries.

Adapters construct argv lists and run harnesses without shell interpolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Literal

from . import config


HarnessName = Literal["codex", "claude", "copilot"]
HARNESS_NAMES: tuple[HarnessName, ...] = ("codex", "claude", "copilot")


class HarnessError(Exception):
    """Raised when a harness adapter cannot build or run a command."""


@dataclass(frozen=True)
class HarnessInvocation:
    """A concrete subprocess invocation for one harness turn."""

    name: HarnessName
    argv: list[str]
    cwd: Path
    last_message_path: Path | None = None


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
    ) -> HarnessInvocation:
        return HarnessInvocation(
            name=self.name,
            argv=self.build_argv(
                cwd=cwd,
                plan_dir=plan_dir,
                prompt=prompt,
                unsafe_bypass=unsafe_bypass,
                last_message_path=last_message_path,
            ),
            cwd=cwd,
            last_message_path=last_message_path,
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
    ) -> HarnessResult:
        invocation = self.build_invocation(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            unsafe_bypass=unsafe_bypass,
            last_message_path=last_message_path,
        )
        if invocation.last_message_path is not None:
            invocation.last_message_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            completed = subprocess.run(
                invocation.argv,
                cwd=invocation.cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as error:
            raise HarnessError(
                f"{self.name} executable not found: {self.executable}"
            ) from error
        except subprocess.TimeoutExpired as error:
            return HarnessResult(
                name=self.name,
                argv=invocation.argv,
                returncode=124,
                stdout=_coerce_timeout_text(error.stdout),
                stderr=_coerce_timeout_text(error.stderr),
                last_message=_read_last_message(invocation.last_message_path),
                timed_out=True,
            )

        return HarnessResult(
            name=self.name,
            argv=invocation.argv,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            last_message=_read_last_message(invocation.last_message_path) or completed.stdout,
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
    ) -> list[str]:
        del cwd, last_message_path
        permission_mode = "bypassPermissions" if unsafe_bypass else "dontAsk"
        return [
            self.executable,
            "--print",
            "--add-dir",
            str(plan_dir),
            "--permission-mode",
            permission_mode,
            prompt,
        ]


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
    ) -> list[str]:
        del unsafe_bypass, last_message_path
        return [
            self.executable,
            "-C",
            str(cwd),
            "--add-dir",
            str(plan_dir),
            "--allow-all-tools",
            "--allow-all-paths",
            "--no-ask-user",
            "--silent",
            "-p",
            prompt,
        ]


def _coerce_timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


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
) -> HarnessResult:
    """Run one harness turn through the selected adapter."""
    return get_adapter(name).run(
        cwd=cwd,
        plan_dir=plan_dir,
        prompt=prompt,
        unsafe_bypass=unsafe_bypass,
        last_message_path=last_message_path,
        timeout=timeout,
    )
