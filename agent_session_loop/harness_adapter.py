"""Construct harness invocations and expose the adapter execution interface."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import (
    config,
    guards,
    guidance_delivery,
    harness_conversation,
    harness_execution,
    harness_lifecycle,
    harness_process,
    harness_transport,
    harness_types,
)


class HarnessAdapter:
    """Base class for harness-specific command construction."""

    name: harness_types.HarnessName
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

    #: Whether this adapter's transport reports the native conversation its
    #: turn runs on. False until a protocol path actually produces an
    #: identifier: the seam alone is not capability.
    captures_conversations: bool = False

    #: Whether this adapter can start a turn on a conversation it is handed.
    #: :meth:`run` refuses a resume intent while this is false — an adapter that
    #: quietly started a fresh conversation instead would leave the caller
    #: believing it had continued one.
    resumes_conversations: bool = False

    def __init__(self, executable: str | None = None) -> None:
        try:
            self.executable = executable or config.configured_harness_executable(
                self.name
            )
        except config.ConfigError as error:
            raise harness_types.HarnessError(str(error)) from error

    def resolve_executable(self) -> str | None:
        """Return the resolved executable path if it is on PATH."""
        return shutil.which(self.executable)

    @staticmethod
    def _with_stderr_note(
        result: harness_types.HarnessResult, note: str
    ) -> harness_types.HarnessResult:
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

    def output_processor(self) -> harness_transport._OutputProcessor:
        return harness_transport._OutputProcessor()

    def _worker_plan_dir(self, plan_dir: Path | None) -> str:
        """The worker's writable grant, refusing to stringify a missing one.

        Every worker branch renders ``plan_dir`` through here so a ``None``
        cannot reach argv as the literal ``"None"`` — a path the harness would
        create rather than reject.
        """
        if plan_dir is None:
            raise harness_types.HarnessError(
                f"{self.name} worker turns require a plan_dir"
            )
        return str(plan_dir)

    def check_access(
        self,
        access: harness_types.AccessProfile,
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
            raise harness_types.HarnessError(
                f"{self.name} audit turns cannot inherit unsafe bypass access"
            )
        if plan_dir is not None:
            raise harness_types.HarnessError(
                f"{self.name} audit turns take read roots, not a writable plan_dir"
            )
        if not self.enforces_audit_read_only:
            raise harness_types.HarnessError(
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
        access: harness_types.AccessProfile = harness_types.WORKER_ACCESS,
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
        access: harness_types.AccessProfile = harness_types.WORKER_ACCESS,
        env: Mapping[str, str] | None = None,
        resume_conversation_id: str | None = None,
    ) -> harness_types.HarnessInvocation:
        self.check_access(access, plan_dir=plan_dir, unsafe_bypass=unsafe_bypass)
        return harness_types.HarnessInvocation(
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
        )

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: harness_types.HarnessInvocation,
        prompt: str,
        lifecycle: harness_lifecycle.TurnLifecycle | None = None,
    ) -> harness_transport.HarnessTransport | None:
        """Open this turn's protocol conversation, if the transport has one.

        ``None`` — the base implementation — is the one-shot case: argv said
        everything, and the turn is a process to watch rather than a dialogue
        to hold. Called once, immediately after the process exists and before
        the output readers start, so a conversation can be underway by the
        time the first byte arrives.

        ``lifecycle`` is this turn's handle on the two things a handshake has
        to be able to do: report the native conversation it just learned, and
        find out that the turn was cancelled while it was talking. A transport
        that submits work calls ``lifecycle.check_not_cancelled()`` immediately
        before doing so, which is what keeps a cancellation arriving mid-
        handshake from costing a whole turn's work. Optional because a caller
        may open a transport outside a turn, where there is no lifecycle to
        speak of.
        """
        del process, invocation, prompt, lifecycle
        return None

    def live_delivery_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: harness_types.HarnessInvocation,
        transport: harness_transport.HarnessTransport | None = None,
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

    def _cancelled_result(
        self,
        invocation: harness_types.HarnessInvocation,
        stage: harness_types.CancelStage,
        *,
        process_sweep: dict[str, Any] | None = None,
        conversation_id: str | None = None,
    ) -> harness_types.HarnessResult:
        """The result for a turn stopped before it produced any output.

        Reported with the same ``aborted``/137 shape a mid-execution
        cancellation uses, so nothing downstream has to learn a second spelling
        of "the operator stopped this"; ``cancel_stage`` is what says how much
        work it cost.
        """
        return harness_types.HarnessResult(
            name=self.name,
            argv=invocation.argv,
            transport=invocation.transport,
            returncode=137,
            stdout="",
            stderr="",
            aborted=True,
            process_sweep=process_sweep,
            conversation_id=conversation_id,
            cancel_stage=stage,
        )

    def _stop_before_submission(
        self,
        process: subprocess.Popen,
    ) -> dict[str, Any] | None:
        """Take down a spawned process that never got work, and account for it.

        The output readers have not started on this path, so the pipes are
        closed here rather than by them. The descendant snapshot is still taken
        before the kill for the same reason it is on the execution path: a
        child that left the process group is only reachable from a list made
        while it was still running.
        """
        prior_snapshot: tuple[Mapping[str, Any], ...] = ()
        if process.poll() is None:
            prior_snapshot = harness_process._snapshot_descendants(
                process.pid, harness_process._sweep_process_table()
            )
            harness_process._kill_process_group(process)
            process.wait()
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.close()
        return harness_process._sweep_turn_descendants(process, prior_snapshot)

    def run(
        self,
        *,
        cwd: Path,
        plan_dir: Path | None,
        prompt: str,
        unsafe_bypass: bool = False,
        last_message_path: Path | None = None,
        timeout: int | None = None,
        stream_callback: harness_types.StreamCallback | None = None,
        model: str | None = None,
        effort: str | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_final_result: Callable[[], None] | None = None,
        on_observed_model: Callable[[str], None] | None = None,
        access: harness_types.AccessProfile = harness_types.WORKER_ACCESS,
        abort: harness_process.AbortSignal | None = None,
        live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None,
        invocation: harness_types.HarnessInvocation | None = None,
        on_transport: Callable[[harness_transport.HarnessTransport | None], None]
        | None = None,
        env: Mapping[str, str] | None = None,
        on_conversation: harness_types.ConversationCallback | None = None,
        on_prompt_submitted: harness_conversation.SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
    ) -> harness_types.HarnessResult:
        return harness_execution.run_adapter(
            self,
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
            invocation=invocation,
            on_transport=on_transport,
            env=env,
            on_conversation=on_conversation,
            on_prompt_submitted=on_prompt_submitted,
            resume_conversation_id=resume_conversation_id,
        )
