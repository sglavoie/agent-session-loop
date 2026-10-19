"""Workbench registry, archive, and discovery tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

# ruff: noqa: F401
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import (
    config,
    plans_files,
    state,
    workbench_archive,
    workbench_discovery,
    workbench_registry,
    workbench_registry_models,
)
from tests.support import _seed_archivable_plan, _write_plan_dir, patched_env

__all__ = [
    "json",
    "os",
    "tempfile",
    "unittest",
    "UTC",
    "datetime",
    "Path",
    "config",
    "plans_files",
    "state",
    "workbench_archive",
    "workbench_discovery",
    "workbench_registry",
    "workbench_registry_models",
    "_seed_archivable_plan",
    "_write_plan_dir",
    "patched_env",
]
