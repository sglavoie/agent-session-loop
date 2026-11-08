"""Rewrites and hashes cases for ParserAndStateTests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_session_loop import (
    plans_content_hash,
    plans_models,
    plans_readiness,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
)


class ParserAndStateTests(ParserAndStateTestsBase):
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

            metadata = plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="deadbeef",
                checked_at="2026-06-19T00:00:00Z",
            )
            changed = plans_readiness.write_readiness_metadata(plan_dir, metadata)

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

            roundtrip = plans_readiness.parse_readiness_metadata(text)
            self.assertEqual(metadata, roundtrip)

            unchanged = plans_readiness.write_readiness_metadata(plan_dir, metadata)
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

            updated = plans_models.ReadinessMetadata(
                state="reviewed",
                readiness_sha256="newhash",
                checked_at="2026-06-19T12:00:00Z",
            )
            changed = plans_readiness.write_readiness_metadata(plan_dir, updated)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("**Readiness**:"))
            self.assertEqual(1, text.count("**Readiness SHA-256**:"))
            self.assertEqual(1, text.count("**Readiness checked at**:"))
            self.assertIn("**Readiness**: reviewed", text)
            self.assertIn("`newhash`", text)
            self.assertNotIn("oldhash", text)
            self.assertNotIn("2026-01-01", text)
            self.assertEqual(updated, plans_readiness.parse_readiness_metadata(text))

    def test_readiness_metadata_leaves_fenced_example_headers(self) -> None:
        # A header quoted inside a fence is an example: it neither overrides the
        # real readiness nor is deleted when readiness is rewritten.
        fenced_example = [
            "```markdown",
            "**Readiness**: draft",
            "**Readiness SHA-256**: `example`",
            "**Readiness checked at**: 2020-01-01T00:00:00Z",
            "```",
        ]
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Readiness**: loop-ready",
                "**Readiness SHA-256**: `aaa`",
                "**Readiness checked at**: 2026-10-27T00:00:00Z",
                "",
                "## Notes",
                "",
                *fenced_example,
                "",
            ]
        )
        self.assertEqual(
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="aaa",
                checked_at="2026-10-27T00:00:00Z",
            ),
            plans_readiness.parse_readiness_metadata(manifest_text),
        )

        updated = plans_models.ReadinessMetadata(
            state="running",
            readiness_sha256="bbb",
            checked_at="2026-10-27T01:00:00Z",
        )
        rewritten = plans_readiness.replace_readiness_metadata_in_text(
            manifest_text, updated
        )

        self.assertIn("\n".join(fenced_example), rewritten)
        self.assertEqual(updated, plans_readiness.parse_readiness_metadata(rewritten))

    def test_readiness_rewrite_keeps_non_newline_line_separators(self) -> None:
        # Form feed, U+2028 and U+0085 are text here, not line breaks.
        notes = "line one still line one, a page\x0cbreak and a \x85 too."
        manifest_text = "\n".join(
            [
                "# Manifest",
                "",
                "**Readiness**: draft",
                "**Readiness SHA-256**: `a`",
                "**Readiness checked at**: x",
                "",
                "## Notes",
                "",
                notes,
                "",
            ]
        )
        updated = plans_models.ReadinessMetadata(
            state="running", readiness_sha256="b", checked_at="y"
        )

        rewritten = plans_readiness.replace_readiness_metadata_in_text(
            manifest_text, updated
        )

        self.assertTrue(rewritten.endswith(f"\n{notes}\n"))
        self.assertEqual(updated, plans_readiness.parse_readiness_metadata(rewritten))

    def test_stable_readiness_sha256_tracks_fenced_readiness_examples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"

            def write_manifest(example_state: str) -> None:
                manifest_path.write_text(
                    "\n".join(
                        [
                            "# Manifest",
                            "",
                            "```markdown",
                            f"**Readiness**: {example_state}",
                            "```",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )

            write_manifest("draft")
            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)
            write_manifest("reviewed")

            self.assertNotEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_stable_readiness_sha256_tracks_prose_after_a_form_feed(self) -> None:
        # The parser reads "\n"-delimited lines, so a readiness header behind a
        # form feed is prose to it, and editing that prose must move the hash.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"

            def write_manifest(quoted_state: str) -> None:
                manifest_path.write_text(
                    f"# Manifest\n\nNotes\f**Readiness**: {quoted_state}\n",
                    encoding="utf-8",
                )

            write_manifest("draft")
            self.assertIsNone(
                plans_readiness.parse_readiness_metadata(
                    manifest_path.read_text(encoding="utf-8")
                )
            )
            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)
            write_manifest("reviewed")

            self.assertNotEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_readiness_stripping_keeps_hashes_of_form_feeds_in_prose(self) -> None:
        # Existing plans keep their identity: a separator outside a readiness
        # line still hashes as the line break `splitlines` always made it.
        manifest_text = "# Plan\n\nPage one\fPage two\n\n**Readiness**: loop-ready"
        self.assertEqual(
            "# Plan\n\nPage one\nPage two\n",
            plans_content_hash._strip_readiness_lines(manifest_text),
        )

    def test_stable_readiness_sha256_ignores_handoff_status_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

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

            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

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

            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)

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

            self.assertEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )

    def test_stable_readiness_sha256_tracks_fenced_status_examples(self) -> None:
        # A fenced example is plan content, not a status ledger: editing it
        # moves readiness like any other context edit.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))
            context_path = plan_dir / "context.md"

            def write_context(status: str) -> None:
                context_path.write_text(
                    "\n".join(
                        [
                            "# Context",
                            "",
                            "```markdown",
                            "| Session | Status |",
                            "| --- | --- |",
                            f"| 1 | {status} |",
                            "```",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )

            write_context("blocked")
            baseline = plans_content_hash.stable_readiness_sha256(plan_dir)
            write_context("in-review")

            self.assertNotEqual(
                baseline, plans_content_hash.stable_readiness_sha256(plan_dir)
            )
