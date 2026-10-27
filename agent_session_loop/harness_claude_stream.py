"""Claude stream-JSON output rendering and the live transport handle."""

from __future__ import annotations

from pathlib import Path

from . import claude_stream, guidance_delivery
from .harness_json import decode_harness_line
from .harnesses import (
    HarnessTransport,
    StreamName,
    _OutputProcessor,
    _read_last_message,
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
        # Split on "\n" alone, as the reader does: ``str.splitlines`` also
        # breaks on U+2028, U+2029 and U+0085, which Node's JSON.stringify
        # leaves unescaped inside strings, and would tear such a record into
        # fragments that are logged verbatim instead of parsed.
        for raw_line in text.split("\n"):
            line = raw_line.removesuffix("\r")
            if not line.strip():
                continue
            try:
                record = decode_harness_line(line)
            except ValueError:
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
            error_text = record.get("message") or record.get("error") or "unknown"
            return [f"claude error: {error_text}"]
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
