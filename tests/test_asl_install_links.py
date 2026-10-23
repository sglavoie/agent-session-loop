"""The checkout installer: wrappers and skills linked into a home, atomically."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("asl_install", REPO_ROOT / "install.py")
assert _spec is not None and _spec.loader is not None
install_links = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install_links)


class InstallLinksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        for name in install_links.WRAPPERS:
            wrapper = self.repo / "bin/.local/bin" / name
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        for name in ("asl", "session-run"):
            (self.repo / "skills" / name).mkdir(parents=True)
            (self.repo / "skills" / name / "SKILL.md").write_text("x\n")
        (self.repo / "skills/references").mkdir()

    def install(self, **kwargs: bool) -> list[str]:
        return install_links.install(self.home, self.repo, **kwargs)

    def test_links_wrappers_and_every_skill_into_each_harness(self) -> None:
        self.install()

        for name in install_links.WRAPPERS:
            link = self.home / ".local/bin" / name
            self.assertEqual(
                os.readlink(link), os.fspath(self.repo / "bin/.local/bin" / name)
            )
        for install_dir in install_links.SKILL_INSTALL_DIRS:
            for name in ("asl", "session-run", "references"):
                link = self.home / install_dir / name
                self.assertEqual(
                    os.readlink(link), os.fspath(self.repo / "skills" / name)
                )

    def test_a_second_install_changes_nothing(self) -> None:
        self.install()

        self.assertEqual(self.install(), [])

    def test_takes_over_a_link_owned_by_another_checkout(self) -> None:
        other = self.root / "other/skills/asl"
        other.mkdir(parents=True)
        link = self.home / ".claude/skills/asl"
        link.parent.mkdir(parents=True)
        link.symlink_to(other)

        changes = self.install()

        self.assertEqual(os.readlink(link), os.fspath(self.repo / "skills/asl"))
        self.assertIn(f"relinked: {link} -> {self.repo / 'skills/asl'}", changes)
        self.assertTrue(other.is_dir())

    def test_refuses_to_shadow_real_content(self) -> None:
        real = self.home / ".codex/skills/asl"
        real.mkdir(parents=True)

        with self.assertRaises(install_links.InstallError):
            self.install()

        self.assertTrue(real.is_dir())
        self.assertFalse(real.is_symlink())

    def test_prunes_links_whose_skill_is_gone(self) -> None:
        self.install()
        (self.repo / "skills/session-run/SKILL.md").unlink()
        (self.repo / "skills/session-run").rmdir()

        self.install()

        for install_dir in install_links.SKILL_INSTALL_DIRS:
            self.assertFalse((self.home / install_dir / "session-run").is_symlink())

    def test_leaves_unrelated_links_alone(self) -> None:
        unrelated = self.home / ".claude/skills/bn"
        unrelated.parent.mkdir(parents=True)
        unrelated.symlink_to(self.root / "dotfiles/skills/bn")

        self.install()

        self.assertTrue(unrelated.is_symlink())

    def test_rejects_a_skill_directory_without_skill_md(self) -> None:
        (self.repo / "skills/stray").mkdir()

        with self.assertRaises(install_links.InstallError):
            self.install()

        self.assertFalse((self.home / ".claude/skills").exists())

    def test_skills_only_leaves_the_wrappers_alone(self) -> None:
        self.install(skills_only=True)

        self.assertFalse((self.home / ".local/bin/asl").exists())
        self.assertTrue((self.home / ".claude/skills/asl").is_symlink())

    def test_dry_run_touches_nothing(self) -> None:
        changes = self.install(dry_run=True)

        self.assertTrue(changes)
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
