# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
"""Q&A context loader and questions CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import (
    events,
    plans_files,
    plans_snapshot,
    qna,
    run_coordination_wait_state,
    run_persistence,
    state,
    state_locks,
    state_payloads,
    state_status,
    state_status_text,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
)
