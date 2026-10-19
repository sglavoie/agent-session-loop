"""Shared fixtures and helpers for :mod:`tests.test_asl_repository_evidence`."""

# ruff: noqa: F401
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    external_write_evidence,
    handoff_guards,
    plans_registry,
    plans_session_scope,
    repository_evidence,
    repository_evidence_workspace,
    review_guard,
    workspace_evidence,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
)


def clone_git_repo(source: Path, target: Path) -> None:
    subprocess.run(
        ["git", "clone", str(source), str(target)],
        text=True,
        capture_output=True,
        check=True,
    )
