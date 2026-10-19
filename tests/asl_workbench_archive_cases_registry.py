from __future__ import annotations

# ruff: noqa: F403, F405
from .asl_workbench_archive_support import *


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
