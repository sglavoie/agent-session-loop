"""Stable checkpoint identities and bounded Git inspection."""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Sequence
from pathlib import Path

from . import commit_message
from .git_subprocess import run_git_reporting
from .overseer_checkpoint_models import _ID_NAMESPACE, CHECKPOINT_TRAILER


def checkpoint_id(*, plan_dir: str, incident_id: int | None, target: str) -> str:
    """Return the identity one checkpoint commit always carries.

    Derived rather than generated, from the four fields an action and a ledger
    row both carry, so a commit made by a process that then died is findable by
    a process that never saw it.
    """
    name = "\x00".join(
        (plan_dir, "" if incident_id is None else str(incident_id), "commit", target)
    )
    return str(uuid.uuid5(_ID_NAMESPACE, name))


def checkpoint_message(message: str, identity: str) -> str:
    """Attach the reconciliation trailer to an action's commit message."""
    body = commit_message.strip_disallowed_trailers(message).strip()
    return f"{body}\n\n{CHECKPOINT_TRAILER}: {identity}\n"


def find_checkpoint(root: Path, identity: str) -> str | None:
    """The sha of the commit carrying ``identity``, ``""`` if none, or ``None``.

    ``None`` means git could not be asked, which is not evidence that the
    commit is absent. Keeping that apart from ``""`` is what lets a caller
    answer ``uncertain`` instead of inventing a negative.
    """
    result = _git(
        root,
        [
            "log",
            "--max-count=1",
            "--format=%H",
            "--fixed-strings",
            f"--grep={CHECKPOINT_TRAILER}: {identity}",
        ],
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``root``, reporting a failure rather than raising.

    Every spawn failure, a missing executable included, reports ``1``.
    """
    return run_git_reporting(root, args, missing_returncode=1)


def dirty_paths(root: Path) -> tuple[str, ...] | None:
    """Every path the worktree currently differs from HEAD on, or ``None``.

    ``None`` is a status that could not be taken, which is not the same fact as
    a clean worktree and must never be read as one.
    """
    result = _git(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if result.returncode != 0:
        return None
    return _porcelain_paths(result.stdout)


def _porcelain_paths(payload: str) -> tuple[str, ...]:
    """Every path a ``status --porcelain -z`` listing names, renames included.

    A rename or copy record is ``XY <new>\\0<old>\\0``: the original path
    follows as its own NUL-terminated field, so it is consumed here rather than
    read as a malformed entry. Both sides are reported, because a checkpoint
    that owns one end of a rename and not the other owns neither.
    """
    fields = payload.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and index < len(fields):
            paths.append(fields[index])
            index += 1
    return tuple(dict.fromkeys(path for path in paths if path))
