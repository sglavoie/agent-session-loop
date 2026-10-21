from __future__ import annotations

import os
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
