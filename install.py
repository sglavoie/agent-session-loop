#!/usr/bin/env python3
"""Link this checkout's ASL wrappers and skills into a home directory.

The wrappers go to ``~/.local/bin`` and every skill under ``skills/`` goes to
each discovered harness skill directory, all as absolute symlinks into this
checkout. A skill is a directory with a ``SKILL.md``; the support directories
named in ``SUPPORT_DIRS`` carry none and are linked the same way, so a skill's
relative read of ``../references/<file>`` resolves in the installed layout too.

Harness roots are discovered, never invented: ``~/.claude``, ``~/.codex`` and
``~/.copilot`` are managed only when the directory already exists, and further
Claude profiles join through ``$CLAUDE_CONFIG_DIR`` or the
``os.pathsep``-separated ``$ASL_CLAUDE_CONFIG_DIRS``
(``agent_session_loop.install_verify_common.discover_harness_roots``).

Every link is replaced with ``os.replace`` on a temporary sibling, so a running
supervisor, launchd job or cockpit that execs ``asl`` mid-install sees the old
link or the new one and never a missing path. That is what lets one checkout
take over from another (or from a stow package) without stopping anything.

An existing symlink is retargeted; an existing real file or directory is never
overwritten, because it is content somebody put there and a link would shadow
it. Links into this checkout whose source has gone are pruned, and only inside
the harness roots this install manages.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_session_loop.install_verify_common import (  # noqa: E402 - needs REPO_ROOT
    HarnessRoot,
    discover_harness_roots,
)

WRAPPERS = ("agent-session-loop", "asl")

WRAPPER_DIR = ".local/bin"

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
    roots: Iterable[HarnessRoot] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Link the wrappers and skills; ``roots`` defaults to the discovered set."""
    if roots is None:
        roots = discover_harness_roots(home, env).found
    changes: list[str] = []
    if not skills_only:
        for name in WRAPPERS:
            source = repo_root / "bin/.local/bin" / name
            change = link(source, home / WRAPPER_DIR / name, dry_run=dry_run)
            if change:
                changes.append(change)
    sources = skill_sources(repo_root)
    for root in roots:
        install_root = home / root.skills_dir
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
    discovery = discover_harness_roots(args.home)
    print(discovery.note())
    if not discovery.found:
        print(
            "install: no harness root exists; skills are linked only into "
            "existing harness directories",
            file=sys.stderr,
        )
    try:
        changes = install(
            args.home,
            skills_only=args.skills_only,
            dry_run=args.dry_run,
            roots=discovery.found,
        )
    except InstallError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for change in changes:
        print(change)
    if not changes:
        if discovery.found or not args.skills_only:
            print("install: links already point at this checkout")
        else:
            print("install: nothing to link")
    if not args.skills_only:
        _warn_if_wrapper_dir_off_path(args.home / WRAPPER_DIR)
    return 0


def _warn_if_wrapper_dir_off_path(wrapper_dir: Path) -> None:
    """Say so when the linked ``asl`` is not reachable by name."""
    entries = os.environ.get("PATH", "").split(os.pathsep)
    resolved = {Path(entry).expanduser().resolve() for entry in entries if entry}
    if wrapper_dir.resolve() not in resolved:
        print(
            f"install: {wrapper_dir} is not on PATH, so `asl` will not be "
            f'found; add it with export PATH="{wrapper_dir}:$PATH"',
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
