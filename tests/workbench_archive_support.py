"""Shared fixtures for the ``test_workbench_archive*`` modules (not collected)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import (
    plans_files,
    state,
    workbench_archive,
    workbench_registry_models,
)

from .support import (
    write_plan,
)

#: A pid this host is not running, so a lock file reads as stale without
#: anything real having to be killed.
DEAD_PID = 4_242_424

#: Fixed so every age in these tests is arithmetic rather than a race with the
#: clock: "expired" means expired by this instant, not by the time the assertion
#: happened to run.
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


def _iso(delta_days: float) -> str:
    """An archive timestamp ``delta_days`` before :data:`NOW`."""
    return (NOW - timedelta(days=delta_days)).isoformat()


class ArchiveFixture(unittest.TestCase):
    """A state root, a registry, and a plan ready to archive."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name).resolve()
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.registry_path = (
            self.tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
        )
        self.plan_dir = self.tmp_path / "plans" / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans_files.canonicalize_path(self.plan_dir)

    def make_plan(self, name: str) -> Path:
        plan_dir = self.tmp_path / "plans" / name
        write_plan(plan_dir, ("pending", "pending"))
        return plans_files.canonicalize_path(plan_dir)

    def archive_one(self, plan_dir: Path | None = None) -> str:
        target = self.canonical if plan_dir is None else plan_dir
        result = workbench_archive.archive_one(
            str(target),
            str(target),
            state_dir=self.state_dir,
            registry_path=self.registry_path,
            include_default_dirs=False,
        )
        return result.archive_id

    def backdate(self, archive_id: str, *, days: float) -> None:
        meta_path = (
            workbench_archive.archive_entry_dir(archive_id, self.state_dir)
            / workbench_archive.ARCHIVE_METADATA_FILE_NAME
        )
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["archived_at"] = _iso(days)
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

    def archive_tree(self) -> dict[str, int]:
        """Every path under the archive root with its size, for exact compare."""
        root = workbench_archive.archive_root(self.state_dir)
        if not root.exists():
            return {}
        return {
            str(path.relative_to(root)): (path.stat().st_size if path.is_file() else -1)
            for path in sorted(root.rglob("*"))
        }

    def lock_path(self) -> Path:
        return (
            state.plan_state_dir(self.canonical, self.state_dir) / state.LOCK_FILE_NAME
        )

    def review(self, *, now: datetime = NOW) -> workbench_archive.ArchiveView:
        return workbench_archive.review_archive(
            state_dir=self.state_dir, registry_path=self.registry_path, now=now
        )
