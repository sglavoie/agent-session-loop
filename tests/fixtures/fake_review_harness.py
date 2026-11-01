#!/usr/bin/env python3
"""Codex app-server and Claude stream-json fixtures for review acceptance.

No provider calls: the prepare, supervisor, review guard, repair materializer,
commit, and verification commands are the production commands.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import fake_lifecycle_codex as protocol
from fake_codex_common import COMMIT_PROMPT_PREFIX, _mark_complete, _pending_sessions
from fake_codex_turn_setup import _validation_plan_dir

validate = protocol.execute


def execute(prompt: str) -> None:
    if prompt.startswith("# ASL preparation validation packet"):
        validate(prompt)
        return
    if prompt.startswith(COMMIT_PROMPT_PREFIX):
        protocol.finish("fix: correct summation off-by-one\n")
        return
    plan = _validation_plan_dir(prompt)
    assert plan is not None
    session = _pending_sessions(plan)[0]
    repo = Path.cwd() / "repo"
    if not repo.is_dir():
        repo = Path.cwd()
    text = session.read_text()
    if "asl:generated-review-session" in text:
        namespace: dict = {}
        exec((repo / "totals.py").read_text(), namespace)
        broken = namespace["total"]([]) != 0
        if os.environ.get("ASL_REVIEW_FIXTURE_MODE") == "premature-all-clear":
            broken = False
        findings = []
        if broken:
            findings.append(
                {
                    "id": "F1",
                    "repo": "APP",
                    "severity": "high",
                    "title": "total returns one too many",
                    "explanation": "total([]) returns 1 instead of the required 0.",
                    "evidence": [{"path": "APP:totals.py", "symbol": "total"}],
                    "repair_group": "summation",
                    "done_criteria": [
                        "total returns the exact sum, including empty input."
                    ],
                    "verification_checks": [
                        {
                            "name": "product-sum",
                            "type": "command",
                            "repo": "APP",
                            "argv": [sys.executable, "verify_review.py", "product"],
                            "timeout_seconds": 15,
                            "review_replay": True,
                        }
                    ],
                }
            )
        status = (
            "Status: 1 Findings (1 high, 0 med, 0 low)"
            if broken
            else "Status: All clear, no findings"
        )
        (plan / "final-review.md").write_text(
            "# Findings\n\nReviewed APP:totals.py against APP:requirements.md "
            "and APP:verify_review.py.\n" + status + "\n"
        )
        (plan / "final-review.json").write_text(
            json.dumps(
                {
                    "version": 3,
                    "outcome": "findings" if broken else "all-clear",
                    "findings": findings,
                }
            )
        )
        files = []
    elif session.name == "session-2.md":
        (repo / "usage.md").write_text("total(values) returns the sum of integers.\n")
        files = ["APP:usage.md"]
    else:
        legacy = (repo / "legacy.txt").read_text()
        if "asl:generated-review-repair-session" in text:
            legacy = legacy.replace("sum(values) + 1", "sum(values)")
            if os.environ.get("ASL_REVIEW_FIXTURE_MODE") == "tamper-verifier":
                (repo / "verify_review.py").write_text("print('{\"ok\": true}')\n")
        (repo / "totals.py").write_text(legacy)
        files = ["APP:totals.py"]
    # Generated repository-aware handoffs start at (none), legacy ones at
    # (none yet); the fixture's shared helper expects the latter placeholder.
    session.write_text(
        text.replace("- **Files modified**: (none)", "- **Files modified**: (none yet)")
    )
    os.environ["FAKE_CODEX_HANDOFF_FILES_JSON"] = json.dumps(
        {session.stem.removeprefix("session-"): files}
    )
    _mark_complete(session)
    protocol.finish("Runner status: complete\n")


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print(
            "2.1.236 (Claude Code)"
            if os.environ.get("ASL_FIXTURE_ROLE") == "reviewer"
            else "codex-cli 0.154.0"
        )
        return 0
    if "--input-format" not in sys.argv:
        protocol.execute = execute
        return protocol.main()
    message = json.loads(sys.stdin.readline())
    prompt = "".join(item["text"] for item in message["message"]["content"])
    # Translate only the fixture's final event to Claude's wire protocol.
    protocol.finish = lambda text: print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": text,
            }
        ),
        flush=True,
    )
    execute(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
