"""Claude turn execution, guidance overlays, and conversation continuity.

Command construction lives in ``harness_claude_invocation`` and stream rendering
and transport in ``harness_claude_stream``. The registry loads this adapter lazily.
"""

from __future__ import annotations

import dataclasses
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from . import claude_stream, guards, guidance_delivery, restore_diagnosis
from .harness_claude_invocation import (
    CLAUDE_AUDIT_DENIED_TOOLS as CLAUDE_AUDIT_DENIED_TOOLS,
)
from .harness_claude_invocation import CLAUDE_AUDIT_TOOLS as CLAUDE_AUDIT_TOOLS
from .harness_claude_invocation import ClaudeInvocationAdapter
from .harness_claude_stream import (
    _ClaudeStreamJsonOutputProcessor as _ClaudeStreamJsonOutputProcessor,
)
from .harness_claude_stream import _ClaudeStreamTransport as _ClaudeStreamTransport
from .harnesses import (
    TRANSPORT_STREAM_JSON,
    WORKER_ACCESS,
    AbortSignal,
    AccessProfile,
    ConversationCallback,
    HarnessInvocation,
    HarnessResult,
    HarnessTransport,
    StreamCallback,
    SubmissionCallback,
    TurnLifecycle,
    _OutputProcessor,
    _SubmissionOnce,
)


class ClaudeAdapter(ClaudeInvocationAdapter):
    name = "claude"
    skill_prefix = "/"
    enforces_audit_read_only = True
    enforces_guard_pretool = True
    captures_conversations = True
    resumes_conversations = True

    def output_processor(self) -> _OutputProcessor:
        return _ClaudeStreamJsonOutputProcessor()

    def open_transport(
        self,
        *,
        process: subprocess.Popen,
        invocation: HarnessInvocation,
        prompt: str,
        lifecycle: TurnLifecycle | None = None,
    ) -> HarnessTransport | None:
        if invocation.transport != TRANSPORT_STREAM_JSON:
            return None
        spool = None
        if invocation.overlay_dir is not None:
            spool = claude_stream.ClaudeGuidanceSpool(invocation.overlay_dir / "spool")
        assigned = invocation.assigned_conversation_id
        # A turn that asked to continue a conversation holds its submission
        # report until the CLI announces a session: see
        # :meth:`_restore_refusal` for why that record, and only that record,
        # separates a ``--resume`` the CLI honoured from one it threw out.
        restorable = invocation.resume_conversation_id is not None
        opened: list[claude_stream.ClaudeStreamSession] = []

        def announce_submission() -> None:
            """The CLI took the conversation up, so the prompt is its now.

            Both halves have to hold. The write below put the words in the
            pipe, and this record says the CLI got past the ``--resume`` it
            could have thrown them out over; a turn missing either half has not
            handed anything to anybody.
            """
            if lifecycle is None or not opened or opened[0].prompt_error is not None:
                return
            lifecycle.note_prompt_submitted()

        session = claude_stream.ClaudeStreamSession(
            process=process,
            prompt=prompt,
            spool=spool,
            session_id=assigned,
            on_session_observed=announce_submission if restorable else None,
        )
        opened.append(session)
        if lifecycle is not None and assigned is not None:
            # Before the prompt, which is the submission: the identifier was
            # settled on argv, so unlike a handshake transport this turn has
            # nothing to learn first and no reason to submit before persisting.
            # A sink that raises comes out of here as a ``ConversationCapture
            # Error`` and the turn is abandoned with nothing written.
            lifecycle.capture_conversation(assigned)
        # The prompt is the submission here, so this is the checkpoint: past it
        # the CLI is working, and a cancellation has to interrupt rather than
        # decline.
        if lifecycle is not None:
            lifecycle.check_not_cancelled()
        # The prompt goes out before the readers start, so the CLI has work to
        # do from its first byte. A write failure here is not raised: the
        # process exists and has to be collected like any other failed turn,
        # with ``prompt_error`` explaining the empty output.
        if session.send_prompt() and lifecycle is not None and not restorable:
            # Only on the write that succeeded. A prompt the pipe refused is one
            # the CLI never saw, and the turn that follows it is a spawned
            # process sitting on a closed stdin -- not a worker holding the
            # operator's words.
            #
            # And only where nothing can still refuse the prompt this write put
            # in flight. A CLI that will not honour a ``--resume`` exits without
            # ever reading its stdin, so the write succeeds against a pipe whose
            # reader is on its way out: the bytes left this process and reached
            # nobody. Such a turn reports its submission from the session
            # announcement above, or from :meth:`_settle_deferred_submission`
            # once the turn is over and the refusal it would have shown is not
            # there.
            lifecycle.note_prompt_submitted()
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
        on_conversation: ConversationCallback | None = None,
        on_prompt_submitted: SubmissionCallback | None = None,
        resume_conversation_id: str | None = None,
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
                resume_conversation_id=resume_conversation_id,
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

        # Wrapped because a resumed turn can report its submission from either
        # side of the turn boundary: from the session announcement while it
        # runs, or from the settlement below once it is over.
        submission = _SubmissionOnce(on_prompt_submitted)
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
                on_conversation=on_conversation,
                on_prompt_submitted=submission,
                resume_conversation_id=invocation.resume_conversation_id,
            )
        finally:
            claude_stream.remove_overlay(overlay_dir)

        transport = opened[0] if opened else None
        session = (
            transport.session if isinstance(transport, _ClaudeStreamTransport) else None
        )
        # A prompt that never reached the CLI produces a turn with no output
        # and no explanation. Saying so on stderr is the difference between a
        # diagnosable failure and a mysteriously empty one.
        if session is not None and session.prompt_error:
            result = self._with_stderr_note(
                result,
                f"claude prompt was not delivered: {session.prompt_error}",
            )
        return self._with_continuity_note(
            result,
            session=session,
            invocation=invocation,
            access=access,
            on_conversation=on_conversation,
            submission=submission,
        )

    def _with_continuity_note(
        self,
        result: HarnessResult,
        *,
        session: claude_stream.ClaudeStreamSession | None,
        invocation: HarnessInvocation,
        access: AccessProfile,
        on_conversation: ConversationCallback | None,
        submission: _SubmissionOnce | None = None,
    ) -> HarnessResult:
        """Report a turn whose conversation is not the one it was promised.

        Two ways that happens, and neither aborts the turn. The identifier is
        settled on argv, so by the time the CLI contradicts it in ``system``/
        ``init`` the prompt is long gone and the workspace is being edited —
        killing the turn there would throw away real work to protest a label.
        And an audit turn persists no session at all, so a caller that asked
        for one is owed the word ``unavailable`` rather than silence.

        A mismatch also drops ``conversation_id``. The turn ran somewhere, but
        not demonstrably where it said it would, and an identifier a later run
        would resume on is exactly the thing that must not be guessed.
        """
        if session is not None and session.session_mismatch is not None:
            note = f"claude session mismatch: {session.session_mismatch}"
            return self._with_stderr_note(
                dataclasses.replace(
                    result,
                    continuity_unavailable=note,
                    conversation_id=None,
                ),
                note,
            )
        if (
            on_conversation is not None
            and access.is_audit
            and invocation.assigned_conversation_id is None
        ):
            note = "claude audit turn persists no session, so it names none"
            return self._with_stderr_note(
                dataclasses.replace(result, continuity_unavailable=note),
                note,
            )
        refused = self._restore_refusal(result, session=session, invocation=invocation)
        if refused is not None:
            # The refusal is also the answer to "did this prompt go out": the
            # gates it passed include the CLI never announcing a session, so
            # nothing on the other side of the pipe ever took the words up. A
            # write into a pipe whose reader is leaving must not read as a
            # worker holding an operator's correction.
            return dataclasses.replace(
                result,
                conversation_restore_refused=refused,
                prompt_submitted=False,
            )
        return self._settle_deferred_submission(
            result,
            session=session,
            invocation=invocation,
            submission=submission,
        )

    def _settle_deferred_submission(
        self,
        result: HarnessResult,
        *,
        session: claude_stream.ClaudeStreamSession | None,
        invocation: HarnessInvocation,
        submission: _SubmissionOnce | None,
    ) -> HarnessResult:
        """Close a resumed turn's held-back submission report, fail-closed.

        A resumed claude turn writes its prompt and then waits to see whether the
        CLI takes the conversation up, because only the CLI can say. It says so
        by announcing a session, and that announcement reports the submission
        while the turn is still running -- so this settles the turns where it
        never came.

        Exactly one of those is proof of anything: the turn whose own output
        diagnoses a refused restore, which is handled by the caller and reports
        nothing. Every other silence is ambiguous -- a timeout, a kill, a CLI
        that died before its first record, a reader that stopped reading -- and
        the prompt was written into that silence. So each of them reports the
        submission here rather than leaving it unreported: an unproven turn that
        claimed non-submission would hand the same words to a second worker,
        which is the one outcome this seam exists to prevent.

        The caller is told directly rather than through the turn's lifecycle,
        which is closed by now precisely so a straggling thread cannot reach it.
        This is not a straggler: the turn is over, its process is collected, and
        this is the boundary that owned it stating what it did.
        """
        if session is None or invocation.resume_conversation_id is None:
            return result
        if result.prompt_submitted:
            return result
        if session.prompt_error is not None:
            # The write itself failed, so there is nothing to settle: no bytes
            # left this process and the result already says so.
            return result
        if submission is not None:
            submission()
        return dataclasses.replace(result, prompt_submitted=True)

    def _restore_refusal(
        self,
        result: HarnessResult,
        *,
        session: claude_stream.ClaudeStreamSession | None,
        invocation: HarnessInvocation,
    ) -> str | None:
        """Read a ``--resume`` the CLI would not honour out of its own output.

        Claude has no restore handshake to fail: ``--resume`` goes on argv and
        the CLI either continues the conversation or complains and exits. So the
        refusal has to be recognized after the fact, and the recognition is
        gated three ways rather than one, because the cost of a false positive
        is a conversation retired while the CLI still holds it:

        - the turn asked to resume something, so there is a restore to refuse;
        - the CLI never announced a session of its own, which every turn it
          actually started does before it runs anything -- this is what
          separates a refused restore from a resumed conversation that then
          failed;
        - the complaint names the very conversation that was asked for, so a
          generic startup error about something else cannot be read as one;
        - and the complaint is a supported missing-history diagnosis in the
          shared vocabulary of :mod:`~agent_session_loop.restore_diagnosis`,
          which is the same one the two protocol transports weigh their error
          responses against -- so a line that names the conversation while
          saying the service is down, the credential expired, or access was
          refused is the outage it says it is.

        A stopped turn is excluded outright. A timeout or an abort says nothing
        about whether the history exists, and both can leave the CLI having
        printed nothing at all.
        """
        resume = invocation.resume_conversation_id
        if resume is None or result.returncode == 0:
            return None
        if result.timed_out or result.aborted or result.interrupted:
            return None
        if session is not None and session.session_observed:
            return None
        text = f"{result.stderr}\n{result.stdout}"
        for line in text.splitlines():
            if resume not in line:
                continue
            if restore_diagnosis.text_diagnoses_missing_history(line):
                return f"could not restore conversation {resume}: {line.strip()}"
        return None
