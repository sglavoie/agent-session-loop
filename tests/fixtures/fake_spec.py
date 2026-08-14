#!/usr/bin/env python3
"""Fake spec executable used by agent-session-loop integration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _append_log(mode: str) -> None:
    log_path = os.environ.get("FAKE_SPEC_LOG")
    if log_path is None:
        return
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "argv": sys.argv,
                    "cwd": str(Path.cwd()),
                    "mode": mode,
                },
                sort_keys=True,
            )
        )
        handle.write("\n")


def _git_commit() -> subprocess.CompletedProcess[str]:
    staged = subprocess.run(
        ["git", "add", "-A"],
        text=True,
        capture_output=True,
        check=False,
    )
    if staged.returncode != 0:
        return staged
    return subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Fake spec commit"],
        text=True,
        capture_output=True,
        check=False,
    )


def main() -> int:
    mode = os.environ.get("FAKE_SPEC_MODE", "success")
    _append_log(mode)

    if sys.argv[1:] != ["commit", "--yes", "--no-verify"]:
        print(f"unexpected fake spec argv: {sys.argv[1:]}", file=sys.stderr)
        return 2

    if mode == "failure":
        print("fake spec failure", file=sys.stderr)
        return 23

    fail_cwd = os.environ.get("FAKE_SPEC_FAIL_CWD_BASENAME")
    if fail_cwd and Path.cwd().name == fail_cwd:
        print(f"fake spec failure in {fail_cwd}", file=sys.stderr)
        return 23

    if mode == "unchanged-head":
        print("fake spec left HEAD unchanged")
        return 0

    if mode == "success":
        result = _git_commit()
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    print(f"unsupported fake spec mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
