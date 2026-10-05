"""The codex harness adapter.

Builds codex argv and runs a turn over either of its two transports: the
app-server conversation (`codex_app_server`) and the one-shot `codex exec`
fallback. The adapter-agnostic turn loop, result types and registry stay in
`harnesses`, which this module imports and which imports this one only lazily.
"""

from __future__ import annotations

import dataclasses
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import codex_app_server, guidance_delivery
from .harnesses import (
    _CONVERSATION_ABANDON_RETURNCODE,
    CANCEL_STAGE_HANDSHAKE,
    TRANSPORT_APP_SERVER,
    TRANSPORT_EXEC,
    WORKER_ACCESS,
    AbortSignal,
    AccessProfile,
    ConversationCallback,
    ConversationCaptureError,
    HarnessAdapter,
    HarnessError,
    HarnessInvocation,
    HarnessResult,
    HarnessTransport,
    StreamCallback,
    StreamName,
    SubmissionCallback,
    TurnLifecycle,
    _OutputProcessor,
    _read_last_message,
)


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

    def interrupt(self) -> bool:
        return self.session.interrupt()

    def shutdown(self) -> None:
        self.session.shutdown()


#: The session identifier ``codex exec`` states in its own header. Anchored on
#: the UUID shape the app-server uses for the same value, so the only thing
#: this can match is a conversation id -- and a header spelled any other way
#: reports unavailable continuity instead of capturing a guess.
_CODEX_EXEC_SESSION_ID = re.compile(
    r"session[ _-]?id[^\n]{0,24}?"
    r"(?P<session_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


class _CodexExecOutputProcessor(_OutputProcessor):
    """Read the legacy transport's one continuity fact out of its own output.

    ``codex exec`` takes its prompt on argv, so the work is submitted the
    moment the process is spawned and there is no handshake in which to learn a
    conversation first. The session it ran on is stated in its header instead,
    which is continuity observed *after* the fact -- the opposite of the
    ordering the app-server transport keeps, and the reason a capture failure
    here cannot retract anything.

    Everything else is the plain processor's behaviour: the text is passed
    through untouched, because this reads the output rather than rendering it.
    """

    def __init__(self, lifecycle: TurnLifecycle | None) -> None:
        self._lifecycle = lifecycle
        self.native_id: str | None = None
        self.capture_error: str | None = None

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if self.native_id is None and self.capture_error is None:
            self._observe(text)
        return text

    def _observe(self, text: str) -> None:
        """Capture the first session id the turn names, and only the first.

        The header is one line and the readers deliver whole lines, so a match
        is either in this chunk or in none. A second id would be a different
        conversation for the same turn, which the capture seam refuses anyway;
        stopping at the first keeps that refusal from being reached at all.
        """
        match = _CODEX_EXEC_SESSION_ID.search(text)
        if match is None:
            return
        native_id = match.group("session_id")
        lifecycle = self._lifecycle
        if lifecycle is not None:
            try:
                lifecycle.capture_conversation(native_id)
            except ConversationCaptureError as error:
                # Raising would surface as a stream-callback failure and lose
                # the turn, which already ran. Reported as continuity that
                # could not be had instead -- the honest outcome for work that
                # cannot be un-submitted.
                self.capture_error = str(error)
                return
        self.native_id = native_id


class _CodexExecTransport(HarnessTransport):
    """The legacy one-shot turn's continuity observer.

    It holds no conversation -- ``codex exec`` has none to hold -- and exists
    only so the turn's own output still yields the session it ran on. There is
    no live channel and nothing to interrupt beyond the process itself, so
    guidance defers and interruption declines exactly as they did when this
    transport was ``None``.
    """

    def __init__(self, lifecycle: TurnLifecycle | None) -> None:
        self._processor = _CodexExecOutputProcessor(lifecycle)
        self._wanted = lifecycle is not None and lifecycle.wants_conversation

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    @property
    def continuity_note(self) -> str | None:
        """Why this turn has no conversation to continue, if it has none.

        ``None`` when one was captured, and also when nobody asked: an ordinary
        one-shot turn is not missing something it was never meant to have. The
        identifier is still read out of the output either way, so a result
        reports the session it ran on whether or not a sink wanted it.
        """
        if not self._wanted:
            return None
        if self._processor.capture_error is not None:
            return self._processor.capture_error
        if self._processor.native_id is not None:
            return None
        return "codex exec output named no session id"


class _ExecContinuityObserver:
    """Keep hold of a legacy turn's transport so its result can report it.

    ``open_transport`` builds the transport and ``run`` returns the result, and
    nothing in between hands one to the other -- so this rides along as the
    ``on_transport`` hook, passing every call through to the caller's own hook
    and keeping the one transport it recognizes.
    """

    def __init__(
        self,
        downstream: Callable[[HarnessTransport | None], None] | None,
    ) -> None:
        self._downstream = downstream
        self.transport: _CodexExecTransport | None = None

    def __call__(self, transport: HarnessTransport | None) -> None:
        if isinstance(transport, _CodexExecTransport):
            self.transport = transport
        if self._downstream is not None:
            self._downstream(transport)

    def apply(self, result: HarnessResult) -> HarnessResult:
        """Report what continuity the turn's output did or did not name.

        The identifier itself is already on the result: it was captured through
        the lifecycle, which is what fills ``conversation_id``. Only its
        *absence* needs stating, because a caller that asked to continue this
        conversation later would otherwise read "none" as "not yet looked".
        """
        transport = self.transport
        if transport is None:
            return result
        note = transport.continuity_note
        if note is None:
            return result
        return dataclasses.replace(result, continuity_unavailable=note)


class CodexAdapter(HarnessAdapter):
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

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_APP_SERVER:
            # The legacy transport holds no conversation, but its own output
            # names the session it ran on, and that is the only place a turn
            # whose prompt was argv can learn its continuity from.
            return _CodexExecTransport(lifecycle)
        if invocation.turn_plan is None:
            raise HarnessError("codex app-server turns require a turn plan")
        session = codex_app_server.CodexAppServerSession(
            process=process,
            plan=invocation.turn_plan,
            prompt=prompt,
            resume_conversation_id=invocation.resume_conversation_id,
            # Both hooks run on the session's driver thread, between the thread
            # call and `turn/start`. Their failures cannot be raised out of
            # here -- the handshake is already backgrounded by then -- so the
            # session records them and `run` below restates them as the
            # abandoned and cancelled results they are.
            on_conversation=None
            if lifecycle is None
            else lifecycle.capture_conversation,
            before_submit=None if lifecycle is None else lifecycle.refuse_submission,
            # The third of the same set, on the far side of the submission: it
            # fires once `turn/start` has been accepted, which is where a codex
            # turn stops being one this process could still decline. A restore
            # the server refuses never reaches it.
            on_submitted=None if lifecycle is None else lifecycle.note_prompt_submitted,
            # And the boundary between the two, which is the one an app-server
            # that dies mid-turn leaves behind: the prompt went out, the answer
            # never came, and the turn must end saying it cannot tell rather
            # than saying it sent nothing.
            on_submission_in_flight=(
                None if lifecycle is None else lifecycle.note_submission_in_flight
            ),
        )
        # The last checkpoint before the thread and the turn are created. A
        # cancellation seen here costs an app-server spawn; the same
        # cancellation a moment later costs the turn.
        if lifecycle is not None:
            lifecycle.check_not_cancelled()
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
        on_conversation: ConversationCallback | None = None,
        on_prompt_submitted: SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
    ) -> HarnessResult:
        """Run the turn on the app-server, falling back only before it starts.

        The fallback window is narrow on purpose. ``session.fallback_reason``
        is non-``None`` only while the conversation failed before ``turn/start``
        was accepted *and* before the app-server emitted anything the turn did,
        which is the only state in which re-running the prompt cannot execute
        it twice. Once the turn is accepted, a protocol failure is reported as
        a failed turn -- never retried. A turn asked to *resume* a conversation
        has no fallback window at all: the legacy transport starts a fresh
        session, so falling back would answer "continue this conversation" with
        one that continues nothing.
        """
        if invocation is not None and invocation.transport == TRANSPORT_EXEC:
            # An explicitly legacy invocation (a fallback re-entry, or a caller
            # that built one deliberately) is run as-is.
            observer = _ExecContinuityObserver(on_transport)
            return observer.apply(
                super().run(
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
                    on_transport=observer,
                    on_conversation=on_conversation,
                    on_prompt_submitted=on_prompt_submitted,
                    resume_conversation_id=invocation.resume_conversation_id,
                )
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
                resume_conversation_id=resume_conversation_id,
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
            on_conversation=on_conversation,
            on_prompt_submitted=on_prompt_submitted,
            resume_conversation_id=invocation.resume_conversation_id,
        )
        if not opened:
            window.release()
            return result
        session = opened[0]

        abandoned = self._abandoned_result(result, session)
        if abandoned is not None:
            # Nothing was submitted, so there is nothing to stream and nothing
            # to fall back to: this turn was stopped on purpose, before it
            # became a turn at all.
            window.discard()
            return abandoned

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
        observer = _ExecContinuityObserver(on_transport)
        fallback_result = observer.apply(
            super().run(
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
                on_transport=observer,
                on_conversation=on_conversation,
                on_prompt_submitted=on_prompt_submitted,
                resume_conversation_id=legacy.resume_conversation_id,
            )
        )
        return self._with_stderr_note(
            fallback_result,
            f"codex app-server unavailable, ran codex exec instead: {fallback}",
        )

    def _abandoned_result(
        self,
        result: HarnessResult,
        session: codex_app_server.CodexAppServerSession,
    ) -> HarnessResult | None:
        """Restate a turn the session refused to submit, or ``None``.

        Both refusals happen on the session's driver thread, after ``run`` has
        stopped being able to raise, so the process exits normally and the base
        result reads like an ordinary short turn. That result cannot stand: a
        conversation that was never recorded, or a turn cancelled a moment
        before ``turn/start``, must not be reported as work that ran.

        The base run's exit code is deliberately overwritten rather than
        consulted. Closing stdin with nothing submitted races the wait loop, so
        the app-server may well have exited cleanly first -- and a clean exit
        from a turn that never started is the one thing it must not be read as.
        """
        conversation_error = session.conversation_error
        if conversation_error is not None:
            return self._with_stderr_note(
                dataclasses.replace(
                    result,
                    returncode=_CONVERSATION_ABANDON_RETURNCODE,
                    conversation_error=conversation_error,
                    # No conversation was recorded, so the turn has none to
                    # report -- the whole reason it refused to run.
                    conversation_id=None,
                    timed_out=False,
                    aborted=False,
                    interrupted=False,
                ),
                f"{self.name} turn abandoned: {conversation_error}",
            )
        if session.cancelled_before_submission is None:
            return None
        return dataclasses.replace(
            result,
            # The same code and the same window the base run reports for a
            # handshake cancellation it *could* raise out of, because it is the
            # same event: a turn stopped before it submitted anything.
            returncode=137,
            aborted=True,
            timed_out=False,
            interrupted=False,
            cancel_stage=CANCEL_STAGE_HANDSHAKE,
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
        refused = session.restore_refused
        if refused is not None:
            result = dataclasses.replace(result, conversation_restore_refused=refused)
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
