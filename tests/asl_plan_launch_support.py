from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    cli_trace,
    config,
    events,
    plan_launch,
    plan_pins,
    plan_pins_models,
    plans_files,
    plans_metadata,
    plans_registry,
    state,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _BrokenStdinFakePopen,
    _FakePopen,
    init_multi_repo_workspace,
    install_fake_codex,
    write_multi_repo_plan,
    write_plan,
)


class LaunchPlanSeedMixin:
    def _seed_launchable_plan(self, tmp_path: Path) -> Path:
        plan_dir = tmp_path / "plan"
        write_plan(plan_dir, ("pending", "pending"))
        return plans_files.canonicalize_path(plan_dir)
