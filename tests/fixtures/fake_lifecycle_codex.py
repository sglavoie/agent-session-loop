#!/usr/bin/env python3
"""Protocol fixture for the command-level lifecycle acceptance suite.

No provider calls. The real prepare, supervisor, redirect journal, run loop,
handoff parser, and verification commands still execute in separate processes.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from fake_codex_common import (
    VALIDATION_PACKET_PREFIX,
    _mark_complete,
    _pending_sessions,
)
from fake_codex_turn_setup import _run_prepare_validator, _validation_plan_dir


def send(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def finish(text: str, *, status: str = "completed") -> None:
    send(
        {
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "agentMessage",
                    "text": text,
                    "phase": "final_answer",
                }
            },
        }
    )
    send(
        {
            "method": "turn/completed",
            "params": {
                "turn": {
                    "id": "turn-1",
                    "status": status,
                }
            },
        }
    )


def execute(prompt: str) -> None:
    if prompt.startswith(VALIDATION_PACKET_PREFIX):
        plan = _validation_plan_dir(prompt)
        assert plan is not None
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "validator.json"
            original = sys.argv
            try:
                sys.argv = [original[0], "-o", str(output)]
                _run_prepare_validator(plan, prompt)
            finally:
                sys.argv = original
            finish(output.read_text())
        return
    plan = _validation_plan_dir(prompt)
    assert plan is not None
    context = (plan / "context.md").read_text()
    nonce = re.search(r"The acceptance nonce is ([a-f0-9]+)", context)
    assert nonce is not None
    token = nonce[1]
    pending = _pending_sessions(plan)
    session = pending[0]
    os.chdir(Path.cwd() / "repo")
    if session.name == "session-1.md":
        if "BEGIN ASL OPERATOR CORRECTION JSONL" not in prompt:
            Path("seed.txt").write_text(token + "\n")
            Path("running.txt").write_text("running\n")
            return  # Wait for the real runner's turn/interrupt request.
        acknowledgement = next(
            line.strip()
            for line in prompt.splitlines()
            if line.strip().startswith("asl inbox acknowledge ")
        )
        subprocess.run(shlex.split(acknowledgement), check=True, capture_output=True)
        with Path("corrections.txt").open("a") as handle:
            handle.write(token + "\n")
        Path("stage1.txt").write_text(token + "\n")
    else:
        Path("result.txt").write_text("complete:" + token + "\n")
    os.environ["FAKE_CODEX_HANDOFF_FILES_JSON"] = json.dumps(
        {
            "1": [
                "APP:seed.txt",
                "APP:running.txt",
                "APP:corrections.txt",
                "APP:stage1.txt",
            ],
            "2": ["APP:result.txt"],
        }
    )
    _mark_complete(session)
    finish("Runner status: complete\n")


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.154.0")
        return 0
    if sys.argv[1:2] != ["app-server"]:
        return 2
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method, params = message.get("method"), message.get("params") or {}
        response: dict = {"id": message["id"], "result": {}}
        if method in {"thread/start", "thread/resume"}:
            response["result"] = {
                "thread": {"id": params.get("threadId", "fixture-thread")},
                "cwd": params.get("cwd"),
                "approvalPolicy": params.get("approvalPolicy"),
                "sandbox": {
                    "type": {
                        "read-only": "readOnly",
                        "workspace-write": "workspaceWrite",
                        "danger-full-access": "dangerFullAccess",
                    }[params["sandbox"]]
                },
                "model": "fixture-model",
            }
        elif method == "turn/start":
            response["result"] = {"turn": {"id": "turn-1"}}
        send(response)
        if method == "turn/start":
            execute("".join(item.get("text", "") for item in params["input"]))
        elif method == "turn/interrupt":
            finish("interrupted", status="interrupted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
