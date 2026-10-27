from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_workbench_archive_support import *


class WorkbenchArchiveStorageTests(unittest.TestCase):
    def _metadata(
        self, tmp_path: Path, archive_id: str
    ) -> workbench_archive.ArchiveMetadata:
        return workbench_archive.ArchiveMetadata(
            archive_id=archive_id,
            original_plan_dir=str(tmp_path / "plans" / "alpha"),
            archived_at="2026-07-03T12:00:00+00:00",
            expires_at="2026-08-02T12:00:00+00:00",
            summary="alpha plan snapshot",
            pin=workbench_registry_models.RegistryPin(
                plan_dir=str(tmp_path / "plans" / "alpha"),
                label="Alpha",
                profile=workbench_registry_models.LaunchProfile(harness="codex"),
            ),
            plan_archived=True,
            state_archived=False,
        )

    def test_new_archive_id_is_valid_and_unique(self) -> None:
        first = workbench_archive.new_archive_id()
        second = workbench_archive.new_archive_id()
        self.assertNotEqual(first, second)
        self.assertNotIn("/", first)
        self.assertTrue(first)

    def test_archive_paths_live_under_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            root = workbench_archive.archive_root(state_root)
            entry_dir = workbench_archive.archive_entry_dir("abc123", state_root)

            self.assertEqual(
                state.state_root(state_root) / workbench_archive.ARCHIVE_DIR_NAME, root
            )
            self.assertEqual(root / "abc123", entry_dir)
            self.assertEqual(
                entry_dir / workbench_archive.ARCHIVE_PLAN_DIR_NAME,
                workbench_archive.archive_plan_path("abc123", state_root),
            )

    def test_invalid_archive_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            for bad in ("", "..", "a/b", "a\\b"):
                with self.assertRaises(workbench_registry_models.WorkbenchError):
                    workbench_archive.archive_entry_dir(bad, state_root)

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            metadata = self._metadata(tmp_path, "20260703T120000Z-abcdef123456")

            meta_path = workbench_archive.save_archive_metadata(metadata, state_root)
            self.assertTrue(meta_path.is_file())

            loaded = workbench_archive.load_archive_metadata(
                metadata.archive_id, state_root
            )
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(metadata.archive_id, loaded.archive_id)
            self.assertEqual(metadata.original_plan_dir, loaded.original_plan_dir)
            self.assertTrue(loaded.plan_archived)
            self.assertFalse(loaded.state_archived)
            self.assertEqual("alpha plan snapshot", loaded.summary)
            self.assertIsNotNone(loaded.pin)
            assert loaded.pin is not None
            self.assertEqual("Alpha", loaded.pin.label)
            self.assertEqual(
                workbench_archive.archive_entry_dir(metadata.archive_id, state_root),
                loaded.archive_dir,
            )
            self.assertEqual(
                workbench_archive.archive_plan_path(metadata.archive_id, state_root),
                loaded.plan_path,
            )
            self.assertEqual((), loaded.warnings)

    def test_load_missing_entry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            self.assertIsNone(
                workbench_archive.load_archive_metadata("nope", state_root)
            )

    def test_list_skips_malformed_entry_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            good = self._metadata(tmp_path, "20260703T120000Z-goodgoodgood")
            workbench_archive.save_archive_metadata(good, state_root)

            bad_dir = (
                workbench_archive.archive_root(state_root) / "20260703T130000Z-bad"
            )
            bad_dir.mkdir(parents=True)
            (bad_dir / workbench_archive.ARCHIVE_METADATA_FILE_NAME).write_text(
                "{ not json", encoding="utf-8"
            )

            listing = workbench_archive.list_archive_entries(state_root)

            self.assertEqual(1, len(listing.entries))
            self.assertEqual(good.archive_id, listing.entries[0].archive_id)
            self.assertTrue(listing.warnings)

    def test_list_missing_root_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            listing = workbench_archive.list_archive_entries(state_root)
            self.assertEqual((), listing.entries)
            self.assertEqual((), listing.warnings)

    def test_remove_archive_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            metadata = self._metadata(tmp_path, "20260703T120000Z-removeme1234")
            workbench_archive.save_archive_metadata(metadata, state_root)

            self.assertTrue(
                workbench_archive.remove_archive_entry(metadata.archive_id, state_root)
            )
            self.assertFalse(
                workbench_archive.archive_entry_dir(
                    metadata.archive_id, state_root
                ).exists()
            )
            self.assertFalse(
                workbench_archive.remove_archive_entry(metadata.archive_id, state_root)
            )

    def test_compute_expires_at_adds_retention_days(self) -> None:
        expires = workbench_archive.compute_expires_at("2026-07-03T12:00:00+00:00", 30)
        self.assertEqual("2026-08-02T12:00:00+00:00", expires)

    def test_compute_expires_at_past_the_calendar_never_expires(self) -> None:
        expires = workbench_archive.compute_expires_at("9999-12-20T12:00:00+00:00", 30)
        self.assertEqual("9999-12-31T23:59:59.999999+00:00", expires)

    def test_archived_entries_excluded_from_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            archive_id = "20260703T120000Z-hiddenplan12"
            archived_plan = workbench_archive.archive_plan_path(archive_id, state_root)
            _write_plan_dir(archived_plan)
            metadata = self._metadata(tmp_path, archive_id)
            workbench_archive.save_archive_metadata(metadata, state_root)

            with patched_env(AGENT_PLANS_DIR=None):
                result = workbench_discovery.discover_plans(
                    state_root=state_root,
                    include_default_dirs=False,
                )

            self.assertEqual((), result.plans)


class WorkbenchArchivePlanTests(unittest.TestCase):
    def test_archive_moves_plan_state_and_captures_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical = _seed_archivable_plan(tmp_path)
            plan_state = state.plan_state_dir(canonical, state_dir)

            result = workbench_archive.archive_plan(
                canonical,
                state_dir=state_dir,
                registry_path=registry_path,
                include_default_dirs=False,
            )

            self.assertTrue(result.plan_archived)
            self.assertTrue(result.state_archived)
            self.assertTrue(result.pin_archived)
            self.assertFalse(canonical.exists())
            self.assertFalse(plan_state.exists())

            entry = workbench_archive.archive_entry_dir(result.archive_id, state_dir)
            self.assertTrue(
                workbench_archive.archive_plan_path(
                    result.archive_id, state_dir
                ).is_dir()
            )
            self.assertTrue(
                workbench_archive.archive_state_path(
                    result.archive_id, state_dir
                ).is_dir()
            )
            self.assertTrue(
                (entry / workbench_archive.ARCHIVE_METADATA_FILE_NAME).is_file()
            )
            self.assertIsNotNone(result.metadata.pin)
            assert result.metadata.pin is not None
            self.assertEqual("Alpha", result.metadata.pin.label)
            self.assertEqual((), workbench_registry.load_registry(registry_path).pins)

    def test_archive_sets_expiry_from_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical = _seed_archivable_plan(
                tmp_path, with_pin=False
            )
            registry = workbench_registry.set_archive_retention_days(
                workbench_registry.load_registry(registry_path), 7
            )
            workbench_registry.save_registry(registry, registry_path)

            result = workbench_archive.archive_plan(
                canonical,
                state_dir=state_dir,
                registry_path=registry_path,
                include_default_dirs=False,
            )

            self.assertEqual(
                workbench_archive.compute_expires_at(result.metadata.archived_at, 7),
                result.metadata.expires_at,
            )

    def test_archive_rejects_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical = _seed_archivable_plan(tmp_path)
            plan_state = state.plan_state_dir(canonical, state_dir)
            (plan_state / state.LOCK_FILE_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "plan_dir": str(canonical),
                        "created_at": "2026-07-03T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.archive_plan(
                    canonical,
                    state_dir=state_dir,
                    registry_path=registry_path,
                    include_default_dirs=False,
                )

            self.assertEqual("active-lock", ctx.exception.reason)
            self.assertEqual(409, ctx.exception.status)
            self.assertTrue(canonical.exists())
            self.assertEqual(
                1, len(workbench_registry.load_registry(registry_path).pins)
            )

    def test_archive_rejects_non_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            target = tmp_path / "not-a-plan"
            target.mkdir()

            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.archive_plan(
                    target,
                    state_dir=state_dir,
                    registry_path=registry_path,
                    include_default_dirs=False,
                )

            self.assertEqual("invalid-plan", ctx.exception.reason)
            self.assertTrue(target.exists())

    def test_archive_missing_plan_dir_archives_state_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical = _seed_archivable_plan(
                tmp_path, create_plan=False
            )
            plan_state = state.plan_state_dir(canonical, state_dir)

            result = workbench_archive.archive_plan(
                canonical,
                state_dir=state_dir,
                registry_path=registry_path,
                include_default_dirs=False,
            )

            self.assertFalse(result.plan_archived)
            self.assertTrue(result.state_archived)
            self.assertTrue(result.pin_archived)
            self.assertFalse(plan_state.exists())
            self.assertFalse(
                workbench_archive.archive_plan_path(
                    result.archive_id, state_dir
                ).exists()
            )
            self.assertTrue(
                workbench_archive.archive_state_path(
                    result.archive_id, state_dir
                ).is_dir()
            )
            self.assertEqual((), workbench_registry.load_registry(registry_path).pins)
