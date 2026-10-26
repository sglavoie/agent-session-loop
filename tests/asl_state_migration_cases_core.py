from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_state_migration_support import *


class OrphanGuardTests(StateMigrationTestBase):
    def test_guard_fires_on_moved_plan(self) -> None:
        self._write_orphan_state()
        candidates = state_abandonment.orphaned_state_candidates(
            self.snapshot, self.state_dir
        )
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
            [],
            state_abandonment.orphaned_state_candidates(self.snapshot, self.state_dir),
        )

    def test_guard_silent_when_content_hash_differs(self) -> None:
        self._write_orphan_state(content_sha256="0" * 64)
        self.assertEqual(
            [],
            state_abandonment.orphaned_state_candidates(self.snapshot, self.state_dir),
        )

    def test_guard_fires_from_final_snapshot_after_completion(self) -> None:
        # A completed run mutates handoffs, so the moved plan no longer matches
        # the initial snapshot; discovery must prefer the final snapshot.
        self._write_orphan_state(
            content_sha256="0" * 64,
            final_content_sha256=self.content_sha256,
        )
        candidates = state_abandonment.orphaned_state_candidates(
            self.snapshot, self.state_dir
        )
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
            [],
            state_abandonment.orphaned_state_candidates(self.snapshot, self.state_dir),
        )

    def test_guard_silent_when_old_plan_still_exists(self) -> None:
        # Recreate the old plan on disk: it is no longer an orphan candidate.
        write_plan(self.old_plan, ("pending",))
        self._write_orphan_state()
        self.assertEqual(
            [],
            state_abandonment.orphaned_state_candidates(self.snapshot, self.state_dir),
        )

    def test_guard_surfaced_in_status_payload(self) -> None:
        self._write_orphan_state()
        payload = state_status.status_payload(self.snapshot, self.state_dir)
        self.assertEqual(1, len(payload["orphaned_state_candidates"]))

    def test_migrate_state_included_in_status_text(self) -> None:
        self._write_orphan_state()
        payload = state_status.status_payload(self.snapshot, self.state_dir)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            state_status_text._print_text_status(payload)
        text = buffer.getvalue()
        self.assertIn("Orphaned state:", text)
        self.assertIn("migrate with:", text)


class MigrateStateTests(StateMigrationTestBase):
    def test_dry_run_does_not_touch_disk(self) -> None:
        self._write_orphan_state()
        self._add_result_artifact()
        report = state_migration.migrate_plan_state(
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
        report = state_migration.migrate_plan_state(
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
        state_migration.migrate_plan_state(
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
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("active-lock", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())
        self.assertFalse(self.new_state_dir.exists())

    def test_dry_run_leaves_a_dead_owner_lock_in_place(self) -> None:
        # The dry run read the source lock with reclaim on, so a lock whose
        # owner had died was deleted ("reclaimed stale lock") by a command
        # documented to write nothing.
        self._write_orphan_state()
        lock_path = self._write_dead_owner_lock(self.old_state_dir, self.old_plan)
        before = lock_path.read_bytes()
        with redirect_stderr(io.StringIO()) as stderr:
            report = state_migration.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=False,
            )
        self.assertFalse(report.applied)
        self.assertEqual(before, lock_path.read_bytes())
        self.assertNotIn("reclaimed stale lock", stderr.getvalue())
        self.assertFalse(self.new_state_dir.exists())

    def test_refuses_occupied_destination(self) -> None:
        self._write_orphan_state()
        self.new_state_dir.mkdir(parents=True, exist_ok=True)
        (self.new_state_dir / events.STATE_FILE_NAME).write_text(
            json.dumps({"plan_dir": str(self.new_plan)}), encoding="utf-8"
        )
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
                self.new_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertEqual("destination-occupied", caught.exception.reason)
        self.assertTrue(self.old_state_dir.is_dir())

    def test_refuses_missing_source_state(self) -> None:
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
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
        report = state_migration.migrate_plan_state(
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
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
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
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
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
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
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
            self.content_sha256,
            plans_snapshot.inspect_plan(self.old_plan).content_sha256,
        )
        self._write_orphan_state()
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
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
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            state_migration.migrate_plan_state(
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
        with self.assertRaises(plans_errors.PlanError):
            state_migration.migrate_plan_state(
                not_a_plan,
                from_plan_dir=self.old_plan,
                state_dir=self.state_dir,
                apply=True,
            )
        self.assertTrue(self.old_state_dir.is_dir())

    def test_status_works_post_migration(self) -> None:
        self._write_orphan_state()
        state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        snapshot = plans_snapshot.inspect_plan(self.new_plan)
        payload = state_status.status_payload(snapshot, self.state_dir)
        # State is now keyed under the new path: no orphan, runner state reads.
        self.assertEqual([], payload["orphaned_state_candidates"])
        self.assertTrue(payload["runner_state"]["present"])
        self.assertEqual(
            "2026-07-23T00:00:00+00:00",
            payload["runner_state"]["run_finished_at"],
        )
