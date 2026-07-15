"""Plan authoring and resplit-script migration tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    REPO_ROOT,
    _command_contract,
    _load_script_module,
)


class PlanAuthoringTests(unittest.TestCase):
    """Central plan authoring and transaction API (`agent_session_loop.authoring`)."""

    def _command_contract(
        self, session_id: str, *, done: str = "Tests pass."
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=plans.parse_session_id(session_id),
            done_when=(done,),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c1",
                        argv=("python3", "-m", "unittest", "-q"),
                        timeout_seconds=60.0,
                    ),
                )
            ),
        )

    # -- inline capture / output resolution parity --------------------------

    def test_capture_inline_plan_persists_source_and_dedups(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = authoring.capture_inline_plan("# My PLAN!! v2\n\nBody\n", tmp)
            second = authoring.capture_inline_plan("# My PLAN!! v2\n\nBody\n", tmp)

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
        self.assertEqual("# New\n\n## Session 1\n", authoring.extract_inline_plan(raw))

    def test_extract_inline_plan_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            authoring.extract_inline_plan("<proposed_plan>   </proposed_plan>")

    def test_resolve_plan_output_is_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "out")
            plan = os.path.join(tmp, "PLAN.md")
            with open(plan, "w", encoding="utf-8") as handle:
                handle.write("# Plan\n")

            first = authoring.resolve_plan_output(plan, base)
            second = authoring.resolve_plan_output(plan, base)

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

    def test_resolve_plan_output_matches_script_helper(self) -> None:
        script_path = os.path.join(
            REPO_ROOT,
            "skills",
            "session-split",
            "scripts",
            "resolve-plan-output.py",
        )
        spec = importlib.util.spec_from_file_location("resolve_plan_output", script_path)
        resolver = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(resolver)

        with tempfile.TemporaryDirectory() as tmp:
            plan = os.path.join(tmp, "PLAN.md")
            with open(plan, "w", encoding="utf-8") as handle:
                handle.write("# Plan\n\n## Session 1\n")

            module_result = authoring.resolve_plan_output(
                plan, os.path.join(tmp, "module-out")
            )
            script_result = resolver.resolve_plan_output(
                plan, os.path.join(tmp, "script-out")
            )

        for key in (
            "content_sha256",
            "source_fingerprint",
            "plan_id",
            "content_hash_prefix",
            "fingerprint_prefix",
            "hash_prefix",
            "slug",
        ):
            self.assertEqual(script_result[key], module_result[key], key)

    # -- fenced-code-aware parsing ------------------------------------------

    def test_parse_plan_document_splits_context_and_sessions(self) -> None:
        text = (
            "# Plan Title\n\nShared context.\n\n"
            "## Session 1: First\n\nDo one.\n\n"
            "### Session 2.1: Nested\n\nDo two.\n"
        )
        parsed = authoring.parse_plan_document(text)

        self.assertIn("Shared context.", parsed.context_lines)
        self.assertEqual(("1", "2.1"), parsed.session_ids)
        self.assertEqual("First", parsed.sessions[0].title)
        self.assertEqual(2, parsed.sessions[0].heading_level)
        self.assertEqual(3, parsed.sessions[1].heading_level)
        self.assertEqual("Nested", parsed.sessions[1].title)

    def test_parse_plan_document_ignores_headings_in_code_fences(self) -> None:
        for fence in ("```", "~~~"):
            with self.subTest(fence=fence):
                text = (
                    "Context\n\n## Session 1: Real\n\n"
                    f"{fence}\n"
                    "###### Session 009 — fake\n"
                    "### Session nope / malformed\n"
                    f"{fence}\n\nBody.\n"
                )
                parsed = authoring.parse_plan_document(text)

                self.assertEqual(("1",), parsed.session_ids)
                self.assertIn("###### Session 009 — fake", parsed.sessions[0].lines)
                self.assertIn(
                    "### Session nope / malformed", parsed.sessions[0].lines
                )

    def test_parse_plan_document_rejects_duplicate_ids(self) -> None:
        text = "## Session 1: A\n\nx\n\n## Session 01: B\n\ny\n"
        with self.assertRaises(plans.PlanError) as raised:
            authoring.parse_plan_document(text)
        message = str(raised.exception)
        self.assertIn("line 5", message)
        self.assertIn("duplicate canonical session id '1'", message)
        self.assertIn("first defined on line 1", message)
        self.assertIn("## Session 01: B", message)

    def test_parse_plan_document_titleless_heading(self) -> None:
        parsed = authoring.parse_plan_document("## Session 3\n\nBody.\n")
        self.assertEqual(("3",), parsed.session_ids)
        self.assertIsNone(parsed.sessions[0].title)

    def test_parse_plan_document_accepts_title_separators_and_padding(self) -> None:
        cases = (
            ("## Session 01: Colon", "1", "Colon"),
            ("## Session 02-ASCII hyphen", "2", "ASCII hyphen"),
            ("## Session 03 – En dash", "3", "En dash"),
            ("## Session 04—Em dash", "4", "Em dash"),
            ("## Session 005", "5", None),
            ("## Session 006.007: Dotted", "6.7", "Dotted"),
        )
        for source, expected_id, expected_title in cases:
            with self.subTest(source=source):
                parsed = authoring.parse_plan_document(source + "\n")
                self.assertEqual((expected_id,), parsed.session_ids)
                self.assertEqual(expected_title, parsed.sessions[0].title)

    def test_parse_plan_document_accepts_mixed_levels_case_and_formatting(self) -> None:
        headings = (
            "## Session 1: Two",
            " ### session 2 - Three ###   ",
            "  #### SESSION 3 – Four",
            "   ##### SeSsIoN 4—Five\t#####",
            "###### Session\t05\t:\tSix",
        )
        parsed = authoring.parse_plan_document("\n".join(headings) + "\n")

        self.assertEqual(("1", "2", "3", "4", "5"), parsed.session_ids)
        self.assertEqual(
            (2, 3, 4, 5, 6),
            tuple(session.heading_level for session in parsed.sessions),
        )
        self.assertEqual(
            ("Two", "Three", "Four", "Five", "Six"),
            tuple(session.title for session in parsed.sessions),
        )

    def test_parse_plan_document_only_strips_atx_closing_markers(self) -> None:
        parsed = authoring.parse_plan_document(
            "## Session 1: Attached###\n"
            "## Session 2: Detached ###\n"
        )
        self.assertEqual("Attached###", parsed.sessions[0].title)
        self.assertEqual("Detached", parsed.sessions[1].title)

    def test_parse_plan_document_rejects_malformed_candidates_with_context(self) -> None:
        cases = (
            ("## Session nope: Bad id", "invalid session id"),
            ("## Session 1..2: Bad id", "invalid session id"),
            ("## Session 0: Zero", "zero-valued session id component"),
            ("## Session 1.0: Zero", "zero-valued session id component"),
            ("## Session 1:", "dangling title separator"),
            ("## Session 1 -   ###", "dangling title separator"),
            ("## Session 1 / Unsupported", "unsupported title separator"),
            ("## Session 1 Missing punctuation", "punctuation-free title suffix"),
        )
        for source, reason in cases:
            with self.subTest(source=source):
                with self.assertRaises(plans.PlanError) as raised:
                    authoring.parse_plan_document("context\n" + source + "\n")
                message = str(raised.exception)
                self.assertIn("line 2", message)
                self.assertIn(reason, message)
                self.assertIn(source, message)

    def test_parse_plan_document_leaves_non_candidates_as_context(self) -> None:
        lines = (
            "# Session 1: H1",
            "####### Session 2: H7",
            "    ## Session 3: indented code",
            "**Session 4: bold**",
            "Session 5: plain",
            "## Phase 6: phase",
        )
        parsed = authoring.parse_plan_document("\n".join(lines) + "\n")
        self.assertEqual((), parsed.sessions)
        self.assertEqual(lines, parsed.context_lines)

    # -- heading normalization ----------------------------------------------

    def test_normalize_heading_levels_promotes_level_three(self) -> None:
        body = ["#### Changes", "prose", "##### Deep"]
        self.assertEqual(
            ["## Changes", "prose", "### Deep"],
            authoring.normalize_heading_levels(body, 3),
        )

    def test_normalize_heading_levels_preserves_level_two(self) -> None:
        body = ["### Changes", "prose"]
        self.assertEqual(body, authoring.normalize_heading_levels(body, 2))

    def test_normalize_heading_levels_skips_code_fences(self) -> None:
        body = ["#### Real", "```", "#### fenced", "```"]
        self.assertEqual(
            ["## Real", "```", "#### fenced", "```"],
            authoring.normalize_heading_levels(body, 3),
        )

    # -- contract rendering --------------------------------------------------

    def test_render_completion_contract_round_trips(self) -> None:
        contract = self._command_contract("2")
        rendered = authoring.render_completion_contract(contract)
        parsed = contracts.parse_completion_contract(
            rendered + "\n" + plans.HANDOFF_HEADING + "\n",
            session_id=plans.parse_session_id("2"),
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(("Tests pass.",), parsed.done_when)
        self.assertEqual("c1", parsed.verify.checks[0].name)
        self.assertEqual(
            ("python3", "-m", "unittest", "-q"), parsed.verify.checks[0].argv
        )

    def test_render_completion_contract_emits_nondefault_baseline(self) -> None:
        contract = contracts.CompletionContract(
            session_id=plans.parse_session_id("2"),
            done_when=("done",),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.FileExistsCheck(
                        name="f1",
                        path="agent_session_loop/authoring.py",
                        baseline_expectation="pass",
                    ),
                )
            ),
        )
        rendered = authoring.render_completion_contract(contract)
        self.assertIn('"baseline_expectation": "pass"', rendered)
        parsed = contracts.parse_completion_contract(
            rendered + "\n" + plans.HANDOFF_HEADING + "\n",
            session_id=plans.parse_session_id("2"),
        )
        self.assertEqual("pass", parsed.verify.checks[0].baseline_expectation)

    # -- session-file rendering ----------------------------------------------

    def test_render_session_file_requires_contract_by_default(self) -> None:
        with self.assertRaises(plans.PlanError):
            authoring.render_session_file(
                session_id="1", title="First", instruction_lines=["body"]
            )

    def test_render_session_file_rejects_mismatched_contract(self) -> None:
        with self.assertRaises(plans.PlanError):
            authoring.render_session_file(
                session_id="1",
                title="First",
                instruction_lines=["body"],
                contract=self._command_contract("2"),
            )

    def test_render_session_file_first_omits_previous_reference(self) -> None:
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=["Do the first thing."],
            is_first=True,
            contract=self._command_contract("1"),
        )
        self.assertIn("# Session 1: First", rendered)
        self.assertIn(authoring.CONTEXT_REFERENCE_LINE, rendered)
        self.assertNotIn("Previous session", rendered)
        self.assertIn("## Completion Contract", rendered)
        self.assertTrue(rendered.rstrip().endswith("- **Notes**:"))

    def test_render_session_file_includes_previous_reference(self) -> None:
        rendered = authoring.render_session_file(
            session_id="2",
            title="Second",
            instruction_lines=["Do the second thing."],
            previous_session_id="1",
            contract=self._command_contract("2"),
        )
        self.assertIn("bottom of `session-1.md`.", rendered)

    def test_render_session_file_normalizes_deeper_accepted_heading(self) -> None:
        parsed = authoring.parse_plan_document(
            "##### Session 004 — Deep ###\n\nIntro.\n\n###### Changes\n\n- item\n"
        )
        session = parsed.sessions[0]
        rendered = authoring.render_session_file(
            session_id=session.session_id,
            title=session.title,
            instruction_lines=session.instruction_lines,
            heading_level=session.heading_level,
            is_first=True,
            contract=self._command_contract("4"),
        )
        self.assertIn("# Session 4: Deep", rendered)
        self.assertIn("## Changes", rendered)
        self.assertNotIn("###### Changes", rendered)

    def test_render_session_file_preserves_fenced_content(self) -> None:
        body = ["Run:", "```python", "x = 1  # not a heading", "```"]
        rendered = authoring.render_session_file(
            session_id="1",
            title="First",
            instruction_lines=body,
            is_first=True,
            contract=self._command_contract("1"),
        )
        self.assertIn("x = 1  # not a heading", rendered)

    # -- context / progress rendering ---------------------------------------

    def test_render_context_uses_template_when_empty(self) -> None:
        rendered = authoring.render_context([], "/plans/PLAN.md")
        self.assertIn("_No shared context section found", rendered)
        self.assertIn("`/plans/PLAN.md`", rendered)

    def test_render_context_preserves_lines(self) -> None:
        rendered = authoring.render_context(
            ["", "# Plan", "", "Shared context.", ""], "/plans/PLAN.md"
        )
        self.assertEqual("# Plan\n\nShared context.\n", rendered)

    def test_render_progress_skeleton_is_header_only(self) -> None:
        self.assertEqual(plans.PROGRESS_HEADER, authoring.render_progress_skeleton())
        self.assertNotIn("## Session", authoring.render_progress_skeleton())

    # -- manifest rendering and table updates -------------------------------

    def _manifest_spec(self) -> authoring.ManifestSpec:
        sessions = (
            authoring.ManifestSession(
                session_id=plans.parse_session_id("1"), title="First"
            ),
            authoring.ManifestSession(
                session_id=plans.parse_session_id("2"), title="Second"
            ),
        )
        return authoring.ManifestSpec(
            plan_title="My Plan",
            source_path="/plans/PLAN.md",
            output_dir="/plans/out",
            plan_id="aaaa-bbbb",
            source_fingerprint="f" * 64,
            content_sha256="c" * 64,
            sessions=sessions,
            split_date="2026-07-10",
            readiness=plans.ReadinessMetadata(
                state="draft", readiness_sha256="d" * 64, checked_at="2026-07-10T00:00:00+00:00"
            ),
            skill_prefix="/",
        )

    def test_render_manifest_includes_readiness_and_table(self) -> None:
        rendered = authoring.render_manifest(self._manifest_spec())
        self.assertIn("**Readiness**: draft", rendered)
        self.assertIn("**Readiness SHA-256**: `" + "d" * 64 + "`", rendered)
        self.assertIn("**Sessions**: 2", rendered)
        self.assertIn("| 1 | First | pending | session-1.md |", rendered)
        self.assertIn("/session-run /plans/out/", rendered)
        readiness = plans.parse_readiness_metadata(rendered)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_add_manifest_session_row_appends(self) -> None:
        rendered = authoring.render_manifest(self._manifest_spec())
        updated = authoring.add_manifest_session_row(
            rendered,
            authoring.ManifestSession(
                session_id=plans.parse_session_id("3"), title="Third"
            ),
        )
        self.assertIn("| 3 | Third | pending | session-3.md |", updated)
        self.assertLess(
            updated.index("| 2 | Second"), updated.index("| 3 | Third")
        )

    def test_add_manifest_session_row_without_table_raises(self) -> None:
        with self.assertRaises(plans.PlanError):
            authoring.add_manifest_session_row("# Plan\n\nNo table here.\n",
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("1"), title="First"
                ),
            )

    # -- archive paths -------------------------------------------------------

    def test_archive_dir_name_is_deterministic(self) -> None:
        now = datetime(2026, 7, 10, 12, 30, 45, tzinfo=timezone.utc)
        self.assertEqual(
            "session-2.1-20260710T123045Z",
            authoring.archive_dir_name("2.1", now),
        )

    def test_create_archive_dir_is_collision_safe(self) -> None:
        now = datetime(2026, 7, 10, 12, 30, 45, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            first = authoring.create_archive_dir(tmp, "2", now)
            second = authoring.create_archive_dir(tmp, "2", now)
            self.assertEqual(authoring.ARCHIVE_ROOT_NAME, first.parent.name)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    # -- atomic multi-file transaction --------------------------------------

    def test_transaction_commits_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            tx = authoring.PlanTransaction(plan_dir)
            tx.stage("context.md", "ctx")
            tx.stage("manifest.md", "man")
            committed = tx.commit()

            self.assertEqual(2, len(committed))
            self.assertEqual("ctx", (plan_dir / "context.md").read_text())
            self.assertEqual("man", (plan_dir / "manifest.md").read_text())
            leftovers = [p.name for p in plan_dir.iterdir() if p.name.startswith(".")]
            self.assertEqual([], leftovers)

    def test_transaction_failed_write_leaves_no_partial_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "blocker").write_text("i am a file")
            tx = authoring.PlanTransaction(plan_dir)
            tx.stage("context.md", "ctx")
            tx.stage("manifest.md", "man")
            # A staged path whose parent is a regular file forces a write failure.
            tx.stage("blocker/session-1.md", "nope")

            with self.assertRaises(plans.PlanError):
                tx.commit()

            self.assertFalse((plan_dir / "context.md").exists())
            self.assertFalse((plan_dir / "manifest.md").exists())
            self.assertEqual(
                ["blocker"], sorted(p.name for p in plan_dir.iterdir())
            )

    def test_transaction_overwrite_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "context.md").write_text("original")
            tx = authoring.PlanTransaction(plan_dir)
            tx.stage("context.md", "updated")
            tx.commit()
            self.assertEqual("updated", (plan_dir / "context.md").read_text())

    # -- readiness refresh ---------------------------------------------------

    def test_refresh_readiness_matches_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = self._author_plan(Path(tmp))
            metadata = authoring.refresh_readiness_metadata(
                plan_dir, checked_at="2026-07-10T12:00:00+00:00"
            )
            self.assertEqual("draft", metadata.state)
            self.assertEqual(
                plans.stable_readiness_sha256(plan_dir), metadata.readiness_sha256
            )
            on_disk = plans.read_readiness_metadata(plan_dir)
            self.assertEqual(metadata, on_disk)

    # -- golden end-to-end determinism --------------------------------------

    def _author_plan(self, plan_dir: Path) -> Path:
        """Author a two-session plan directory deterministically; return its path."""
        spec = authoring.ManifestSpec(
            plan_title="My Plan",
            source_path="/plans/PLAN.md",
            output_dir="/plans/out",
            plan_id="aaaa-bbbb",
            source_fingerprint="f" * 64,
            content_sha256="c" * 64,
            sessions=(
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("1"), title="First"
                ),
                authoring.ManifestSession(
                    session_id=plans.parse_session_id("2"), title="Second"
                ),
            ),
            split_date="2026-07-10",
            readiness=plans.ReadinessMetadata(
                state="draft", readiness_sha256="0" * 64, checked_at="pending"
            ),
            skill_prefix="/",
        )
        tx = authoring.PlanTransaction(plan_dir)
        tx.stage("manifest.md", authoring.render_manifest(spec))
        tx.stage("context.md", authoring.render_context(["# My Plan", "", "ctx"], "/plans/PLAN.md"))
        tx.stage("progress.md", authoring.render_progress_skeleton())
        for sid, title in (("1", "First"), ("2", "Second")):
            tx.stage(
                f"session-{sid}.md",
                authoring.render_session_file(
                    session_id=sid,
                    title=title,
                    instruction_lines=["Do the work."],
                    is_first=(sid == "1"),
                    previous_session_id=("1" if sid == "2" else None),
                    contract=self._command_contract(sid),
                ),
            )
        tx.commit()
        return plan_dir

    def test_authored_plan_is_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = self._author_plan(Path(tmp))
            authoring.refresh_readiness_metadata(
                plan_dir, checked_at="2026-07-10T12:00:00+00:00"
            )
            snapshot = plans.inspect_plan(plan_dir)
            self.assertEqual(("1", "2"), snapshot.session_ids)
            self.assertEqual(
                {"1": "pending", "2": "pending"}, snapshot.status_by_id
            )
            for session in snapshot.sessions:
                contract = contracts.read_completion_contract(
                    plan_dir, session.session_id
                )
                self.assertIsNotNone(contract)

    def test_authoring_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first_dir = self._author_plan(Path(first_tmp))
            second_dir = self._author_plan(Path(second_tmp))
            for name in ("manifest.md", "context.md", "progress.md", "session-1.md", "session-2.md"):
                self.assertEqual(
                    (first_dir / name).read_text(),
                    (second_dir / name).read_text(),
                    name,
                )


class PlanAuthoringResplitTests(unittest.TestCase):
    """Split/resplit migration onto `agent_session_loop.authoring` (Session 12)."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.resplit = _load_script_module(
            "resplit_session_module",
            "skills",
            "session-resplit",
            "scripts",
            "resplit-session.py",
        )
        cls.capture = _load_script_module(
            "capture_inline_plan_module",
            "skills",
            "session-split",
            "scripts",
            "capture-inline-plan.py",
        )
        cls.resolver = _load_script_module(
            "resolve_plan_output_module",
            "skills",
            "session-split",
            "scripts",
            "resolve-plan-output.py",
        )

    # -- shared structural operations ----------------------------------------

    def test_replace_previous_session_reference_repoints_line(self) -> None:
        text = (
            "# Session 3: Wrap\n\n"
            + authoring.previous_session_reference_line("2")
            + "\n\nBody.\n"
        )
        updated, changed = authoring.replace_previous_session_reference(text, "2", "2.3")
        self.assertTrue(changed)
        self.assertIn(authoring.previous_session_reference_line("2.3"), updated)
        self.assertNotIn(authoring.previous_session_reference_line("2"), updated)

        unchanged, changed_again = authoring.replace_previous_session_reference(
            updated, "2", "2.3"
        )
        self.assertFalse(changed_again)
        self.assertEqual(updated, unchanged)

    def test_replace_session_status_rows_updates_only_status_tables(self) -> None:
        text = "\n".join(
            [
                "| # | Title | Status | File |",
                "| --- | --- | --- | --- |",
                "| 1 | Setup | complete | session-1.md |",
                "| 2 | Parent | pending | session-2.md |",
                "",
                "| Session | Title | Status |",
                "|---|---|---|",
                "| S2 | Parent | pending |",
                "",
                "| Wave | Sessions | Notes |",
                "|---|---|---|",
                "| 2 | 2 | untouched |",
                "",
            ]
        )
        replacements = (
            authoring.ManifestSession(
                session_id=plans.parse_session_id("2.1"), title="Parent: A"
            ),
            authoring.ManifestSession(
                session_id=plans.parse_session_id("2.2"), title="Parent: B"
            ),
        )
        updated, changed = authoring.replace_session_status_rows(text, "2", replacements)
        self.assertTrue(changed)
        self.assertIn("| 2.1 | Parent: A | pending | session-2.1.md |", updated)
        self.assertIn("| 2.2 | Parent: B | pending | session-2.2.md |", updated)
        self.assertNotIn("| 2 | Parent | pending | session-2.md |", updated)
        self.assertIn("| 2.1 | Parent: A | pending |", updated)
        # Tables without a Status column are never rewritten.
        self.assertIn("| 2 | 2 | untouched |", updated)

    def test_replace_manifest_sessions_count_rewrites_metadata_line(self) -> None:
        text = "# Manifest\n\n**Sessions**: 3\n"
        self.assertIn(
            "**Sessions**: 5",
            authoring.replace_manifest_sessions_count(text, 5),
        )

    def test_contract_from_payload_round_trips_and_fails_closed(self) -> None:
        contract = authoring.contract_from_payload(
            "2.1",
            ["Child lands."],
            {
                "session": "2.1",
                "checks": [
                    {
                        "name": "suite",
                        "type": "command",
                        "argv": ["python3", "-m", "unittest", "-q"],
                        "timeout_seconds": 60,
                    }
                ],
            },
        )
        rendered = authoring.render_completion_contract(contract)
        reparsed = contracts.parse_completion_contract(rendered, session_id="2.1")
        self.assertEqual(contract, reparsed)

        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload("2.1", [], {"checks": [{"type": "command"}]})
        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload("2.1", ["Done."], {"checks": []})
        with self.assertRaises(plans.PlanError):
            authoring.contract_from_payload(
                "2.1",
                ["Done."],
                {"session": "3", "checks": [{"type": "file_exists", "path": "x"}]},
            )

    # -- split scripts are thin authoring wrappers ---------------------------

    def test_split_scripts_delegate_to_authoring(self) -> None:
        self.assertIs(self.capture.capture_inline_plan, authoring.capture_inline_plan)
        self.assertIs(self.capture.extract_plan, authoring.extract_inline_plan)
        self.assertIs(self.capture.default_base_dir, authoring.default_base_dir)
        self.assertIs(self.capture.sanitize_slug, authoring.slugify)
        self.assertIs(self.resolver.resolve_plan_output, authoring.resolve_plan_output)
        self.assertIs(self.resolver.sanitize_slug, authoring.slug_from_source_path)
        self.assertIs(self.resolver.source_fingerprint, authoring.source_fingerprint)

    # -- resplit fixture ------------------------------------------------------

    def _write_resplit_plan(
        self,
        plan_dir: Path,
        *,
        parent_contract: bool = True,
    ) -> None:
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
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
                    "| 2 | Parent | pending | session-2.md |",
                    "| 3 | Wrap | pending | session-3.md |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        contract_lines: list[str] = []
        if parent_contract:
            verify_json = json.dumps(
                {
                    "session": "2",
                    "checks": [
                        {
                            "name": "parent-suite",
                            "type": "command",
                            "argv": ["python3", "-m", "unittest", "-q"],
                            "timeout_seconds": 90,
                        }
                    ],
                },
                indent=2,
            )
            contract_lines = [
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "- Parent goal is met.",
                "",
                "### Verify",
                "",
                "```json",
                *verify_json.splitlines(),
                "```",
            ]
        sessions = {
            "1": ("Setup", "complete", ["Setup instructions."]),
            "2": (
                "Parent",
                "pending",
                ["Implement these deliverables: alpha, beta."] + contract_lines,
            ),
            "3": (
                "Wrap",
                "pending",
                [
                    authoring.previous_session_reference_line("2"),
                    "",
                    "Wrap instructions.",
                ],
            ),
        }
        for sid, (title, status, body_lines) in sessions.items():
            (plan_dir / f"session-{sid}.md").write_text(
                "\n".join(
                    [
                        f"# Session {sid}: {title}",
                        "",
                        *body_lines,
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

    # -- resplit behavior on shared authoring seams ---------------------------

    def test_resplit_derives_child_contracts_and_refreshes_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            result = self.resplit.resplit_session(
                plan_dir,
                session_id="2",
                parts=2,
                state_dir=state_dir,
                now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(["2.1", "2.2"], result["children"])
            archive_name = authoring.archive_dir_name(
                "2", datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
            )
            self.assertEqual(
                f"{authoring.ARCHIVE_ROOT_NAME}/{archive_name}",
                result["archive_dir"],
            )

            for child_id in ("2.1", "2.2"):
                child_text = (
                    plan_dir / plans.session_id_to_filename(child_id)
                ).read_text(encoding="utf-8")
                self.assertEqual(1, child_text.count("## Completion Contract"))
                contract = contracts.parse_completion_contract(
                    child_text, session_id=child_id
                )
                self.assertEqual(
                    ["parent-suite"],
                    [check.name for check in contract.verify.checks],
                )

            manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
            self.assertIn("**Sessions**: 4", manifest)
            metadata = plans.read_readiness_metadata(plan_dir)
            self.assertEqual("draft", metadata.state)
            self.assertEqual(
                plans.stable_readiness_sha256(plan_dir), metadata.readiness_sha256
            )
            self.assertEqual(
                metadata.readiness_sha256, result["readiness"]["readiness_sha256"]
            )

            next_session = (plan_dir / "session-3.md").read_text(encoding="utf-8")
            self.assertIn(
                authoring.previous_session_reference_line("2.2"), next_session
            )

    def test_resplit_without_parent_contract_rejects_no_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir, parent_contract=False)

            with self.assertRaises(self.resplit.ResplitError) as raised:
                self.resplit.resplit_session(
                    plan_dir, session_id="2", parts=2, state_dir=state_dir
                )

            self.assertEqual("no-contract", raised.exception.reason)
            self.assertTrue((plan_dir / "session-2.md").is_file())
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / authoring.ARCHIVE_ROOT_NAME).exists())

    def test_resplit_rejects_ambiguous_check_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)

            with self.assertRaises(self.resplit.ResplitError) as raised:
                self.resplit.resplit_session(
                    plan_dir,
                    session_id="2",
                    child_specs=[
                        {
                            "title": "Alpha",
                            "instructions": "Do alpha.",
                            "done_when": ["Alpha lands."],
                            "inherit_checks": ["missing-check"],
                        },
                        {
                            "title": "Beta",
                            "instructions": "Do beta.",
                            "done_when": ["Beta lands."],
                            "inherit_checks": ["parent-suite"],
                        },
                    ],
                    state_dir=state_dir,
                )

            self.assertEqual("ambiguous-contract", raised.exception.reason)
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / authoring.ARCHIVE_ROOT_NAME).exists())

    def test_resplit_rolls_back_archive_when_transaction_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            self._write_resplit_plan(plan_dir)
            parent_text = (plan_dir / "session-2.md").read_text(encoding="utf-8")

            with mock.patch.object(
                authoring.PlanTransaction,
                "commit",
                side_effect=plans.PlanError("simulated commit failure"),
            ):
                with self.assertRaises(plans.PlanError):
                    self.resplit.resplit_session(
                        plan_dir,
                        session_id="2",
                        parts=2,
                        state_dir=state_dir,
                        now=datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
                    )

            self.assertEqual(
                parent_text,
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )
            self.assertFalse((plan_dir / "session-2.1.md").exists())
            self.assertFalse((plan_dir / authoring.ARCHIVE_ROOT_NAME).exists())
            # The plan is still resplittable after rollback (lock released,
            # parent restored), so a retry succeeds.
            retry = self.resplit.resplit_session(
                plan_dir, session_id="2", parts=2, state_dir=state_dir
            )
            self.assertTrue(retry["ok"])


if __name__ == "__main__":
    unittest.main()
