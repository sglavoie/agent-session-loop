"""A plan named in another letter case is still the same plan's state.

On the default case-insensitive APFS volume ``realpath`` keeps whatever case
the caller typed, so a supervisor polling ``~/p/plan`` while the runner holds
``~/p/Plan`` would read a different state directory -- an empty mailbox, a
second lock. :func:`canonicalize_path` respells the plan directory as the
volume stores it, so both spellings hash to one state directory.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import plans_files, state


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


@unittest.skipUnless(sys.platform == "darwin", "stored-case lookup is Darwin's")
class PlanPathCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        if not _case_insensitive(self.root):
            self.skipTest("temporary directory is on a case-sensitive volume")
        self.plan = self.root / "Plan"
        self.plan.mkdir()

    def test_a_respelled_plan_directory_canonicalizes_to_its_stored_case(
        self,
    ) -> None:
        self.assertEqual(self.plan, plans_files.canonicalize_path(self.root / "PLAN"))
        self.assertEqual(
            plans_files.path_sha256(self.plan),
            plans_files.path_sha256(self.root / "plan"),
        )

    def test_both_spellings_share_one_state_directory(self) -> None:
        state_root = self.root / "state"
        self.assertEqual(
            state.plan_state_dir(self.plan, state_root),
            state.plan_state_dir(self.root / "pLaN", state_root),
        )

    def test_a_missing_tail_keeps_its_spelling_under_the_stored_parent(
        self,
    ) -> None:
        self.assertEqual(
            self.plan / "Not-Yet" / "File.md",
            plans_files.canonicalize_path(self.root / "plan" / "Not-Yet/File.md"),
        )

    def test_a_correctly_cased_path_is_unchanged(self) -> None:
        self.assertEqual(self.plan, plans_files.canonicalize_path(self.plan))


if __name__ == "__main__":
    unittest.main()
