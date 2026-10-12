"""ParserAndStateTests (part 4).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans,
    run_inputs,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    fail_open_for_path,
    write_plan,
    write_plan_with_ids,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_sync_derived_status_ledgers_accepts_id_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(plan_dir, (("1.1", "complete"),))
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| Session ID | Status |",
                        "|---|---|",
                        "| 1.1 | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| ID | Status |",
                        "|---|---|",
                        "| 1.1 | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| 1.1 | complete |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 1.1 | complete |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_leaves_ambiguous_context_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete",))
            context_path = plan_dir / "context.md"
            original_context = "\n".join(
                [
                    "# Context",
                    "",
                    "| Item | Status |",
                    "|---|---|",
                    "| Draft -> review | pending |",
                    "",
                ]
            )
            context_path.write_text(original_context, encoding="utf-8")

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual((), changed)
            self.assertEqual(original_context, context_path.read_text(encoding="utf-8"))

    def test_unknown_and_empty_statuses_are_reported_as_invalid_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Status**: maybe\n",
                encoding="utf-8",
            )
            unknown = plans.inspect_plan(plan_dir)
            self.assertEqual("unknown", unknown.sessions[0].status)
            self.assertEqual(
                "unknown-status", run_inputs._invalid_status_pause(unknown)[0]
            )

            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Files modified**: none\n",
                encoding="utf-8",
            )
            empty = plans.inspect_plan(plan_dir)
            self.assertEqual("empty", empty.sessions[0].status)
            self.assertEqual(
                "malformed-plan", run_inputs._invalid_status_pause(empty)[0]
            )

    def test_inspect_plan_wraps_plan_file_read_failures(self) -> None:
        cases = (
            "context.md",
            "manifest.md",
            "session-1.expand.md",
        )

        for relative_name in cases:
            with (
                self.subTest(relative_name=relative_name),
                tempfile.TemporaryDirectory() as tmp,
            ):
                plan_dir = Path(tmp) / "plan"
                write_plan(plan_dir, ("pending",))
                target = plan_dir / relative_name
                if not target.exists():
                    target.write_text("extra plan metadata\n", encoding="utf-8")

                with (
                    fail_open_for_path(target),
                    self.assertRaises(plans.PlanError) as raised,
                ):
                    plans.inspect_plan(plan_dir)

                message = str(raised.exception)
                self.assertIn(str(plans.canonicalize_path(target)), message)
                self.assertIn("permission denied", message.lower())
                self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_inspect_plan_wraps_session_discovery_read_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            canonical_plan_dir = plans.canonicalize_path(plan_dir)
            original_iterdir = Path.iterdir

            def failing_iterdir(path: Path):
                if plans.canonicalize_path(path) == canonical_plan_dir:
                    raise PermissionError(
                        errno.EACCES, "permission denied", os.fspath(canonical_plan_dir)
                    )
                return original_iterdir(path)

            with (
                mock.patch.object(Path, "iterdir", failing_iterdir),
                self.assertRaises(plans.PlanError) as raised,
            ):
                plans.inspect_plan(plan_dir)

            message = str(raised.exception)
            self.assertIn(str(canonical_plan_dir), message)
            self.assertIn("permission denied", message.lower())
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_readiness_states_cover_full_lifecycle(self) -> None:
        self.assertEqual(
            ("draft", "reviewed", "loop-ready", "running", "complete"),
            plans.READINESS_STATES,
        )
        for state_value in plans.READINESS_STATES:
            with self.subTest(state=state_value):
                self.assertEqual(state_value, plans.parse_readiness_state(state_value))
        self.assertEqual("loop-ready", plans.parse_readiness_state("  LOOP-READY  "))
        for invalid in ("", "ready", "loop_ready", "DONE"):
            with (
                self.subTest(value=invalid),
                self.assertRaises(plans.PlanError),
            ):
                plans.parse_readiness_state(invalid)

    def test_parse_readiness_metadata_reads_all_three_lines(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Sessions**: 2",
                "**Readiness**: reviewed",
                "**Readiness SHA-256**: `abc123`",
                "**Readiness checked at**: 2026-06-19T12:34:56Z",
                "",
                "## Sessions",
                "",
            ]
        )

        metadata = plans.parse_readiness_metadata(manifest_text)

        self.assertIsNotNone(metadata)
        self.assertEqual("reviewed", metadata.state)
        self.assertEqual("abc123", metadata.readiness_sha256)
        self.assertEqual("2026-06-19T12:34:56Z", metadata.checked_at)

    def test_parse_readiness_metadata_returns_none_when_absent(self) -> None:
        manifest_text = "# Plan\n\n**Sessions**: 1\n\n## Sessions\n"

        self.assertIsNone(plans.parse_readiness_metadata(manifest_text))

    def test_parse_readiness_metadata_rejects_partial_or_invalid_blocks(self) -> None:
        cases = (
            (
                "missing state",
                "**Readiness SHA-256**: `abc`\n**Readiness checked at**: now\n",
            ),
            (
                "missing sha",
                "**Readiness**: draft\n**Readiness checked at**: now\n",
            ),
            (
                "missing checked_at",
                "**Readiness**: draft\n**Readiness SHA-256**: `abc`\n",
            ),
            (
                "invalid state",
                "**Readiness**: green\n"
                "**Readiness SHA-256**: `abc`\n"
                "**Readiness checked at**: now\n",
            ),
            (
                "empty sha",
                "**Readiness**: draft\n"
                "**Readiness SHA-256**: ``\n"
                "**Readiness checked at**: now\n",
            ),
        )

        for label, text in cases:
            with (
                self.subTest(label=label),
                self.assertRaises(plans.PlanError),
            ):
                plans.parse_readiness_metadata(text)

    def test_parse_split_from_reads_a_backticked_pointer(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Split from**: `~/.claude/plans/source.md`",
                "**Sessions**: 2",
                "",
                "## Sessions",
                "",
            ]
        )

        self.assertEqual(
            "~/.claude/plans/source.md", plans.parse_split_from(manifest_text)
        )

    def test_parse_split_from_drops_prose_beside_the_pointer(self) -> None:
        cases = (
            (
                "annotated backtick span",
                "- **Split from**: `plans/source.md` (harness plan store)",
                "plans/source.md",
            ),
            (
                "bare value with trailing prose",
                "**Split from**: /srv/plans/source.md at revision abc123",
                "/srv/plans/source.md",
            ),
        )

        for label, line, expected in cases:
            with self.subTest(label=label):
                manifest_text = f"# Plan\n\n{line}\n\n## Sessions\n"
                self.assertEqual(expected, plans.parse_split_from(manifest_text))

    def test_parse_split_from_ignores_a_pointer_below_the_first_heading(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Sessions**: 1",
                "",
                "## Sessions",
                "",
                "**Split from**: `/quoted/in/a/narrative.md`",
                "",
            ]
        )

        self.assertIsNone(plans.parse_split_from(manifest_text))

    def test_parse_split_from_returns_none_when_absent_or_empty(self) -> None:
        cases = (
            ("absent", "# Plan\n\n**Sessions**: 1\n\n## Sessions\n"),
            ("empty value", "# Plan\n\n**Split from**:\n\n## Sessions\n"),
            ("empty backticks", "# Plan\n\n**Split from**: ``\n\n## Sessions\n"),
        )

        for label, text in cases:
            with self.subTest(label=label):
                self.assertIsNone(plans.parse_split_from(text))

    def test_read_split_from_survives_a_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(plans.read_split_from(Path(tmp) / "absent"))

    def test_write_readiness_metadata_inserts_after_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Split from**: `/source.md`",
                        "**Sessions**: 1",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            metadata = plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="deadbeef",
                checked_at="2026-06-19T00:00:00Z",
            )
            changed = plans.write_readiness_metadata(plan_dir, metadata)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertIn("**Readiness**: loop-ready", text)
            self.assertIn("**Readiness SHA-256**: `deadbeef`", text)
            self.assertIn("**Readiness checked at**: 2026-06-19T00:00:00Z", text)

            sessions_index = text.index("## Sessions")
            readiness_index = text.index("**Readiness**:")
            sessions_count_index = text.index("**Sessions**: 1")
            self.assertLess(sessions_count_index, readiness_index)
            self.assertLess(readiness_index, sessions_index)

            roundtrip = plans.parse_readiness_metadata(text)
            self.assertEqual(metadata, roundtrip)

            unchanged = plans.write_readiness_metadata(plan_dir, metadata)
            self.assertFalse(unchanged)

    def test_write_readiness_metadata_replaces_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 1",
                        "**Readiness**: draft",
                        "**Readiness SHA-256**: `oldhash`",
                        "**Readiness checked at**: 2026-01-01T00:00:00Z",
                        "",
                        "## Sessions",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            updated = plans.ReadinessMetadata(
                state="reviewed",
                readiness_sha256="newhash",
                checked_at="2026-06-19T12:00:00Z",
            )
            changed = plans.write_readiness_metadata(plan_dir, updated)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("**Readiness**:"))
            self.assertEqual(1, text.count("**Readiness SHA-256**:"))
            self.assertEqual(1, text.count("**Readiness checked at**:"))
            self.assertIn("**Readiness**: reviewed", text)
            self.assertIn("`newhash`", text)
            self.assertNotIn("oldhash", text)
            self.assertNotIn("2026-01-01", text)
            self.assertEqual(updated, plans.parse_readiness_metadata(text))

    def test_stable_readiness_sha256_ignores_handoff_status_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans.stable_readiness_sha256(plan_dir)

            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1: Test",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: src/foo.py",
                        "- **Key decisions**: chose option A",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**: tests green",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_status_table_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 2",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | pending |",
                        "| 2 | Two | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            baseline = plans.stable_readiness_sha256(plan_dir)

            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 2",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | complete |",
                        "| 2 | Two | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))
