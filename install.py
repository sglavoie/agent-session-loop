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

Every destination is checked before anything is touched, so a conflict (real
content where a link belongs, or a file where a parent directory belongs) fails
the install with nothing changed and names every path in the way.
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


#: What every conflict tells the operator to do about it.
RESOLUTION = "move it aside or delete it, then re-run"


class InstallError(Exception):
    """The install cannot proceed; ``changes`` lists what it already did."""

    def __init__(self, message: str, changes: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.changes = list(changes)


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


def conflict(destination: Path) -> str | None:
    """Why a link cannot be placed at ``destination``, or ``None`` if it can.

    Real content at the destination is in the way, and so is a non-directory
    -- a regular file, or a symlink that dangles or points at a file -- where
    one of its parent directories belongs.
    """
    if destination.is_symlink():
        return None
    if destination.exists():
        return f"{destination} exists and is not a symlink; {RESOLUTION}"
    for parent in destination.parents:
        if parent.is_dir():
            return None
        if parent.is_symlink() or parent.exists():
            return (
                f"{parent} is not a directory (a file or a dangling symlink) "
                f"but must hold links; {RESOLUTION}"
            )
    return None


def link(source: Path, destination: Path, *, dry_run: bool) -> str | None:
    """Point ``destination`` at ``source`` atomically; return what changed."""
    target = os.fspath(source)
    if destination.is_symlink():
        if os.readlink(destination) == target:
            return None
        action = "relinked"
    elif problem := conflict(destination):
        raise InstallError(problem)
    else:
        action = "linked"
    if not dry_run:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InstallError(
                f"cannot create {destination.parent}: {error.strerror or error}; "
                f"{RESOLUTION}"
            ) from error
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
    sources = skill_sources(repo_root)
    wrappers = (
        []
        if skills_only
        else [
            (repo_root / "bin/.local/bin" / name, home / WRAPPER_DIR / name)
            for name in WRAPPERS
        ]
    )
    install_roots = [home / root.skills_dir for root in roots]
    links = wrappers + [
        (source, install_root / source.name)
        for install_root in install_roots
        for source in sources
    ]
    # Preflight: one conflict anywhere fails the install before any link,
    # prune or directory has been touched, and every conflict is named at once.
    # A parent in the way blocks every link under it; name it once.
    problems = list(dict.fromkeys(filter(None, (conflict(dest) for _, dest in links))))
    if problems:
        raise InstallError(
            "nothing was changed, because these are in the way:\n  "
            + "\n  ".join(problems)
        )
    changes: list[str] = []
    try:
        for source, destination in wrappers:
            if change := link(source, destination, dry_run=dry_run):
                changes.append(change)
        for install_root in install_roots:
            changes.extend(prune(install_root, repo_root, dry_run=dry_run))
            for source in sources:
                destination = install_root / source.name
                if change := link(source, destination, dry_run=dry_run):
                    changes.append(change)
    except InstallError as error:
        raise InstallError(str(error), changes) from error
    except OSError as error:
        raise InstallError(f"install stopped: {error}", changes) from error
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
            REPO_ROOT,
            skills_only=args.skills_only,
            dry_run=args.dry_run,
            roots=discovery.found,
        )
    except InstallError as error:
        for change in error.changes:
            print(change)
        if error.changes:
            print(
                f"install: {len(error.changes)} change(s) above were made "
                "before the error",
                file=sys.stderr,
            )
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
