"""ParserAndStateTests (part 2).

Split from tests/test_asl_core.py."""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_session_loop import (
    plans_content_hash,
    plans_errors,
    plans_files,
    plans_handoff,
    plans_registry,
    plans_session_scope,
    plans_snapshot,
)
from tests.asl_core_support import ParserAndStateTestsBase
from tests.support import (
    write_plan,
    write_plan_with_ids,
)


class ParserAndStateTests(ParserAndStateTestsBase):
    def test_inspect_plan_sorts_dotted_session_files_and_finds_nested_next_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(
                plan_dir,
                (
                    ("1", "complete"),
                    ("2.10", "pending"),
                    ("2.1", "complete"),
                    ("2", "complete"),
                    ("10", "pending"),
                    ("2.2", "pending"),
                ),
            )
            (plan_dir / "session-2.1.expand.md").write_text(
                "ignored for discovery\n", encoding="utf-8"
            )
            (plan_dir / "session-2.01.md").write_text(
                "invalid id ignored\n", encoding="utf-8"
            )

            snapshot = plans_snapshot.inspect_plan(plan_dir)

            self.assertEqual(
                ["1", "2", "2.1", "2.2", "2.10", "10"],
                [session.id for session in snapshot.sessions],
            )
            self.assertEqual("2.2", snapshot.next_pending.id)
            self.assertEqual(
                ["complete", "complete", "complete", "pending", "pending", "pending"],
                [session.status for session in snapshot.sessions],
            )

    def test_inspect_plan_reads_status_from_final_handoff_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "Instructions.",
                        "",
                        "**Handoff (fill on completion)**:",
                        "",
                        "- **Status**:",
                        "- **Files modified**:",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans_snapshot.inspect_plan(plan_dir)

            self.assertEqual("complete", snapshot.sessions[0].status)
            self.assertEqual("complete", snapshot.sessions[0].raw_value)
            self.assertEqual(12, snapshot.sessions[0].line_number)

    def test_manifest_registry_parser_ignores_fenced_example_for_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete",))
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n```markdown\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| Example | example | https://example.com/example.git |\n"
                "```\n",
                encoding="utf-8",
            )

            registry = plans_registry.read_repository_registry(plan_dir)
            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertIsNone(registry)
            self.assertEqual("complete", handoff.status)

    def test_manifest_registry_parser_preserves_legacy_repositories_prose(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "manifest.md").write_text(
                "# Plan\n\n## Sessions\n\n"
                "| Session | Title | Status |\n"
                "| --- | --- | --- |\n"
                "| 1 | First | pending |\n\n"
                "## Repositories\n\n"
                "We touch SVC and OAK by hand.\n",
                encoding="utf-8",
            )

            registry = plans_registry.read_repository_registry(plan_dir)
            snapshot = plans_snapshot.inspect_plan(plan_dir)

            self.assertIsNone(registry)
            self.assertEqual(["1"], [session.id for session in snapshot.sessions])

    def test_session_scope_and_qualified_paths_are_canonical(self) -> None:
        scope = plans_session_scope.parse_session_scope(
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: SVC\n"
            "```markdown\n"
            "**Reads repositories**: Example\n"
            "**Writes repositories**: Example\n"
            "```\n",
            ("SVC", "OAK"),
        )

        self.assertEqual(("SVC", "OAK"), scope.reads)
        self.assertEqual(("SVC",), scope.writes)
        self.assertEqual(
            "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC\n",
            plans_session_scope.render_session_scope(scope),
        )
        qualified = plans_session_scope.parse_qualified_path(
            "SVC:internal/foo.go", scope.reads
        )
        self.assertEqual("SVC", qualified.alias)
        self.assertEqual("internal/foo.go", qualified.path)
        self.assertEqual(
            "SVC:internal/foo.go", plans_session_scope.render_qualified_path(qualified)
        )
        for invalid in (
            "internal/foo.go",
            "SVC:/tmp/foo",
            "SVC:../foo",
            "OAK:./foo",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(ValueError),
            ):
                plans_session_scope.parse_qualified_path(invalid, ("SVC",))

    def test_completed_multi_repo_handoff_requires_qualified_write_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n"
                "| OAK | oak | https://example.com/oak.git |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            template = (
                "# Session 1\n\n"
                "**Reads repositories**: SVC, OAK\n"
                "**Writes repositories**: SVC\n\n"
                "Do work.\n\n## Handoff\n\n"
                "- **Status**: complete\n"
                "- **Files modified**: {files}\n"
                "- **Key decisions**: none\n"
                "- **Blockers for next session**: none\n"
                "- **Notes**:\n"
            )
            session_path.write_text(
                template.format(files="internal/foo.go"), encoding="utf-8"
            )

            with self.assertRaisesRegex(plans_errors.PlanError, "Alias:path"):
                plans_handoff.read_session_handoff(self._session_file(plan_dir))

            session_path.write_text(
                template.format(files="OAK:internal/foo.go"), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                plans_errors.PlanError, "outside Writes repositories"
            ):
                plans_handoff.read_session_handoff(self._session_file(plan_dir))

            session_path.write_text(
                template.format(files="SVC:internal/foo.go, SVC:README.md"),
                encoding="utf-8",
            )
            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))
            self.assertEqual(
                ("SVC:internal/foo.go, SVC:README.md",),
                handoff.files_modified,
            )

    def test_generated_review_artifacts_are_not_repository_handoff_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            template = (
                "# Session 1: Final Review\n\n"
                "**Reads repositories**: SVC\n"
                "**Writes repositories**: (none)\n\n"
                "<!-- asl:generated-review-session -->\n\n"
                "## Handoff\n\n"
                "- **Status**: complete\n"
                "- **Files modified**: {files}\n"
                "- **Key decisions**: reviewed the repository\n"
                "- **Blockers for next session**: (none)\n"
                "- **Notes**:\n"
            )
            session_path.write_text(
                template.format(files="final-review.md, final-review.json"),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))
            snapshot = plans_snapshot.inspect_plan(plan_dir)

            self.assertEqual("complete", handoff.status)
            self.assertEqual(("(none)",), handoff.files_modified)
            self.assertEqual("complete", snapshot.sessions[0].status)
            self.assertIn(
                "- **Files modified**: final-review.md, final-review.json",
                session_path.read_text(encoding="utf-8"),
            )

            self.assertEqual(
                (plans_files.canonicalize_path(session_path),),
                plans_handoff.normalize_generated_review_handoff_artifacts(plan_dir),
            )
            self.assertIn(
                "- **Files modified**: (none)",
                session_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (), plans_handoff.normalize_generated_review_handoff_artifacts(plan_dir)
            )

            session_path.write_text(
                template.format(files="final-review.md, README.md"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(plans_errors.PlanError, "Alias:path"):
                plans_handoff.read_session_handoff(self._session_file(plan_dir))

    def test_completed_handoff_omits_files_modified_only_when_writes_are_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n"
                "| Alias | Path | Origin |\n"
                "| --- | --- | --- |\n"
                "| SVC | svc | https://example.com/svc.git |\n",
                encoding="utf-8",
            )
            session_path = plan_dir / "session-1.md"
            template = (
                "# Session 1\n\n"
                "**Reads repositories**: SVC\n"
                "**Writes repositories**: {writes}\n\n"
                "Do work.\n\n## Handoff\n\n"
                "- **Status**: complete\n"
                "{files}"
                "- **Key decisions**: none\n"
                "- **Blockers for next session**: none\n"
                "- **Notes**:\n"
            )
            for files in ("", "- **Files modified**:\n"):
                with self.subTest(files=files):
                    session_path.write_text(
                        template.format(writes="(none)", files=files),
                        encoding="utf-8",
                    )

                    handoff = plans_handoff.read_session_handoff(
                        self._session_file(plan_dir)
                    )

                    self.assertEqual("complete", handoff.status)
                    self.assertEqual((), handoff.files_modified)
                    self.assertIsNone(
                        plans_registry.completed_handoff_registry_blocker(
                            handoff,
                            scope=plans_session_scope.parse_session_scope(
                                "**Reads repositories**: SVC\n"
                                "**Writes repositories**: (none)\n",
                                ("SVC",),
                            ),
                            registry_aliases=("SVC",),
                        )
                    )

                    session_path.write_text(
                        template.format(writes="SVC", files=files),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        plans_errors.PlanError, "missing Files modified"
                    ):
                        plans_handoff.read_session_handoff(self._session_file(plan_dir))

    def test_session_scope_participates_in_readiness_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            roots = (Path(tmp) / "left", Path(tmp) / "right")
            for root, writes in zip(roots, ("SVC", "(none)"), strict=True):
                root.mkdir()
                (root / "context.md").write_text("# Context\n", encoding="utf-8")
                (root / "manifest.md").write_text(
                    "# Manifest\n\n## Repositories\n\n"
                    "| Alias | Path | Origin |\n"
                    "| --- | --- | --- |\n"
                    "| SVC | svc | https://example.com/svc.git |\n",
                    encoding="utf-8",
                )
                (root / "session-1.md").write_text(
                    "# Session 1\n\n"
                    "**Reads repositories**: SVC\n"
                    f"**Writes repositories**: {writes}\n\n"
                    "Do work.\n\n## Handoff\n\n- **Status**: pending\n",
                    encoding="utf-8",
                )

            self.assertNotEqual(
                plans_content_hash.stable_readiness_sha256(roots[0]),
                plans_content_hash.stable_readiness_sha256(roots[1]),
            )

    def test_read_session_handoff_round_trip_captures_all_bullets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: plans.py, runner.py",
                        "- **Key decisions**: chose option A over B",
                        "- **Blockers for next session**: none",
                        "- **Notes**: verified hash idempotency",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual("complete", handoff.raw_value)
            self.assertEqual(7, handoff.line_number)
            self.assertEqual(("plans.py, runner.py",), handoff.files_modified)
            self.assertEqual("chose option A over B", handoff.key_decisions)
            self.assertEqual("none", handoff.blockers)
            self.assertEqual("verified hash idempotency", handoff.notes)
            self.assertEqual((), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_returns_empty_for_missing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                "# Session 1\n\nNo handoff section here.\n",
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("empty", handoff.status)
            self.assertEqual("", handoff.raw_value)
            self.assertIsNone(handoff.line_number)
            self.assertEqual((), handoff.files_modified)
            self.assertIsNone(handoff.key_decisions)
            self.assertIsNone(handoff.blockers)
            self.assertIsNone(handoff.notes)
            self.assertEqual((), handoff.discovered_facts)
            self.assertEqual((), handoff.open_questions)

    def test_read_session_handoff_block_without_status_line_yields_empty_status(
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
                        "- **Files modified**: only files captured",
                        "- **Notes**: still parses",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("empty", handoff.status)
            self.assertEqual("", handoff.raw_value)
            self.assertIsNone(handoff.line_number)
            self.assertEqual(("only files captured",), handoff.files_modified)
            self.assertEqual("still parses", handoff.notes)

    def test_read_session_handoff_uses_only_final_handoff_block(self) -> None:
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
                        "- **Notes**: stale",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Notes**: real",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            handoff = plans_handoff.read_session_handoff(self._session_file(plan_dir))

            self.assertEqual("complete", handoff.status)
            self.assertEqual("real", handoff.notes)
