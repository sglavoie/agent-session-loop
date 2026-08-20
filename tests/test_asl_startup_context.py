"""Startup-context renderer and installed-context verification regressions."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import install_verify, startup_context
from tests.support import REPO_ROOT

CODEX_DEFAULTS = "\n".join(
    [
        "# Global Codex Instructions",
        "",
        "## Interaction Defaults",
        "",
        "- Treat unanswered questions as blocking.",
        "",
        "<!-- CODEGRAPH_START -->",
        "## CodeGraph",
        "",
        "Use codegraph before grep.",
        "<!-- CODEGRAPH_END -->",
        "",
    ]
)

CLAUDE_DEFAULTS = "\n".join(
    [
        "@RTK.md",
        "",
        "<!-- CODEGRAPH_START -->",
        "## CodeGraph",
        "",
        "Use codegraph before grep.",
        "<!-- CODEGRAPH_END -->",
        "",
    ]
)


class StartupContextRendererTests(unittest.TestCase):
    def build_repo(self, temp_dir: str) -> Path:
        repo_root = Path(temp_dir) / "repo"
        for target in startup_context.CONTEXT_TARGETS:
            (repo_root / target.package_path).parent.mkdir(parents=True, exist_ok=True)
        codex = repo_root / "codex/.codex/AGENTS.md"
        codex.write_text(CODEX_DEFAULTS, encoding="utf-8")
        (repo_root / "claude/.claude/CLAUDE.md").write_text(
            CLAUDE_DEFAULTS, encoding="utf-8"
        )
        return repo_root

    def test_sync_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)

            first = startup_context.sync(repo_root)
            snapshot = {
                record["path"]: (repo_root / record["path"]).read_bytes()
                for record in first["files"]
            }
            second = startup_context.sync(repo_root)

            self.assertEqual(second["written"], [])
            for path, content in snapshot.items():
                self.assertEqual((repo_root / path).read_bytes(), content, path)

    def test_sync_preserves_unrelated_startup_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)

            startup_context.sync(repo_root)

            codex = (repo_root / "codex/.codex/AGENTS.md").read_text(encoding="utf-8")
            claude = (repo_root / "claude/.claude/CLAUDE.md").read_text(
                encoding="utf-8"
            )
            self.assertTrue(codex.startswith(CODEX_DEFAULTS.rstrip("\n")), codex)
            self.assertTrue(claude.startswith(CLAUDE_DEFAULTS.rstrip("\n")), claude)
            self.assertIn("<!-- CODEGRAPH_END -->", codex)
            self.assertIn("<!-- CODEGRAPH_END -->", claude)
            self.assertIn("@RTK.md", claude)

    def test_rerendering_replaces_the_block_without_appending_a_second_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            stale = codex.read_text(encoding="utf-8").replace(
                "- status: `asl status PLAN_DIR --json`",
                "- status: `asl status PLAN_DIR`",
            )
            codex.write_text(stale, encoding="utf-8")

            startup_context.sync(repo_root)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(rendered.count(startup_context.END_MARKER), 1)
            self.assertIn(startup_context.start_marker("codex"), rendered)
            self.assertTrue(rendered.startswith(CODEX_DEFAULTS.rstrip("\n")), rendered)

    def test_malformed_start_marker_is_replaced_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    startup_context.start_marker("codex"),
                    f"{startup_context.START_MARKER_PREFIX} not-a-digest -->",
                ),
                encoding="utf-8",
            )

            startup_context.sync(repo_root)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered),
                [startup_context.render_block("codex")],
                rendered,
            )
            self.assertTrue(rendered.startswith(CODEX_DEFAULTS.rstrip("\n")), rendered)
            self.assertIn("<!-- CODEGRAPH_END -->", rendered)

    def test_duplicate_blocks_collapse_into_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            block = startup_context.render_block("codex")
            codex.write_text(
                codex.read_text(encoding="utf-8")
                + "\nTrailing note.\n\n"
                + block
                + "\n",
                encoding="utf-8",
            )

            payload = startup_context.sync(repo_root)

            self.assertIn("codex/.codex/AGENTS.md", payload["written"])
            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered), [block], rendered
            )
            self.assertIn("Trailing note.", rendered)
            self.assertTrue(rendered.startswith(CODEX_DEFAULTS.rstrip("\n")), rendered)

    def test_orphaned_start_marker_replaces_the_stale_card_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            orphaned = (
                codex.read_text(encoding="utf-8")
                .replace(f"\n{startup_context.END_MARKER}", "")
                .replace(
                    "- status: `asl status PLAN_DIR --json`",
                    "- status: `asl status PLAN_DIR`",
                )
            )
            codex.write_text(orphaned + "\n## My Notes\n\nKeep me.\n", encoding="utf-8")

            startup_context.sync(repo_root)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered),
                [startup_context.render_block("codex")],
                rendered,
            )
            self.assertNotIn("- status: `asl status PLAN_DIR`\n", rendered)
            self.assertEqual(rendered.count(startup_context.CARD_HEADING), 1, rendered)
            self.assertTrue(rendered.startswith(CODEX_DEFAULTS.rstrip("\n")), rendered)
            self.assertIn("<!-- CODEGRAPH_END -->", rendered)
            self.assertTrue(rendered.endswith("## My Notes\n\nKeep me.\n"), rendered)

    def test_orphaned_start_marker_with_corrupted_heading_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            orphaned = (
                codex.read_text(encoding="utf-8")
                .replace(f"\n{startup_context.END_MARKER}", "")
                .replace(startup_context.CARD_HEADING, "## ASL Supervison")
                .replace(
                    "- status: `asl status PLAN_DIR --json`",
                    "- status: `asl obsolete-command`",
                )
            )
            codex.write_text(orphaned + "\n## My Notes\n\nKeep me.\n", encoding="utf-8")

            startup_context.sync(repo_root)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered),
                [startup_context.render_block("codex")],
                rendered,
            )
            self.assertNotIn("ASL Supervison", rendered)
            self.assertNotIn("asl obsolete-command", rendered)
            self.assertTrue(rendered.endswith("## My Notes\n\nKeep me.\n"), rendered)

    def test_orphaned_start_marker_with_missing_heading_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            orphaned = (
                codex.read_text(encoding="utf-8")
                .replace(f"\n{startup_context.END_MARKER}", "")
                .replace(startup_context.CARD_HEADING + "\n", "")
                .replace(
                    "- status: `asl status PLAN_DIR --json`",
                    "- status: `asl obsolete-command`",
                )
            )
            codex.write_text(orphaned + "\n## My Notes\n\nKeep me.\n", encoding="utf-8")

            startup_context.sync(repo_root)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered),
                [startup_context.render_block("codex")],
                rendered,
            )
            self.assertNotIn("asl obsolete-command", rendered)
            self.assertTrue(rendered.endswith("## My Notes\n\nKeep me.\n"), rendered)

    def test_unrelated_trailing_bytes_including_blank_lines_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            suffix = "\nTrailing note.\n\n\n"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    "- Route: `asl supervisor-guide`",
                    "- Route: `asl nonexistent-command`",
                )
                + suffix,
                encoding="utf-8",
            )

            startup_context.sync(repo_root)

            block = startup_context.render_block("codex")
            self.assertEqual(
                codex.read_text(encoding="utf-8"),
                CODEX_DEFAULTS.rstrip("\n") + "\n\n" + block + "\n" + suffix,
            )

    def test_blank_only_eof_suffix_is_preserved_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            corrupted = codex.read_text(encoding="utf-8").replace(
                "- Route: `asl supervisor-guide`",
                "- Route: `asl nonexistent-command`",
            )
            codex.write_text(corrupted.rstrip("\n") + "\n\n\n", encoding="utf-8")

            startup_context.sync(repo_root)

            self.assertEqual(
                codex.read_text(encoding="utf-8"),
                CODEX_DEFAULTS.rstrip("\n")
                + "\n\n"
                + startup_context.render_block("codex")
                + "\n\n\n",
            )

    def test_corrupted_body_under_the_original_marker_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            corrupted = codex.read_text(encoding="utf-8").replace(
                "- Route: `asl supervisor-guide`",
                "- Route: `asl nonexistent-command`",
            )
            codex.write_text(corrupted, encoding="utf-8")

            self.assertFalse(startup_context.sync(repo_root, check=True)["ok"])
            startup_context.sync(repo_root)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered),
                [startup_context.render_block("codex")],
                rendered,
            )
            self.assertNotIn("asl nonexistent-command", rendered)

    def test_personal_claude_file_receives_only_the_card(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)

            startup_context.sync(repo_root)

            personal = (repo_root / "claude/.claude-personal/CLAUDE.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                personal, startup_context.render_block("claude") + "\n", personal
            )

    def test_personal_claude_file_keeps_pre_existing_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            personal = repo_root / "claude/.claude-personal/CLAUDE.md"
            personal.write_text("# Personal\n\nAlways answer in French.\n", "utf-8")

            startup_context.sync(repo_root)

            rendered = personal.read_text(encoding="utf-8")
            self.assertTrue(rendered.startswith("# Personal\n"), rendered)
            self.assertIn("Always answer in French.", rendered)
            self.assertIn(startup_context.start_marker("claude"), rendered)

    def test_check_reports_stale_output_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root)
            codex = repo_root / "codex/.codex/AGENTS.md"
            stale = codex.read_text(encoding="utf-8").replace(
                "## ASL Supervision", "## Outdated Heading"
            )
            codex.write_text(stale, encoding="utf-8")

            payload = startup_context.sync(repo_root, check=True)

            self.assertFalse(payload["ok"])
            self.assertEqual(payload["stale"], ["codex/.codex/AGENTS.md"])
            self.assertEqual(payload["written"], [])
            self.assertEqual(codex.read_text(encoding="utf-8"), stale)

    def test_checked_in_generated_blocks_are_current(self) -> None:
        payload = startup_context.sync(REPO_ROOT, check=True)

        self.assertTrue(payload["ok"], payload["stale"])

    def test_card_stays_under_the_always_loaded_line_ceiling(self) -> None:
        for target in startup_context.CONTEXT_TARGETS:
            block = startup_context.render_block(target.harness)
            self.assertLessEqual(len(block.splitlines()), 20, target.package_path)

    def test_card_commands_come_from_the_supervisor_guide(self) -> None:
        block = startup_context.render_block("claude")

        self.assertIn("`asl supervisor-guide`", block)
        self.assertIn(
            "asl outbox supervise PLAN_DIR --supervision-id SUPERVISION_ID", block
        )
        self.assertIn("/asl asl run PLAN_DIR", block)
        self.assertNotIn("$asl", block)

    def test_card_routes_unlisted_questions_to_the_guide(self) -> None:
        block = startup_context.render_block("claude")

        self.assertIn("Run the route command for anything not listed", block)
        self.assertIn("detached helper launch", block)
        self.assertIn("`asl inbox resolve`", block)
        self.assertIn("leaf `--help`", block)


class InstalledStartupContextTests(unittest.TestCase):
    def build_home(self, temp_dir: str) -> tuple[Path, Path]:
        home = Path(temp_dir) / "home"
        fake_bin = home / ".local/bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "asl").symlink_to(REPO_ROOT / "bin/.local/bin/asl")

        skill_dirs = [
            path
            for path in (REPO_ROOT / "skills").iterdir()
            if path.is_dir() and (path / "SKILL.md").exists()
        ]
        for install_dir in install_verify.SKILL_INSTALL_DIRS:
            install_root = home / install_dir
            install_root.mkdir(parents=True)
            for skill_dir in skill_dirs:
                (install_root / skill_dir.name).symlink_to(
                    skill_dir, target_is_directory=True
                )

        for target in startup_context.CONTEXT_TARGETS:
            path = home / target.installed_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                startup_context.render_block(target.harness) + "\n", encoding="utf-8"
            )
        return home, fake_bin

    def verify(self, home: Path, fake_bin: Path) -> dict[str, Any]:
        return install_verify.verify_install(
            repo_root=REPO_ROOT,
            home=home,
            path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        )

    def test_rendered_home_passes_install_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)

            payload = self.verify(home, fake_bin)

            self.assertTrue(payload["ok"], payload["failures"])
            names = {check["name"] for check in payload["checks"]}
            self.assertIn("startup-context", names)
            self.assertIn("asl-skill", names)
            self.assertIn("asl-reference", names)
            self.assertIn("asl-helper", names)

    def test_missing_context_file_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            (home / ".claude/CLAUDE.md").unlink()

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            self.assertTrue(
                any(
                    failure["name"] == "startup-context"
                    for failure in payload["failures"]
                ),
                payload["failures"],
            )

    def test_stale_context_marker_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    startup_context.start_marker("codex"),
                    f"{startup_context.START_MARKER_PREFIX} 000000000000 -->",
                ),
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            stale = next(
                failure
                for failure in payload["failures"]
                if failure["name"] == "startup-context"
            )
            self.assertIn("stale ASL block", stale["detail"])

    def test_corrupted_body_under_the_expected_marker_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    "- Route: `asl supervisor-guide`",
                    "- Route: `asl nonexistent-command`",
                ),
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "startup-context"
            )
            self.assertIn("modified ASL block body", failure["detail"])

    def test_orphaned_start_marker_over_stale_card_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8")
                .replace(f"\n{startup_context.END_MARKER}", "")
                .replace(
                    "- status: `asl status PLAN_DIR --json`",
                    "- status: `asl status PLAN_DIR`",
                ),
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "startup-context"
            )
            self.assertIn("modified ASL block body", failure["detail"])

    def test_corrupted_heading_orphan_fails_install_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8")
                .replace(f"\n{startup_context.END_MARKER}", "")
                .replace(startup_context.CARD_HEADING, "## ASL Supervison")
                .replace(
                    "- status: `asl status PLAN_DIR --json`",
                    "- status: `asl obsolete-command`",
                ),
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "startup-context"
            )
            self.assertIn("modified ASL block body", failure["detail"])

    def test_unmanaged_card_copy_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8")
                + "\n"
                + startup_context.CARD_HEADING
                + "\n\n- status: `asl status PLAN_DIR`\n",
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "startup-context"
            )
            self.assertIn("ASL card text outside", failure["detail"])

    def test_duplicate_context_blocks_fail_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            stale = codex.read_text(encoding="utf-8").replace(
                startup_context.start_marker("codex"),
                f"{startup_context.START_MARKER_PREFIX} 000000000000 -->",
            )
            codex.write_text(
                stale + "\n" + codex.read_text(encoding="utf-8"), encoding="utf-8"
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "startup-context"
            )
            self.assertIn("2 ASL startup blocks", failure["detail"])

    def test_malformed_context_marker_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            codex = home / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    startup_context.start_marker("codex"),
                    f"{startup_context.START_MARKER_PREFIX} not-a-digest -->",
                ),
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "startup-context"
            )
            self.assertIn("stale ASL block", failure["detail"])

    def test_corrupted_reference_body_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            installed = home / ".codex/skills/asl"
            installed.unlink()
            installed.mkdir()
            (installed / "SKILL.md").write_text(
                install_verify.ASL_SKILL_REQUIRED_TEXT, encoding="utf-8"
            )
            reference = installed / "references/supervisor-context.md"
            reference.parent.mkdir(parents=True)
            reference.write_text(
                startup_context.render_reference().replace(
                    "- Route: `asl supervisor-guide`",
                    "- Route: `asl nonexistent-command`",
                ),
                encoding="utf-8",
            )

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            failure = next(
                check
                for check in payload["failures"]
                if check["name"] == "asl-reference"
            )
            self.assertIn("modified ASL block body", failure["detail"])

    def test_skill_without_the_reference_instruction_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            installed = home / ".codex/skills/asl"
            installed.unlink()
            installed.mkdir()
            (installed / "SKILL.md").write_text("# stale asl\n", encoding="utf-8")

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            self.assertTrue(
                any(failure["name"] == "asl-skill" for failure in payload["failures"]),
                payload["failures"],
            )


if __name__ == "__main__":
    unittest.main()
