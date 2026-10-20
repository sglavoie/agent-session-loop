"""Resplit session regression tests."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from resplit_test_support import (
    add_multi_repo_scopes,
    resplit,
    write_resplit_plan,
    write_session,
)

from agent_session_loop import (
    contracts,
    plans_content_hash,
    plans_files,
    plans_session_scope,
)


class ResplitSessionTest(unittest.TestCase):
    def test_multi_repo_children_inherit_parent_scope_without_broadening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_resplit_plan(plan_dir)
            add_multi_repo_scopes(plan_dir)

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=Path(tmp) / "state",
                skill_prefix="$",
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            for child_id in ("2.1", "2.2"):
                child = (plan_dir / f"session-{child_id}.md").read_text(
                    encoding="utf-8"
                )
                self.assertEqual(1, child.count("**Reads repositories**: SVC, OAK"))
                self.assertEqual(1, child.count("**Writes repositories**: SVC"))
                scope = plans_session_scope.parse_session_scope(child, ("SVC", "OAK"))
                self.assertEqual(("SVC", "OAK"), scope.reads)
                self.assertEqual(("SVC",), scope.writes)

    def test_resplit_success_generates_nested_children_archive_and_ledgers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=3,
                state_dir=state_dir,
                skill_prefix="$",
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            self.assertEqual("2", result["parent_id"])
            self.assertEqual(["2.1", "2.2", "2.3"], result["children"])
            self.assertEqual(
                ["session-2.1.md", "session-2.2.md", "session-2.3.md"],
                result["child_files"],
            )
            self.assertEqual(
                f"$session-expand {plans_files.canonicalize_path(plan_dir)}/",
                result["next_command"],
            )
            self.assertEqual(["manifest.md", "context.md"], result["ledger_updates"])
            self.assertEqual(["session-3.md"], result["reference_updates"])
            self.assertNotIn("stale_validation_archived", result)
            self.assertEqual(
                ["session-2.md", "session-2.expand.md"],
                result["archived_files"],
            )

            archive_dir = plan_dir / result["archive_dir"]
            self.assertTrue((archive_dir / "session-2.md").is_file())
            self.assertTrue((archive_dir / "session-2.expand.md").is_file())
            self.assertFalse((plan_dir / "session-2.md").exists())
            self.assertFalse((plan_dir / "session-2.expand.md").exists())

            child_one = (plan_dir / "session-2.1.md").read_text(encoding="utf-8")
            child_two = (plan_dir / "session-2.2.md").read_text(encoding="utf-8")
            next_session = (plan_dir / "session-3.md").read_text(encoding="utf-8")
            self.assertIn("# Session 2.1: Parent Work: Schema setup", child_one)
            self.assertIn(
                "> **Previous session**: Read the **Handoff** section at the bottom "
                "of `session-1.md`.",
                child_one,
            )
            self.assertIn(
                "- **Parent archive**: `resplit-archive/session-2-20260615T120000Z`",
                child_one,
            )
            self.assertIn(
                "Archived parent expansion notes are at "
                "`resplit-archive/session-2-20260615T120000Z/session-2.expand.md`",
                child_one,
            )
            self.assertIn("Focus deliverable: schema setup.", child_one)
            self.assertNotIn("## Parallel Metadata", child_one)
            self.assertNotIn("## Parallel Metadata", child_two)
            self.assertIn("## Completion Contract", child_one)
            self.assertIn("### Done when", child_one)
            self.assertIn("### Verify", child_one)
            self.assertIn(
                "> **Previous session**: Read the **Handoff** section at the bottom "
                "of `session-2.3.md`.",
                next_session,
            )

            # Contracts are derived from the parent contract, not placeholders,
            # and the parent's own contract block must not leak into the child
            # scope (exactly one contract heading per child file).
            self.assertEqual(1, child_one.count("## Completion Contract"))
            child_one_contract = contracts.parse_completion_contract(
                child_one, session_id="2.1"
            )
            self.assertIsNotNone(child_one_contract)
            self.assertEqual((2, 1), child_one_contract.session_id.components)
            self.assertEqual(1, len(child_one_contract.verify.checks))
            self.assertEqual("parent-suite", child_one_contract.verify.checks[0].name)
            self.assertEqual(
                ("python3", "-m", "unittest", "-q", "tests.parent_suite"),
                child_one_contract.verify.checks[0].argv,
            )
            self.assertNotIn(
                "Parent deliverables ship together.", child_one_contract.done_when
            )

            child_three = (plan_dir / "session-2.3.md").read_text(encoding="utf-8")
            child_three_contract = contracts.parse_completion_contract(
                child_three, session_id="2.3"
            )
            self.assertIn(
                "Parent deliverables ship together.", child_three_contract.done_when
            )

            self.assertEqual("draft", result["readiness"]["state"])
            self.assertEqual(
                plans_content_hash.stable_readiness_sha256(plan_dir),
                result["readiness"]["readiness_sha256"],
            )

            manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
            context = (plan_dir / "context.md").read_text(encoding="utf-8")
            self.assertIn("**Sessions**: 5", manifest)
            self.assertIn(
                "| 2.1 | Parent Work: Schema setup | pending | session-2.1.md |",
                manifest,
            )
            self.assertIn(
                "| 2.2 | Parent Work: CLI parser | pending | session-2.2.md |",
                manifest,
            )
            self.assertIn(
                "| 2.3 | Parent Work: Docs update | pending | session-2.3.md |",
                manifest,
            )
            # Legacy parallel ownership table must be left untouched.
            self.assertIn(
                "| 3 | serial-required | wrap.py | 2 (serial work-session sequence) | "
                "none | serial |",
                manifest,
            )
            # Legacy wave table row for the parent must be left untouched.
            self.assertIn("| 2 | 2 | none | parent |", manifest)
            self.assertIn("| 2.1 | Parent Work: Schema setup | pending |", context)

    def test_resplit_leaves_existing_parallel_validation_file_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            legacy_validation = plan_dir / "parallel-validation.md"
            legacy_validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "### Session 2: Parent Work",
                        "",
                        "- **Validated classification**: blocked",
                        "- **Run condition**: blocked",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
            )

            self.assertTrue(result["ok"])
            self.assertNotIn("parallel-validation.md", result["archived_files"])
            self.assertTrue(legacy_validation.is_file())

    def test_cli_target_parsing_accepts_dotted_explicit_and_direct_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            write_session(plan_dir, "2.1", "pending")

            parser = resplit.build_parser()
            explicit_args = parser.parse_args(
                [str(plan_dir), "--session", "2.1", "--parts", "3"]
            )
            explicit_plan_dir, explicit_session = resplit._resolve_cli_target(
                explicit_args
            )
            self.assertEqual(plans_files.canonicalize_path(plan_dir), explicit_plan_dir)
            self.assertEqual("2.1", explicit_session)
            self.assertEqual(3, resplit._parse_count(explicit_args))

            direct_args = parser.parse_args([str(plan_dir / "session-2.1.md"), "2"])
            direct_plan_dir, direct_session = resplit._resolve_cli_target(direct_args)
            self.assertEqual(plans_files.canonicalize_path(plan_dir), direct_plan_dir)
            self.assertEqual("2.1", direct_session)
            self.assertEqual(2, resplit._parse_count(direct_args))


if __name__ == "__main__":
    unittest.main()
