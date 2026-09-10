"""State orphan-guard and ``asl migrate-state`` tests.

Covers the guard that surfaces state directories stranded under a plan's old
path hash after a move, and the ``migrate-state`` command that relocates them.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    agent_mux_projection,
    cli,
    conversations,
    events,
    plans,
    state,
    workspaces,
)
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
                str(self.old_plan) if recorded_plan_dir is None else recorded_plan_dir
            ),
            "state_dir": str(self.old_state_dir),
            "state_path": str(state_path),
            "events_path": str(events_path),
            "event_path": str(events_path),
            "lock_path": str(self.old_state_dir / state.LOCK_FILE_NAME),
            "run_finished_at": run_finished_at,
            "initial_snapshot": {
                "content_sha256": (
                    self.content_sha256 if content_sha256 is None else content_sha256
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
        result_path = self.old_state_dir / "runs" / "run-1" / "turn-1" / "result.json"
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
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
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


class RelocationSchemaUpgradeTests(StateMigrationTestBase):
    """Relocation brings the record it rewrites forward in the same write."""

    def _write_projectable_orphan_state(self, *, version: int | None = 2) -> Path:
        state_path = self._write_orphan_state()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["run_id"] = "20260723T000000Z-1"
        payload["run_started_at"] = "2026-07-22T23:00:00+00:00"
        payload["workspace_root"] = str(self.tmp_path)
        if version is not None:
            payload["version"] = version
        state_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return state_path

    def test_apply_upgrades_the_relocated_record(self) -> None:
        self._write_projectable_orphan_state()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertEqual(state.MIGRATION_MODE_RELOCATE, report.mode)
        self.assertTrue(report.schema_upgraded)
        self.assertTrue(report.projection_written)
        self.assertEqual(2, report.schema_version_before)
        self.assertEqual(
            agent_mux_projection.STATE_SCHEMA_VERSION, report.schema_version_after
        )

        migrated = json.loads(
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )
        projection = migrated[agent_mux_projection.PROJECTION_KEY]
        # Projected after the path rewrite, so it names where the state now
        # lives rather than where it was stranded.
        self.assertEqual(str(self.new_plan), projection["plan_dir"])
        self.assertEqual(agent_mux_projection.PHASE_FINISHED, projection["phase"])
        # The migrator does not own this run, so it names no process.
        self.assertIsNone(projection["process"])

    def test_apply_records_the_upgrade_in_the_migration_event(self) -> None:
        self._write_projectable_orphan_state()
        state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        payload = [
            record["payload"]
            for record in read_jsonl(self.new_state_dir / events.EVENTS_FILE_NAME)
            if record.get("name") == state.STATE_MIGRATED_EVENT
        ][0]
        self.assertEqual(2, payload["from_schema_version"])
        self.assertEqual(
            agent_mux_projection.STATE_SCHEMA_VERSION, payload["to_schema_version"]
        )
        self.assertEqual(1, payload["projection_generation"])

    def test_dry_run_reports_the_upgrade_without_writing(self) -> None:
        state_path = self._write_projectable_orphan_state()
        before = state_path.read_bytes()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertTrue(report.schema_upgraded)
        self.assertIn("→", report.schema_note)
        self.assertEqual(before, state_path.read_bytes())

    def test_relocation_still_succeeds_for_a_record_that_cannot_project(self) -> None:
        # The default orphan fixture records no run identity. Restoring a
        # stranded directory's paths is worth doing whether or not the record
        # can carry a projection, so this relocates and reports the shortfall.
        self._write_orphan_state()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertFalse(report.schema_upgraded)
        self.assertFalse(report.projection_written)
        self.assertIn("no run identity", report.schema_note)
        migrated = json.loads(
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )
        self.assertNotIn(agent_mux_projection.PROJECTION_KEY, migrated)

    def test_relocation_leaves_an_unrecognised_version_alone(self) -> None:
        self._write_projectable_orphan_state(version=99)
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertFalse(report.schema_upgraded)
        self.assertIn("cannot upgrade", report.schema_note)
        migrated = json.loads(
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(99, migrated["version"])
        self.assertEqual(str(self.new_plan), migrated["plan_dir"])


class UpgradeStateSchemaTests(StateMigrationTestBase):
    """``migrate-state`` with no ``--from``: upgrade a plan's own state."""

    def _write_own_state(
        self,
        *,
        version: int | None = None,
        paused: bool = False,
        run_finished_at: str | None = None,
        projectable: bool = True,
        extra: dict | None = None,
    ) -> Path:
        self.new_state_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.new_state_dir / events.STATE_FILE_NAME
        payload: dict = {
            "ok": True,
            "command": "run",
            "plan_dir": str(self.new_plan),
            "state_dir": str(self.new_state_dir),
            "state_path": str(state_path),
            "workspace_root": str(self.tmp_path),
            "turns": [{"turn_index": 0, "session_id": "1", "harness": "claude"}],
            "initial_snapshot": {"content_sha256": self.content_sha256},
        }
        if projectable:
            payload["run_id"] = "20260723T000000Z-1"
            payload["run_started_at"] = "2026-07-22T23:00:00+00:00"
        if version is not None:
            payload["version"] = version
        if paused:
            payload["paused"] = True
            payload["pause_reason"] = "blocked"
            payload["pause_details"] = {"question_id": "q-7"}
        if run_finished_at is not None:
            payload["run_finished_at"] = run_finished_at
        if extra:
            payload.update(extra)
        state_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        return state_path

    def _write_own_active_lock(self) -> Path:
        self.new_state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.new_state_dir / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.new_plan),
                    "created_at": "2026-07-23T00:00:00+00:00",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return lock_path

    def _upgrade(self, *, apply: bool = True) -> state.MigrationReport:
        return state.migrate_plan_state(
            self.new_plan, state_dir=self.state_dir, apply=apply
        )

    def _read(self) -> dict:
        return json.loads(
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )

    def test_absent_version_upgrades(self) -> None:
        self._write_own_state()
        report = self._upgrade()
        self.assertEqual(state.MIGRATION_MODE_UPGRADE, report.mode)
        self.assertIsNone(report.from_plan_dir)
        self.assertIsNone(report.schema_version_before)
        self.assertTrue(report.schema_upgraded)
        record = self._read()
        self.assertEqual(agent_mux_projection.STATE_SCHEMA_VERSION, record["version"])
        self.assertEqual(
            agent_mux_projection.PHASE_BETWEEN,
            record[agent_mux_projection.PROJECTION_KEY]["phase"],
        )

    def test_v1_and_v2_both_upgrade(self) -> None:
        for version in agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS:
            with self.subTest(version=version):
                self._write_own_state(version=version)
                report = self._upgrade()
                self.assertEqual(version, report.schema_version_before)
                self.assertTrue(report.schema_upgraded)
                self.assertEqual(
                    agent_mux_projection.STATE_SCHEMA_VERSION, self._read()["version"]
                )

    def test_paused_state_keeps_its_pause_evidence(self) -> None:
        self._write_own_state(version=2, paused=True)
        self._upgrade()
        record = self._read()
        self.assertTrue(record["paused"])
        self.assertEqual("blocked", record["pause_reason"])
        self.assertEqual({"question_id": "q-7"}, record["pause_details"])
        projection = record[agent_mux_projection.PROJECTION_KEY]
        self.assertEqual(agent_mux_projection.PHASE_PAUSED, projection["phase"])
        self.assertEqual("q-7", projection["question_id"])
        self.assertIsNone(projection["process"])

    def test_terminal_state_keeps_its_ending(self) -> None:
        self._write_own_state(version=2, run_finished_at="2026-07-23T00:00:00+00:00")
        self._upgrade()
        record = self._read()
        self.assertEqual("2026-07-23T00:00:00+00:00", record["run_finished_at"])
        projection = record[agent_mux_projection.PROJECTION_KEY]
        self.assertEqual(agent_mux_projection.PHASE_FINISHED, projection["phase"])
        self.assertEqual("2026-07-23T00:00:00+00:00", projection["finished_at"])
        self.assertIsNone(projection["process"])

    def test_upgrade_preserves_every_other_field(self) -> None:
        self._write_own_state(version=2, extra={"run_config": {"resume": True}})
        before = self._read()
        self._upgrade()
        after = self._read()
        for key, value in before.items():
            if key == "version":
                continue
            with self.subTest(key=key):
                self.assertEqual(value, after[key])

    def test_upgrade_is_idempotent(self) -> None:
        self._write_own_state(version=2)
        self._upgrade()
        first = self._read()
        second_report = self._upgrade()
        self.assertFalse(second_report.applied)
        self.assertFalse(second_report.schema_upgraded)
        self.assertTrue(second_report.projection_written)
        self.assertIn("already at schema version", second_report.schema_note)
        # No second write, so no second generation either.
        self.assertEqual(first, self._read())

    def test_dry_run_writes_nothing(self) -> None:
        state_path = self._write_own_state(version=2)
        before = state_path.read_bytes()
        report = self._upgrade(apply=False)
        self.assertFalse(report.applied)
        self.assertTrue(report.schema_upgraded)
        self.assertEqual(before, state_path.read_bytes())

    def test_upgrade_appends_a_state_migrated_event(self) -> None:
        self._write_own_state(version=2)
        self._upgrade()
        migrated = [
            record["payload"]
            for record in read_jsonl(self.new_state_dir / events.EVENTS_FILE_NAME)
            if record.get("name") == state.STATE_MIGRATED_EVENT
        ]
        self.assertEqual(1, len(migrated))
        self.assertEqual(2, migrated[0]["from_schema_version"])
        self.assertEqual(str(self.new_plan), migrated[0]["plan_dir"])

    def test_refuses_a_live_incompatible_run(self) -> None:
        # An old process holds this payload in memory and rewrites the whole
        # file on its next transition, so an upgrade underneath it would be
        # silently reverted — and it emits schema-1 hooks meanwhile.
        state_path = self._write_own_state(version=2)
        before = state_path.read_bytes()
        self._write_own_active_lock()
        with self.assertRaises(state.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("incompatible-active-run", caught.exception.reason)
        self.assertTrue(caught.exception.details["incompatible_active_run"])
        self.assertEqual(os.getpid(), caught.exception.details["pid"])
        self.assertEqual(before, state_path.read_bytes())

    def test_a_live_run_already_at_v3_is_a_benign_noop(self) -> None:
        self._write_own_state(version=2)
        self._upgrade()
        self._write_own_active_lock()
        report = self._upgrade()
        self.assertFalse(report.applied)
        self.assertFalse(report.schema_upgraded)

    def test_refuses_an_unrecognised_version(self) -> None:
        self._write_own_state(version=99)
        with self.assertRaises(state.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("unknown-schema-version", caught.exception.reason)
        self.assertEqual(99, self._read()["version"])

    def test_refuses_a_record_with_no_run_identity(self) -> None:
        self._write_own_state(version=2, projectable=False)
        with self.assertRaises(state.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("unprojectable-state", caught.exception.reason)
        self.assertEqual(2, self._read()["version"])

    def test_refuses_when_there_is_no_state_to_upgrade(self) -> None:
        with self.assertRaises(state.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("no-source-state", caught.exception.reason)

    def test_state_schema_version_reads_every_spelling(self) -> None:
        self.assertIsNone(state.state_schema_version({}))
        self.assertEqual(2, state.state_schema_version({"version": 2}))
        self.assertEqual(-1, state.state_schema_version({"version": "two"}))


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


class ConversationRelocationTests(StateMigrationTestBase):
    """The conversation ledger moves with the state directory it lives in.

    Conversation references are plan-owned operational state stored beside
    ``state.json`` rather than inside it, so relocating a stranded state
    directory has to repoint them too -- and has to refuse a ledger that belongs
    to some other plan.
    """

    def _seed_conversation(
        self, *, plan_dir: Path | None = None, session_id: str = "1"
    ) -> conversations.ConversationReference:
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        reference = conversations.ConversationReference(
            binding=conversations.ConversationBinding(
                plan_dir=str(self.old_plan if plan_dir is None else plan_dir),
                session_id=session_id,
                harness="claude",
                workspace=workspaces.WorkspaceBinding(
                    workspace_root=str(plans.canonicalize_path(self.tmp_path)),
                    registry_digest="a" * 64,
                ),
                access_profile="worker",
            ),
            native_id="conv-1",
        )
        conversations.open_conversation(self.old_state_dir, reference)
        conversations.confirm_conversation(self.old_state_dir, reference)
        return reference

    def _binding_for(self, plan_dir: Path) -> conversations.ConversationBinding:
        return conversations.ConversationBinding(
            plan_dir=str(plan_dir),
            session_id="1",
            harness="claude",
            workspace=workspaces.WorkspaceBinding(
                workspace_root=str(plans.canonicalize_path(self.tmp_path)),
                registry_digest="a" * 64,
            ),
            access_profile="worker",
        )

    def test_dry_run_reports_the_relocation_without_writing(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        ledger_path = conversations.conversation_ledger_path(self.old_state_dir)
        before = ledger_path.read_bytes()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        )
        self.assertEqual(2, report.conversation_records)
        self.assertEqual(2, report.conversation_records_rewritten)
        self.assertIsNotNone(report.conversation_note)
        self.assertEqual(before, ledger_path.read_bytes())

    def test_apply_repoints_the_ledger_and_keeps_it_resumable(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertEqual(2, report.conversation_records_rewritten)
        self.assertFalse(
            conversations.conversation_ledger_path(self.old_state_dir).exists()
        )
        self.assertEqual(
            (str(self.new_plan),),
            conversations.recorded_plan_dirs(self.new_state_dir),
        )
        selection = conversations.select_conversation(
            self.new_state_dir, self._binding_for(self.new_plan)
        )
        self.assertTrue(selection.resumable)
        self.assertEqual("conv-1", selection.reference.native_id)

    def test_migrated_event_records_the_conversation_counts(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        records = read_jsonl(self.new_state_dir / events.EVENTS_FILE_NAME)
        migrated = [
            record
            for record in records
            if record.get("name") == state.STATE_MIGRATED_EVENT
        ]
        self.assertEqual(1, len(migrated))
        self.assertEqual(2, migrated[0]["payload"]["conversation_records"])
        self.assertEqual(2, migrated[0]["payload"]["conversation_records_rewritten"])

    def test_a_foreign_ledger_refuses_the_migration(self) -> None:
        self._write_orphan_state()
        stranger = plans.canonicalize_path(self.tmp_path / "plans" / "stranger")
        self._seed_conversation(plan_dir=stranger)
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("conversation-plan-mismatch", caught.exception.reason)
        # Refused before anything moved, exactly like the other identity gates.
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_unreadable_lines_are_reported_not_refused(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        ledger_path = conversations.conversation_ledger_path(self.old_state_dir)
        with ledger_path.open("ab") as handle:
            handle.write(b"{not json}\n")
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        # One truncated append must not strand the whole state directory.
        self.assertTrue(report.applied)
        self.assertIn("unreadable", report.conversation_note)
        self.assertIn(
            "{not json}",
            conversations.conversation_ledger_path(self.new_state_dir).read_text(
                encoding="utf-8"
            ),
        )

    def test_an_unreadable_ledger_file_refuses_before_the_rename(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        ledger_path = conversations.conversation_ledger_path(self.old_state_dir)
        ledger_path.chmod(0o000)
        self.addCleanup(ledger_path.chmod, 0o600)
        if os.access(ledger_path, os.R_OK):
            self.skipTest("running as a user that ignores file permissions")
        with self.assertRaises(state.MigrationError) as caught:
            state.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertEqual("conversation-ledger-error", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_a_plan_with_no_ledger_migrates_unchanged(self) -> None:
        self._write_orphan_state()
        report = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertTrue(report.applied)
        self.assertEqual(0, report.conversation_records)
        self.assertIsNone(report.conversation_note)

    def test_report_dict_carries_the_conversation_fields(self) -> None:
        self._write_orphan_state()
        self._seed_conversation()
        payload = state.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=False,
        ).to_dict()
        self.assertEqual(2, payload["conversation_records"])
        self.assertEqual(2, payload["conversation_records_rewritten"])
        self.assertIn("conversation_note", payload)


if __name__ == "__main__":
    unittest.main()
