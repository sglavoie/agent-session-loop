#!/usr/bin/env python3
"""Tests for capture-inline-plan.py."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "capture-inline-plan.py")
SPEC = importlib.util.spec_from_file_location("capture_inline_plan", SCRIPT_PATH)
capture = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(capture)


class CaptureInlinePlanTest(unittest.TestCase):
    def test_extracts_last_proposed_plan_block(self) -> None:
        raw = """Intro
<proposed_plan>
# Old Plan
</proposed_plan>
Middle
<proposed_plan>
# New Plan

## Session 1
</proposed_plan>
Outro
"""

        self.assertEqual("# New Plan\n\n## Session 1\n", capture.extract_plan(raw))

    def test_capture_creates_persistent_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = capture.capture_inline_plan("# Durable Plan\n\n## Session 1\n", tmp)

            self.assertEqual(os.path.realpath(tmp), result["base_dir"])
            self.assertEqual(os.path.realpath(os.path.join(tmp, "sources")), result["sources_dir"])
            self.assertTrue(result["source_path"].endswith(".md"))
            self.assertTrue(os.path.exists(result["source_path"]))
            with open(result["source_path"], encoding="utf-8") as source_file:
                self.assertEqual("# Durable Plan\n\n## Session 1\n", source_file.read())

    def test_capture_reuses_same_source_path_for_identical_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = capture.capture_inline_plan("# Same Plan\n\nBody\n", tmp)
            second = capture.capture_inline_plan("# Same Plan\n\nBody\n", tmp)

            self.assertEqual(first["source_path"], second["source_path"])
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])

    def test_slugifies_from_first_h1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = capture.capture_inline_plan("# My PLAN!! v2\n\nBody\n", tmp)

            self.assertEqual("my-plan-v2", result["slug"])
            self.assertTrue(os.path.basename(result["source_path"]).startswith("my-plan-v2-"))

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            capture.capture_inline_plan("<proposed_plan>  </proposed_plan>")

    def test_base_dir_argument_is_used_by_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [sys.executable, SCRIPT_PATH, "--base-dir", tmp],
                input="# CLI Plan\n",
                text=True,
                check=True,
                capture_output=True,
            )

            self.assertIn(os.path.realpath(tmp), completed.stdout)

    def test_agent_plans_dir_is_used_when_base_dir_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = os.environ.get("AGENT_PLANS_DIR")
            os.environ["AGENT_PLANS_DIR"] = tmp
            try:
                result = capture.capture_inline_plan("# Env Plan\n")
            finally:
                if original is None:
                    os.environ.pop("AGENT_PLANS_DIR", None)
                else:
                    os.environ["AGENT_PLANS_DIR"] = original

            self.assertEqual(os.path.realpath(tmp), result["base_dir"])


if __name__ == "__main__":
    unittest.main()
