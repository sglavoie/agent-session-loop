"""The codex harness adapter.

Builds codex argv and runs a turn over either of its two transports: the
app-server conversation (`codex_app_server`) and the one-shot `codex exec`
fallback. Argv builders live in `harness_codex_argv` and the transports in
`harness_codex_transports`. The adapter-agnostic turn loop, result types and
registry stay in `harnesses`, which this module imports and which imports this
one only lazily.
"""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from . import codex_app_server, guidance_delivery
from .harness_codex_argv import CodexArgvBuilder
from .harness_codex_transports import (
    _CodexAppServerTransport,
    _CodexExecTransport,
    _ExecContinuityObserver,
    _FallbackWindowStream,
)
from .harnesses import (
    _CONVERSATION_ABANDON_RETURNCODE,
    CANCEL_STAGE_HANDSHAKE,
    TRANSPORT_APP_SERVER,
    TRANSPORT_EXEC,
    WORKER_ACCESS,
    AbortSignal,
    AccessProfile,
    ConversationCallback,
    HarnessError,
    HarnessInvocation,
    HarnessResult,
    HarnessTransport,
    StreamCallback,
    SubmissionCallback,
    TurnLifecycle,
)


class CodexAdapter(CodexArgvBuilder):
    """Codex turns over the app-server, with the one-shot exec fallback."""

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
