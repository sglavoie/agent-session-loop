"""ACP protocol branch of the fake Codex fixture."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from fake_codex_common import _ACP_PROMPTS, _REAL_STDOUT, _arg_value


def _step_sleep() -> None:
    time.sleep(float(os.environ.get("FAKE_CODEX_STEP_SLEEP", "0.1")))


def _stream_gate_dir() -> Path | None:
    value = os.environ.get("FAKE_CODEX_STREAM_GATE_DIR")
    if not value:
        return None
    return Path(value)


def _signal_stream_ready(session_label: str) -> None:
    gate_dir = _stream_gate_dir()
    if gate_dir is None:
        return
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / f"session-{session_label}.ready").write_text(
        "ready\n",
        encoding="utf-8",
    )


def _wait_for_stream_continue(session_label: str) -> bool:
    gate_dir = _stream_gate_dir()
    if gate_dir is None:
        return True

    continue_path = gate_dir / f"session-{session_label}.continue"
    timeout = float(os.environ.get("FAKE_CODEX_STREAM_GATE_TIMEOUT", "30"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if continue_path.exists():
            return True
        time.sleep(0.02)
    print(
        f"timed out waiting for stream gate: {continue_path}",
        file=sys.stderr,
        flush=True,
    )
    return False


class _AcpStdout:
    """A stdout that publishes each write as an ACP ``agent_message_chunk``.

    The turn body prints its progress and its ``Runner status`` line to stdout
    and every suite reads them back from there. Over ACP that stream belongs to
    the protocol, so the body's writes are forwarded as session updates, which
    the adapter reassembles into the same text in the same order. Forwarding
    per write rather than buffering matters: the streaming modes below expect
    their first line to reach the runner while the turn is still running.
    """

    encoding = "utf-8"

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id

    def write(self, text: str) -> int:
        if text:
            _acp_send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": self._session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            )
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False


def _acp_send(payload: dict[str, object]) -> None:
    # Always the real stdout: the body's stdout is redirected while a turn runs.
    _REAL_STDOUT.write(json.dumps(payload, separators=(",", ":")) + "\n")
    _REAL_STDOUT.flush()


def _acp_result(request_id: object, result: dict[str, object]) -> None:
    _acp_send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _acp_config_options() -> list[dict[str, object]]:
    """Echo the launch controls the way an ACP agent reports its own settings."""
    options: list[dict[str, object]] = []
    for option_id, flag in (("model", "--model"), ("reasoning_effort", "--effort")):
        value = _arg_value(flag)
        if value is not None:
            options.append({"id": option_id, "currentValue": value})
    return options


def _acp_prompt_text(params: dict[str, object]) -> str:
    blocks = params.get("prompt")
    if not isinstance(blocks, list):
        return ""
    return "".join(
        str(block.get("text", "")) for block in blocks if isinstance(block, dict)
    )


def run_acp(run_turn: Callable[[], int]) -> int:
    """Speak enough ACP to carry one copilot worker turn.

    The app-server branch in ``main`` declines because codex keeps a one-shot
    fallback to decline *into*; copilot has none, so refusing here would take
    every fake-harness suite that drives copilot off the road. The handshake is
    answered, the ordinary turn body runs with its stdout forwarded as agent
    message chunks, and its exit code is restated as a stop reason: only a turn
    that finished its work gets ``end_turn``, because the adapter reads the
    protocol's verdict and an ACP agent's own exit code says nothing.
    """
    session_id = "fake-copilot-acp-session"
    exit_code = 0
    while True:
        line = sys.stdin.readline()
        if not line:
            return exit_code
        stripped = line.strip()
        if not stripped:
            continue
        message = json.loads(stripped)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "initialize":
            _acp_result(
                request_id,
                {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "fake-copilot", "version": "0.0.0"},
                    "agentCapabilities": {},
                },
            )
        elif method == "session/new":
            _acp_result(
                request_id,
                {"sessionId": session_id, "configOptions": _acp_config_options()},
            )
        elif method == "session/prompt":
            _ACP_PROMPTS.append(_acp_prompt_text(params))
            with contextlib.redirect_stdout(_AcpStdout(session_id)):
                exit_code = run_turn()
            _acp_result(
                request_id,
                {"stopReason": "end_turn" if exit_code == 0 else "refusal"},
            )
        elif request_id is not None:
            _acp_send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"fake copilot does not implement {method}",
                    },
                }
            )
