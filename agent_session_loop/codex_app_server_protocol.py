"""Decode app-server output and establish the required thread and turn."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any

from . import __version__, restore_diagnosis
from .codex_app_server_transport import AppServerTransport
from .codex_app_server_types import (
    _RESTORED_SETTINGS,
    _WORK_NOTIFICATIONS,
    CLIENT_NAME,
    CLIENT_TITLE,
    TURN_STATUS_COMPLETED,
    TURN_STATUS_FAILED,
    AppServerError,
    RestoreRefused,
    _ConversationNotRecorded,
    _RequestRejected,
    _SubmissionRefused,
)


class AppServerProtocol(AppServerTransport):
    """Decode app-server output and establish the required thread and turn."""

    def feed(self, chunk: str) -> str:
        """Consume one stdout read and return what should be logged for it.

        Never raises: this runs on the reader thread, under the harness's
        side-effect lock, and an exception there would cost the turn its
        output. A message that cannot be understood is recorded as a protocol
        error and rendered as plain text instead.
        """
        rendered: list[str] = []
        for line in self._decoder.feed(chunk):
            rendered.append(self._consume_line(line))
        return "".join(rendered)

    def flush(self) -> str:
        """Render whatever a final partial read left in the buffer."""
        return "".join(self._consume_line(line) for line in self._decoder.flush())

    def _consume_line(self, line: str) -> str:
        stripped = line.strip()
        if not stripped:
            return ""
        try:
            message = json.loads(stripped)
        except ValueError:
            # Not every byte on an app-server's stdout is protocol: a wrapper
            # script or a misconfigured binary can print plain text there. It
            # is passed through rather than dropped, so a failure that only
            # says something in prose still reaches the log.
            return line + "\n"
        if not isinstance(message, dict):
            return line + "\n"
        try:
            return self._dispatch(message)
        except Exception as error:  # noqa: BLE001 - the reader must not die
            self._note_protocol_error(f"{type(error).__name__}: {error}")
            return ""

    def _dispatch(self, message: dict[str, Any]) -> str:
        if "method" in message:
            if "id" in message:
                # A server-to-client *request* (an approval prompt, say):
                # a method and an id together. Every turn runs with
                # `approvalPolicy: never`, so one arriving means the access
                # profile is not the one that was asked for.
                self._note_protocol_error(
                    f"unroutable app-server request: {message['method']!r}"
                )
                return ""
            return self._handle_notification(
                str(message["method"]),
                message.get("params") or {},
            )
        raw_id = message.get("id")
        if not isinstance(raw_id, int):
            self._note_protocol_error(f"unroutable app-server message: {raw_id!r}")
            return ""
        with self._state_lock:
            pending = self._pending.pop(raw_id, None)
        if pending is None:
            self._note_protocol_error(f"response to unknown request id {raw_id}")
            return ""
        if "error" in message:
            pending.resolve(
                rejection=restore_diagnosis.ProviderRejection.from_payload(
                    message.get("error")
                )
            )
            return ""
        pending.resolve(result=message.get("result"))
        return ""

    def _handle_notification(self, method: str, params: Mapping[str, Any]) -> str:
        if method in _WORK_NOTIFICATIONS or method.startswith("item/"):
            with self._state_lock:
                self._work_observed = True
        if method == "turn/started":
            return ""
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            return delta if isinstance(delta, str) else ""
        if method == "item/completed":
            return self._render_completed_item(params.get("item"))
        if method == "error":
            error = params.get("error")
            message = ""
            if isinstance(error, Mapping):
                message = str(error.get("message", ""))
            return f"error: {message}\n" if message else ""
        if method == "turn/completed":
            return self._handle_turn_completed(params)
        return ""

    def _render_completed_item(self, item: object) -> str:
        if not isinstance(item, Mapping):
            return ""
        item_type = item.get("type")
        if item_type == "userMessage":
            # Deliberately unrendered. The app-server echoes the prompt and
            # every steered guidance body back as a user message, and this is
            # the one place they could reach a persisted artifact.
            return ""
        if item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                self._note_agent_message(text, phase=item.get("phase"))
            return "\n"
        if item_type == "commandExecution":
            command = item.get("command")
            if isinstance(command, list):
                command = " ".join(str(token) for token in command)
            exit_code = item.get("exitCode")
            suffix = "" if exit_code is None else f" (exit {exit_code})"
            return f"$ {command}{suffix}\n"
        return ""

    def _handle_turn_completed(self, params: Mapping[str, Any]) -> str:
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            self._note_protocol_error("turn/completed carried no turn")
            return ""
        turn_id = turn.get("id")
        with self._state_lock:
            expected = self._turn_id
        if expected is not None and turn_id != expected:
            # Another turn on the same thread finished. This session owns one
            # turn and must not treat a stranger's completion as its own.
            return ""
        status = str(turn.get("status", TURN_STATUS_COMPLETED))
        items = turn.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str):
                        self._note_agent_message(text, phase=item.get("phase"))
        error = turn.get("error")
        detail = ""
        if isinstance(error, Mapping):
            detail = str(error.get("message", ""))
        with self._state_lock:
            self._turn_status = status
            # Only `failed` is an error. An interrupted turn ran the work it
            # ran and was stopped on purpose, and recording that as a protocol
            # error would restate a cancellation as a harness that broke.
            if status == TURN_STATUS_FAILED and self._protocol_error is None:
                self._protocol_error = detail or "turn failed"
        self._turn_finished.set()
        line = f"turn {status}"
        if detail:
            line = f"{line}: {detail}"
        return line + "\n"

    def _note_agent_message(self, text: str, *, phase: object) -> None:
        with self._state_lock:
            if phase == "final_answer":
                self._final_message = text
                self._final_answer_seen = True
            elif not self._final_answer_seen:
                self._final_message = text

    def _handshake(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_TITLE,
                    "version": __version__,
                }
            },
        )
        self._notify("initialized", None)
        self._open_thread()
        # Between the thread and the turn, which is the only ordering that
        # makes continuity survive a crash: after this returns the identifier
        # is durable, and before it returns there is no work to be lost.
        self._capture_conversation(self._require_thread_id())
        # The last checkpoint. Everything above is recoverable; the request
        # below is the one that spends the turn.
        self._gate_submission()
        turn_params = dict(self._plan.turn_params)
        turn_params["threadId"] = self._require_thread_id()
        turn_params["input"] = [{"type": "text", "text": self._prompt}]
        # Two reports, because the request has two edges and a process can be
        # lost between them. The first fires as the prompt goes out and says
        # only that it did; the second fires on the answer and says the
        # app-server has it. Everything above this line is a conversation that
        # can still be declined -- a refused ``thread/resume`` most of all --
        # and a caller told the prompt was submitted there would have consumed
        # words this process is still holding.
        turn = self._request(
            "turn/start",
            turn_params,
            on_sending=self._note_submission_in_flight,
        )
        self._adopt_turn(turn)
        self._note_submitted()

    def _open_thread(self) -> None:
        """Start a fresh thread, or restore exactly the one that was asked for.

        The two calls answer in the same shape, so they share an adoption path;
        what differs is how much of the answer has to be re-checked, and that a
        resume names the thread it expects back.
        """
        params = dict(self._plan.thread_params)
        resume = self._resume_conversation_id
        if resume is None:
            self._adopt_thread(self._request("thread/start", params), resumed=False)
            return
        params["threadId"] = resume
        try:
            response = self._request("thread/resume", params)
        except _RequestRejected as error:
            detail = f"could not restore conversation {resume}: {error}"
            if not error.rejection.diagnoses_missing_history():
                # The app-server answered, but it did not say the history is
                # gone -- it said something about itself, its credentials, or
                # this moment. The turn still fails; the thread stays a thread,
                # so repairing whatever this was and re-running continues it.
                raise AppServerError(detail) from error
            # This answer *is* about the history. Nothing a later launch does
            # changes it, so the refusal is allowed to outlive the turn.
            raise RestoreRefused(detail) from error
        self._adopt_thread(response, resumed=True)

    def _adopt_thread(self, response: Any, *, resumed: bool) -> None:
        method = "thread/resume" if resumed else "thread/start"
        if not isinstance(response, Mapping):
            raise AppServerError(f"{method} returned no result")
        thread = response.get("thread")
        if not isinstance(thread, Mapping) or not isinstance(thread.get("id"), str):
            raise AppServerError(f"{method} returned no thread id")
        thread_id = str(thread["id"])
        if resumed and thread_id != self._resume_conversation_id:
            # "Resume the latest" is the failure mode this whole path exists to
            # avoid, and an id that came back different is exactly that -- a
            # conversation nobody asked to continue, about to be handed a turn.
            raise AppServerError(
                f"thread/resume returned thread {thread_id!r}, "
                f"expected {self._resume_conversation_id!r}"
            )
        sandbox = response.get("sandbox")
        actual = sandbox.get("type") if isinstance(sandbox, Mapping) else None
        if actual != self._plan.expected_sandbox:
            # The access profile is a promise made to the operator, and the
            # only honest place to check it is the app-server's own echo of
            # what it applied. Failing here is before `turn/start`, so the
            # legacy transport -- which states the profile in argv -- can
            # still run this turn.
            raise AppServerError(
                f"{method} applied sandbox "
                f"{actual!r}, expected {self._plan.expected_sandbox!r}"
            )
        if resumed:
            self._require_restored_settings(response)
        model = response.get("model")
        with self._state_lock:
            self._thread_id = thread_id
            if isinstance(model, str) and model:
                self._observed_model = model

    def _require_restored_settings(self, response: Mapping[str, Any]) -> None:
        """Refuse a restored thread whose settings are not the ones requested.

        A fresh thread applies what ``thread/start`` asked for, so its echo can
        only disagree about the sandbox the server derived. A restored one
        carries the settings of the thread it restores, and only the echo says
        which of the two won -- so every requested setting that changes what
        the turn may do is re-checked here rather than assumed.
        """
        for key in _RESTORED_SETTINGS:
            if key not in self._plan.thread_params:
                continue
            requested = self._plan.thread_params[key]
            applied = response.get(key)
            if applied != requested:
                raise AppServerError(
                    f"thread/resume applied {key} {applied!r}, expected {requested!r}"
                )

    def _capture_conversation(self, thread_id: str) -> None:
        """Hand the thread identifier to the caller, or abandon the turn."""
        sink = self._on_conversation
        if sink is None:
            return
        try:
            sink(thread_id)
        except Exception as error:
            raise _ConversationNotRecorded(
                str(error) or type(error).__name__
            ) from error

    def _gate_submission(self) -> None:
        """Ask the caller whether this turn may still be submitted."""
        gate = self._before_submit
        if gate is None:
            return
        try:
            gate()
        except Exception as error:
            raise _SubmissionRefused(str(error) or type(error).__name__) from error

    def _note_submission_in_flight(self) -> None:
        """Record that ``turn/start`` is going out, before it can be seen to.

        Runs from inside the write, after the pipe has been found open and
        before a byte of the prompt is handed to it, which is the last instant
        at which "nothing was sent" is still true. Placing it after the write
        would leave the partial write -- prompt on the wire, exception here --
        looking exactly like a turn that never sent anything.

        Never raises, for the same reason :meth:`_note_submitted` does not: it
        would surface as a protocol error the app-server never made, and it
        would do so while the prompt is already leaving.
        """
        with self._state_lock:
            self._turn_dispatched = True
        sink = self._on_submission_in_flight
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink()

    def _note_submitted(self) -> None:
        """Tell the caller the prompt is the worker's now.

        Never raises: this runs on the driver thread, after the turn has been
        accepted, where there is nothing left to abandon and an exception would
        only be swallowed by :meth:`_drive` as a protocol error the app-server
        never made.
        """
        sink = self._on_submitted
        if sink is None:
            return
        with contextlib.suppress(Exception):
            sink()

    def _abandon_before_submission(
        self,
        *,
        conversation_error: str | None = None,
        submission_refused: str | None = None,
    ) -> None:
        """End a healthy conversation that was told not to submit its turn.

        No protocol error is recorded, because none happened: the app-server is
        working and is simply being let go without a turn. That distinction is
        what keeps this out of the fallback window.
        """
        with self._state_lock:
            if conversation_error is not None:
                self._conversation_error = conversation_error
            if submission_refused is not None:
                self._submission_refused = submission_refused
        self._turn_finished.set()
        self._close_stdin()

    def _adopt_turn(self, response: Any) -> None:
        turn = response.get("turn") if isinstance(response, Mapping) else None
        if not isinstance(turn, Mapping) or not isinstance(turn.get("id"), str):
            raise AppServerError("turn/start returned no turn id")
        with self._state_lock:
            self._turn_id = str(turn["id"])
            self._turn_accepted = True
            self._work_observed = True

    def _require_thread_id(self) -> str:
        with self._state_lock:
            thread_id = self._thread_id
        if thread_id is None:
            raise AppServerError("no thread has been started")
        return thread_id
