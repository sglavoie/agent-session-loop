# mypy: ignore-errors
from .asl_startup_context_support import *


class InstalledStartupContextTests(unittest.TestCase):
    def build_home(self, temp_dir: str) -> tuple[Path, Path]:
        home = Path(temp_dir) / "home"
        fake_bin = home / ".local/bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "asl").symlink_to(REPO_ROOT / "bin/.local/bin/asl")

        skill_dirs = [
            path
            for path in (REPO_ROOT / "skills").iterdir()
            if path.is_dir()
            and (
                (path / "SKILL.md").exists()
                or path.name in install_verify_common.SUPPORT_DIRS
            )
        ]
        for root in HARNESS_ROOT_DIRS:
            install_root = home / root / "skills"
            install_root.mkdir(parents=True)
            for skill_dir in skill_dirs:
                (install_root / skill_dir.name).symlink_to(
                    skill_dir, target_is_directory=True
                )

        for target in startup_context.context_targets(fixture_roots(home)):
            path = home / target.installed_path
            path.write_text(
                startup_context.render_block(target.harness) + "\n", encoding="utf-8"
            )
        return home, fake_bin

    def verify(self, home: Path, fake_bin: Path) -> dict[str, Any]:
        return install_verify.verify_install(
            repo_root=REPO_ROOT,
            home=home,
            path_env=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
            env=HARNESS_ENV,
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

    def test_verification_covers_only_discovered_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            for root in (".claude-personal", ".copilot"):
                shutil.rmtree(home / root)

            payload = self.verify(home, fake_bin)

            self.assertTrue(payload["ok"], payload["failures"])
            self.assertEqual(
                [root["path"] for root in payload["harness_roots"]["found"]],
                [".claude", ".codex"],
            )
            details = " ".join(check["detail"] for check in payload["checks"])
            self.assertNotIn(".claude-personal", details)
            self.assertNotIn(".copilot", details)

    def test_home_without_any_harness_root_fails_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir) / "home"
            fake_bin = home / ".local/bin"
            fake_bin.mkdir(parents=True)
            (fake_bin / "asl").symlink_to(REPO_ROOT / "bin/.local/bin/asl")

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            self.assertIn(
                "harness-roots", {failure["name"] for failure in payload["failures"]}
            )

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

    def test_missing_copilot_context_file_is_named_missing_with_remedy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home, fake_bin = self.build_home(temp_dir)
            missing = home / ".copilot/copilot-instructions.md"
            missing.unlink()

            payload = self.verify(home, fake_bin)

            self.assertFalse(payload["ok"])
            detail = next(
                failure["detail"]
                for failure in payload["failures"]
                if failure["name"] == "startup-context"
            )
            self.assertTrue(
                detail.endswith(
                    ".copilot/copilot-instructions.md is missing; run "
                    "`just sync-skills` to render the startup card"
                ),
                detail,
            )
            self.assertNotIn("Errno", detail)

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
                install_verify_skills.ASL_SKILL_REQUIRED_TEXT, encoding="utf-8"
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
