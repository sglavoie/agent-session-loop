"""Frozen two-session project for opt-in crash-recovery acceptance."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from tests.asl_live_redirect_workspace import Workspace

FROZEN = ("README.md", "requirements.txt", "verify_recovery.py")
PRESERVED = ("seed.txt", "session1-receipts.txt", "checkpoint.txt", "sleeping.json")


def write_project(workspace: Workspace, nonce: str) -> Path:
    requirements = (
        f"Acceptance nonce: {nonce}\n"
        "Session 1 writes seed.txt and appends one nonce line "
        "to session1-receipts.txt.\n"
        "Session 2 preserves these, checkpoints once, and survives a runner crash.\n"
        "Final result.txt contains complete:<nonce> and a newline.\n"
        "Never change these requirements or the verifier.\n"
    )
    checker = """import json, pathlib, sys
p = pathlib.Path
nonce = sys.argv[2]
assert p("seed.txt").read_text() == nonce + "\\n", "seed changed"
receipts = p("session1-receipts.txt").read_text().splitlines()
assert receipts == [nonce], "session 1 replayed"
if sys.argv[1] == "2":
    assert p("checkpoint.txt").read_text() == nonce + "\\n", "checkpoint changed"
    assert json.loads(p("sleeping.json").read_text())["nonce"] == nonce, "tool missing"
    assert p("result.txt").read_text() == "complete:" + nonce + "\\n", "result missing"
print(json.dumps({"ok": True}))
"""
    (workspace.repo / "requirements.txt").write_text(requirements)
    (workspace.repo / "verify_recovery.py").write_text(checker)
    for args in (
        ["add", "requirements.txt", "verify_recovery.py"],
        ["commit", "-qm", "Freeze crash recovery acceptance inputs"],
        ["remote", "add", "origin", "https://example.invalid/asl-recovery.git"],
    ):
        subprocess.run(
            ["git", *args], cwd=workspace.repo, check=True, capture_output=True
        )
    source = workspace.root / "recovery.md"
    lines = [
        "# ASL live crash recovery acceptance",
        "",
        f"The acceptance nonce is {nonce}.",
        "This disposable plan deliberately crashes during session 2. The external",
        "acceptance driver stops the old processes and resumes through ASL.",
        "Do not commit. Follow session-run's handoff protocol; the runner verifies.",
        "Never change README.md, requirements.txt, or verify_recovery.py.",
        "All paths below are relative to APP (repo/); run tools from that root.",
        "",
        "## Repositories",
        "",
        "| Alias | Path | Origin |",
        "| --- | --- | --- |",
        "| APP | repo | https://example.invalid/asl-recovery.git |",
        "",
    ]
    for number in (1, 2):
        lines.extend(
            [
                f"## Session {number}: "
                + (
                    "Create verified durable work"
                    if number == 1
                    else "Recover partial work"
                ),
                "",
                "**Reads repositories**: APP",
                "**Writes repositories**: APP",
                "",
            ]
        )
        if number == 1:
            lines.extend(
                [
                    "Write seed.txt containing the nonce and a newline.",
                    "Append one nonce line to session1-receipts.txt. Do not",
                    "deduplicate or erase lines: replay must remain visible.",
                    "Complete only this session, including its complete handoff.",
                ]
            )
        else:
            tool = (
                "import json, os, time; from pathlib import Path; "
                f"Path('sleeping.json').write_text(json.dumps({{'nonce': '{nonce}', "
                "'pid': os.getpid()})); time.sleep(600)"
            )
            lines.extend(
                [
                    "Preserve seed.txt and session1-receipts.txt byte for byte.",
                    "If checkpoint.txt does not exist, write the nonce and a newline",
                    "to checkpoint.txt, then run this single blocking shell tool:",
                    f"`{shlex.join([sys.executable, '-c', tool])}`",
                    "The driver will SIGKILL the runner during that tool. Do not",
                    "finish this initial attempt or start the tool in the background.",
                    "If checkpoint.txt already exists on resumption, preserve it and",
                    "sleeping.json byte for byte. Do not restart the sleeping tool.",
                    "Write result.txt containing complete: followed by the nonce and",
                    "a newline; complete session 2 and its handoff.",
                    "Never replay session 1.",
                ]
            )
        lines.extend(
            [
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "",
                f"- Stage {number} passes the frozen independent checker.",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": str(number),
                        "checks": [
                            {
                                "name": f"stage-{number}",
                                "type": "command",
                                "repo": "APP",
                                "argv": [
                                    sys.executable,
                                    "verify_recovery.py",
                                    str(number),
                                    nonce,
                                ],
                                "timeout_seconds": 15,
                            }
                        ],
                    }
                ),
                "```",
                "",
            ]
        )
    source.write_text("\n".join(lines))
    return source
