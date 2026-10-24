from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    install_verify,
    install_verify_common,
    install_verify_skills,
    startup_context,
)
from tests.support import REPO_ROOT

#: The harness roots every fixture home carries, relative to that home.
HARNESS_ROOT_DIRS = (".claude", ".claude-personal", ".codex", ".copilot")

#: Names the second Claude profile explicitly and nothing else, so an ambient
#: ``CLAUDE_CONFIG_DIR`` on the developer's machine (which points outside the
#: fixture home) cannot change what a test renders or verifies.
HARNESS_ENV = {"ASL_CLAUDE_CONFIG_DIRS": "~/.claude-personal"}


def fixture_roots(home: Path) -> tuple[install_verify_common.HarnessRoot, ...]:
    return install_verify_common.discover_harness_roots(home, HARNESS_ENV).found


CODEX_DEFAULTS = "\n".join(
    [
        "# Global Codex Instructions",
        "",
        "## Interaction Defaults",
        "",
        "- Treat unanswered questions as blocking.",
        "",
        "<!-- CODEGRAPH_START -->",
        "## CodeGraph",
        "",
        "Use codegraph before grep.",
        "<!-- CODEGRAPH_END -->",
        "",
    ]
)

CLAUDE_DEFAULTS = "\n".join(
    [
        "`sed` here is GNU `sed`.",
        "",
        "<!-- CODEGRAPH_START -->",
        "## CodeGraph",
        "",
        "Use codegraph before grep.",
        "<!-- CODEGRAPH_END -->",
        "",
    ]
)
