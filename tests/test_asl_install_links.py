"""The checkout installer: wrappers and skills linked into a home, atomically."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ASL_TEST_PORTABLE = True

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The harness roots a stock machine with every harness installed carries.
DEFAULT_SKILL_DIRS = (".claude/skills", ".codex/skills", ".copilot/skills")

#: An environment that names no extra Claude root, so an ambient
#: ``CLAUDE_CONFIG_DIR`` on the developer's machine cannot leak in.
NO_EXTRA_ROOTS: dict[str, str] = {}

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
        for root in (".claude", ".codex", ".copilot"):
            (self.home / root).mkdir(parents=True)

    def install(self, env: dict[str, str] | None = None, **kwargs: bool) -> list[str]:
        return install_links.install(
            self.home,
            self.repo,
            env=NO_EXTRA_ROOTS if env is None else env,
            **kwargs,
        )

    def test_links_wrappers_and_every_skill_into_each_harness(self) -> None:
        self.install()

        for name in install_links.WRAPPERS:
            link = self.home / ".local/bin" / name
            self.assertEqual(
                os.readlink(link), os.fspath(self.repo / "bin/.local/bin" / name)
            )
        for install_dir in DEFAULT_SKILL_DIRS:
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
        link.parent.mkdir()
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

    def test_a_conflict_in_a_later_root_changes_nothing_anywhere(self) -> None:
        real = self.home / ".copilot/skills/session-run"
        real.mkdir(parents=True)
        (self.home / ".claude/skills").mkdir()
        stale = self.home / ".claude/skills/gone"
        stale.symlink_to(self.repo / "skills/gone")

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        message = str(raised.exception)
        self.assertIn("nothing was changed", message)
        self.assertIn(f"{real} exists and is not a symlink", message)
        self.assertIn("move it aside or delete it, then re-run", message)
        self.assertEqual(raised.exception.changes, [])
        self.assertFalse((self.home / ".local").exists())
        self.assertFalse((self.home / ".claude/skills/asl").exists())
        self.assertFalse((self.home / ".codex/skills").exists())
        self.assertTrue(stale.is_symlink(), "a failed preflight prunes nothing")

    def test_every_conflict_is_named_at_once(self) -> None:
        for install_dir in (".claude/skills/asl", ".codex/skills/references"):
            (self.home / install_dir).mkdir(parents=True)

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        self.assertIn(str(self.home / ".claude/skills/asl"), str(raised.exception))
        self.assertIn(
            str(self.home / ".codex/skills/references"), str(raised.exception)
        )

    def test_a_dangling_symlink_parent_is_a_conflict_not_a_crash(self) -> None:
        local = self.home / ".local"
        local.mkdir()
        (local / "bin").symlink_to(self.root / "gone")

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        self.assertIn(f"{local / 'bin'} is not a directory", str(raised.exception))
        self.assertEqual(1, str(raised.exception).count(str(local / "bin")))
        self.assertFalse((self.home / ".claude/skills").exists())

    def test_a_regular_file_parent_is_a_conflict_not_a_crash(self) -> None:
        (self.home / ".local").write_text("not a directory\n")

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        self.assertIn(
            f"{self.home / '.local'} is not a directory", str(raised.exception)
        )

    def test_link_reports_an_unmakeable_parent_as_an_install_error(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("x\n")

        with (
            mock.patch.object(install_links, "conflict", return_value=None),
            self.assertRaises(install_links.InstallError) as raised,
        ):
            install_links.link(
                self.repo / "skills/asl", blocker / "sub/asl", dry_run=False
            )

        self.assertIn(f"cannot create {blocker / 'sub'}", str(raised.exception))

    def test_main_prints_the_changes_made_before_a_late_error(self) -> None:
        def failing_prune(*_args: object, **_kwargs: object) -> list[str]:
            raise OSError("disk on fire")

        environ = {
            name: value
            for name, value in os.environ.items()
            if name not in ("CLAUDE_CONFIG_DIR", "ASL_CLAUDE_CONFIG_DIRS")
        }
        environ["PATH"] = str(self.home / ".local/bin")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(install_links, "REPO_ROOT", self.repo),
            mock.patch.object(install_links, "prune", failing_prune),
            mock.patch.dict(os.environ, environ, clear=True),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = install_links.main(["--home", str(self.home)])

        self.assertEqual(code, 1)
        self.assertIn(f"linked: {self.home / '.local/bin/asl'}", stdout.getvalue())
        self.assertIn("2 change(s) above were made", stderr.getvalue())
        self.assertIn("disk on fire", stderr.getvalue())

    def test_a_leftover_staging_directory_does_not_stop_a_relink(self) -> None:
        # A killed install of an older version left its fixed-name staging
        # entry behind as a directory; the preflight never looks there.
        self.install()
        skills = self.home / ".claude/skills"
        (skills / ".asl.asl-install").mkdir()
        (skills / "asl").unlink()
        (skills / "asl").symlink_to(self.root / "elsewhere")

        self.install()

        self.assertEqual(
            os.fspath(self.repo / "skills/asl"), os.readlink(skills / "asl")
        )
        self.assertEqual(
            [".asl.asl-install"],
            sorted(
                entry.name for entry in skills.iterdir() if entry.name.startswith(".")
            ),
        )

    def test_a_failed_swap_leaves_no_staging_link_behind(self) -> None:
        destination = self.home / ".claude/skills/asl"
        with (
            mock.patch.object(
                install_links.os, "replace", side_effect=OSError(5, "EIO")
            ),
            self.assertRaises(OSError),
        ):
            install_links.link(self.repo / "skills/asl", destination, dry_run=False)
        self.assertEqual([], list(destination.parent.iterdir()))

    def test_prunes_links_whose_skill_is_gone(self) -> None:
        self.install()
        (self.repo / "skills/session-run/SKILL.md").unlink()
        (self.repo / "skills/session-run").rmdir()

        self.install()

        for install_dir in DEFAULT_SKILL_DIRS:
            self.assertFalse((self.home / install_dir / "session-run").is_symlink())

    def test_leaves_unrelated_links_alone(self) -> None:
        unrelated = self.home / ".claude/skills/bn"
        unrelated.parent.mkdir()
        unrelated.symlink_to(self.root / "dotfiles/skills/bn")

        self.install()

        self.assertTrue(unrelated.is_symlink())

    def test_rejects_a_skill_directory_without_skill_md(self) -> None:
        (self.repo / "skills/stray").mkdir()
        (self.repo / "skills/stray/notes.md").write_text("x\n")

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        self.assertFalse((self.home / ".claude/skills").exists())
        message = str(raised.exception)
        self.assertIn("SUPPORT_DIRS", message)
        self.assertIn("delete it if it is left over from a removed skill", message)

    def make_leftover(self, name: str) -> Path:
        """What removing a skill from git leaves: ignored cache residue only."""
        leftover = self.repo / "skills" / name
        (leftover / "scripts/__pycache__").mkdir(parents=True)
        (leftover / "scripts/__pycache__/helper.cpython-314.pyc").write_bytes(b"x")
        (leftover / ".DS_Store").write_bytes(b"x")
        return leftover

    def test_a_cache_only_leftover_directory_is_skipped_not_fatal(self) -> None:
        self.make_leftover("removed")
        (self.repo / "skills/empty").mkdir()

        self.install()

        self.assertTrue((self.home / ".claude/skills/asl").is_symlink())
        for install_dir in DEFAULT_SKILL_DIRS:
            for name in ("removed", "empty"):
                self.assertFalse((self.home / install_dir / name).is_symlink())

    def test_a_leftover_with_real_content_is_still_rejected(self) -> None:
        leftover = self.make_leftover("half")
        (leftover / "scripts/helper.py").write_text("x\n")

        with self.assertRaises(install_links.InstallError):
            self.install()

    def test_prunes_links_to_a_skill_reduced_to_cache_residue(self) -> None:
        self.install()
        skill = self.repo / "skills/session-run"
        (skill / "SKILL.md").unlink()
        (skill / "__pycache__").mkdir()
        (skill / "__pycache__/x.cpython-314.pyc").write_bytes(b"x")

        changes = self.install()

        for install_dir in DEFAULT_SKILL_DIRS:
            link = self.home / install_dir / "session-run"
            self.assertFalse(link.is_symlink())
            self.assertIn(f"removed stale link: {link}", changes)
        self.assertTrue((self.home / ".claude/skills/asl").is_symlink())

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        environ = {
            name: value
            for name, value in os.environ.items()
            if name not in ("CLAUDE_CONFIG_DIR", "ASL_CLAUDE_CONFIG_DIRS")
        }
        environ["PATH"] = str(self.home / ".local/bin")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(install_links, "REPO_ROOT", self.repo),
            mock.patch.dict(os.environ, environ, clear=True),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = install_links.main(["--home", str(self.home), *argv])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_main_notes_a_skipped_leftover_directory(self) -> None:
        self.make_leftover("removed")

        code, _stdout, stderr = self.run_main("--dry-run")

        self.assertEqual(code, 0)
        self.assertIn("skipped skills/removed", stderr)
        self.assertIn("delete it", stderr)

    def test_main_names_sync_skills_for_a_missing_context_file(self) -> None:
        (self.home / ".claude/CLAUDE.md").write_text("x\n")
        (self.home / ".codex/AGENTS.md").write_text("x\n")

        with mock.patch.object(install_links.Path, "home", return_value=self.home):
            code, _stdout, stderr = self.run_main("--dry-run")

        self.assertEqual(code, 0)
        missing = self.home / ".copilot/copilot-instructions.md"
        self.assertIn(f"{missing} does not exist yet", stderr)
        self.assertIn("just sync-skills", stderr)
        self.assertNotIn("startup_context --home", stderr)
        self.assertNotIn("CLAUDE.md does not exist", stderr)
        self.assertNotIn("AGENTS.md does not exist", stderr)
        self.assertFalse(missing.exists(), "install.py never renders the card")

    def test_main_points_the_renderer_at_a_non_default_home(self) -> None:
        # `just sync-skills` renders into the real $HOME, never into --home.
        code, _stdout, stderr = self.run_main("--dry-run")

        self.assertEqual(code, 0)
        self.assertIn(
            f"python3 -m agent_session_loop.startup_context --home {self.home}",
            stderr,
        )
        self.assertNotIn("just sync-skills", stderr)

    def test_skills_only_leaves_the_wrappers_alone(self) -> None:
        self.install(skills_only=True)

        self.assertFalse((self.home / ".local/bin/asl").exists())
        self.assertTrue((self.home / ".claude/skills/asl").is_symlink())

    def test_prunes_stale_links_however_their_target_is_spelled(self) -> None:
        # A stow-style relative target, a doubled separator, and an aliased
        # spelling of the checkout (the /tmp vs /private/tmp case) all name a
        # skill in this checkout that has gone.
        alias = self.root / "alias"
        alias.symlink_to(self.repo)
        skills = self.home / ".claude/skills"
        skills.mkdir()
        stale = {
            "relative": os.path.relpath(self.repo / "skills/gone-a", skills),
            "doubled": f"{self.repo}//skills/gone-b",
            "aliased": os.fspath(alias / "skills/gone-c"),
        }
        for name, target in stale.items():
            (skills / name).symlink_to(target)
        live = skills / "live-relative"
        live.symlink_to(os.path.relpath(self.repo / "skills/asl", skills))
        outside = skills / "outside"
        outside.symlink_to(os.path.relpath(self.root / "dotfiles/skills/x", skills))

        changes = self.install()

        for name in stale:
            self.assertFalse((skills / name).is_symlink(), name)
            self.assertIn(f"removed stale link: {skills / name}", changes)
        self.assertTrue(live.is_symlink(), "a live link is not stale")
        self.assertTrue(outside.is_symlink(), "a link outside the checkout stays")

    def test_dry_run_says_would_for_every_change(self) -> None:
        (self.home / ".claude/skills").mkdir()
        stale = self.home / ".claude/skills/gone"
        stale.symlink_to(self.repo / "skills/gone")

        changes = self.install(dry_run=True)

        self.assertTrue(all(change.startswith("would ") for change in changes), changes)
        self.assertIn(
            f"would link: {self.home / '.local/bin/asl'} -> "
            f"{self.repo / 'bin/.local/bin/asl'}",
            changes,
        )
        self.assertIn(f"would remove stale link: {stale}", changes)
        self.assertTrue(stale.is_symlink())
        self.assertFalse((self.home / ".claude/skills/asl").exists())

    def test_dry_run_touches_nothing(self) -> None:
        changes = self.install(dry_run=True)

        self.assertTrue(changes)
        self.assertFalse((self.home / ".local").exists())
        for install_dir in DEFAULT_SKILL_DIRS:
            self.assertFalse((self.home / install_dir).exists())

    def test_absent_harness_roots_are_skipped_and_never_created(self) -> None:
        (self.home / ".copilot").rmdir()
        (self.home / ".codex").rmdir()

        changes = self.install()

        self.assertTrue((self.home / ".claude/skills/asl").is_symlink())
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".copilot").exists())
        self.assertFalse((self.home / ".claude-personal").exists())
        self.assertFalse(any(".codex" in change for change in changes))

    def test_claude_profiles_join_through_the_environment(self) -> None:
        (self.home / ".claude-personal").mkdir()
        (self.home / ".claude-work").mkdir()
        env = {
            "CLAUDE_CONFIG_DIR": str(self.home / ".claude-personal"),
            "ASL_CLAUDE_CONFIG_DIRS": os.pathsep.join(
                ["~/.claude-work", "~/.claude-absent", "~/.claude-personal"]
            ),
        }

        self.install(env=env)

        for root in (".claude-personal", ".claude-work"):
            link = self.home / root / "skills/asl"
            self.assertEqual(os.readlink(link), os.fspath(self.repo / "skills/asl"))
        self.assertFalse((self.home / ".claude-absent").exists())
        discovery = install_links.discover_harness_roots(self.home, env)
        self.assertEqual(
            [root.relative for root in discovery.found],
            [".claude", ".claude-personal", ".claude-work", ".codex", ".copilot"],
        )
        self.assertIn(("~/.claude-absent", "not present"), discovery.skipped)
        self.assertIn("~/.claude-work (claude)", discovery.note())

    def test_a_root_outside_home_is_skipped(self) -> None:
        outside = self.root / "elsewhere/.claude-personal"
        outside.mkdir(parents=True)
        env = {"CLAUDE_CONFIG_DIR": str(outside)}

        discovery = install_links.discover_harness_roots(self.home, env)
        self.install(env=env)

        self.assertEqual(
            [root.relative for root in discovery.found],
            [".claude", ".codex", ".copilot"],
        )
        self.assertEqual(discovery.skipped[0][0], str(outside))
        self.assertFalse((outside / "skills").exists())

    def test_a_harness_root_symlinked_outside_home_is_skipped(self) -> None:
        outside = self.root / "elsewhere/codex"
        outside.mkdir(parents=True)
        (self.home / ".codex").rmdir()
        (self.home / ".codex").symlink_to(outside, target_is_directory=True)

        discovery = install_links.discover_harness_roots(self.home, NO_EXTRA_ROOTS)
        changes = self.install()

        self.assertNotIn(".codex", [root.relative for root in discovery.found])
        self.assertTrue(any(".codex" in path for path, _ in discovery.skipped))
        self.assertFalse((outside / "skills").exists())
        self.assertFalse(any(".codex" in change for change in changes))

    def test_nested_skills_symlink_outside_home_blocks_links_and_pruning(self) -> None:
        outside = self.root / "shared-skills"
        outside.mkdir()
        (self.home / ".codex/skills").symlink_to(outside, target_is_directory=True)
        stale = outside / "gone"
        stale.symlink_to(self.repo / "skills/gone")

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        self.assertIn(
            f"{self.home / '.codex/skills'} resolves outside", str(raised.exception)
        )
        self.assertEqual(raised.exception.changes, [])
        self.assertTrue(stale.is_symlink())
        self.assertFalse((outside / "asl").exists())
        self.assertFalse((self.home / ".local").exists())

    def test_wrapper_parent_symlink_outside_home_blocks_relink(self) -> None:
        outside = self.root / "shared-bin"
        outside.mkdir()
        (self.home / ".local").mkdir()
        (self.home / ".local/bin").symlink_to(outside, target_is_directory=True)
        existing = outside / "asl"
        existing.symlink_to(self.root / "other-asl")

        with self.assertRaises(install_links.InstallError) as raised:
            self.install()

        self.assertIn(
            f"{self.home / '.local/bin'} resolves outside", str(raised.exception)
        )
        self.assertEqual(raised.exception.changes, [])
        self.assertEqual(os.readlink(existing), os.fspath(self.root / "other-asl"))
        self.assertFalse((self.home / ".claude/skills").exists())

    def test_a_child_name_starting_with_two_dots_is_inside_home(self) -> None:
        profile = self.home / "..claude-work"
        profile.mkdir()
        env = {"CLAUDE_CONFIG_DIR": str(profile)}

        discovery = install_links.discover_harness_roots(self.home, env)
        self.install(env=env)

        self.assertIn("..claude-work", [root.relative for root in discovery.found])
        self.assertTrue((profile / "skills/asl").is_symlink())

    def test_prune_leaves_unmanaged_roots_alone(self) -> None:
        stale = self.home / ".claude-personal/skills/gone"
        stale.parent.mkdir(parents=True)
        stale.symlink_to(self.repo / "skills/gone")

        self.install()

        self.assertTrue(stale.is_symlink())
        self.assertFalse((self.home / ".claude-personal/skills/asl").exists())


class WrapperInterpreterTests(unittest.TestCase):
    WRAPPER = REPO_ROOT / "bin/.local/bin/agent-session-loop"

    def test_wrapper_finds_python_on_path(self) -> None:
        first_line = self.WRAPPER.read_text(encoding="utf-8").splitlines()[0]

        self.assertEqual(first_line, "#!/usr/bin/env python3")

    def test_entry_points_parse_under_an_old_interpreter(self) -> None:
        # The guard can only report the version if the file reaches it.
        for path in (
            self.WRAPPER,
            REPO_ROOT / "install.py",
            REPO_ROOT / "agent_session_loop/__init__.py",
            REPO_ROOT / "agent_session_loop/__main__.py",
        ):
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_package_import_on_old_interpreter_gets_a_readable_error(self) -> None:
        # ``install.py`` and the ``python -m agent_session_loop.<module>``
        # recipes reach the package without the wrapper, so the initializer
        # must refuse on its own.
        script = (
            "import runpy, sys\n"
            "sys.version_info = (3, 12, 0, 'final', 0)\n"
            f"runpy.run_path({str(REPO_ROOT / 'agent_session_loop/__init__.py')!r})\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("asl requires Python 3.14+; found 3.12 at", result.stderr)

    def test_old_interpreter_gets_a_readable_error(self) -> None:
        script = (
            "import runpy, sys\n"
            "sys.version_info = (3, 12, 0, 'final', 0)\n"
            f"runpy.run_path({str(self.WRAPPER)!r}, run_name='__main__')\n"
        )

        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("asl requires Python 3.14+; found 3.12 at", result.stderr)
        self.assertIn(sys.executable, result.stderr)


if __name__ == "__main__":
    unittest.main()
