"""Workbench registry, archive, and discovery tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import (
    config,
    plans_files,
    state,
    workbench_archive,
    workbench_discovery,
    workbench_registry,
    workbench_registry_models,
)
from tests.support import (
    _seed_archivable_plan,
    _write_plan_dir,
    patched_env,
)


class WorkbenchRegistryTests(unittest.TestCase):
    def test_default_registry_path_uses_xdg_config_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            xdg = Path(tmp) / "config"
            with patched_env(XDG_CONFIG_HOME=str(xdg)):
                path = workbench_registry.default_registry_path()
            self.assertEqual(
                path,
                xdg.resolve()
                / config.APP_NAME
                / workbench_registry_models.REGISTRY_FILE_NAME,
            )

    def test_load_returns_empty_registry_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / workbench_registry_models.REGISTRY_FILE_NAME

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(
                workbench_registry_models.REGISTRY_SCHEMA_VERSION,
                registry.schema_version,
            )
            self.assertEqual((), registry.pins)
            self.assertEqual((), registry.plan_roots)
            self.assertEqual((), registry.warnings)
            self.assertEqual(registry_path.resolve(), registry.path)

    def test_load_parses_pins_roots_and_profile_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            plan_root = tmp_path / "plans"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            payload = {
                "schema_version": workbench_registry_models.REGISTRY_SCHEMA_VERSION,
                "pins": [
                    {
                        "plan_dir": str(plan_dir),
                        "label": "Alpha plan",
                        "profile": {
                            "cwd": str(tmp_path),
                            "harness": "claude",
                            "harness_model": "claude-haiku",
                            "effort": "high",
                            "timeout": 1200,
                            "commit_mode": "per-session",
                            "review_harness": "codex",
                            "review_harness_model": "o4-mini",
                            "max_review_fix_cycles": 3,
                            "unsafe_bypass": True,
                            "allow_not_loop_ready": False,
                            "note": "ship soon",
                        },
                    },
                    {"plan_dir": str(plan_dir / "missing-but-canonical")},
                ],
                "plan_roots": [str(plan_root)],
            }
            registry_path.write_text(json.dumps(payload), encoding="utf-8")

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual((), registry.warnings)
            self.assertEqual(2, len(registry.pins))
            first, second = registry.pins
            self.assertEqual(str(plan_dir), first.plan_dir)
            self.assertEqual("Alpha plan", first.label)
            self.assertEqual("claude", first.profile.harness)
            self.assertEqual("claude-haiku", first.profile.harness_model)
            self.assertEqual("codex", first.profile.review_harness)
            self.assertEqual("o4-mini", first.profile.review_harness_model)
            self.assertEqual(3, first.profile.max_review_fix_cycles)
            self.assertTrue(first.profile.unsafe_bypass)
            self.assertFalse(first.profile.allow_not_loop_ready)
            self.assertEqual("per-session", first.profile.commit_mode)
            self.assertEqual(1200, first.profile.timeout)
            self.assertIsNone(second.label)
            self.assertEqual(workbench_registry_models.LaunchProfile(), second.profile)
            self.assertEqual((str(plan_root),), registry.plan_roots)

    def test_load_v1_profile_ignores_legacy_model_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pins": [
                            {
                                "plan_dir": str(plan_dir),
                                "profile": {
                                    "harness": "codex",
                                    "model": "o3-mini",
                                },
                            }
                        ],
                        "plan_roots": [],
                    }
                ),
                encoding="utf-8",
            )

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(1, registry.schema_version)
            self.assertIsNone(registry.pins[0].profile.harness_model)
            self.assertEqual(
                (
                    "pin[0]: ignoring legacy profile.model from registry schema v1; "
                    "use harness_model and review_harness_model",
                ),
                registry.warnings,
            )

            workbench_registry.save_registry(registry, registry_path)

            saved = json.loads(registry_path.read_text(encoding="utf-8"))
            self.assertEqual(2, saved["schema_version"])
            saved_profile = saved["pins"][0]["profile"]
            self.assertNotIn("model", saved_profile)
            self.assertIn("harness_model", saved_profile)
            self.assertIn("review_harness_model", saved_profile)

    def test_load_profile_without_schema_warns_for_legacy_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps(
                    {
                        "pins": [
                            {
                                "plan_dir": str(plan_dir),
                                "profile": {
                                    "harness": "codex",
                                    "model": "o3-mini",
                                },
                            }
                        ],
                        "plan_roots": [],
                    }
                ),
                encoding="utf-8",
            )

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(
                workbench_registry_models.REGISTRY_SCHEMA_VERSION,
                registry.schema_version,
            )
            self.assertIsNone(registry.pins[0].profile.harness_model)
            self.assertEqual(
                (
                    "schema_version: missing; assuming current version",
                    "pin[0]: ignoring legacy profile.model from registry schema v2; "
                    "use harness_model and review_harness_model",
                ),
                registry.warnings,
            )

    def test_load_warns_on_malformed_entries_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            payload = {
                "schema_version": "not-a-number",
                "pins": [
                    "not-an-object",
                    {"label": "missing plan_dir"},
                    {
                        "plan_dir": str(plan_dir),
                        "profile": {
                            "harness": "bogus",
                            "commit_mode": "bogus",
                            "review_harness": "bogus",
                        },
                    },
                    {"plan_dir": str(plan_dir)},
                ],
                "plan_roots": "not-a-list",
            }
            registry_path.write_text(json.dumps(payload), encoding="utf-8")

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(1, len(registry.pins))
            self.assertEqual((), registry.plan_roots)
            self.assertEqual(
                workbench_registry_models.REGISTRY_SCHEMA_VERSION,
                registry.schema_version,
            )
            joined_warnings = "\n".join(registry.warnings)
            self.assertIn("schema_version", joined_warnings)
            self.assertIn("entry must be an object", joined_warnings)
            self.assertIn("missing or invalid plan_dir", joined_warnings)
            self.assertIn("unsupported harness 'bogus'", joined_warnings)
            self.assertIn("unsupported commit_mode 'bogus'", joined_warnings)
            self.assertIn("duplicate plan_dir", joined_warnings)
            self.assertIn("plan_roots: must be a list", joined_warnings)

    def test_load_raises_on_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text("not json", encoding="utf-8")

            with self.assertRaises(workbench_registry_models.WorkbenchError):
                workbench_registry.load_registry(registry_path)

    def test_load_warns_when_schema_version_is_newer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": (
                            workbench_registry_models.REGISTRY_SCHEMA_VERSION + 5
                        ),
                        "pins": [],
                        "plan_roots": [],
                    }
                ),
                encoding="utf-8",
            )

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(
                workbench_registry_models.REGISTRY_SCHEMA_VERSION + 5,
                registry.schema_version,
            )
            joined_warnings = "\n".join(registry.warnings)
            self.assertIn("newer than supported", joined_warnings)

    def test_save_round_trip_through_upsert_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            other_dir = _write_plan_dir(tmp_path / "plans" / "beta")
            registry_path = (
                tmp_path / "config" / workbench_registry_models.REGISTRY_FILE_NAME
            )

            registry = workbench_registry.load_registry(registry_path)
            self.assertEqual((), registry.pins)

            registry = workbench_registry.upsert_pin(
                registry,
                plan_dir,
                label="Alpha",
                profile=workbench_registry_models.LaunchProfile(
                    harness="codex", timeout=600
                ),
            )
            registry = workbench_registry.upsert_pin(registry, other_dir)
            workbench_registry.save_registry(registry, registry_path)
            self.assertTrue(registry_path.is_file())

            reloaded = workbench_registry.load_registry(registry_path)
            self.assertEqual(2, len(reloaded.pins))
            alpha_pin = next(
                pin for pin in reloaded.pins if pin.plan_dir == str(plan_dir)
            )
            self.assertEqual("Alpha", alpha_pin.label)
            self.assertEqual("codex", alpha_pin.profile.harness)
            self.assertEqual(600, alpha_pin.profile.timeout)

            updated = workbench_registry.upsert_pin(
                reloaded, plan_dir, label="Renamed alpha"
            )
            self.assertEqual(2, len(updated.pins))
            renamed_pin = next(
                pin for pin in updated.pins if pin.plan_dir == str(plan_dir)
            )
            self.assertEqual("Renamed alpha", renamed_pin.label)
            self.assertEqual(
                workbench_registry_models.LaunchProfile(), renamed_pin.profile
            )

            after_remove = workbench_registry.remove_pin(updated, plan_dir)
            self.assertEqual(1, len(after_remove.pins))
            self.assertEqual(str(other_dir), after_remove.pins[0].plan_dir)


class WorkbenchArchiveRetentionTests(unittest.TestCase):
    def test_missing_retention_defaults_without_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps({"schema_version": 1, "pins": [], "plan_roots": []}),
                encoding="utf-8",
            )

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(
                workbench_registry_models.DEFAULT_ARCHIVE_RETENTION_DAYS,
                registry.archive_retention_days,
            )
            self.assertEqual((), registry.warnings)

    def test_invalid_retention_coerces_to_default_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / workbench_registry_models.REGISTRY_FILE_NAME
            registry_path.write_text(
                json.dumps({"archive_retention_days": 0}), encoding="utf-8"
            )

            registry = workbench_registry.load_registry(registry_path)

            self.assertEqual(
                workbench_registry_models.DEFAULT_ARCHIVE_RETENTION_DAYS,
                registry.archive_retention_days,
            )
            self.assertTrue(
                any("archive_retention_days" in w for w in registry.warnings)
            )

    def test_retention_round_trips_through_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / workbench_registry_models.REGISTRY_FILE_NAME
            registry = workbench_registry.load_registry(registry_path)
            registry = workbench_registry.set_archive_retention_days(registry, 7)

            workbench_registry.save_registry(registry, registry_path)
            reloaded = workbench_registry.load_registry(registry_path)

            self.assertEqual(7, reloaded.archive_retention_days)
            self.assertEqual((), reloaded.warnings)

    def test_set_retention_rejects_non_positive(self) -> None:
        registry = workbench_registry_models.WorkbenchRegistry()
        with self.assertRaises(workbench_registry_models.WorkbenchError):
            workbench_registry.set_archive_retention_days(registry, 0)
        with self.assertRaises(workbench_registry_models.WorkbenchError):
            workbench_registry.set_archive_retention_days(registry, True)


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


class WorkbenchRestoreArchiveTests(unittest.TestCase):
    def _archive(self, tmp_path: Path) -> tuple[Path, Path, Path, str]:
        state_dir, registry_path, canonical = _seed_archivable_plan(tmp_path)
        result = workbench_archive.archive_plan(
            canonical,
            state_dir=state_dir,
            registry_path=registry_path,
            include_default_dirs=False,
        )
        return state_dir, registry_path, canonical, result.archive_id

    def test_restore_returns_plan_state_and_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical, archive_id = self._archive(tmp_path)
            plan_state = state.plan_state_dir(canonical, state_dir)

            result = workbench_archive.restore_archive(
                archive_id, state_dir=state_dir, registry_path=registry_path
            )

            self.assertTrue(result.plan_restored)
            self.assertTrue(result.state_restored)
            self.assertTrue(result.pin_restored)
            self.assertTrue(canonical.is_dir())
            self.assertTrue((canonical / "manifest.md").is_file())
            self.assertTrue(plan_state.is_dir())
            self.assertFalse(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )
            pins = workbench_registry.load_registry(registry_path).pins
            self.assertEqual(1, len(pins))
            self.assertEqual("Alpha", pins[0].label)
            self.assertEqual("codex", pins[0].profile.harness)

    def test_restore_missing_entry_raises_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.restore_archive(
                    "20260703T120000Z-doesnotexist", state_dir=state_dir
                )
            self.assertEqual("not-found", ctx.exception.reason)
            self.assertEqual(404, ctx.exception.status)

    def test_restore_conflict_on_existing_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical, archive_id = self._archive(tmp_path)
            _write_plan_dir(canonical)

            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.restore_archive(
                    archive_id, state_dir=state_dir, registry_path=registry_path
                )

            self.assertEqual("restore-conflict", ctx.exception.reason)
            self.assertEqual(409, ctx.exception.status)
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )

    def test_restore_conflict_on_existing_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir, registry_path, canonical, archive_id = self._archive(tmp_path)
            plan_state = state.plan_state_dir(canonical, state_dir)
            plan_state.mkdir(parents=True, exist_ok=True)

            with self.assertRaises(workbench_archive.ArchiveError) as ctx:
                workbench_archive.restore_archive(
                    archive_id, state_dir=state_dir, registry_path=registry_path
                )

            self.assertEqual("restore-conflict", ctx.exception.reason)
            self.assertFalse(canonical.exists())
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )


class WorkbenchPurgeArchiveTests(unittest.TestCase):
    def _metadata(
        self, tmp_path: Path, archive_id: str, archived_at: str
    ) -> workbench_archive.ArchiveMetadata:
        return workbench_archive.ArchiveMetadata(
            archive_id=archive_id,
            original_plan_dir=str(tmp_path / "plans" / archive_id),
            archived_at=archived_at,
            expires_at=workbench_archive.compute_expires_at(archived_at, 30),
            summary=None,
            pin=None,
            plan_archived=False,
            state_archived=False,
        )

    def test_purge_single_and_missing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            archive_id = "20260703T120000Z-purgeme12345"
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, archive_id, "2026-07-03T12:00:00+00:00"),
                state_dir,
            )

            result = workbench_archive.purge_archive_entries(
                [archive_id], state_dir=state_dir
            )
            self.assertEqual(1, result.removed_count)
            self.assertEqual(0, result.failed_count)
            self.assertTrue(result.outcomes[0].removed)
            self.assertFalse(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )

            again = workbench_archive.purge_archive_entries(
                [archive_id], state_dir=state_dir
            )
            self.assertFalse(again.outcomes[0].removed)
            self.assertIsNone(again.outcomes[0].error)

    def test_purge_bulk_reports_per_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            good_a = "20260703T120000Z-aaaaaaaaaaaa"
            good_b = "20260703T130000Z-bbbbbbbbbbbb"
            for archive_id in (good_a, good_b):
                workbench_archive.save_archive_metadata(
                    self._metadata(tmp_path, archive_id, "2026-07-03T12:00:00+00:00"),
                    state_dir,
                )

            result = workbench_archive.purge_archive_entries(
                [good_a, "../escape", good_b], state_dir=state_dir
            )

            by_id = {o.archive_id: o for o in result.outcomes}
            self.assertTrue(by_id[good_a].removed)
            self.assertTrue(by_id[good_b].removed)
            self.assertIsNotNone(by_id["../escape"].error)
            self.assertEqual(2, result.removed_count)
            self.assertEqual(1, result.failed_count)

    def test_retention_marks_expired_entries_without_removing_them(self) -> None:
        # The scenario the deleted `maintain_archive` pass used to be given,
        # held to the opposite outcome. One entry is well past the window and
        # one is inside it; the old entry comes back flagged, and both are
        # still on disk when the review is over.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            old_id = "20260524T120000Z-oldoldold123"
            new_id = "20260701T120000Z-newnewnew123"
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, old_id, "2026-05-24T12:00:00+00:00"),
                state_dir,
            )
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, new_id, "2026-07-01T12:00:00+00:00"),
                state_dir,
            )
            now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

            view = workbench_archive.review_archive(
                state_dir=state_dir, registry_path=registry_path, now=now
            )

            self.assertEqual([old_id], list(view.review.candidate_ids))
            self.assertEqual(1, view.review.candidate_count)
            self.assertTrue(
                workbench_archive.archive_entry_dir(old_id, state_dir).exists()
            )
            self.assertTrue(
                workbench_archive.archive_entry_dir(new_id, state_dir).exists()
            )
            # There is no removal count to report, and that absence is the
            # change: a caller cannot read "0 purged" as "the pass ran".
            self.assertNotIn("removed_count", view.review.to_payload())

    def test_shortening_retention_marks_more_candidates_only(self) -> None:
        # Shortening the window used to be a deletion trigger: the next read
        # emptied everything the new window had just expired. It is now a
        # reclassification, so the operator can widen it back.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            archive_id = "20260701T120000Z-shortretain1"
            workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, archive_id, "2026-07-01T12:00:00+00:00"),
                state_dir,
            )
            now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)

            before = workbench_archive.review_archive(
                state_dir=state_dir, registry_path=registry_path, now=now
            )
            self.assertEqual([], list(before.review.candidate_ids))

            workbench_archive.set_retention(1, registry_path=registry_path)
            after = workbench_archive.review_archive(
                state_dir=state_dir, registry_path=registry_path, now=now
            )

            self.assertEqual([archive_id], list(after.review.candidate_ids))
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )

    def test_an_unreadable_archive_date_is_reported_not_swept(self) -> None:
        # The old pass skipped a malformed `archived_at` with a warning, which
        # left the entry in a state nothing ever acted on. It is now its own
        # cleanup state, so an operator can see there is something to decide.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_dir = tmp_path / "state"
            registry_path = tmp_path / workbench_registry_models.REGISTRY_FILE_NAME
            archive_id = "20260701T120000Z-baddate12345"
            meta_path = workbench_archive.save_archive_metadata(
                self._metadata(tmp_path, archive_id, "2026-07-01T12:00:00+00:00"),
                state_dir,
            )
            # Corrupt the stored timestamp after the fact: the writer refuses
            # to produce one, which is why an entry only reaches this state by
            # something outside the writer damaging it.
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            payload["archived_at"] = "not-a-timestamp"
            meta_path.write_text(json.dumps(payload), encoding="utf-8")

            view = workbench_archive.review_archive(
                state_dir=state_dir,
                registry_path=registry_path,
                now=datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC),
            )

            self.assertEqual(1, view.review.undated_count)
            self.assertEqual([], list(view.review.candidate_ids))
            self.assertTrue(
                workbench_archive.archive_entry_dir(archive_id, state_dir).exists()
            )


class WorkbenchDiscoveryTests(unittest.TestCase):
    def test_discovers_plans_from_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            plan_state_dir = state_root / plans_files.path_sha256(plan_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / "state.json").write_text(
                json.dumps({"plan_dir": str(plan_dir)}, sort_keys=True),
                encoding="utf-8",
            )

            with patched_env(AGENT_PLANS_DIR=None):
                result = workbench_discovery.discover_plans(
                    state_root=state_root,
                    include_default_dirs=False,
                )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(plan_dir, discovered.plan_dir)
            self.assertEqual(("state",), discovered.sources)
            self.assertTrue(discovered.exists)
            self.assertIsNone(discovered.label)
            self.assertIsNone(discovered.profile)
            self.assertEqual((), result.warnings)

    def test_discovers_plans_from_registry_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            missing_dir = tmp_path / "plans" / "ghost"
            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(plan_dir),
                        label="Alpha",
                        profile=workbench_registry_models.LaunchProfile(
                            harness="claude"
                        ),
                    ),
                    workbench_registry_models.RegistryPin(plan_dir=str(missing_dir)),
                ),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual(2, len(result.plans))
            by_path = {plan.plan_dir: plan for plan in result.plans}
            alpha = by_path[plan_dir]
            self.assertEqual(("registry",), alpha.sources)
            self.assertEqual("Alpha", alpha.label)
            self.assertEqual("claude", alpha.profile.harness)
            self.assertTrue(alpha.exists)
            ghost = by_path[missing_dir]
            self.assertFalse(ghost.exists)
            self.assertEqual(("registry",), ghost.sources)

    def test_discovers_plans_from_explicit_plan_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "plans"
            plan_root.mkdir()
            real_plan = _write_plan_dir(plan_root / "alpha")
            (plan_root / "bare-file").write_text("ignored", encoding="utf-8")
            (plan_root / "incomplete").mkdir()
            (plan_root / "incomplete" / "manifest.md").write_text(
                "no context", encoding="utf-8"
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                plan_roots=[plan_root],
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            self.assertEqual(real_plan, result.plans[0].plan_dir)
            self.assertEqual(("plan-root",), result.plans[0].sources)

    def test_discovers_hash_only_plan_directory_names(self) -> None:
        # Regression: hash-only split-plan output directories (named
        # <content-hash-prefix>-<source-fingerprint-prefix>) must be discovered
        # exactly like slug-prefixed ones. Discovery keys on manifest.md +
        # context.md, not on the directory basename format.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "plans"
            plan_root.mkdir()
            hash_only_plan = _write_plan_dir(plan_root / "9e3ab88dedcf-7d2caa1117db")
            agent_dir = tmp_path / "agent-plans"
            agent_dir.mkdir()
            default_hash_only = _write_plan_dir(agent_dir / "9e3ab88dedcf-7d2caa1117db")

            with patched_env(
                AGENT_PLANS_DIR=str(agent_dir),
                HOME=str(tmp_path / "home-overridden"),
            ):
                result = workbench_discovery.discover_plans(
                    state_root=tmp_path / "state-empty",
                    plan_roots=[plan_root],
                    include_default_dirs=True,
                )

            paths_to_sources = {plan.plan_dir: plan.sources for plan in result.plans}
            self.assertIn(hash_only_plan, paths_to_sources)
            self.assertEqual(("plan-root",), paths_to_sources[hash_only_plan])
            self.assertIn(default_hash_only, paths_to_sources)
            self.assertEqual(("agent-plans-dir",), paths_to_sources[default_hash_only])

    def test_discovers_plans_from_environment_default_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            agent_dir = tmp_path / "agent-plans"
            agent_dir.mkdir()
            plan_a = _write_plan_dir(agent_dir / "alpha")
            tmp_default = tmp_path / "tmp-root"
            tmp_default.mkdir()
            (tmp_default / "agent-plans").mkdir()
            plan_b = _write_plan_dir(tmp_default / "agent-plans" / "beta")
            share_default = tmp_path / "share-root"
            share_default.mkdir()
            (share_default / "agent-plans").mkdir()
            plan_c = _write_plan_dir(share_default / "agent-plans" / "gamma")

            with patched_env(
                AGENT_PLANS_DIR=str(agent_dir),
                TMPDIR=str(tmp_default),
                XDG_DATA_HOME=str(share_default),
                HOME=str(tmp_path / "home-overridden"),
            ):
                result = workbench_discovery.discover_plans(
                    state_root=tmp_path / "state-empty",
                    include_default_dirs=True,
                )

            paths_to_sources = {plan.plan_dir: plan.sources for plan in result.plans}
            self.assertIn(plan_a, paths_to_sources)
            self.assertIn(plan_b, paths_to_sources)
            self.assertIn(plan_c, paths_to_sources)
            self.assertEqual(("agent-plans-dir",), paths_to_sources[plan_a])
            self.assertEqual(("tmp-default",), paths_to_sources[plan_b])
            self.assertEqual(("share-default",), paths_to_sources[plan_c])

    def test_dedupes_across_sources_and_records_each_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            state_root = tmp_path / "state"
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            plan_state_dir = state_root / plans_files.path_sha256(plan_dir)
            plan_state_dir.mkdir(parents=True)
            (plan_state_dir / "state.json").write_text(
                json.dumps({"plan_dir": str(plan_dir)}, sort_keys=True),
                encoding="utf-8",
            )
            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(plan_dir),
                        label="Alpha",
                        profile=workbench_registry_models.LaunchProfile(
                            harness="codex"
                        ),
                    ),
                ),
            )

            result = workbench_discovery.discover_plans(
                state_root=state_root,
                registry=registry,
                plan_roots=[tmp_path / "plans"],
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(plan_dir, discovered.plan_dir)
            self.assertEqual(("state", "registry", "plan-root"), discovered.sources)
            self.assertEqual("Alpha", discovered.label)
            self.assertEqual("codex", discovered.profile.harness)

    def test_discovers_plans_from_registry_plan_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "registry-roots"
            plan_root.mkdir()
            real_plan = _write_plan_dir(plan_root / "alpha")
            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(plan_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(real_plan, discovered.plan_dir)
            self.assertEqual(("plan-root",), discovered.sources)
            self.assertTrue(discovered.exists)

    def test_dedupes_registry_and_cli_plan_roots_into_single_plan_root_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            registry_root = tmp_path / "registry-roots"
            registry_root.mkdir()
            cli_root = tmp_path / "cli-roots"
            cli_root.mkdir()
            shared_plan = _write_plan_dir(registry_root / "alpha")
            cli_only_plan = _write_plan_dir(cli_root / "beta")
            # Symlink the same plan into both roots so a single plan is
            # surfaced by both registry roots and CLI roots.
            (cli_root / "alpha").symlink_to(shared_plan)

            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(registry_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                plan_roots=[cli_root],
                include_default_dirs=False,
            )

            by_path = {plan.plan_dir: plan for plan in result.plans}
            self.assertIn(shared_plan, by_path)
            self.assertIn(cli_only_plan, by_path)
            self.assertEqual(("plan-root",), by_path[shared_plan].sources)
            self.assertEqual(("plan-root",), by_path[cli_only_plan].sources)
            # Order: registry roots scanned first, so shared_plan precedes
            # the CLI-only plan in the discovery list.
            ordered = [plan.plan_dir for plan in result.plans]
            self.assertEqual([shared_plan, cli_only_plan], ordered)

    def test_registry_pin_and_registry_root_merge_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_root = tmp_path / "registry-roots"
            plan_root.mkdir()
            plan_dir = _write_plan_dir(plan_root / "alpha")
            registry = workbench_registry_models.WorkbenchRegistry(
                pins=(
                    workbench_registry_models.RegistryPin(
                        plan_dir=str(plan_dir),
                        label="Alpha",
                        profile=workbench_registry_models.LaunchProfile(
                            harness="codex"
                        ),
                    ),
                ),
                plan_roots=(str(plan_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual(1, len(result.plans))
            discovered = result.plans[0]
            self.assertEqual(plan_dir, discovered.plan_dir)
            self.assertEqual(("registry", "plan-root"), discovered.sources)
            self.assertEqual("Alpha", discovered.label)
            self.assertEqual("codex", discovered.profile.harness)

    def test_missing_directories_and_invalid_inputs_become_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            not_a_dir = tmp_path / "not-a-dir"
            not_a_dir.write_text("file not directory", encoding="utf-8")

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                plan_roots=[tmp_path / "missing", not_a_dir],
                include_default_dirs=False,
            )

            self.assertEqual((), result.plans)
            joined = "\n".join(result.warnings)
            self.assertIn("is not a directory", joined)

    def test_registry_plan_root_missing_emits_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            missing_root = tmp_path / "missing-registry-root"
            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(missing_root),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual((), result.plans)
            self.assertEqual((), result.warnings)

    def test_registry_plan_root_warns_when_not_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            not_a_dir = tmp_path / "registry-root-file"
            not_a_dir.write_text("file not directory", encoding="utf-8")
            registry = workbench_registry_models.WorkbenchRegistry(
                plan_roots=(str(not_a_dir),),
            )

            result = workbench_discovery.discover_plans(
                state_root=tmp_path / "state-empty",
                registry=registry,
                include_default_dirs=False,
            )

            self.assertEqual((), result.plans)
            joined = "\n".join(result.warnings)
            self.assertIn("registry_plan_root[0]", joined)
            self.assertIn("is not a directory", joined)


if __name__ == "__main__":
    unittest.main()
