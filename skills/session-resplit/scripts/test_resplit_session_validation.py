"""Resplit session regression tests."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from resplit_test_support import (
    resplit,
    write_resplit_plan,
    write_session,
)

from agent_session_loop import state_locks


class ResplitSessionTest(unittest.TestCase):
    def test_resplit_rejects_complete_parent_without_mutating_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_resplit_plan(plan_dir, parent_status="complete")

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(plan_dir, session_id="2", parts=2)

            self.assertEqual("blocked", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_rejects_existing_descendant_session_as_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            write_session(plan_dir, "2.1", "pending", title="Leftover child")
            descendant_path = plan_dir / "session-2.1.md"
            original_descendant = descendant_path.read_text(encoding="utf-8")

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    parts=3,
                    state_dir=state_dir,
                )

            self.assertEqual("collision", raised.exception.reason)
            self.assertIn("session-2.1.md", raised.exception.detail)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertEqual(
                original_descendant, descendant_path.read_text(encoding="utf-8")
            )
            self.assertFalse((plan_dir / "session-2.2.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_rejects_existing_child_expansion_as_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            (plan_dir / "session-2.2.expand.md").write_text(
                "stale expansion\n", encoding="utf-8"
            )

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    parts=3,
                    state_dir=state_dir,
                )

            self.assertEqual("collision", raised.exception.reason)
            self.assertIn("session-2.2.expand.md", raised.exception.detail)
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_rejects_active_lock_without_mutating_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            state_locks.acquire_lock(plan_dir, state_dir)
            try:
                with self.assertRaises(resplit.ResplitError) as raised:
                    resplit.resplit_session(
                        plan_dir,
                        session_id="2",
                        parts=2,
                        state_dir=state_dir,
                    )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual("lock-contention", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_active_lock_remediation_advises_waiting_not_unlock(self) -> None:
        # A live active lock is never stale, so the helper cannot self-clear it
        # and must refuse. Its remediation must point at the active runner, not
        # at unlock/--force (which the skill policy forbids for an active lock).
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            state_locks.acquire_lock(plan_dir, state_dir, owner="other-runner")
            try:
                json_out = io.StringIO()
                with redirect_stdout(json_out):
                    json_code = resplit.main(
                        [
                            str(plan_dir),
                            "--session",
                            "2",
                            "--parts",
                            "2",
                            "--state-dir",
                            str(state_dir),
                            "--json",
                        ]
                    )
                text_out = io.StringIO()
                with redirect_stdout(text_out):
                    text_code = resplit.main(
                        [
                            str(plan_dir),
                            "--session",
                            "2",
                            "--parts",
                            "2",
                            "--state-dir",
                            str(state_dir),
                        ]
                    )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual(1, json_code)
            self.assertEqual(1, text_code)

            payload = json.loads(json_out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual("lock-contention", payload["reason"])
            remediation = payload["remediation"]
            self.assertEqual(resplit.PAUSE_REMEDIATION["lock-contention"], remediation)
            lowered = remediation.lower()
            self.assertIn("wait", lowered)
            self.assertIn("active", lowered)
            self.assertNotIn("unlock", lowered)
            self.assertNotIn("force", lowered)

            text = text_out.getvalue()
            self.assertIn("RESPLIT_STATUS: paused", text)
            self.assertIn("RESPLIT_PAUSE: lock-contention", text)
            self.assertIn(f"RESPLIT_REMEDIATION: {remediation}", text)

            # A refused active lock leaves the plan untouched.
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())


if __name__ == "__main__":
    unittest.main()
