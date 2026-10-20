"""Shared constants for the ``tests/test_workbench_foundations*.py`` modules.

Not collected as a test module: the focused foundations modules import these so
the repository root and the portable-package names are defined once.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTABLE_PACKAGE_NAME = "asl_workbench"
FORBIDDEN_PACKAGE_NAME = "agent_session_loop"

#: The Ruff table that holds the portable package to the Pi's interpreter while
#: the rest of the repository stays on the newer one.
RUFF_PER_FILE_TARGET_TABLE = "[tool.ruff.per-file-target-version]"
RUFF_PORTABLE_TARGET = '"asl_workbench/**/*.py" = "py313"'
