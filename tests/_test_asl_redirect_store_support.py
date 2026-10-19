# mypy: ignore-errors
# ruff: noqa
"""The redirect journal's durability, identity, reduction, and bounds.

Every test here is about a way feedback could be lost, duplicated, or falsely
reported as accepted. The journal's job is to make those outcomes structurally
unavailable, so the tests drive the real file on a temporary state root rather
than a fake: locking, ``fsync``, short-write rollback and replay are the
subject, and none of them exist in a substitute.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from agent_session_loop import (
    clock,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
)
from tests.support import write_plan


def _target(
    turn_index: int = 1, *, run_id: str = "run-1"
) -> redirect_store_models.RedirectTarget:
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id="session-9",
        turn_index=turn_index,
        harness="claude",
    )


class RedirectStoreTestCase(unittest.TestCase):
    """A plan with its own state root, so every test drives a real journal."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))

    def submit(self, **kwargs) -> redirect_store_models.RedirectRequest:
        params = {
            "target": _target(),
            "feedback": "stop editing README and re-read the contract",
            "state_dir": self.state_dir,
        }
        params.update(kwargs)
        return redirect_store_requests.submit_request(self.plan_dir, **params)

    def read(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def journal_path(self) -> Path:
        return redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        )

    def raw_lines(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.journal_path().read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append_raw(self, line: str) -> None:
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


__all__ = [name for name in globals() if not name.startswith("__")]
