#!/usr/bin/env python3
"""A deterministic stand-in for ``copilot --acp``.

Speaks the same newline-delimited JSON-RPC the real agent speaks, on the same
two pipes, and nothing else: no model, no tools, no timing. Every departure from
the happy path is chosen by ``FAKE_ACP_MODE`` so a test names the failure it
wants instead of racing for it.

What crossed the wire is recorded to ``FAKE_ACP_RECORD`` -- the launch argv, the
negotiated client capabilities, the session parameters, the restore parameters,
the prompt, every permission answer, and every cancellation -- which is how the
tests assert that the access restrictions really travelled the protocol and that
the prompt never came back out in the turn's log.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MODE = os.environ.get("FAKE_ACP_MODE", "success")
RECORD_PATH = os.environ.get("FAKE_ACP_RECORD")
#: The whole ``error`` member ``session/load`` answers with under the
#: ``load-rejected`` mode. A mode per error response would be a mode per
#: sentence; what the client has to tell apart is which *kind* of refusal it is
#: looking at, so the error itself is the parameter.
LOAD_ERROR = os.environ.get("FAKE_ACP_LOAD_ERROR")

#: The prompt the fake echoes back the way the real agent does: once as a
#: ``user_message_chunk`` and once as the title of a ``session_info_update``.
#: Emitting both is the point -- the turn's log must contain neither.
SESSION_ID = "acp-session-1"

#: What ``session/load`` says it restored. Named so a test can prove the client
#: refused a restore that came back as a different conversation.
OTHER_SESSION_ID = "acp-session-somewhere-else"

record: dict[str, Any] = {
    "argv": sys.argv[1:],
    "client_capabilities": None,
    "client_info": None,
    "protocol_version": None,
    "session_params": None,
    "load_params": None,
    "prompt": None,
    "permissions": [],
    "unadvertised": [],
    "cancelled": [],
}


def _save() -> None:
    if RECORD_PATH:
        Path(RECORD_PATH).write_text(json.dumps(record), encoding="utf-8")


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _update(update: dict[str, Any]) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": update},
        }
    )


def _text_of(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    return "".join(
        str(block.get("text", "")) for block in blocks if isinstance(block, dict)
    )


def _config_options() -> list[dict[str, Any]]:
    """The launch flags coming back as ``session/new`` echoes them."""
    model = _flag_value("--model") or "fake-model-1"
    effort = _flag_value("--effort") or "low"
    if MODE == "model-drift":
        model = "some-other-model"
    return [
        {"id": "model", "currentValue": model},
        {"id": "reasoning_effort", "currentValue": effort},
        {"id": "allow_all", "currentValue": "off"},
    ]


def _flag_value(flag: str) -> str | None:
    argv = record["argv"]
    if flag in argv:
        index = argv.index(flag)
        if index + 1 < len(argv):
            return str(argv[index + 1])
    return None


def _ask_permission() -> None:
    """Ask the client for permission and record what it answered.

    The options offered always include both standing kinds, so a test can prove
    the client refused to pick either one.
    """
    _send(
        {
            "jsonrpc": "2.0",
            "id": "perm-1",
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION_ID,
                "toolCall": {
                    "toolCallId": "tool-1",
                    "title": "rm -rf /",
                    "kind": "execute",
                },
                "options": [
                    {"optionId": "aa", "name": "Always", "kind": "allow_always"},
                    {"optionId": "ao", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "ro", "name": "Reject", "kind": "reject_once"},
                    {"optionId": "ra", "name": "Never", "kind": "reject_always"},
                ],
            },
        }
    )
    answer = _read_message()
    record["permissions"].append(answer)
    _save()


def _ask_unadvertised() -> None:
    """Ask the client to read a file, which it never advertised it would do."""
    _send(
        {
            "jsonrpc": "2.0",
            "id": "fs-1",
            "method": "fs/read_text_file",
            "params": {"sessionId": SESSION_ID, "path": "/etc/passwd"},
        }
    )
    record["unadvertised"].append(_read_message())
    _save()


def _read_message() -> Any:
    line = sys.stdin.readline()
    if not line.strip():
        return None
    return json.loads(line)


def _run_turn() -> str:
    """Emit the streamed shape of one turn and return its stop reason."""
    # The echoes the real agent emits. Neither may reach the turn's log.
    _update({"sessionUpdate": "user_message_chunk", "content": _prompt_block()})
    _update({"sessionUpdate": "session_info_update", "title": record["prompt"]})
    if MODE == "permission":
        _ask_permission()
    if MODE == "unadvertised":
        _ask_unadvertised()
    _update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tool-1",
            "title": "just test-fast",
            "kind": "execute",
            "status": "pending",
        }
    )
    _update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tool-1",
            "status": "completed",
        }
    )
    _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "work"},
        }
    )
    _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "ing\n"},
        }
    )
    _update({"sessionUpdate": "usage_update", "usage": {"input": 1, "output": 2}})
    if MODE == "refusal":
        return "refusal"
    return "end_turn"


def _prompt_block() -> dict[str, Any]:
    return {"type": "text", "text": record["prompt"] or ""}


#: The two texts a replayed transcript carries. Distinct from anything this
#: turn produces, so a test can assert neither reached the log.
REPLAYED_PROMPT = "the prompt of the turn that came before"
REPLAYED_ANSWER = "what the earlier turn answered\n"


def _replay() -> None:
    """Stream the restored conversation back, the way ``session/load`` does.

    Sent before the load is answered and shaped like a real transcript: an
    earlier prompt echo, an earlier tool call, and an earlier answer. Every one
    of them is something the turn's log must not gain and the turn's final
    message must not become.
    """
    _update({"sessionUpdate": "user_message_chunk", "content": _replayed_prompt()})
    _update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "old-tool-1",
            "title": "an earlier command",
            "kind": "execute",
            "status": "completed",
        }
    )
    _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": REPLAYED_ANSWER},
        }
    )


def _replayed_prompt() -> dict[str, Any]:
    return {"type": "text", "text": REPLAYED_PROMPT}


def _wait_for_cancel() -> None:
    """Read messages until the client cancels the turn, and record it.

    A mode rather than a timer: the fake holds the turn open until the
    cancellation actually arrives, so the test asserts on an ordering instead of
    racing one.
    """
    while True:
        message = _read_message()
        if message is None:
            return
        if message.get("method") == "session/cancel":
            record["cancelled"].append(message.get("params"))
            _save()
            return


def main() -> int:
    # Recorded before anything is read, so a turn that never speaks ACP -- the
    # audit profile's one-shot form -- still says what it was launched with.
    _save()

    if "-p" in record["argv"]:
        # The one-shot form: the prompt was on argv, the answer goes to stdout,
        # and stdin is never read. Answering here rather than falling into the
        # protocol loop is what keeps an audit turn from blocking on a pipe
        # nobody is writing to.
        print("one-shot answer")
        return 0

    if MODE == "decline":
        # Exits before reading a byte: the agent that has no ACP mode at all.
        print("this build has no acp mode", file=sys.stderr)
        return 2

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            # A notification. ``session/cancel`` is the only one this client
            # sends, and it is answered by the pending prompt rather than here.
            if method == "session/cancel":
                record["cancelled"].append(params)
                _save()
            continue
        request_id = message["id"]

        if method == "initialize":
            record["protocol_version"] = params.get("protocolVersion")
            record["client_capabilities"] = params.get("clientCapabilities")
            record["client_info"] = params.get("clientInfo")
            _save()
            if MODE == "refuse-initialize":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "initialize refused"},
                    }
                )
                return 3
            version = 99 if MODE == "future-protocol" else 1
            capabilities: dict[str, Any] = {"loadSession": True}
            if MODE == "no-load-capability":
                # The build that kept no session on disk. It says so here, which
                # is the only place the client can find out.
                capabilities = {"loadSession": False}
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": version,
                        "agentCapabilities": capabilities,
                        "agentInfo": {"name": "FakeCopilot", "version": "0.0.1"},
                    },
                }
            )
        elif method == "session/new":
            record["session_params"] = params
            _save()
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "sessionId": SESSION_ID,
                        "configOptions": _config_options(),
                    },
                }
            )
        elif method == "session/load":
            record["load_params"] = params
            _save()
            if MODE == "load-missing":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "no such session"},
                    }
                )
                continue
            if MODE == "load-rejected":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": json.loads(LOAD_ERROR or "{}"),
                    }
                )
                continue
            # The transcript comes back *before* the answer, which is the
            # ordering the client has to survive: it is already filtering
            # notifications for this session by the time the load returns.
            _replay()
            result: dict[str, Any] = {"configOptions": _config_options()}
            if MODE == "load-drift":
                # Restored something, but not what was asked for.
                result["sessionId"] = OTHER_SESSION_ID
            _send({"jsonrpc": "2.0", "id": request_id, "result": result})
        elif method == "session/prompt":
            record["prompt"] = _text_of(params.get("prompt"))
            _save()
            if MODE == "die-after-submit":
                # Took the prompt, then died. The turn was spent, so nothing
                # downstream may report it as a clean run.
                return 9
            stop_reason = _run_turn()
            if MODE == "cancel":
                _wait_for_cancel()
                stop_reason = "cancelled"
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"stopReason": stop_reason},
                }
            )
            if MODE == "trailing-prose":
                # A last word on the way out, with no newline after it: the
                # shape a diagnostic printed by the binary itself takes.
                sys.stdout.write("copilot: goodbye")
                sys.stdout.flush()
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
