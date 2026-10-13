"""ParserAndStateTests (part 3).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_session_loop import (
    plans_content_hash,
    plans_files,
    plans_handoff,
    plans_snapshot,
    plans_status_tables,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
    write_plan_with_ids,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_read_session_handoff_body_fields_do_not_leak_across_subsection_heading(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Notes**:",
                        "",
                        "### Discovered Facts",
                        "- a captured fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertIsNone(handoff.notes)
            self.assertEqual(("a captured fact",), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_captures_subsections_in_canonical_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Notes**: body notes",
                        "",
                        "### Discovered Facts",
                        "- lockfile uses tar format",
                        "- retry path is foo/bar.py:142",
                        "",
                        "### Open Questions",
                        "- should we cache parser output?",
                        "- defer to perf owner",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual("body notes", handoff.notes)
            self.assertEqual(
                (
                    "lockfile uses tar format",
                    "retry path is foo/bar.py:142",
                ),
                handoff.discovered_facts,
            )
            self.assertEqual(
                (
                    "should we cache parser output?",
                    "defer to perf owner",
                ),
                handoff.open_questions,
            )

    def test_read_session_handoff_accepts_reversed_subsection_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Open Questions",
                        "- question one",
                        "",
                        "### Discovered Facts",
                        "- fact one",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(("fact one",), handoff.discovered_facts)
            self.assertEqual(("question one",), handoff.open_questions)

    def test_read_session_handoff_only_one_subsection_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "- the only fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(("the only fact",), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_subsection_without_bullets_yields_empty_tuple(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "",
                        "### Open Questions",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual((), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_skips_nested_subsection_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "- top-level fact",
                        "  - nested detail ignored",
                        "    - deeper nesting also ignored",
                        "- another top-level fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(
                ("top-level fact", "another top-level fact"),
                handoff.discovered_facts,
            )

    def test_read_session_handoff_ignores_unknown_subsection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Future Notes",
                        "- bullet under unknown heading",
                        "",
                        "### Discovered Facts",
                        "- a real fact",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual(("a real fact",), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_subsections_stop_at_h2_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "",
                        "### Discovered Facts",
                        "- fact inside the block",
                        "",
                        "## Trailing Section",
                        "- bullet after the block",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual(("fact inside the block",), handoff.discovered_facts)

    def test_read_session_status_delegates_to_read_session_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: pending",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            session_file = self._session_file(plan_dir)

            status = plans_handoff.read_session_status(session_file)
            handoff = plans_handoff.read_session_handoff(session_file)

            self.assertEqual(handoff.status, status.status)
            self.assertEqual(handoff.raw_value, status.raw_value)
            self.assertEqual(handoff.line_number, status.line_number)
            self.assertEqual(session_file, status.session)

    def test_plan_content_hash_tracks_matching_dotted_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(plan_dir, (("1", "complete"), ("1.1", "pending")))
            ignored = plan_dir / "session-1.1.expand.md.bak"
            ignored.write_text("ignored\n", encoding="utf-8")

            without_expansion = plans_content_hash.plan_content_sha256(plan_dir)
            expansion = plan_dir / "session-1.1.expand.md"
            expansion.write_text("first\n", encoding="utf-8")
            with_expansion = plans_content_hash.plan_content_sha256(plan_dir)
            expansion.write_text("second\n", encoding="utf-8")
            changed_expansion = plans_content_hash.plan_content_sha256(plan_dir)

            self.assertNotEqual(without_expansion, with_expansion)
            self.assertNotEqual(with_expansion, changed_expansion)
            ignored_hash = plans_content_hash.plan_content_sha256(plan_dir)
            ignored.write_text("still ignored\n", encoding="utf-8")
            self.assertEqual(
                ignored_hash, plans_content_hash.plan_content_sha256(plan_dir)
            )

    def test_sync_derived_status_ledgers_updates_clear_status_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending"))
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| # | Title | Status | File |",
                        "|---|---|---|---|",
                        "| 1 | One | pending | [session-1.md](session-1.md) |",
                        "| 2 | Two | complete | session-2.md |",
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
                        "| Session | Title | Status |",
                        "|---|---|---|",
                        "| S1 | One | pending |",
                        "| S2 | Two | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans_snapshot.inspect_plan(plan_dir)
            changed = plans_status_tables.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans_files.canonicalize_path(manifest_path),
                    plans_files.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| 1 | One | complete | [session-1.md](session-1.md) |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 2 | Two | pending | session-2.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S1 | One | complete |",
                context_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S2 | Two | pending |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_updates_dotted_session_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(
                plan_dir,
                (("1", "complete"), ("1.1", "pending"), ("2", "pending")),
            )
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| Session | Title | Status | File |",
                        "|---|---|---|---|",
                        "| S1 | One | pending | session-1.md |",
                        "| S1.1 | Nested | complete | session-1.1.md |",
                        "| 2 | Two | complete | session-2.md |",
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
                        "| Session | Title | Status |",
                        "|---|---|---|",
                        "| `S1` | One | pending |",
                        "| `S1.1` | Nested | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans_snapshot.inspect_plan(plan_dir)
            changed = plans_status_tables.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans_files.canonicalize_path(manifest_path),
                    plans_files.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| S1 | One | complete | session-1.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S1.1 | Nested | pending | session-1.1.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 2 | Two | pending | session-2.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| `S1.1` | Nested | pending |",
                context_path.read_text(encoding="utf-8"),
            )
