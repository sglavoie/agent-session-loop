# mypy: ignore-errors
from __future__ import annotations

import ast
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    commit_groups,
    commit_settlement,
    commit_settlement_finish,
    commit_settlement_groups,
    commit_settlement_ledger,
    commit_settlement_paused,
    durability,
    events,
    run_persistence,
)
from tests.support import assert_no_runner_import


def target(alias: str) -> dict[str, object]:
    return commit_groups.create_target(
        alias=alias,
        root=Path("/workspace") / alias,
        repository_path=alias,
        qualified_paths=(f"{alias}:changed.txt",),
    )
