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

    def test_same_content_different_paths_shares_content_prefix_but_differs_in_plan_id(self) -> None:
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

            # Same content => identical content hash and content_hash_prefix.
            self.assertEqual(left["content_sha256"], right["content_sha256"])
            self.assertEqual(left["content_hash_prefix"], right["content_hash_prefix"])
            # Different source paths => distinct fingerprint prefix and plan_id.
            self.assertNotEqual(left["fingerprint_prefix"], right["fingerprint_prefix"])
            self.assertNotEqual(left["hash_prefix"], right["hash_prefix"])
            self.assertNotEqual(left["source_fingerprint"], right["source_fingerprint"])
            self.assertNotEqual(left["plan_id"], right["plan_id"])

    def test_changed_content_same_path_changes_content_prefix_and_plan_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_base = os.path.join(tmp, "out")
            plan = self.write_plan(tmp, content="# First\n")
            first = resolver.resolve_plan_output(plan, output_base)
            with open(plan, "w", encoding="utf-8") as plan_file:
                plan_file.write("# Second\n")

            second = resolver.resolve_plan_output(plan, output_base)

            self.assertNotEqual(first["content_sha256"], second["content_sha256"])
            self.assertNotEqual(first["content_hash_prefix"], second["content_hash_prefix"])
            self.assertNotEqual(first["hash_prefix"], second["hash_prefix"])
            self.assertNotEqual(first["plan_id"], second["plan_id"])

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
            plan_id = first["plan_id"]
            self.assertEqual(
                f"{first['content_hash_prefix']}-{first['fingerprint_prefix']}", plan_id
            )
            self.assertEqual(plan_id, os.path.basename(first["output_dir"]))
            self.assertEqual(f"{plan_id}-2", os.path.basename(second["output_dir"]))
            self.assertEqual(f"{plan_id}-3", os.path.basename(third["output_dir"]))

    def test_unsafe_filenames_produce_clean_slugs(self) -> None:
        cases = {
            "My PLAN!! v2.md": "my-plan-v2",
            "___###.md": "plan",
            "Spacing_AND.symbols.md": "spacing-and-symbols",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(expected, resolver.sanitize_slug(filename))

    def _swap_env(self, name: str, value: str | None) -> str | None:
        """Set or unset an env var, returning its prior value for restoration."""
        original = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        return original

    def _restore_env(self, name: str, original: str | None) -> None:
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original

    def test_default_uses_xdg_data_home_when_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orig_agent = self._swap_env("AGENT_PLANS_DIR", None)
            orig_xdg = self._swap_env("XDG_DATA_HOME", tmp)
            try:
                self.assertEqual(
                    os.path.realpath(os.path.join(tmp, "agent-plans")),
                    resolver.default_base_dir(),
                )
            finally:
                self._restore_env("AGENT_PLANS_DIR", orig_agent)
                self._restore_env("XDG_DATA_HOME", orig_xdg)

    def test_default_falls_back_to_local_share_when_xdg_unset(self) -> None:
        orig_agent = self._swap_env("AGENT_PLANS_DIR", None)
        orig_xdg = self._swap_env("XDG_DATA_HOME", None)
        try:
            result = resolver.default_base_dir()
            self.assertTrue(
                result.endswith(os.path.join(".local", "share", "agent-plans")),
                f"expected default to end with .local/share/agent-plans, got {result}",
            )
        finally:
            self._restore_env("AGENT_PLANS_DIR", orig_agent)
            self._restore_env("XDG_DATA_HOME", orig_xdg)

    def test_agent_plans_dir_wins_over_xdg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = os.path.join(tmp, "agent")
            xdg_dir = os.path.join(tmp, "xdg")
            orig_agent = self._swap_env("AGENT_PLANS_DIR", agent_dir)
            orig_xdg = self._swap_env("XDG_DATA_HOME", xdg_dir)
            try:
                self.assertEqual(os.path.realpath(agent_dir), resolver.default_base_dir())
            finally:
                self._restore_env("AGENT_PLANS_DIR", orig_agent)
                self._restore_env("XDG_DATA_HOME", orig_xdg)

    def test_default_does_not_use_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            orig_agent = self._swap_env("AGENT_PLANS_DIR", None)
            orig_xdg = self._swap_env("XDG_DATA_HOME", None)
            orig_tmp = self._swap_env("TMPDIR", tmp)
            try:
                result = resolver.default_base_dir()
                self.assertFalse(
                    result.startswith(os.path.realpath(tmp)),
                    f"default should ignore TMPDIR but got {result}",
                )
            finally:
                self._restore_env("AGENT_PLANS_DIR", orig_agent)
                self._restore_env("XDG_DATA_HOME", orig_xdg)
                self._restore_env("TMPDIR", orig_tmp)

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
