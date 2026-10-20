"""Argv, turn-plan and invocation builders for the codex adapter.

`harness_codex.CodexAdapter` extends this class with the transport and turn
loop; nothing here touches a process."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import codex_app_server
from .harnesses import (
    TRANSPORT_APP_SERVER,
    TRANSPORT_EXEC,
    WORKER_ACCESS,
    AccessProfile,
    HarnessAdapter,
    HarnessError,
    HarnessInvocation,
)


class CodexArgvBuilder(HarnessAdapter):
    name = "codex"
    skill_prefix = "$"
    enforces_audit_read_only = True
    #: Both halves of continuity, on both transports. The app-server captures
    #: its thread id between ``thread/resume``/``thread/start`` and
    #: ``turn/start`` and can restore exactly the thread it is handed; the
    #: legacy transport captures the session id its own output names, and
    #: refuses a resume outright rather than starting a fresh session under one.
    captures_conversations = True
    resumes_conversations = True

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
        resume_conversation_id: str | None = None,
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
            resume_conversation_id=resume_conversation_id,
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
        resume_conversation_id: str | None = None,
    ) -> HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        if resume_conversation_id is not None:
            # `codex exec` has no resume: it starts a new session every time.
            # Building this invocation anyway would produce a turn that claims
            # to continue a conversation and silently starts a fresh one, which
            # is the exact substitution the app-server path refuses to make.
            raise HarnessError(
                "codex exec cannot resume conversation "
                f"{resume_conversation_id}; it starts a fresh session"
            )
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
