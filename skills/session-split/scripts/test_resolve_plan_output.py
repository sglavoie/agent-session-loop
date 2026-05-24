#!/usr/bin/env python3
"""Tests for resolve-plan-output.py."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "resolve-plan-output.py")
SPEC = importlib.util.spec_from_file_location("resolve_plan_output", SCRIPT_PATH)
resolver = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(resolver)


class ResolvePlanOutputTest(unittest.TestCase):
    def write_plan(self, directory: str, name: str = "PLAN.md", content: str = "# Plan\n") -> str:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as plan_file:
            plan_file.write(content)
        return path

    def test_same_basename_in_different_temp_dirs_produces_different_hash_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left_dir = os.path.join(tmp, "left")
            right_dir = os.path.join(tmp, "right")
            output_base = os.path.join(tmp, "out")
            os.mkdir(left_dir)
            os.mkdir(right_dir)
            left_plan = self.write_plan(left_dir, content="# Shared Name\n")
            right_plan = self.write_plan(right_dir, content="# Shared Name\n")

            left = resolver.resolve_plan_output(left_plan, output_base)
            right = resolver.resolve_plan_output(right_plan, output_base)

            self.assertNotEqual(left["hash_prefix"], right["hash_prefix"])
            self.assertNotEqual(left["source_fingerprint"], right["source_fingerprint"])

    def test_same_path_with_changed_content_produces_different_hash_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_base = os.path.join(tmp, "out")
            plan = self.write_plan(tmp, content="# First\n")
            first = resolver.resolve_plan_output(plan, output_base)
            with open(plan, "w", encoding="utf-8") as plan_file:
                plan_file.write("# Second\n")

            second = resolver.resolve_plan_output(plan, output_base)

            self.assertNotEqual(first["content_sha256"], second["content_sha256"])
            self.assertNotEqual(first["hash_prefix"], second["hash_prefix"])

    def test_existing_output_directory_causes_incrementing_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_base = os.path.join(tmp, "out")
            plan = self.write_plan(tmp, "My Plan.md", "# Plan\n")

            first = resolver.resolve_plan_output(plan, output_base)
            second = resolver.resolve_plan_output(plan, output_base)
            third = resolver.resolve_plan_output(plan, output_base)

            self.assertEqual(1, first["collision_index"])
            self.assertEqual(2, second["collision_index"])
            self.assertEqual(3, third["collision_index"])
            self.assertTrue(first["output_dir"].endswith(f"{first['slug']}-{first['hash_prefix']}"))
            self.assertTrue(second["output_dir"].endswith(f"{first['slug']}-{first['hash_prefix']}-2"))
            self.assertTrue(third["output_dir"].endswith(f"{first['slug']}-{first['hash_prefix']}-3"))

    def test_unsafe_filenames_produce_clean_slugs(self) -> None:
        cases = {
            "My PLAN!! v2.md": "my-plan-v2",
            "___###.md": "plan",
            "Spacing_AND.symbols.md": "spacing-and-symbols",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(expected, resolver.sanitize_slug(filename))

    def test_base_dir_overrides_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_base = os.path.join(tmp, "env-plans")
            override_base = os.path.join(tmp, "override-plans")
            plan = self.write_plan(tmp)
            original = os.environ.get("AGENT_PLANS_DIR")
            os.environ["AGENT_PLANS_DIR"] = env_base
            try:
                result = resolver.resolve_plan_output(plan, override_base)
            finally:
                if original is None:
                    os.environ.pop("AGENT_PLANS_DIR", None)
                else:
                    os.environ["AGENT_PLANS_DIR"] = original

            self.assertEqual(os.path.realpath(override_base), result["base_dir"])
            self.assertTrue(result["output_dir"].startswith(os.path.realpath(override_base)))
            self.assertFalse(os.path.exists(env_base))


if __name__ == "__main__":
    unittest.main()
