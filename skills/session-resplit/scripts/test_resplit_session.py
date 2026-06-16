#!/usr/bin/env python3
"""Tests for resplit-session.py."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "resplit-session.py")
SPEC = importlib.util.spec_from_file_location("resplit_session", SCRIPT_PATH)
resplit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resplit
assert SPEC.loader is not None
SPEC.loader.exec_module(resplit)

from agent_session_loop import plans, state  # noqa: E402


def write_session(
    plan_dir: Path,
    session_id: str,
    status: str,
    *,
    title: str = "Test",
    body: str = "Instructions.",
) -> None:
    (plan_dir / plans.session_id_to_filename(session_id)).write_text(
        "\n".join(
            [
                f"# Session {session_id}: {title}",
                "",
                body,
                "",
                "## Handoff",
                "",
                f"- **Status**: {status}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        ),
        encoding="utf-8",
    )


def write_resplit_plan(plan_dir: Path, *, parent_status: str = "pending") -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text(
        "\n".join(
            [
                "# Context",
                "",
                "| Session | Title | Status |",
                "|---|---|---|",
                "| S1 | Setup | complete |",
                f"| S2 | Parent Work | {parent_status} |",
                "| S3 | Wrap | pending |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (plan_dir / "manifest.md").write_text(
        "\n".join(
            [
                "# Manifest",
                "",
                "**Sessions**: 3",
                "",
                "| # | Title | Status | File |",
                "|---|---|---|---|",
                "| 1 | Setup | complete | session-1.md |",
                f"| 2 | Parent Work | {parent_status} | session-2.md |",
                "| 3 | Wrap | pending | session-3.md |",
                "",
                "### Dependency Waves",
                "",
                "| Wave | Sessions | Safe concurrent set | Notes |",
                "|---|---|---|---|",
                "| 1 | 1 | none | setup |",
                "| 2 | 2 | none | parent |",
                "| 3 | 3 | none | wrap |",
                "",
                "### Session Ownership",
                "",
                "| Session | Classification | Owned write paths | Required prior sessions | "
                "Allowed concurrent sessions | Serial blockers |",
                "|---|---|---|---|---|---|",
                "| 1 | serial-required | setup.py | none | none | serial |",
                "| 2 | serial-required | app.py, docs.md | 1 | none | serial |",
                "| 3 | serial-required | wrap.py | 2 (serial work-session sequence) | none | serial |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_session(plan_dir, "1", "complete", title="Setup")
    write_session(
        plan_dir,
        "2",
        parent_status,
        title="Parent Work",
        body="\n".join(
            [
                "> **Before starting**: Read `context.md` in this directory "
                "for shared project context.",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: `serial-required`",
                "- **Required prior sessions**: 1",
                "- **Allowed concurrent sessions**: none",
                "- **Owned write paths**: app.py, docs.md",
                "- **Forbidden/shared paths**: shared files require coordinator ownership",
                "- **Serial blockers**: run serially",
                "- **Optional internal tracks**: none",
                "",
                "---",
                "",
                "Implement these deliverables: schema setup, CLI parser, docs update.",
            ]
        ),
    )
    write_session(
        plan_dir,
        "3",
        "pending",
        title="Wrap",
        body="\n".join(
            [
                "> **Before starting**: Read `context.md` in this directory "
                "for shared project context.",
                "> **Previous session**: Read the **Handoff** section at the "
                "bottom of `session-2.md`.",
                "",
                "## Parallel Metadata",
                "",
                "- **Safety classification**: `serial-required`",
                "- **Required prior sessions**: 2 (serial work-session sequence)",
                "- **Allowed concurrent sessions**: none",
                "- **Owned write paths**: wrap.py",
                "- **Forbidden/shared paths**: shared files require coordinator ownership",
                "- **Serial blockers**: run serially",
                "- **Optional internal tracks**: none",
                "",
                "---",
                "",
                "Use parent output.",
            ]
        ),
    )
    (plan_dir / "session-2.expand.md").write_text(
        "historical facts\n", encoding="utf-8"
    )
    (plan_dir / "parallel-validation.md").write_text(
        "\n".join(
            [
                "# Parallel Validation",
                "",
                "### Session 2: Parent Work",
                "",
                "- **Run condition**: dependent on unresolved work",
                "",
            ]
        ),
        encoding="utf-8",
    )


class ResplitSessionTest(unittest.TestCase):
    def test_resplit_success_generates_nested_children_archive_and_ledgers(self) -> None:
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
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            )

            self.assertTrue(result["ok"])
            self.assertEqual("2", result["parent_id"])
            self.assertEqual(["2.1", "2.2", "2.3"], result["children"])
            self.assertEqual(
                ["session-2.1.md", "session-2.2.md", "session-2.3.md"],
                result["child_files"],
            )
            self.assertEqual(
                f"$session-expand {plans.canonicalize_path(plan_dir)}/",
                result["next_command"],
            )
            self.assertEqual(["manifest.md", "context.md"], result["ledger_updates"])
            self.assertEqual(["session-3.md"], result["reference_updates"])
            self.assertTrue(result["stale_validation_archived"])

            archive_dir = plan_dir / result["archive_dir"]
            self.assertTrue((archive_dir / "session-2.md").is_file())
            self.assertTrue((archive_dir / "session-2.expand.md").is_file())
            self.assertTrue((archive_dir / "parallel-validation.md").is_file())
            self.assertFalse((plan_dir / "session-2.md").exists())
            self.assertFalse((plan_dir / "session-2.expand.md").exists())
            self.assertFalse((plan_dir / "parallel-validation.md").exists())

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
            self.assertIn(
                "- **Required prior sessions**: 2.1 (generated child sequence)",
                child_two,
            )
            self.assertIn(
                "> **Previous session**: Read the **Handoff** section at the bottom "
                "of `session-2.3.md`.",
                next_session,
            )
            self.assertIn(
                "- **Required prior sessions**: 2.3 (serial work-session sequence)",
                next_session,
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
            self.assertIn(
                "| 3 | serial-required | wrap.py | 2.3 (serial work-session sequence) | none | serial |",
                manifest,
            )
            self.assertIn("| 2.1 | Parent Work: Schema setup | pending |", context)

    def test_resplit_rejects_complete_parent_without_mutating_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_resplit_plan(plan_dir, parent_status="complete")

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(plan_dir, session_id="2", parts=2)

            self.assertEqual("blocked", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_resplit_ignores_non_marker_blocked_text_in_parallel_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            (plan_dir / "parallel-validation.md").write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Summary",
                        "",
                        "- **Blocked sessions**: none",
                        "",
                        "## Session Results",
                        "",
                        "### Session 2: Parent Work",
                        "",
                        "- **Validated classification**: serial-required",
                        "- **Run condition**: dependent on unresolved work",
                        "- **Findings**: session is not blocked for resplitting",
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
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(["2.1", "2.2"], result["children"])

    def test_resplit_rejects_blocked_parallel_validation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_resplit_plan(plan_dir)
            (plan_dir / "parallel-validation.md").write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Session Results",
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

            with self.assertRaises(resplit.ResplitError) as raised:
                resplit.resplit_session(plan_dir, session_id="2", parts=2)

            self.assertEqual("blocked", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())

    def test_resplit_rejects_active_lock_without_mutating_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_resplit_plan(plan_dir)
            state.acquire_lock(plan_dir, state_dir)
            try:
                with self.assertRaises(resplit.ResplitError) as raised:
                    resplit.resplit_session(
                        plan_dir,
                        session_id="2",
                        parts=2,
                        state_dir=state_dir,
                    )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual("lock-contention", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / "resplit-archive").exists())

    def test_cli_target_parsing_accepts_dotted_explicit_and_direct_sessions(self) -> None:
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
            self.assertEqual(plans.canonicalize_path(plan_dir), explicit_plan_dir)
            self.assertEqual("2.1", explicit_session)
            self.assertEqual(3, resplit._parse_count(explicit_args))

            direct_args = parser.parse_args([str(plan_dir / "session-2.1.md"), "2"])
            direct_plan_dir, direct_session = resplit._resolve_cli_target(direct_args)
            self.assertEqual(plans.canonicalize_path(plan_dir), direct_plan_dir)
            self.assertEqual("2.1", direct_session)
            self.assertEqual(2, resplit._parse_count(direct_args))


if __name__ == "__main__":
    unittest.main()
