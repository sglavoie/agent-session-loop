#!/usr/bin/env python3
"""Real blocking child and deterministic Codex protocol; no provider calls."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import fake_lifecycle_codex as protocol
from fake_codex_common import (
    VALIDATION_PACKET_PREFIX,
    _mark_complete,
    _pending_sessions,
)
from fake_codex_turn_setup import _validation_plan_dir

validate = protocol.execute


def execute(prompt: str) -> None:
    if prompt.startswith(VALIDATION_PACKET_PREFIX):
        validate(prompt)
        return
    plan = _validation_plan_dir(prompt)
    assert plan is not None
    nonce = re.search(
        r"The acceptance nonce is ([a-f0-9]+)", (plan / "context.md").read_text()
    )
    assert nonce is not None
    token = nonce[1]
    session = _pending_sessions(plan)[0]
    os.chdir(Path.cwd() / "repo")
    mode = os.environ.get("ASL_RECOVERY_FIXTURE_MODE", "")
    if session.name == "session-1.md":
        Path("seed.txt").write_text(token + "\n")
        with Path("session1-receipts.txt").open("a") as receipt:
            receipt.write(token + "\n")
    elif not Path("checkpoint.txt").exists():
        Path("checkpoint.txt").write_text(token + "\n")
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, os, time; from pathlib import Path; "
                f"Path('sleeping.json').write_text(json.dumps({{'nonce': {token!r}, "
                "'pid': os.getpid()})); time.sleep(600)",
            ],
            check=True,
        )
        raise RuntimeError("initial blocking tool must be interrupted by the crash")
    else:
        Path("result.txt").write_text("complete:" + token + "\n")
        if mode == "tamper-verifier":
            Path("verify_recovery.py").write_text("print('{\"ok\":true}')\n")
        elif mode == "replay-session-1":
            with Path("session1-receipts.txt").open("a") as receipt:
                receipt.write(token + "\n")
        elif mode == "replace-checkpoint":
            Path("checkpoint.txt").write_text("replaced\n")
    os.environ["FAKE_CODEX_HANDOFF_FILES_JSON"] = json.dumps(
        {
            "1": ["APP:seed.txt", "APP:session1-receipts.txt"],
            "2": ["APP:checkpoint.txt", "APP:sleeping.json", "APP:result.txt"],
        }
    )
    _mark_complete(session)
    protocol.finish("Runner status: complete\n")


if __name__ == "__main__":
    protocol.execute = execute
    raise SystemExit(protocol.main())
