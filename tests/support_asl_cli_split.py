"""Shared argument, contract and runner helpers for the split CLI test modules."""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

from agent_session_loop import (
    authoring_split_cli,
)

if TYPE_CHECKING:
    _Base = unittest.TestCase
else:
    _Base = object


class SplitCliHelpers(_Base):
    """Shared argument, contract and runner helpers for the split CLI test modules."""

    _TWO_SESSION_PLAN = (
        "# My Plan\n\nShared context.\n\n"
        "## Session 1: First\n\nDo one.\n\n"
        "## Session 2: Second\n\nDo two.\n"
    )

    _MULTI_REPO_PLAN = (
        "# My Plan\n\n"
        "## Repositories\n\n"
        "| Alias | Path | Origin |\n"
        "| --- | --- | --- |\n"
        "| SVC | tundra_svc | git@github.com:tundra/SVC.git |\n"
        "| OAK | tundra_OAK | https://github.com/tundra/OAK.git |\n\n"
        "Shared context.\n\n"
        "## Session 1: First\n\n"
        "**Reads repositories**: SVC, OAK\n"
        "**Writes repositories**: SVC\n\nDo one.\n\n"
        "## Session 2: Second\n\n"
        "**Reads repositories**: OAK\n"
        "**Writes repositories**: (none)\n\nDo two.\n"
    )

    def _split_args(self, **overrides: object) -> argparse.Namespace:
        args = argparse.Namespace(
            plan_file=None,
            inline_stdin=False,
            base_dir=None,
            check=False,
            contracts_file=None,
            skill_prefix="codex",
            analysis_file=None,
            json=True,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def _write_contracts(self, path: Path, ids: tuple[str, ...]) -> None:
        payload = {
            sid: {
                "done_when": [f"Session {sid} lands."],
                "verify": {
                    "session": sid,
                    "checks": [
                        {
                            "type": "command",
                            "argv": ["python3", "-m", "unittest", "-q"],
                            "timeout_seconds": 60,
                        }
                    ],
                },
            }
            for sid in ids
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _run_split(
        self, args: argparse.Namespace, *, stdin: str | None = None
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    code = authoring_split_cli.split_from_args(args)
            else:
                code = authoring_split_cli.split_from_args(args)
        return code, stdout.getvalue(), stderr.getvalue()

    # -- `asl split --check` -------------------------------------------------

    _BROKEN_PLAN = (
        "# Broken Plan\n\nShared context.\n\n"
        "## Session 1 No colon\n\nDo one.\n\n"
        "## Session 2 / Bad sep\n\nDo two.\n\n"
        "## Session 3a: Glued\n\nDo three.\n"
    )
