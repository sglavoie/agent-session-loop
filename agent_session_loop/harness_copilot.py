"""The copilot harness adapter.

Builds copilot argv and runs a turn either one-shot (the audit profile) or over
the ACP conversation (`copilot_acp`). The adapter-agnostic turn loop, result
types and registry stay in `harnesses`, which this module imports and which
imports this one only lazily.
"""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from . import copilot_acp, guards, guidance_delivery
from .harnesses import (
    _CONVERSATION_ABANDON_RETURNCODE,
    CANCEL_STAGE_HANDSHAKE,
    TRANSPORT_ACP,
    WORKER_ACCESS,
    AbortSignal,
    AccessProfile,
    ConversationCallback,
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

#: Copilot tools that read or search. Verified against the installed CLI rather
#: than inferred from its help text, because an unknown name in
#: ``--available-tools`` silently yields an agent with no tools at all.
COPILOT_AUDIT_TOOLS = "view,rg,glob"

#: Copilot tool families denied outright. Denial beats any allow rule, so these
#: hold even if a future default widens ``--available-tools``.
COPILOT_AUDIT_DENIED_TOOLS = ("write", "shell", "url")

#: The bypass flag a worker turn is launched with, named here because the ACP
#: permission responder reads it back off argv to decide what it may answer.
COPILOT_ALLOW_ALL_TOOLS = "--allow-all-tools"


def _decline_active_steer(
    request: guidance_delivery.GuidanceRequest,
) -> guidance_delivery.GuidanceOutcome:
    """Copilot's answer to a live steer, which is a considered "no".

    Neither copilot transport exposes a same-turn steer. The one-shot form
    takes its whole prompt on argv and has nowhere to put a second one; the ACP
    form holds a conversation, but the only in-turn control it offers is
    cancellation, and cancelling a turn to say something to it would destroy
    the work the guidance was meant to redirect.

    Declining explicitly is the point: it turns a silent ``harness-unsupported``
    into ``unsupported-active-steer``, which says the harness was asked and the
    answer is next turn. The guidance stays deliverable, and the next turn's
    prompt carries it.
    """
    del request
    return guidance_delivery.deferred(guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER)


class _CopilotOneShotTransport(HarnessTransport):
    """The audit turn's transport: no conversation, only a steer to decline."""

    def output_processor(self) -> _OutputProcessor:
        return _OutputProcessor()

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return _decline_active_steer


class _CopilotAcpOutputProcessor(_OutputProcessor):
    """Render one ACP conversation as the turn's readable stdout.

    Thin by design: every decision about the protocol lives in the session, and
    this only routes stdout into it and reports what it learned. stderr is left
    alone, because copilot writes its own diagnostics there in prose.
    """

    def __init__(self, session: copilot_acp.CopilotAcpSession) -> None:
        self._session = session

    @property
    def final_result_seen(self) -> bool:
        # The stop reason is copilot's terminal record, and it is as
        # authoritative as claude's ``result`` record: the turn is over even if
        # the process has not exited yet.
        return self._session.stop_reason is not None

    @property
    def observed_model(self) -> str | None:
        return self._session.observed_model

    def process_chunk(self, stream_name: StreamName, text: str) -> str:
        if stream_name != "stdout":
            return text
        rendered = self._session.feed(text)
        if text and not text.endswith("\n"):
            # The reader hands over whatever ``readline`` produced, and that
            # only lacks a trailing newline at EOF -- so this is the agent's
            # last word. Flushing it is what keeps prose printed on the way out
            # of the process from sitting in the decoder's buffer unlogged.
            rendered += self._session.flush()
        return rendered

    def last_message(
        self,
        *,
        stdout: str,
        stderr: str,
        last_message_path: Path | None,
    ) -> str:
        # The protocol streams the assistant message directly, so the
        # reassembled text is preferred over whatever prose the one-shot form
        # used to leave on stdout.
        final = self._session.final_message
        if final:
            return final if final.endswith("\n") else f"{final}\n"
        del stderr
        return _read_last_message(last_message_path) or stdout


class _CopilotAcpTransport(HarnessTransport):
    """The adapter's handle on one live copilot ACP conversation."""

    def __init__(self, session: copilot_acp.CopilotAcpSession) -> None:
        self.session = session
        self._processor = _CopilotAcpOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return _decline_active_steer

    def interrupt(self) -> bool:
        return self.session.cancel()

    def shutdown(self) -> None:
        # No cancel here, like the claude transport and unlike the app-server.
        # By the time the harness reaches its ``finally`` the process has been
        # waited on and the stdout readers abandoned, so a cancellation sent
        # from here would have nothing left to stop and no reader to observe
        # the ``cancelled`` stop reason it exists to produce. The execution
        # window sends it, where both are still true.
        self.session.shutdown()


class CopilotAdapter(HarnessAdapter):
    name = "copilot"
    skill_prefix = "/"
    enforces_audit_read_only = True
    delivers_guidance_next_turn = True
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
                argv.extend([COPILOT_ALLOW_ALL_TOOLS, "--allow-all-paths"])
        argv.extend(["--no-ask-user", "--silent"])
        if model is not None:
            argv.extend(["--model", model])
        if effort is not None:
            argv.extend(["--effort", effort])
        if access.is_audit:
            # The audit turn keeps the one-shot form exactly as it was. Its
            # restrictions are the argv above -- a fixed tool set, denied
            # families, no MCPs, no remote, no temp dir -- and moving it onto a
            # protocol would put a permission responder in front of tools that
            # currently cannot be asked for at all. Nothing about an audit needs
            # a conversation, so it does not get one.
            argv.extend(["-p", prompt])
            return argv
        # The prompt is deliberately absent from a worker argv: it becomes the
        # ACP ``session/prompt`` payload, so it never reaches a process listing,
        # a crash report, or a printed invocation.
        del prompt
        argv.append("--acp")
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
        resume_conversation_id: str | None = None,
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
            resume_conversation_id=resume_conversation_id,
        )
        if access.is_audit:
            if resume_conversation_id is not None:
                # The audit profile keeps the one-shot form, which holds no
                # conversation at all. Refusing here rather than in
                # ``open_transport`` is what stops the adapter's now-true
                # ``resumes_conversations`` from letting an audit turn through
                # to a transport that would ignore the request and run fresh.
                raise HarnessError(
                    "a copilot audit turn cannot resume conversation "
                    f"{resume_conversation_id}; it runs one-shot and holds none"
                )
            return invocation
        return dataclasses.replace(invocation, transport=TRANSPORT_ACP)

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_ACP:
            if invocation.resume_conversation_id is not None:
                # ``build_invocation`` already refuses this, so reaching it means
                # a caller assembled the invocation itself. Refusing again is
                # cheaper than the alternative, which is a turn that ran fresh
                # while its caller believed it had continued something.
                raise HarnessError(
                    "a copilot one-shot turn cannot resume conversation "
                    f"{invocation.resume_conversation_id}"
                )
            # The audit turn submits nothing here: its prompt already went out
            # on argv, so there is no checkpoint to offer and a cancellation is
            # handled by the execution window like any other one-shot turn.
            del process, prompt, lifecycle
            return _CopilotOneShotTransport()
        session = copilot_acp.CopilotAcpSession(
            process=process,
            prompt=prompt,
            cwd=str(invocation.cwd),
            # Read back off the argv the process was actually spawned with,
            # which is what makes "the responder restates the launch grant and
            # never exceeds it" structurally true rather than a convention two
            # call sites have to keep in step.
            permissions=copilot_acp.PermissionPolicy.for_turn(
                is_audit=False,
                unsafe_bypass=COPILOT_ALLOW_ALL_TOOLS in invocation.argv,
            ),
            expected_model=invocation.model,
            expected_effort=invocation.effort,
            # Intent, carried through unchanged. The session either restores
            # exactly this conversation or fails the turn; there is no path
            # from here to a fresh one.
            resume_conversation_id=invocation.resume_conversation_id,
            # Runs on the driver thread between the session and the prompt, so
            # the identifier is durable before any work is submitted. Like the
            # gate below it cannot raise out of here, so the session records
            # the failure and ``run`` restates it as the abandonment it is.
            on_conversation=(
                None if lifecycle is None else lifecycle.capture_conversation
            ),
            # Runs on the session's driver thread, immediately before
            # ``session/prompt``. Its failure cannot be raised out of here --
            # the handshake is already backgrounded by then -- so the session
            # records it and ``run`` below restates it as the cancellation it
            # is. Session 2's handoff noted that the one-shot adapter ignored
            # the lifecycle entirely, so a copilot cancellation could only ever
            # be seen ``unstarted`` or ``execution``; this is the missing
            # ``handshake`` window.
            before_submit=None if lifecycle is None else lifecycle.refuse_submission,
            # Runs on the driver thread the instant ``session/prompt`` is
            # written, which is the only moment a copilot turn can honestly be
            # called submitted: the request itself does not answer until the
            # turn is over, and every refusal above it -- a declined
            # ``session/load`` most of all -- leaves the prompt unsent.
            on_submitted=None if lifecycle is None else lifecycle.note_prompt_submitted,
        )
        # The last checkpoint before the session is created. A cancellation seen
        # here costs a copilot spawn; the same cancellation a moment later costs
        # the turn.
        if lifecycle is not None:
            lifecycle.check_not_cancelled()
        session.start()
        return _CopilotAcpTransport(session)

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
        """Run the turn, then report what the protocol said rather than argv.

        There is no fallback to the one-shot form. Copilot's ACP mode is the
        same binary with the same flags reading its prompt from a pipe instead
        of argv, so a conversation that fails after ``session/prompt`` has spent
        the turn -- and re-running it would execute the same work twice.
        """
        # Captured rather than stored on ``self``: one adapter instance serves
        # every turn of a run, and the session belongs to exactly one of them.
        opened: list[copilot_acp.CopilotAcpSession] = []

        def capture(transport: HarnessTransport | None) -> None:
            if isinstance(transport, _CopilotAcpTransport):
                opened.append(transport.session)
            if on_transport is not None:
                on_transport(transport)

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
            on_conversation=on_conversation,
            on_prompt_submitted=on_prompt_submitted,
            resume_conversation_id=(
                resume_conversation_id
                if invocation is None
                else invocation.resume_conversation_id
            ),
        )
        if not opened:
            if on_conversation is not None and access.is_audit:
                # The audit turn never opened a conversation, so nothing
                # captured one. A caller that asked which conversation the turn
                # ran on is owed the word "unavailable" rather than a silent
                # ``None`` that reads like a transport that simply failed.
                note = "copilot audit turn runs one-shot, so it names no conversation"
                return self._with_stderr_note(
                    dataclasses.replace(result, continuity_unavailable=note),
                    note,
                )
            return result
        return self._acp_result(result, opened[0])

    def _acp_result(
        self,
        result: HarnessResult,
        session: copilot_acp.CopilotAcpSession,
    ) -> HarnessResult:
        """Restate the turn's outcome from the protocol rather than the exit code.

        An ACP agent exits 0 whether the turn it ran succeeded or failed, and
        exits by our own ``terminate`` on the shutdown path, so its return code
        carries no verdict. The turn's own stop reason does -- and only
        ``end_turn`` is one: ``refusal``, ``cancelled`` and either of the two
        budget exhaustions all describe a turn that stopped short of the work it
        was given.

        Two outcomes are settled before that, because in both of them nothing
        was submitted at all and the agent's clean exit describes a turn that
        never started rather than one that succeeded.
        """
        conversation_error = session.conversation_error
        if conversation_error is not None:
            # The session was refused the moment its identifier could not be
            # persisted, before ``session/prompt``. Running the work anyway
            # would spend the turn into a conversation nothing could find again,
            # so the turn is reported abandoned and names no conversation --
            # which is the whole reason it refused to run.
            return self._with_stderr_note(
                dataclasses.replace(
                    result,
                    returncode=_CONVERSATION_ABANDON_RETURNCODE,
                    conversation_error=conversation_error,
                    conversation_id=None,
                    timed_out=False,
                    aborted=False,
                    interrupted=False,
                ),
                f"{self.name} turn abandoned: {conversation_error}",
            )
        cancelled = session.cancelled_before_submission
        if cancelled is not None:
            # The same code and the same window the base run reports for a
            # handshake cancellation it *could* raise out of, because it is the
            # same event: a turn stopped before it submitted anything. The
            # base run's exit code is overwritten rather than consulted --
            # closing stdin with nothing submitted races the wait loop, and a
            # clean exit from a turn that never started is the one thing it
            # must not be read as.
            return dataclasses.replace(
                result,
                returncode=137,
                aborted=True,
                timed_out=False,
                interrupted=False,
                cancel_stage=CANCEL_STAGE_HANDSHAKE,
            )
        for note in self._acp_notes(session):
            result = self._with_stderr_note(result, note)
        refused = session.restore_refused
        if refused is not None:
            result = dataclasses.replace(result, conversation_restore_refused=refused)
        if result.timed_out or result.aborted or result.interrupted:
            # The turn was stopped rather than failed, and the operator asked
            # for it, so these codes stand exactly as the base run reported.
            return result
        if (
            session.stop_reason == copilot_acp.STOP_REASON_END_TURN
            and session.protocol_error is None
        ):
            returncode = 0
        elif result.returncode > 0:
            returncode = result.returncode
        else:
            # 0 (a clean agent exit after an unfinished turn) and any negative
            # signal code from our own shutdown both mean "no verdict"; neither
            # may be reported as success.
            returncode = 1
        return dataclasses.replace(result, returncode=returncode)

    def _acp_notes(self, session: copilot_acp.CopilotAcpSession) -> list[str]:
        """Everything the conversation learned that the turn's log should say."""
        notes: list[str] = []
        error = session.protocol_error
        if error is not None:
            notes.append(f"copilot acp: {error}")
        if session.resumed:
            # Both counts, because only their difference says what happened: a
            # replayed transcript proves the conversation came back, and new
            # activity is the only evidence the resumed worker did anything
            # with it. A turn that replayed a hundred updates and produced none
            # of its own must not read like a turn that worked.
            notes.append(
                f"copilot acp resumed conversation {session.session_id}: "
                f"{session.replayed_updates} transcript updates replayed, "
                f"{session.live_updates} new"
            )
        stop_reason = session.stop_reason
        if stop_reason is not None and stop_reason != copilot_acp.STOP_REASON_END_TURN:
            notes.append(f"copilot acp stopped early: {stop_reason}")
        if stop_reason is None and session.prompt_submitted:
            notes.append("copilot acp turn ended without a stop reason")
        if session.cancel_requested:
            # Said whatever the turn ended as. The cancellation is issued
            # alongside the forced kill, so the process usually dies before the
            # prompt can answer ``cancelled`` -- and a turn's log that recorded
            # only the kill would not say the conversation had been told.
            notes.append("copilot acp cancelled the turn in flight")
        for mismatch in session.option_mismatches:
            notes.append(f"copilot acp launch control mismatch: {mismatch}")
        rejected = [
            decision
            for decision in session.permission_decisions
            if decision.decision != copilot_acp.DECISION_ALLOWED
        ]
        for decision in rejected:
            notes.append(
                f"copilot acp {decision.decision} a permission request "
                f"({decision.reason}): {decision.tool}"
            )
        return notes
