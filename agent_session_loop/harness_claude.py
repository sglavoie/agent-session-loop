"""The claude harness adapter.

Builds claude argv and runs a turn over the stream-JSON conversation
(`claude_stream`), rendering its records as readable progress. The
adapter-agnostic turn loop, result types and registry stay in `harnesses`,
which this module imports and which imports this one only lazily.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

from . import claude_stream, guards, guidance_delivery, restore_diagnosis
from .harnesses import (
    TRANSPORT_STREAM_JSON,
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
    _SubmissionOnce,
)

#: Claude tools that read or search without mutating anything.
CLAUDE_AUDIT_TOOLS = "Read,Glob,Grep"

#: Claude tools denied outright, so an audit turn cannot edit, shell out, reach
#: the network, spawn a subagent, or write session state.
CLAUDE_AUDIT_DENIED_TOOLS = (
    "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Task,TodoWrite"
)


class _ClaudeStreamJsonOutputProcessor(_OutputProcessor):
    """Render Claude stream-json records as readable line-oriented progress.

    Also the turn's only reader of the CLI's protocol acknowledgements: when a
    session is attached, the terminal ``result`` records and the ``user`` echoes
    are reported to it. Rendering and acknowledgement share this one parse
    because there is only ever one reader of the stream.
    """

    def __init__(
        self, session: claude_stream.ClaudeStreamSession | None = None
    ) -> None:
        self._session = session
        self._text_buffer = ""
        self._emitted_text = False
        self._assistant_messages: list[str] = []
        self._final_result: str | None = None
        self._final_result_seen = False
        self._observed_model: str | None = None

    @property
    def final_result_seen(self) -> bool:
        return self._final_result_seen

    @property
    def observed_model(self) -> str | None:
        return self._observed_model

    def _note_observed_model(self, model: object) -> None:
        # First wins: a resumed session's later records can name a different
        # model than the one this turn started under.
        if self._observed_model is None and isinstance(model, str) and model:
            self._observed_model = model

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
        if record_type == "user":
            # The CLI echoes a user message back when it takes it up, which is
            # the protocol acknowledgement a stdin write on its own is not.
            # Never rendered: an echo carries the prompt or a guidance body,
            # and neither belongs in a turn log.
            if self._session is not None:
                self._session.note_user_echo(
                    self._assistant_text(record.get("message"))
                )
            self._final_result_seen = False
            return []
        if record_type == claude_stream.CONTROL_RESPONSE:
            # Protocol bookkeeping rather than turn output: the session
            # correlates it by ``request_id`` and nothing about it belongs in a
            # turn log, where an operator reading arrival order would be
            # guessing at the very attribution the identifier settles.
            if self._session is not None:
                self._session.note_control_response(record.get("response"))
            return []
        if record_type == "result":
            self._final_result_seen = True
            if self._session is not None:
                origin = record.get("origin")
                background_followup = (
                    isinstance(origin, dict)
                    and origin.get("kind") == "task-notification"
                )
                ended = self._session.note_result(
                    background_followup=background_followup,
                    is_error=record.get("is_error") is True,
                )
                # "Terminal result seen" and "stdin closed" are different
                # facts. A main-turn result is terminal even when the session
                # keeps stdin open for a background task or an unanswered
                # message: without it the hang sentinel never learns a result
                # arrived and post-result recovery cannot arm (ASI-151). Only a
                # task-notification follow-up that leaves the conversation
                # open is not terminal.
                self._final_result_seen = ended or not background_followup
            result = record.get("result")
            if isinstance(result, str):
                self._final_result = result
            subtype = record.get("subtype") or "unknown"
            details = self._result_details(record)
            if record.get("is_error") is True:
                details.insert(0, f"provider subtype {subtype}")
                suffix = "" if not details else f" ({', '.join(details)})"
                message = self._result_error_message(result)
                return [f"claude result: error{suffix}: {message}"]
            suffix = "" if not details else f" ({', '.join(details)})"
            return [f"claude result: {subtype}{suffix}"]
        if record_type == "error":
            message = record.get("message") or record.get("error") or "unknown"
            return [f"claude error: {message}"]
        return []

    def _render_system(self, record: dict[str, object]) -> list[str]:
        subtype = record.get("subtype")
        if subtype in ("task_started", "task_notification", "task_progress"):
            task_id = record.get("task_id")
            if isinstance(task_id, str) and task_id:
                self._final_result_seen = False
                if self._session is not None and subtype != "task_progress":
                    self._session.note_background_task(
                        task_id, finished=subtype == "task_notification"
                    )
                # Also invalidates a prior result in the heartbeat sentinel.
                # Do not render task output paths, summaries, or identifiers.
                return ["claude status: requesting"]
        if subtype == "init":
            # A long-lived CLI can emit another initialization after an early
            # result. From this point that result belongs to the prior request,
            # so the reader may notify the next terminal result independently.
            self._final_result_seen = False
            parts = ["claude init"]
            model = record.get("model")
            if model:
                self._note_observed_model(model)
                parts.append(f"model {model}")
            session_id = record.get("session_id")
            mismatch: str | None = None
            if session_id:
                parts.append(f"session {session_id}")
                if self._session is not None:
                    mismatch = self._session.note_session_id(str(session_id))
            rendered = ["; ".join(str(part) for part in parts)]
            if mismatch is not None:
                # Named on its own line rather than folded into the init line:
                # this is the one thing in that record an operator has to act
                # on, and it says the turn's recorded conversation is not the
                # one the work is about to go into.
                rendered.append(f"claude session mismatch: {mismatch}")
            return rendered
        if subtype == "status":
            status = record.get("status") or "unknown"
            if status == "requesting":
                self._final_result_seen = False
            return [f"claude status: {status}"]
        return []

    def _render_stream_event(self, event: dict[str, object]) -> list[str]:
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict) and message.get("model"):
                self._note_observed_model(message["model"])
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
        if isinstance(content, str):
            # User echoes may carry bare text where an assistant message
            # always carries blocks.
            return content.strip()
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

    def _result_error_message(self, result: object) -> str:
        """Render one bounded line from a terminal error result."""
        if not isinstance(result, str) or not result.strip():
            return "unknown"
        message = " ".join(result.split())
        if len(message) > 500:
            return f"{message[:499].rstrip()}…"
        return message

    def _ensure_trailing_newline(self, text: str) -> str:
        return text if text.endswith("\n") else f"{text}\n"


class _ClaudeStreamTransport(HarnessTransport):
    """The adapter's handle on one live claude stream-JSON conversation."""

    def __init__(self, session: claude_stream.ClaudeStreamSession) -> None:
        self.session = session
        self._processor = _ClaudeStreamJsonOutputProcessor(session)

    def output_processor(self) -> _OutputProcessor:
        return self._processor

    def guidance_transport(self) -> guidance_delivery.GuidanceTransport | None:
        return self.session.steer

    def interrupt(self) -> bool:
        return self.session.interrupt()

    def input_hold_reason(self) -> str | None:
        return self.session.stdin_hold_reason

    def release_input(self) -> bool:
        return self.session.release_stdin()

    def shutdown(self) -> None:
        # No interrupt here, unlike the app-server transport. A claude turn is
        # the process itself rather than a turn inside a server, and by the time
        # the harness reaches its ``finally`` that process has been waited on
        # and the stdout readers abandoned — so a control request sent from here
        # could never be correlated and would have nothing left to stop. The
        # execution window sends it, where a reader is still there to answer.
        self.session.shutdown()


class ClaudeAdapter(HarnessAdapter):
    name = "claude"
    skill_prefix = "/"
    enforces_audit_read_only = True
    enforces_guard_pretool = True
    captures_conversations = True
    resumes_conversations = True

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
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        # Claude's guard lane is the settings overlay written at spawn time,
        # not argv, so the turn environment says nothing this command line
        # needs.
        del cwd, last_message_path, env
        argv = [
            self.executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            # Input is a stream too, which is what makes a turn a conversation
            # rather than a one-shot: the prompt below is written to stdin as
            # the first message, and guidance can follow it while the turn runs.
            "--input-format",
            "stream-json",
            # Echoes prove which messages a result covers, including guidance
            # consumed inside the current tool loop rather than a new turn.
            "--replay-user-messages",
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
        # The prompt is deliberately absent from argv: under streaming input it
        # is the conversation's first stdin message, so it never reaches a
        # process listing, a crash report, or a printed invocation.
        del prompt
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
        argv, assigned = self._with_session_controls(
            invocation.argv,
            access=access,
            resume_conversation_id=resume_conversation_id,
        )
        return dataclasses.replace(
            invocation,
            argv=argv,
            transport=TRANSPORT_STREAM_JSON,
            assigned_conversation_id=assigned,
        )

    def _with_session_controls(
        self,
        argv: list[str],
        *,
        access: AccessProfile,
        resume_conversation_id: str | None,
    ) -> tuple[list[str], str | None]:
        """Name this turn's conversation on argv, and say which one it is.

        Two supported controls and a deliberate omission. ``--resume`` restores
        exactly the conversation asked for; ``--session-id`` assigns one the CLI
        would otherwise have minted itself, which is what makes the identifier
        knowable before the prompt is written rather than only after the CLI's
        first record arrives. ``--fork-session`` is never passed, because it
        answers "continue conversation X" with a different one — the silent
        substitution this whole path exists to prevent.

        An audit turn gets neither: it runs with ``--no-session-persistence``,
        so there is no conversation to name, assign, or come back to.
        """
        if access.is_audit:
            if resume_conversation_id is not None:
                raise HarnessError(
                    "a claude audit turn cannot resume conversation "
                    f"{resume_conversation_id}; it persists no session"
                )
            return list(argv), None
        if resume_conversation_id is not None:
            return [*argv, "--resume", resume_conversation_id], resume_conversation_id
        assigned = claude_stream.new_session_id()
        return [*argv, "--session-id", assigned], assigned

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
