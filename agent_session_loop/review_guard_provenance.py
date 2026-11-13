"""Content digests a repair child's commit preflight may sanction dirt against.

The repair-child preflight refuses every dirty path its child did not author,
because a legacy child's commit is repository-wide and would publish that dirt
under the repair's name. Two kinds of dirt are nevertheless the run's own, and
refusing them dead-ends the very transition that produced them:

* the plan artifacts a review cycle leaves behind when the plan directory is
  tracked inside the repository -- the review output and artifact, the review
  session file, the progress journal, the manifest and every materialized
  child (ASI-170); and
* the work a repair child's own earlier turn retained when that turn ended
  without completing the child -- a runner question the loop absorbed, a retry,
  a verification repair cycle -- and the loop continues the same child in the
  same run (ASI-168).

A pathname is not an origin, so neither is sanctioned by name. What this module
provides is the measurement both sanctions are made with: the repository's
dirty paths, each with a digest of its porcelain status and of the worktree
bytes a commit would stage. The run records that measurement at the moment it
can attest every recorded path is its own (``repair_provenance``), and the
preflight measures again and sanctions only a path whose digest is unchanged.
A path rewritten since, a path that was never recorded, and a path whose
content cannot be digested (a directory, an unreadable or special file) all
stay refused.

Nothing here writes, and git is reached only through ``review_guard_git``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
from pathlib import Path

from . import review_guard_git
from .run_errors import RunError

#: Recorded for a dirty path that no longer exists in the worktree: a deletion
#: or the source side of a rename. It is a value like any other, so a path the
#: child deleted stays sanctioned only while it is still deleted.
ABSENT_CONTENT = "absent"


@dataclasses.dataclass(frozen=True)
class DirtyPathEvidence:
    """One repository's dirty paths, relative to its top level, with statuses."""

    toplevel: Path
    statuses: dict[str, str]

    def digest(self, path: str) -> str | None:
        """Digest one dirty path, or ``None`` when its content cannot be proven."""
        status = self.statuses.get(path)
        if status is None:
            return None
        content = _worktree_content_digest(self.toplevel / path)
        if content is None:
            return None
        return f"{status}|{content}"


def _porcelain_status_entries(output: str) -> dict[str, str]:
    """Map every dirty path in NUL-framed porcelain output to its ``XY`` status.

    Parsed the same way as ``review_guard_git._porcelain_dirty_paths`` so the
    two name exactly the same paths: a rename or copy names its source as a
    second field, and that source carries the entry's status too.
    """
    fields = output.split("\0")
    statuses: dict[str, str] = {}
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if path:
            statuses.setdefault(path, status)
        if "R" in status or "C" in status:
            if index < len(fields) and fields[index]:
                statuses.setdefault(fields[index], status)
            index += 1
    return statuses


def _worktree_content_digest(path: Path) -> str | None:
    """Digest the worktree node at ``path`` as a commit would stage it.

    A regular file is its bytes plus the executable bit Git records, a symlink
    is its link text, and a missing node is :data:`ABSENT_CONTENT`. Anything
    else -- a directory such as an untracked embedded repository, a socket, a
    file that cannot be read -- returns ``None``: its content cannot be pinned
    by one digest, so it can never be sanctioned.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ABSENT_CONTENT
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return None
        return "symlink:" + hashlib.sha256(os.fsencode(target)).hexdigest()
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    mode = "100755" if info.st_mode & 0o111 else "100644"
    return f"file:{mode}:{hashlib.sha256(data).hexdigest()}"


def _dirty_path_evidence(root: Path) -> DirtyPathEvidence:
    """Read one repository's dirty paths, raising :class:`RunError` on failure.

    Porcelain paths are relative to the repository's top level whatever
    directory git runs in, so the top level is resolved here and every digest
    is taken against it rather than against ``root``.
    """
    toplevel = review_guard_git._run_git(root, ["rev-parse", "--show-toplevel"])
    if toplevel.returncode != 0 or not toplevel.stdout.strip():
        detail = toplevel.stderr.strip() or toplevel.stdout.strip()
        raise RunError(
            "could not resolve the repository top level"
            + (f": {detail}" if detail else "")
        )
    status = review_guard_git._run_git(
        root, ["status", "--porcelain", "-z", "--untracked-files=all"]
    )
    if status.returncode != 0:
        detail = status.stderr.strip() or status.stdout.strip()
        raise RunError(
            "could not inspect repository status" + (f": {detail}" if detail else "")
        )
    return DirtyPathEvidence(
        toplevel=Path(toplevel.stdout.strip()),
        statuses=_porcelain_status_entries(status.stdout),
    )
