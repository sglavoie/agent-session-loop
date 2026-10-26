"""Refreshing a pending review session's scope."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_models,
    plans_session_ids,
    plans_snapshot,
    review,
    review_delta,
    review_session_body,
    run_persistence,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
from tests.asl_review_delta_support import DeltaHarness
from tests.support import (
    read_jsonl,
)


class PendingScopeRefreshTests(DeltaHarness):
    """A pending review's file list is rebuilt from current evidence."""

    def setUp(self) -> None:
        super().setUp()
        self.persistence = run_persistence._RunPersistence(write_state=False)

    def _write_pending_review_session(self, delta: dict[str, Any] | None) -> Path:
        path = self.plan_dir / "session-3.md"
        path.write_text(
            review_session_body.build_generated_review_session_body(
                plans_session_ids.parse_session_id("3"),
                review_harness="codex",
                plan_dir=self.plan_dir,
                delta=delta,
            ),
            encoding="utf-8",
        )
        return path

    def _session_status(self, session_id: str) -> plans_models.SessionStatus:
        snapshot = plans_snapshot.inspect_plan(self.plan_dir)
        return next(
            session for session in snapshot.sessions if session.id == session_id
        )

    def _refresh(self, path: Path) -> bool:
        return review_delta._refresh_pending_review_scope(
            session=self._session_status(path.stem.removeprefix("session-")),
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
            workspace=None,
            run_id="run-1",
            persistence=self.persistence,
        )

    def test_a_full_scope_session_is_never_refreshed(self) -> None:
        # A session that already tells the reviewer to read everything cannot
        # be widened, so there is nothing to rewrite and no event to append.
        path = self._write_pending_review_session(None)
        before = path.read_text(encoding="utf-8")

        self.assertFalse(self._refresh(path))
        self.assertEqual(before, path.read_text(encoding="utf-8"))
        self.assertFalse(self.event_path.exists())

    def test_a_stale_file_list_is_rebuilt_and_the_rewrite_is_recorded(self) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        path = self._write_pending_review_session(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": base,
                "changed_files": ["stale.py"],
                "baseline_dirty": False,
                "prior_findings": [self.finding],
            }
        )
        # Created after the session was authored: invisible to the file list
        # the reviewer would otherwise have been handed.
        (self.repo_root / "appeared.py").write_text("new\n", encoding="utf-8")

        self.assertTrue(self._refresh(path))
        rewritten = path.read_text(encoding="utf-8")
        self.assertIn("appeared.py", rewritten)
        self.assertNotIn("stale.py", rewritten)
        # The session keeps its identity and its pending handoff.
        self.assertIn(review.REVIEW_SESSION_MARKER, rewritten)
        self.assertIn("- **Status**: pending", rewritten)

        recorded = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.REVIEW_SCOPE_EVENT
        ]
        self.assertEqual(1, len(recorded))
        payload = recorded[0]["payload"]
        self.assertEqual("run-1", payload["run_id"])
        self.assertEqual("3", payload["session_id"])
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, payload["scope"])
        self.assertEqual(base, payload["base_sha"])
        self.assertIsNone(payload["fallback_reason"])
        self.assertIs(True, payload["refreshed"])

    def test_a_crlf_session_keeps_its_line_endings_when_rescoped(self) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        path = self._write_pending_review_session(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": base,
                "changed_files": ["stale.py"],
                "baseline_dirty": False,
                "prior_findings": [self.finding],
            }
        )
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        (self.repo_root / "appeared.py").write_text("new\n", encoding="utf-8")

        self.assertTrue(self._refresh(path))
        raw = path.read_bytes()
        self.assertIn(b"appeared.py", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))

    def test_an_unchanged_scope_writes_nothing(self) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        path = self._write_pending_review_session(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": base,
                "changed_files": [],
                "baseline_dirty": False,
                "prior_findings": [self.finding],
            }
        )

        self.assertFalse(self._refresh(path))
        self.assertEqual(
            [],
            [
                record
                for record in read_jsonl(self.event_path)
                if record["name"] == events.REVIEW_SCOPE_EVENT
            ],
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
