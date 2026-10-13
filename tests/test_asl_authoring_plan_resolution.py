"""Plan capture, split output resolution, and base-dir tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    authoring_paths,
    authoring_split,
    cli,
)
from tests.asl_authoring_support import PlanAuthoringBase


class PlanResolutionTests(PlanAuthoringBase):
    def test_capture_inline_plan_persists_source_and_dedups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = authoring_paths.capture_inline_plan("# My PLAN!! v2\n\nBody\n", tmp)
            second = authoring_paths.capture_inline_plan(
                "# My PLAN!! v2\n\nBody\n", tmp
            )

            self.assertEqual(os.path.realpath(tmp), first["base_dir"])
            self.assertEqual("my-plan-v2", first["slug"])
            self.assertTrue(first["source_path"].endswith(".md"))
            self.assertTrue(os.path.exists(first["source_path"]))
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["source_path"], second["source_path"])

    def test_extract_inline_plan_uses_last_proposed_block(self) -> None:
        raw = (
            "Intro\n<proposed_plan>\n# Old\n</proposed_plan>\n"
            "<proposed_plan>\n# New\n\n## Session 1\n</proposed_plan>\n"
        )
        self.assertEqual(
            "# New\n\n## Session 1\n", authoring_paths.extract_inline_plan(raw)
        )

    def test_extract_inline_plan_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            authoring_paths.extract_inline_plan("<proposed_plan>   </proposed_plan>")

    def test_split_plan_refuses_duplicate_plan_id_without_force_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            plan = os.path.join(tmp, "PLAN.md")
            with open(plan, "w", encoding="utf-8") as handle:
                handle.write(
                    "# Plan\n\n"
                    "## Session 1: First\n\nDo one.\n\n"
                    "## Session 2: Second\n\nDo two.\n"
                )
            contracts_by_id = {
                "1": {
                    "done_when": ["Session 1 lands."],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "type": "command",
                                "argv": ["python3", "-m", "unittest", "-q"],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["Session 2 lands."],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "type": "command",
                                "argv": ["python3", "-m", "unittest", "-q"],
                                "timeout_seconds": 60,
                            }
                        ],
                    },
                },
            }

            first = authoring_split.split_plan(
                plan_file=plan,
                contracts_by_id=contracts_by_id,
                base_dir=base,
            )

            with self.assertRaises(authoring_paths.DuplicateSplitError) as raised:
                authoring_split.split_plan(
                    plan_file=plan, contracts_by_id=contracts_by_id, base_dir=base
                )

            self.assertIn(first.plan_id, str(raised.exception))
            self.assertIn(str(first.output_dir), str(raised.exception))
            self.assertIn("[draft]", str(raised.exception))
            self.assertEqual(
                [first.output_dir.name],
                sorted(path.name for path in Path(base).iterdir()),
            )

    def test_resolve_plan_output_force_new_keeps_collision_safe_suffixing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            plan = os.path.join(tmp, "PLAN.md")
            with open(plan, "w", encoding="utf-8") as handle:
                handle.write("# Plan\n")

            first = authoring_paths.resolve_plan_output(plan, base)
            second = authoring_paths.resolve_plan_output(plan, base, force_new=True)

            self.assertEqual(1, first["collision_index"])
            self.assertEqual(2, second["collision_index"])
            self.assertEqual(first["hash_prefix"], first["fingerprint_prefix"])
            self.assertEqual(
                f"{first['content_hash_prefix']}-{first['fingerprint_prefix']}",
                first["plan_id"],
            )
            self.assertEqual(first["plan_id"], os.path.basename(first["output_dir"]))
            self.assertEqual(
                f"{first['plan_id']}-2", os.path.basename(second["output_dir"])
            )

    def test_split_and_prepare_parsers_accept_force_new(self) -> None:
        parser = cli.build_parser()

        split_args = parser.parse_args(
            [
                "split",
                "--plan-file",
                "P.md",
                "--contracts-file",
                "c.json",
                "--force-new",
            ]
        )
        prepare_args = parser.parse_args(["prepare", "/tmp/plan.md", "--force-new"])

        self.assertTrue(split_args.force_new)
        self.assertTrue(prepare_args.force_new)

    def test_resolve_plan_output_fingerprints_content_and_path(self) -> None:
        # Migrated from the retired resolve-plan-output.py script tests.
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            left_dir = os.path.join(tmp, "left")
            right_dir = os.path.join(tmp, "right")
            os.mkdir(left_dir)
            os.mkdir(right_dir)
            for directory in (left_dir, right_dir):
                with open(
                    os.path.join(directory, "PLAN.md"), "w", encoding="utf-8"
                ) as h:
                    h.write("# Shared Name\n")

            left = authoring_paths.resolve_plan_output(
                os.path.join(left_dir, "PLAN.md"), base
            )
            right = authoring_paths.resolve_plan_output(
                os.path.join(right_dir, "PLAN.md"), base
            )

            # Same content => identical content prefix; distinct paths => distinct id.
            self.assertEqual(left["content_hash_prefix"], right["content_hash_prefix"])
            self.assertNotEqual(left["fingerprint_prefix"], right["fingerprint_prefix"])
            self.assertNotEqual(left["plan_id"], right["plan_id"])

    def test_slug_from_source_path_sanitizes_unsafe_filenames(self) -> None:
        # Migrated from the retired resolve-plan-output.py script tests.
        cases = {
            "My PLAN!! v2.md": "my-plan-v2",
            "___###.md": "plan",
            "Spacing_AND.symbols.md": "spacing-and-symbols",
        }
        for filename, expected in cases.items():
            with self.subTest(filename=filename):
                self.assertEqual(
                    expected, authoring_paths.slug_from_source_path(filename)
                )

    def test_default_base_dir_resolution_precedence(self) -> None:
        # Migrated from the retired capture/resolve script tests.
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = os.path.join(tmp, "agent")
            xdg_dir = os.path.join(tmp, "xdg")
            with mock.patch.dict(
                os.environ,
                {"AGENT_PLANS_DIR": agent_dir, "XDG_DATA_HOME": xdg_dir},
            ):
                self.assertEqual(
                    os.path.realpath(agent_dir), authoring_paths.default_base_dir()
                )
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": xdg_dir}, clear=False):
                os.environ.pop("AGENT_PLANS_DIR", None)
                self.assertEqual(
                    os.path.realpath(os.path.join(xdg_dir, "agent-plans")),
                    authoring_paths.default_base_dir(),
                )
            with mock.patch.dict(os.environ, {"TMPDIR": tmp}, clear=False):
                os.environ.pop("AGENT_PLANS_DIR", None)
                os.environ.pop("XDG_DATA_HOME", None)
                result = authoring_paths.default_base_dir()
                self.assertTrue(
                    result.endswith(os.path.join(".local", "share", "agent-plans"))
                )
                self.assertFalse(result.startswith(os.path.realpath(tmp)))


if __name__ == "__main__":
    unittest.main()
