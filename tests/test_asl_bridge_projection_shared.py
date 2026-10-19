# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
"""Read-only stdio bridge: protocol framing and run projection.

Split from tests/test_asl_workbench_summary.py: the bridge reuses Workbench
classification but owns its own boundary — what a snapshot may contain, what
observation may touch, and what a malformed client may do to the server loop.
"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    agent_mux_projection,
    bridge_projection,
    bridge_protocol,
    bridge_server,
    bridge_session,
    bridge_workers,
    cli,
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_vocabulary,
    mailbox_waits,
    plans_files,
    state,
    workbench_discovery,
    workbench_registry_models,
    workbench_summary,
    workbench_summary_model,
)

from .support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
