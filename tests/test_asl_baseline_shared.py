# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
"""Completion-contract baseline schema, cache, and CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    baseline,
    baseline_cache,
    baseline_evidence,
    baseline_run,
    contracts,
    plans_content_hash,
    plans_errors,
    verification_models,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _baseline_contract_text,
    _command_contract,
    _contract_block,
    git_stdout,
    init_git_repo,
    init_multi_repo_workspace,
    write_multi_repo_plan,
    write_plan,
)
