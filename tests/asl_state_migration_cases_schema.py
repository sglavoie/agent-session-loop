from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_state_migration_support import *


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
        report = state_migration.migrate_plan_state(
            self.new_plan,
            from_plan_dir=self.old_plan,
            state_dir=self.state_dir,
            apply=True,
        )
        self.assertEqual(state_schema_upgrade.MIGRATION_MODE_RELOCATE, report.mode)
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
        state_migration.migrate_plan_state(
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
        report = state_migration.migrate_plan_state(
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
        report = state_migration.migrate_plan_state(
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
        report = state_migration.migrate_plan_state(
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

    def _upgrade(self, *, apply: bool = True) -> state_schema_upgrade.MigrationReport:
        return state_migration.migrate_plan_state(
            self.new_plan, state_dir=self.state_dir, apply=apply
        )

    def _read(self) -> dict:
        return json.loads(
            (self.new_state_dir / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )

    def test_absent_version_upgrades(self) -> None:
        self._write_own_state()
        report = self._upgrade()
        self.assertEqual(state_schema_upgrade.MIGRATION_MODE_UPGRADE, report.mode)
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
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("incompatible-active-run", caught.exception.reason)
        self.assertTrue(caught.exception.details["incompatible_active_run"])
        self.assertEqual(os.getpid(), caught.exception.details["pid"])
        self.assertEqual(before, state_path.read_bytes())

    def test_dry_run_leaves_a_dead_owner_lock_and_apply_reclaims_it(self) -> None:
        # The dry run used to delete a lock whose owner had died; reclaiming
        # it is a write, so only --apply may do it.
        state_path = self._write_own_state(version=2)
        before = state_path.read_bytes()
        lock_path = self._write_dead_owner_lock(self.new_state_dir, self.new_plan)
        lock_before = lock_path.read_bytes()
        with redirect_stderr(io.StringIO()) as stderr:
            report = self._upgrade(apply=False)
        self.assertFalse(report.applied)
        self.assertTrue(report.schema_upgraded)
        self.assertEqual(lock_before, lock_path.read_bytes())
        self.assertEqual(before, state_path.read_bytes())
        self.assertNotIn("reclaimed stale lock", stderr.getvalue())

        with redirect_stderr(io.StringIO()):
            applied = self._upgrade(apply=True)
        self.assertTrue(applied.applied)
        self.assertFalse(lock_path.exists())

    def test_a_live_run_already_at_v3_is_a_benign_noop(self) -> None:
        self._write_own_state(version=2)
        self._upgrade()
        self._write_own_active_lock()
        report = self._upgrade()
        self.assertFalse(report.applied)
        self.assertFalse(report.schema_upgraded)

    def test_refuses_an_unrecognised_version(self) -> None:
        self._write_own_state(version=99)
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("unknown-schema-version", caught.exception.reason)
        self.assertEqual(99, self._read()["version"])

    def test_refuses_a_record_with_no_run_identity(self) -> None:
        self._write_own_state(version=2, projectable=False)
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("unprojectable-state", caught.exception.reason)
        self.assertEqual(2, self._read()["version"])

    def test_refuses_when_there_is_no_state_to_upgrade(self) -> None:
        with self.assertRaises(state_schema_upgrade.MigrationError) as caught:
            self._upgrade()
        self.assertEqual("no-source-state", caught.exception.reason)

    def test_state_schema_version_reads_every_spelling(self) -> None:
        self.assertIsNone(state_schema_upgrade.state_schema_version({}))
        self.assertEqual(2, state_schema_upgrade.state_schema_version({"version": 2}))
        self.assertEqual(
            -1, state_schema_upgrade.state_schema_version({"version": "two"})
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

    def test_cli_missing_plan_directory_is_plan_not_found(self) -> None:
        missing = self.tmp_path / "plans" / "no-such-plan"
        for extra in ([], ["--from", str(self.old_plan)]):
            with self.subTest(extra=extra):
                code, payload, _ = self._run_cli(
                    [
                        "migrate-state",
                        str(missing),
                        *extra,
                        "--state-dir",
                        str(self.state_dir),
                        "--json",
                    ]
                )
                self.assertEqual(1, code)
                self.assertEqual("plan-not-found", payload["reason"])
                self.assertIn("plan directory does not exist", payload["error"])

    def test_cli_reads_orphaned_state_whose_plan_directory_is_gone(self) -> None:
        # The old path never exists on disk, yet its state is still read for an
        # in-place upgrade: a missing directory only refuses when no state
        # exists either, so the refusal here is about the record itself.
        self._write_orphan_state()
        _, payload, _ = self._run_cli(
            [
                "migrate-state",
                str(self.old_plan),
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )
        self.assertNotIn(payload["reason"], {"plan-not-found", "no-source-state"})
