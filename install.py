#!/usr/bin/env python3
"""Link this checkout's ASL wrappers and skills into a home directory.

The wrappers go to ``~/.local/bin`` and every skill under ``skills/`` goes to
each discovered harness skill directory, all as absolute symlinks into this
checkout. A skill is a directory with a ``SKILL.md``; the support directories
named in ``SUPPORT_DIRS`` carry none and are linked the same way, so a skill's
relative read of ``../references/<file>`` resolves in the installed layout too.
A directory holding nothing but cache residue (``__pycache__``, ``*.pyc``,
``.DS_Store``) is what removing a skill from git leaves behind; it is skipped
with a note, and an installed link to it is pruned like any other stale link.

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
import secrets
import shlex
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_session_loop.install_verify_common import (  # noqa: E402 - needs REPO_ROOT
    SUPPORT_DIRS,
    HarnessRoot,
    discover_harness_roots,
)

WRAPPERS = ("agent-session-loop", "asl")

WRAPPER_DIR = ".local/bin"


#: What every conflict tells the operator to do about it.
RESOLUTION = "move it aside or delete it, then re-run"

#: Entries git ignores and nothing installs: what keeps a removed skill's
#: directory alive. A directory holding only these is residue.
LEFTOVER_DIR_NAMES = frozenset({"__pycache__"})
LEFTOVER_FILE_NAMES = frozenset({".DS_Store"})
LEFTOVER_FILE_SUFFIXES = (".pyc", ".pyo")


class InstallError(Exception):
    """The install cannot proceed; ``changes`` lists what it already did."""

    def __init__(self, message: str, changes: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.changes = list(changes)


def is_leftover(directory: Path) -> bool:
    """Whether ``directory`` holds nothing but cache residue (or nothing at all).

    A plain filesystem walk rather than a git query, so it behaves the same in
    a checkout, an export, or a test fixture.
    """
    for parent, dirs, files in os.walk(directory):
        if os.path.basename(parent) in LEFTOVER_DIR_NAMES:
            continue
        for name in files:
            if name not in LEFTOVER_FILE_NAMES and not name.endswith(
                LEFTOVER_FILE_SUFFIXES
            ):
                return False
        for name in dirs:
            if os.path.islink(os.path.join(parent, name)):
                return False
    return True


def leftover_skill_dirs(repo_root: Path) -> list[Path]:
    """Directories under ``skills/`` without a ``SKILL.md`` that are residue."""
    return [
        path
        for path in sorted((repo_root / "skills").iterdir())
        if path.is_dir()
        and not path.is_symlink()
        and not (path / "SKILL.md").is_file()
        and path.name not in SUPPORT_DIRS
        and is_leftover(path)
    ]


def skill_sources(repo_root: Path) -> list[Path]:
    leftovers = set(leftover_skill_dirs(repo_root))
    sources = []
    for path in sorted((repo_root / "skills").iterdir()):
        if not path.is_dir() or path in leftovers:
            continue
        if not (path / "SKILL.md").is_file() and path.name not in SUPPORT_DIRS:
            raise InstallError(
                f"missing SKILL.md in {path.relative_to(repo_root)}; add one, "
                "add the directory to SUPPORT_DIRS in "
                "agent_session_loop/install_verify_common.py if it is shared "
                "support content, or delete it if it is left over from a removed "
                "skill"
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
    """Point ``destination`` at ``source`` atomically; return what changed.

    A dry run reports the same change as ``would link``/``would relink``, so
    its output cannot be mistaken for a record of links that were placed.
    """
    target = os.fspath(source)
    if destination.is_symlink():
        if os.readlink(destination) == target:
            return None
        action = "would relink" if dry_run else "relinked"
    elif problem := conflict(destination):
        raise InstallError(problem)
    else:
        action = "would link" if dry_run else "linked"
    if not dry_run:
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InstallError(
                f"cannot create {destination.parent}: {error.strerror or error}; "
                f"{RESOLUTION}"
            ) from error
        # Unique per call, so neither a leftover from a killed install (which
        # may even be a directory) nor a concurrent install can collide with it
        # after the preflight has passed and other links have already moved.
        staging = destination.with_name(
            f".{destination.name}.asl-install.{os.getpid()}.{secrets.token_hex(4)}"
        )
        os.symlink(target, staging)
        try:
            os.replace(staging, destination)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
    return f"{action}: {destination} -> {target}"


def prune(install_root: Path, repo_root: Path, *, dry_run: bool) -> list[str]:
    """Remove links into this checkout's skills whose source is gone.

    A link counts as into this checkout by where it points, not by how its
    target is spelled: a relative (stow-style) target is read against the
    link's own directory, and both sides are resolved, so a doubled ``/`` or
    an aliased spelling of the checkout (``/tmp`` for ``/private/tmp``) is
    still recognized as stale. A source that survives only as cache residue
    (see ``is_leftover``) counts as gone.
    """
    if not install_root.is_dir():
        return []
    skills_root = Path(os.path.realpath(repo_root / "skills"))
    leftovers = {
        Path(os.path.realpath(path)) for path in leftover_skill_dirs(repo_root)
    }
    action = "would remove stale link" if dry_run else "removed stale link"
    removed = []
    for entry in sorted(install_root.iterdir()):
        if not entry.is_symlink():
            continue
        target = Path(os.path.realpath(entry.parent / os.readlink(entry)))
        inside = target != skills_root and target.is_relative_to(skills_root)
        if inside and (not target.is_dir() or target in leftovers):
            if not dry_run:
                entry.unlink()
            removed.append(f"{action}: {entry}")
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
    # Flushed, because the warnings below go to stderr and an interleaved
    # capture (`2>&1 | less`, a CI log) must still show the note first.
    print(discovery.note(), flush=True)
    for leftover in leftover_skill_dirs(REPO_ROOT):
        print(
            f"install: skipped {leftover.relative_to(REPO_ROOT)}: no SKILL.md and "
            "only cache residue left (a removed skill?); delete it to silence this",
            file=sys.stderr,
        )
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
            print(change, flush=True)
        if error.changes:
            made = "would have been made" if args.dry_run else "were made"
            print(
                f"install: {len(error.changes)} change(s) above {made} "
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
        sys.stdout.flush()
        _warn_if_wrapper_dir_off_path(args.home / WRAPPER_DIR)
    _note_missing_context_files(args.home, discovery.found)
    return 0


def missing_context_files(home: Path, roots: Iterable[HarnessRoot]) -> list[Path]:
    """Context files of discovered roots that do not exist yet."""
    paths = (home / root.context_file for root in roots if root.context_file)
    return [path for path in paths if not path.is_file()]


def _note_missing_context_files(home: Path, roots: Iterable[HarnessRoot]) -> None:
    """Say which context files the startup card has yet to be rendered into.

    This script links; it never renders the card. ``just install`` and
    ``just sync-skills`` run the renderer after it and create these files, but
    only under the real ``$HOME``; for any other ``--home`` the hint names the
    renderer pointed at that home instead.
    """
    if home.expanduser().resolve() == Path.home().resolve():
        remedy = "run `just sync-skills` (or `just install`)"
    else:
        command = (
            "python3 -m agent_session_loop.startup_context "
            f"--home {shlex.quote(os.fspath(home))}"
        )
        remedy = f"run `{command}` from this checkout"
    for path in missing_context_files(home, roots):
        print(
            f"install: {path} does not exist yet; this script does not render "
            f"the ASL startup card, so {remedy} to create it",
            file=sys.stderr,
        )


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
