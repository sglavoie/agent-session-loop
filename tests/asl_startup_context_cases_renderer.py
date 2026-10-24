# mypy: ignore-errors
from .asl_startup_context_support import *


class StartupContextRendererTests(unittest.TestCase):
    def build_repo(self, temp_dir: str) -> Path:
        repo_root = Path(temp_dir) / "repo"
        for root in HARNESS_ROOT_DIRS:
            (repo_root / root).mkdir(parents=True)
        codex = repo_root / ".codex/AGENTS.md"
        codex.write_text(CODEX_DEFAULTS, encoding="utf-8")
        (repo_root / ".claude/CLAUDE.md").write_text(CLAUDE_DEFAULTS, encoding="utf-8")
        return repo_root

    def test_sync_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)

            first = startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            snapshot = {
                record["path"]: (repo_root / record["path"]).read_bytes()
                for record in first["files"]
            }
            second = startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            self.assertEqual(second["written"], [])
            for path, content in snapshot.items():
                self.assertEqual((repo_root / path).read_bytes(), content, path)

    def test_sync_preserves_unrelated_startup_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            codex = (repo_root / ".codex/AGENTS.md").read_text(encoding="utf-8")
            claude = (repo_root / ".claude/CLAUDE.md").read_text(encoding="utf-8")
            self.assertTrue(codex.startswith(CODEX_DEFAULTS.rstrip("\n")), codex)
            self.assertTrue(claude.startswith(CLAUDE_DEFAULTS.rstrip("\n")), claude)
            self.assertIn("<!-- CODEGRAPH_END -->", codex)
            self.assertIn("<!-- CODEGRAPH_END -->", claude)
            self.assertIn("`sed` here is GNU `sed`.", claude)

    def test_rerendering_replaces_the_block_without_appending_a_second_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            stale = codex.read_text(encoding="utf-8").replace(
                "- status: `asl status PLAN_DIR --json`",
                "- status: `asl status PLAN_DIR`",
            )
            codex.write_text(stale, encoding="utf-8")

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(rendered.count(startup_context.END_MARKER), 1)
            self.assertIn(startup_context.start_marker("codex"), rendered)
            self.assertTrue(rendered.startswith(CODEX_DEFAULTS.rstrip("\n")), rendered)

    def test_malformed_start_marker_is_replaced_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    startup_context.start_marker("codex"),
                    f"{startup_context.START_MARKER_PREFIX} not-a-digest -->",
                ),
                encoding="utf-8",
            )

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

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
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            block = startup_context.render_block("codex")
            codex.write_text(
                codex.read_text(encoding="utf-8")
                + "\nTrailing note.\n\n"
                + block
                + "\n",
                encoding="utf-8",
            )

            payload = startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            self.assertIn(".codex/AGENTS.md", payload["written"])
            rendered = codex.read_text(encoding="utf-8")
            self.assertEqual(
                startup_context.managed_blocks(rendered), [block], rendered
            )
            self.assertIn("Trailing note.", rendered)
            self.assertTrue(rendered.startswith(CODEX_DEFAULTS.rstrip("\n")), rendered)

    def test_orphaned_start_marker_replaces_the_stale_card_body(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            orphaned = (
                codex.read_text(encoding="utf-8")
                .replace(f"\n{startup_context.END_MARKER}", "")
                .replace(
                    "- status: `asl status PLAN_DIR --json`",
                    "- status: `asl status PLAN_DIR`",
                )
            )
            codex.write_text(orphaned + "\n## My Notes\n\nKeep me.\n", encoding="utf-8")

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

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
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
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

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

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
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
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

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

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
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            suffix = "\nTrailing note.\n\n\n"
            codex.write_text(
                codex.read_text(encoding="utf-8").replace(
                    "- Route: `asl supervisor-guide`",
                    "- Route: `asl nonexistent-command`",
                )
                + suffix,
                encoding="utf-8",
            )

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            block = startup_context.render_block("codex")
            self.assertEqual(
                codex.read_text(encoding="utf-8"),
                CODEX_DEFAULTS.rstrip("\n") + "\n\n" + block + "\n" + suffix,
            )

    def test_blank_only_eof_suffix_is_preserved_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            corrupted = codex.read_text(encoding="utf-8").replace(
                "- Route: `asl supervisor-guide`",
                "- Route: `asl nonexistent-command`",
            )
            codex.write_text(corrupted.rstrip("\n") + "\n\n\n", encoding="utf-8")

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

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
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            corrupted = codex.read_text(encoding="utf-8").replace(
                "- Route: `asl supervisor-guide`",
                "- Route: `asl nonexistent-command`",
            )
            codex.write_text(corrupted, encoding="utf-8")

            self.assertFalse(
                startup_context.sync(
                    repo_root, home=repo_root, env=HARNESS_ENV, check=True
                )["ok"]
            )
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

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

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            personal = (repo_root / ".claude-personal/CLAUDE.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                personal, startup_context.render_block("claude") + "\n", personal
            )

    def test_personal_claude_file_keeps_pre_existing_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            personal = repo_root / ".claude-personal/CLAUDE.md"
            personal.write_text("# Personal\n\nAlways answer in French.\n", "utf-8")

            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)

            rendered = personal.read_text(encoding="utf-8")
            self.assertTrue(rendered.startswith("# Personal\n"), rendered)
            self.assertIn("Always answer in French.", rendered)
            self.assertIn(startup_context.start_marker("claude"), rendered)

    def test_check_reports_stale_output_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = self.build_repo(temp_dir)
            startup_context.sync(repo_root, home=repo_root, env=HARNESS_ENV)
            codex = repo_root / ".codex/AGENTS.md"
            stale = codex.read_text(encoding="utf-8").replace(
                "## ASL Supervision", "## Outdated Heading"
            )
            codex.write_text(stale, encoding="utf-8")

            payload = startup_context.sync(
                repo_root, home=repo_root, env=HARNESS_ENV, check=True
            )

            self.assertFalse(payload["ok"])
            self.assertEqual(payload["stale"], [".codex/AGENTS.md"])
            self.assertEqual(payload["written"], [])
            self.assertEqual(codex.read_text(encoding="utf-8"), stale)

    def test_checked_in_generated_blocks_are_current(self) -> None:
        payload = startup_context.sync(REPO_ROOT, check=True, reference_only=True)

        self.assertTrue(payload["ok"], payload["stale"])

    def test_card_stays_under_the_always_loaded_line_ceiling(self) -> None:
        for harness in startup_context.CONTEXT_HARNESSES:
            block = startup_context.render_block(harness)
            self.assertLessEqual(len(block.splitlines()), 20, harness)

    def test_absent_harness_roots_are_skipped_and_never_created(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            (home / ".codex").mkdir(parents=True)

            payload = startup_context.sync(REPO_ROOT, home=home, check=True, env={})
            written = startup_context.sync(REPO_ROOT, home=home, env={})

            self.assertEqual(
                [record["path"] for record in payload["files"]][:1],
                [".codex/AGENTS.md"],
            )
            self.assertIn(".codex/AGENTS.md", written["written"])
            self.assertFalse((home / ".claude").exists())
            self.assertFalse((home / ".claude-personal").exists())
            self.assertEqual(
                [root["path"] for root in payload["harness_roots"]["found"]],
                [".codex"],
            )
            self.assertIn(
                {"path": "~/.claude", "reason": "not present"},
                payload["harness_roots"]["skipped"],
            )

    def test_extra_claude_roots_come_from_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            (home / ".claude-work").mkdir(parents=True)

            startup_context.sync(
                REPO_ROOT,
                home=home,
                env={"CLAUDE_CONFIG_DIR": str(home / ".claude-work")},
            )

            self.assertEqual(
                (home / ".claude-work/CLAUDE.md").read_text(encoding="utf-8"),
                startup_context.render_block("claude") + "\n",
            )

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
