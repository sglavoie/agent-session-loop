"""State orphan-guard and ``asl migrate-state`` tests.

Covers the guard that surfaces state directories stranded under a plan's old
path hash after a move, and the ``migrate-state`` command that relocates them.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_session_loop import cli, events, plans, state
from tests.support import read_jsonl, write_plan


class StateMigrationTestBase(unittest.TestCase):
    """Shared fixture: a plan moved from ``old`` to ``new`` with orphan state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"

        # The plan lives at its *new* location; the *old* path never exists on
        # disk (it was renamed away), which is exactly the orphan condition.
        self.new_plan = self.tmp_path / "plans" / "beta"
        write_plan(self.new_plan, ("pending",))
        self.new_plan = plans.canonicalize_path(self.new_plan)
        self.old_plan = plans.canonicalize_path(self.tmp_path / "plans" / "alpha")

        self.snapshot = plans.inspect_plan(self.new_plan)
        self.content_sha256 = self.snapshot.content_sha256

        self.old_state_dir = state.plan_state_dir(self.old_plan, self.state_dir)
        self.new_state_dir = state.plan_state_dir(self.new_plan, self.state_dir)

    def _write_orphan_state(
        self,
        *,
        content_sha256: str | None = None,
        final_content_sha256: str | None = None,
        recorded_plan_dir: str | None = None,
        run_finished_at: str | None = "2026-07-23T00:00:00+00:00",
    ) -> Path:
        """Seed an orphaned ``state.json`` under the old plan's state dir.

        By default only an ``initial_snapshot`` is written (legacy shape). Pass
        ``final_content_sha256`` to also persist a ``final_snapshot`` — the
        shape a run leaves behind after mutating handoffs. ``recorded_plan_dir``
        overrides the recorded ``plan_dir`` to exercise identity mismatches.
        """
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.old_state_dir / events.STATE_FILE_NAME
        events_path = self.old_state_dir / events.EVENTS_FILE_NAME
        payload = {
            "ok": True,
            "command": "run",
            "plan_dir": (
                str(self.old_plan)
                if recorded_plan_dir is None
                else recorded_plan_dir
            ),
            "state_dir": str(self.old_state_dir),
            "state_path": str(state_path),
            "events_path": str(events_path),
            "event_path": str(events_path),
            "lock_path": str(self.old_state_dir / state.LOCK_FILE_NAME),
            "run_finished_at": run_finished_at,
            "initial_snapshot": {
                "content_sha256": (
                    self.content_sha256
                    if content_sha256 is None
                    else content_sha256
                ),
            },
        }
        if final_content_sha256 is not None:
            payload["final_snapshot"] = {"content_sha256": final_content_sha256}
        state_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return state_path

    def _write_active_lock(self) -> Path:
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.old_state_dir / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.old_plan),
                    "created_at": "2026-07-23T00:00:00+00:00",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return lock_path

    def _add_result_artifact(self) -> Path:
        result_path = (
            self.old_state_dir / "runs" / "run-1" / "turn-1" / "result.json"
        )
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"after": {"plan_dir": str(self.old_plan)}}),
            encoding="utf-8",
        )
        return result_path


class OrphanGuardTests(StateMigrationTestBase):
    def test_guard_fires_on_moved_plan(self) -> None:
        self._write_orphan_state()
        candidates = state.orphaned_state_candidates(self.snapshot, self.state_dir)
        self.assertEqual(1, len(candidates))
        candidate = candidates[0]
        self.assertEqual(str(self.old_plan), candidate["old_plan_dir"])
        self.assertEqual(str(self.old_state_dir), candidate["state_dir"])
        self.assertEqual("2026-07-23T00:00:00+00:00", candidate["run_finished_at"])
        self.assertIn("migrate-state", candidate["migrate_command"])
        self.assertIn(str(self.new_plan), candidate["migrate_command"])
        self.assertIn(str(self.old_plan), candidate["migrate_command"])

    def test_guard_silent_when_own_state_present(self) -> None:
        self._write_orphan_state()
        # Once this plan has its own state.json, the (expensive) scan is skipped.
        self.new_state_dir.mkdir(parents=True, exist_ok=True)
        (self.new_state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(self.new_plan)}), encoding="utf-8"
        )
        self.assertEqual(
            [], state.orphaned_state_candidates(self.snapshot, self.state_dir)
        )

    def test_guard_silent_when_content_hash_differs(self) -> None:
        self._write_orphan_state(content_sha256="0" * 64)
        self.assertEqual(
            [], state.orphaned_state_candidates(self.snapshot, self.state_dir)
        )

    def test_guard_fires_from_final_snapshot_after_completion(self) -> None:
        # A completed run mutates handoffs, so the moved plan no longer matches
        # the initial snapshot; discovery must prefer the final snapshot.
        self._write_orphan_state(
            content_sha256="0" * 64,
            final_content_sha256=self.content_sha256,
        )
        candidates = state.orphaned_state_candidates(self.snapshot, self.state_dir)
        self.assertEqual(1, len(candidates))
        self.assertEqual(str(self.old_plan), candidates[0]["old_plan_dir"])

    def test_guard_prefers_final_over_initial(self) -> None:
        # Even when the initial snapshot happens to match, a present final
        # snapshot is authoritative: a stale (non-matching) final hides it.
        self._write_orphan_state(
            content_sha256=self.content_sha256,
            final_content_sha256="0" * 64,
        )
        self.assertEqual(
            [], state.orphaned_state_candidates(self.snapshot, self.state_dir)
        )

    def test_guard_silent_when_old_plan_still_exists(self) -> None:
        # Recreate the old plan on disk: it is no longer an orphan candidate.
        write_plan(self.old_plan, ("pending",))
        self._write_orphan_state()
        self.assertEqual(
            [], state.orphaned_state_candidates(self.snapshot, self.state_dir)
        )

    def test_guard_surfaced_in_status_payload(self) -> None:
        self._write_orphan_state()
        payload = state.status_payload(self.snapshot, self.state_dir)
        self.assertEqual(1, len(payload["orphaned_state_candidates"]))

    def test_migrate_state_included_in_status_text(self) -> None:
        self._write_orphan_state()
        payload = state.status_payload(self.snapshot, self.state_dir)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            state._print_text_status(payload)
        text = buffer.getvalue()
        self.assertIn("Orphaned state:", text)
        self.assertIn("migrate with:", text)


class MigrateStateTests(StateMigrationTestBase):
    def test_dry_run_does_not_touch_disk(self) -> None:
        self._write_orphan_state()
        self._add_result_artifact()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertFalse(report.applied)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())
        self.assertIn("plan_dir", report.rewritten_fields)
        self.assertIn("state_path", report.rewritten_fields)
        self.assertIn("lock_path", report.rewritten_fields)
        self.assertEqual(1, report.result_artifacts)
        self.assertIsNotNone(report.unmigrated_artifacts_note)

    def test_apply_renames_and_rewrites_paths(self) -> None:
        self._write_orphan_state()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertFalse(self.old_state_dir.exists())
        self.assertTrue(self.new_state_dir.is_dir())

        migrated = json.loads(
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(str(self.new_plan), migrated["plan_dir"])
        self.assertEqual(str(self.new_state_dir), migrated["state_dir"])
        self.assertEqual(
            str(self.new_state_dir / events.STATE_FILE_NAME),
            migrated["state_path"],
        )
        self.assertEqual(
            str(self.new_state_dir / events.EVENTS_FILE_NAME),
            migrated["events_path"],
        )
        self.assertEqual(
            str(self.new_state_dir / events.EVENTS_FILE_NAME),
            migrated["event_path"],
        )
        self.assertEqual(
            str(self.new_state_dir / state.LOCK_FILE_NAME),
            migrated["lock_path"],
        )

    def test_apply_appends_state_migrated_event(self) -> None:
        self._write_orphan_state()
        state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        records = read_jsonl(self.new_state_dir / events.EVENTS_FILE_NAME)
        migrated_events = [
            record
            for record in records
            if record.get("name") == state.STATE_MIGRATED_EVENT
        ]
        self.assertEqual(1, len(migrated_events))
        payload = migrated_events[0]["payload"]
        self.assertEqual(str(self.old_plan), payload["from_plan_dir"])
        self.assertEqual(str(self.new_plan), payload["to_plan_dir"])

    def test_refuses_active_lock(self) -> None:
        self._write_orphan_state()
        self._write_active_lock()
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("active-lock", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_refuses_occupied_destination(self) -> None:
        self._write_orphan_state()
        self.new_state_dir.mkdir(parents=True, exist_ok=True)
        (self.new_state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(self.new_plan)}), encoding="utf-8"
        )
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("destination-occupied", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())

    def test_refuses_missing_source_state(self) -> None:
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("no-source-state", caught.exception.reason)

    def test_migrates_using_final_snapshot_content(self) -> None:
        # A completed-then-moved plan: only the final snapshot matches the
        # current content, and migration must accept it on that basis.
        self._write_orphan_state(
            content_sha256="0" * 64,
            final_content_sha256=self.content_sha256,
        )
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertTrue(self.new_state_dir.is_dir())
        self.assertFalse(self.old_state_dir.exists())

    def test_refuses_source_plan_mismatch(self) -> None:
        # State that records some other plan must not be reattached here.
        self._write_orphan_state(
            recorded_plan_dir=str(self.tmp_path / "plans" / "gamma"),
        )
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("source-plan-mismatch", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_refuses_content_mismatch_on_apply(self) -> None:
        self._write_orphan_state(content_sha256="0" * 64)
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("content-mismatch", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_refuses_content_mismatch_on_dry_run(self) -> None:
        # The dry-run must reject an incompatible source too, not just --apply.
        self._write_orphan_state(content_sha256="0" * 64)
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("content-mismatch", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())

    def test_refuses_when_source_plan_still_exists_on_apply(self) -> None:
        # Two existing plans with identical content and an unlocked source
        # state: identity and content checks pass, so without the guard --apply
        # would rename the live source's state dir to the target hash.
        write_plan(self.old_plan, ("pending",))
        self.assertEqual(
            self.content_sha256, plans.inspect_plan(self.old_plan).content_sha256
        )
        self._write_orphan_state()
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("source-plan-exists", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_refuses_when_source_plan_still_exists_on_dry_run(self) -> None:
        # The dry-run must reject a still-existing source too, not just --apply.
        write_plan(self.old_plan, ("pending",))
        self._write_orphan_state()
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("source-plan-exists", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_refuses_target_that_is_not_a_plan(self) -> None:
        self._write_orphan_state()
        not_a_plan = self.tmp_path / "plans" / "empty"
        not_a_plan.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(plans.PlanError):
            state.migrate_plan_state(
                not_a_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertTrue(self.old_state_dir.is_dir())

    def test_status_works_post_migration(self) -> None:
        self._write_orphan_state()
        state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        snapshot = plans.inspect_plan(self.new_plan)
        payload = state.status_payload(snapshot, self.state_dir)
        # State is now keyed under the new path: no orphan, runner state reads.
        self.assertEqual([], payload["orphaned_state_candidates"])
        self.assertTrue(payload["runner_state"]["present"])
        self.assertEqual(
            "2026-07-23T00:00:00+00:00",
            payload["runner_state"]["run_finished_at"],
        )


class MigrateStateCliTests(StateMigrationTestBase):
    def _run_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        parser = cli.build_parser(prog="asl")
        args = parser.parse_args(argv)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.resolve_handler(args)(args)
        stdout = out.getvalue()
        parsed = json.loads(stdout) if stdout.strip() else {}
        return code, parsed, err.getvalue()

    def test_cli_dry_run_then_apply(self) -> None:
        self._write_orphan_state()
        base = [
            "migrate-state",
            str(self.new_plan),
            "--from",
            str(self.old_plan),
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        code, dry, _ = self._run_cli(base)
        self.assertEqual(0, code)
        self.assertEqual("migrate-state", dry["command"])
        self.assertFalse(dry["applied"])
        self.assertTrue(self.old_state_dir.is_dir())

        code, applied, _ = self._run_cli(base + ["--apply"])
        self.assertEqual(0, code)
        self.assertTrue(applied["applied"])
        self.assertTrue(self.new_state_dir.is_dir())
        self.assertFalse(self.old_state_dir.exists())

    def test_cli_active_lock_json_error(self) -> None:
        self._write_orphan_state()
        self._write_active_lock()
        code, payload, _ = self._run_cli(
            [
                "migrate-state",
                str(self.new_plan),
                "--from",
                str(self.old_plan),
                "--state-dir",
                str(self.state_dir),
                "--apply",
                "--json",
            ]
        )
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("active-lock", payload["reason"])


if __name__ == "__main__":
    unittest.main()
