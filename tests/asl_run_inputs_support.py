from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    plans_errors,
    plans_files,
    plans_metadata,
    plans_snapshot,
    repository_registration,
    run_inputs,
    run_selection,
    state,
    verification_models,
)
from agent_session_loop.run_errors import RunError
from tests.support import _contract_block, assert_no_runner_import, write_plan


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {"cwd": ".", "commit_mode": None, "spark": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class _RecordingResolver:
    """Stand-in for the review guard's allowlist parser, remembering calls."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[argparse.Namespace] = []
        self._error = error

    def __call__(self, args: argparse.Namespace) -> tuple[str, ...]:
        self.calls.append(args)
        if self._error is not None:
            raise self._error
        return ()
