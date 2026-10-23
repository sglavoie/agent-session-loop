#!/usr/bin/env python3
"""Link this checkout's ASL wrappers and skills into a home directory.

The wrappers go to ``~/.local/bin`` and every skill under ``skills/`` goes to
each harness skill directory, all as absolute symlinks into this checkout. A
skill is a directory with a ``SKILL.md``; the support directories named in
``SUPPORT_DIRS`` carry none and are linked the same way, so a skill's relative
read of ``../references/<file>`` resolves in the installed layout too.

Every link is replaced with ``os.replace`` on a temporary sibling, so a running
supervisor, launchd job or cockpit that execs ``asl`` mid-install sees the old
link or the new one and never a missing path. That is what lets one checkout
take over from another (or from a stow package) without stopping anything.

An existing symlink is retargeted; an existing real file or directory is never
overwritten, because it is content somebody put there and a link would shadow
it. Links into this checkout whose source has gone are pruned.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

WRAPPERS = ("agent-session-loop", "asl")

WRAPPER_DIR = ".local/bin"

SKILL_INSTALL_DIRS = (
    ".claude/skills",
    ".claude-personal/skills",
    ".codex/skills",
    ".copilot/skills",
)

SUPPORT_DIRS = ("references",)


class InstallError(Exception):
    pass


def skill_sources(repo_root: Path) -> list[Path]:
    sources = []
    for path in sorted((repo_root / "skills").iterdir()):
        if not path.is_dir():
            continue
        if not (path / "SKILL.md").is_file() and path.name not in SUPPORT_DIRS:
            raise InstallError(
                f"missing SKILL.md in {path.relative_to(repo_root)}; add one, or "
                "add the directory to SUPPORT_DIRS if it is shared support content"
            )
        sources.append(path)
    return sources


def link(source: Path, destination: Path, *, dry_run: bool) -> str | None:
    """Point ``destination`` at ``source`` atomically; return what changed."""
    target = os.fspath(source)
    if destination.is_symlink():
        if os.readlink(destination) == target:
            return None
        action = "relinked"
    elif destination.exists():
        raise InstallError(f"{destination} exists and is not a symlink")
    else:
        action = "linked"
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name(f".{destination.name}.asl-install")
        if staging.is_symlink() or staging.exists():
            staging.unlink()
        os.symlink(target, staging)
        os.replace(staging, destination)
    return f"{action}: {destination} -> {target}"


def prune(install_root: Path, repo_root: Path, *, dry_run: bool) -> list[str]:
    """Remove links into this checkout's skills whose source is gone."""
    if not install_root.is_dir():
        return []
    skills_root = os.fspath(repo_root / "skills") + os.sep
    removed = []
    for entry in sorted(install_root.iterdir()):
        if not entry.is_symlink():
            continue
        target = os.readlink(entry)
        if target.startswith(skills_root) and not Path(target).is_dir():
            if not dry_run:
                entry.unlink()
            removed.append(f"removed stale link: {entry}")
    return removed


def install(
    home: Path,
    repo_root: Path = REPO_ROOT,
    *,
    skills_only: bool = False,
    dry_run: bool = False,
) -> list[str]:
    changes: list[str] = []
    if not skills_only:
        for name in WRAPPERS:
            source = repo_root / "bin/.local/bin" / name
            change = link(source, home / WRAPPER_DIR / name, dry_run=dry_run)
            if change:
                changes.append(change)
    sources = skill_sources(repo_root)
    for install_dir in SKILL_INSTALL_DIRS:
        install_root = home / install_dir
        changes.extend(prune(install_root, repo_root, dry_run=dry_run))
        for source in sources:
            change = link(source, install_root / source.name, dry_run=dry_run)
            if change:
                changes.append(change)
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Home directory to install into (default: $HOME)",
    )
    parser.add_argument(
        "--skills-only",
        action="store_true",
        help="Link the skills and leave the wrappers alone",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without touching anything",
    )
    args = parser.parse_args(argv)
    try:
        changes = install(args.home, skills_only=args.skills_only, dry_run=args.dry_run)
    except InstallError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for change in changes:
        print(change)
    if not changes:
        print("install: links already point at this checkout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
