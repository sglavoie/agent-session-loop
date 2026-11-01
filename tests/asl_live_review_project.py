"""Disposable project with one reproducible defect and an independent verifier."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.asl_live_redirect_workspace import Workspace

FROZEN_FILES = ("requirements.md", "legacy.txt", "verify_review.py", "README.md")


def write_project(workspace: Workspace, nonce: str) -> Path:
    legacy = (
        f"# Acceptance nonce: {nonce}\ndef total(values):\n    return sum(values) + 1\n"
    )
    (workspace.repo / "legacy.txt").write_text(legacy)
    (workspace.repo / "requirements.md").write_text(
        "# Product requirements\n\n"
        "totals.py exports total(values), returning the mathematical sum of a list "
        "of integers. Empty input returns zero. Negative values are supported.\n"
        "This requirement applies to the delivered product after review and repair.\n"
        "The initial session reproduces a legacy implementation for review; its "
        "known defect MUST be reported as an actionable finding, not waived.\n"
        "Fix totals.py only. Preserve the acceptance nonce comment. Never edit "
        "requirements.md, legacy.txt, verify_review.py, or README.md.\n"
        f"Acceptance nonce: {nonce}\n"
    )
    (workspace.repo / "verify_review.py").write_text(
        '"""Independent baseline/product checker. Never modify this file."""\n'
        "import json, pathlib, random, sys\n"
        "p = pathlib.Path\n"
        "try:\n"
        "    source = p('totals.py').read_text()\n"
        "    if sys.argv[1] == 'baseline':\n"
        "        assert source == p('legacy.txt').read_text(), 'legacy changed'\n"
        "    else:\n"
        "        namespace = {}\n"
        "        exec(compile(source, 'totals.py', 'exec'), namespace)\n"
        "        total = namespace['total']\n"
        f"        assert {nonce!r} in source, 'nonce changed'\n"
        f"        rng = random.Random({nonce!r})\n"
        "        cases = [[], [0], [1, 2], [-4, 2], [8, -8]]\n"
        "        cases += [[rng.randrange(-100, 100) for _ in range(n)] "
        "for n in range(1, 20)]\n"
        "        for values in cases:\n"
        "            actual = total(values)\n"
        "            assert actual == sum(values), "
        "f'total({values}) = {actual}; expected {sum(values)}'\n"
        "except Exception as error:\n"
        "    print(json.dumps({'ok': False, 'error': str(error)}))\n"
        "    sys.exit(1)\n"
        "print(json.dumps({'ok': True, 'mode': sys.argv[1]}))\n"
    )
    for argv in (
        ["git", "add", *FROZEN_FILES],
        ["git", "commit", "-qm", "Add independent review acceptance inputs"],
        ["git", "remote", "add", "origin", "https://example.invalid/asl-review.git"],
    ):
        subprocess.run(argv, cwd=workspace.repo, check=True, capture_output=True)
    source = workspace.root / "review.md"
    check = {
        "name": "legacy-reproduced",
        "type": "command",
        "repo": "APP",
        "argv": [sys.executable, "verify_review.py", "baseline"],
        "timeout_seconds": 15,
        "review_replay": False,
    }
    source.write_text(
        "# ASL cross-harness review acceptance\n\n"
        "This disposable acceptance project exercises one review/repair cycle.\n"
        f"The acceptance nonce is {nonce}.\n"
        "All project paths are relative to the APP repository root, repo/.\n"
        "Never edit requirements.md, legacy.txt, verify_review.py, or README.md.\n"
        "Do not commit yourself; ASL owns generated repair commits.\n"
        "Follow session-run's handoff protocol. ASL runs authoritative checks.\n\n"
        "For final review, read requirements.md and inspect totals.py. The legacy "
        "defect is intentionally seeded but must be reported as actionable. "
        "Review the actual files and write the generated review's structured "
        "findings; do not fix repository files from a review turn. "
        "Use the independent `python3 verify_review.py product` command as a "
        "finding's verification check with repo APP and review_replay true. "
        "Reference APP:totals.py, APP:requirements.md, and APP:verify_review.py "
        "in the review evidence. There is one defect and one repair group. "
        "After repair, re-review the actual files and require the product checker "
        "to pass before declaring all-clear. The legacy baseline check is a "
        "point-in-time check and must not be replayed after repair.\n\n"
        "## Repositories\n\n"
        "| Alias | Path | Origin |\n| --- | --- | --- |\n"
        "| APP | repo | https://example.invalid/asl-review.git |\n\n"
        "## Session 1: Reproduce the legacy implementation\n\n"
        "**Reads repositories**: APP\n**Writes repositories**: APP\n\n"
        "Create totals.py by copying legacy.txt byte for byte. Do not improve "
        "or correct it in this initial session: the independent reviewer must "
        "identify the defect and ASL must generate the repair. Modify only "
        "totals.py and this session's handoff. Mark only this session complete.\n\n"
        "## Completion Contract\n\n### Done when\n\n"
        "- totals.py reproduces legacy.txt exactly for independent review.\n\n"
        "### Verify\n\n```json\n"
        + json.dumps({"session": "1", "checks": [check]})
        + "\n```\n\n"
        "## Session 2: Document the public API\n\n"
        "**Reads repositories**: APP\n**Writes repositories**: APP\n\n"
        "Create usage.md documenting the public total(values) API from "
        "requirements.md. Leave totals.py and the legacy baseline unchanged "
        "for the independent review. Modify only usage.md and this session's "
        "handoff. Mark only this session complete.\n\n"
        "## Completion Contract\n\n### Done when\n\n"
        "- usage.md documents the public API.\n\n### Verify\n\n```json\n"
        + json.dumps(
            {
                "session": "2",
                "checks": [
                    {
                        "name": "usage-written",
                        "type": "file_exists",
                        "repo": "APP",
                        "path": "usage.md",
                    }
                ],
            }
        )
        + "\n```\n"
    )
    return source
