#!/usr/bin/env python3
"""A deterministic stand-in for ``codex app-server``.

Speaks the same newline-delimited JSON-RPC the real app-server speaks, on the
same two pipes, and nothing else: no model, no sandbox, no timing. Every
departure from the happy path is chosen by ``FAKE_APP_SERVER_MODE`` so a test
names the failure it wants instead of racing for it.

What crossed the wire is recorded to ``FAKE_APP_SERVER_RECORD`` -- the thread
and resume parameters, the prompt, every steered body, and every interrupt --
which is how the tests assert that the access profile, the resumption target,
and the guidance really travelled the protocol, and that neither the prompt nor
a guidance body came back out in the turn's log.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MODE = os.environ.get("FAKE_APP_SERVER_MODE", "success")
RECORD_PATH = os.environ.get("FAKE_APP_SERVER_RECORD")
#: The whole ``error`` member ``thread/resume`` answers with under the
#: ``resume-rejected`` mode. A mode per error response would be a mode per
#: sentence; what the client has to tell apart is which *kind* of refusal it is
#: looking at, so the error itself is the parameter.
RESUME_ERROR = os.environ.get("FAKE_APP_SERVER_RESUME_ERROR")
LAUNCH_LOG = os.environ.get("FAKE_APP_SERVER_LAUNCH_LOG")

#: The sandbox discriminator the real app-server echoes for each policy name.
APPLIED_SANDBOX = {
    "read-only": "readOnly",
    "workspace-write": "workspaceWrite",
    "danger-full-access": "dangerFullAccess",
}

record: dict[str, Any] = {
    "thread_params": None,
    "resume_params": None,
    "prompt": None,
    "steers": [],
    "interrupts": [],
}


def _save() -> None:
    if RECORD_PATH:
        Path(RECORD_PATH).write_text(json.dumps(record), encoding="utf-8")


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _text_of(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return "".join(
        str(item.get("text", "")) for item in items if isinstance(item, dict)
    )


def _thread_result(params: dict[str, Any], thread_id: str) -> dict[str, Any]:
    """The shape both ``thread/start`` and ``thread/resume`` answer with.

    The real responses echo the settings that ended up in force at the top
    level, which is what a resuming client re-checks -- so the echo here is
    built from what was asked for, and the drift modes below change it.
    """
    applied = APPLIED_SANDBOX[params["sandbox"]]
    if MODE == "widen-sandbox":
        # Answers with a *wider* sandbox than was asked for, which the client
        # must refuse before it sends any turn.
        applied = "dangerFullAccess"
    result: dict[str, Any] = {
        "thread": {"id": thread_id},
        "sandbox": {"type": applied},
        "model": "fake-model-1",
        "cwd": params.get("cwd"),
        "approvalPolicy": params.get("approvalPolicy"),
    }
    if MODE == "resume-elsewhere":
        # Restored the thread, but somewhere other than where the turn was
        # asked to run: the settings came from the thread, not from the call.
        result["cwd"] = "/somewhere/else"
    if MODE == "resume-approvals-drifted":
        result["approvalPolicy"] = "on-request"
    return result


def _finish_turn(status: str = "completed") -> None:
    """Emit the streamed shape of a completed turn, echoes included."""
    _send({"method": "item/agentMessage/delta", "params": {"delta": "work"}})
    _send({"method": "item/agentMessage/delta", "params": {"delta": "ing\n"}})
    # The real app-server echoes the prompt and every steered body back as a
    # user message. Emitting it here is the point: the turn's log must not
    # contain it.
    _send(
        {
            "method": "item/completed",
            "params": {"item": {"type": "userMessage", "text": record["prompt"]}},
        }
    )
    _send(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "command": ["just", "test-fast"],
                    "exitCode": 0,
                }
            },
        }
    )
    _send(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": "the turn is done",
                    "phase": "final_answer",
                }
            },
        }
    )
    _send(
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": status}},
        }
    )


def main() -> int:
    if LAUNCH_LOG:
        with open(LAUNCH_LOG, "a", encoding="utf-8") as handle:
            handle.write("app-server\n")

    if MODE == "decline":
        # Exits before reading a byte: the pre-start fallback window.
        print("this build has no app-server", file=sys.stderr)
        return 2

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        params = message.get("params") or {}
        if "id" not in message:
            continue
        request_id = message["id"]

        if method == "initialize":
            if MODE == "refuse-initialize":
                _send(
                    {
                        "id": request_id,
                        "error": {"code": -32603, "message": "initialize refused"},
                    }
                )
                return 3
            _send({"id": request_id, "result": {"userAgent": "fake-app-server"}})
        elif method == "thread/start":
            record["thread_params"] = params
            _save()
            _send({"id": request_id, "result": _thread_result(params, "thread-1")})
        elif method == "thread/resume":
            record["resume_params"] = params
            _save()
            if MODE == "resume-unknown":
                _send(
                    {
                        "id": request_id,
                        "error": {"code": -32602, "message": "no such thread"},
                    }
                )
                continue
            if MODE == "resume-rejected":
                _send({"id": request_id, "error": json.loads(RESUME_ERROR or "{}")})
                continue
            restored = str(params.get("threadId"))
            if MODE == "resume-other-thread":
                # Answered with a thread nobody asked for. The client must
                # refuse it rather than hand a turn to a stranger.
                restored = "thread-somebody-else"
            _send({"id": request_id, "result": _thread_result(params, restored)})
        elif method == "turn/interrupt":
            record["interrupts"].append(
                {"threadId": params.get("threadId"), "turnId": params.get("turnId")}
            )
            _save()
            if params.get("turnId") != "turn-1":
                _send(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32600,
                            "message": f"no such turn `{params.get('turnId')}`",
                        },
                    }
                )
                continue
            _send({"id": request_id, "result": {}})
            if MODE in {"await-interrupt", "await-steer"}:
                # The turn ends *interrupted*, which is the whole distinction:
                # it ran what it ran and was stopped, rather than failing.
                _finish_turn(status="interrupted")
        elif method == "turn/start":
            record["prompt"] = _text_of(params.get("input"))
            _save()
            _send({"id": request_id, "result": {"turn": {"id": "turn-1"}}})
            _send({"method": "turn/started", "params": {"turnId": "turn-1"}})
            if MODE == "die-after-accept":
                # Accepted the turn, then died. Re-running it would execute the
                # same turn twice, so the client must not fall back.
                return 9
            if MODE not in {"await-steer", "await-interrupt"}:
                _finish_turn()
        elif method == "turn/steer":
            record["steers"].append(_text_of(params.get("input")))
            _save()
            if MODE == "not-steerable":
                _send(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32603,
                            "message": "activeTurnNotSteerable",
                        },
                    }
                )
                continue
            if params.get("expectedTurnId") != "turn-1":
                _send(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32600,
                            "message": (
                                "expected active turn id "
                                f"`{params.get('expectedTurnId')}` but found `turn-1`"
                            ),
                        },
                    }
                )
                continue
            _send({"id": request_id, "result": {"turnId": "turn-1"}})
            if MODE == "await-steer":
                _finish_turn()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
