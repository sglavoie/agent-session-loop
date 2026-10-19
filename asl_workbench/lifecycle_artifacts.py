"""Lifecycle artifacts."""

from __future__ import annotations

import io
import stat
import time
import zipfile
from hashlib import sha256
from pathlib import Path, PurePosixPath

from . import lifecycle_common as common
from .compatibility import check_sources
from .installation import (
    PRIVATE_DIRECTORY_MODE,
    atomic,
)

# ---------------------------------------------------------------------------
# Artifacts


def packable(name: str) -> bool:
    """Whether one path belongs in a production artifact."""
    member = PurePosixPath(name)
    if not name.startswith(common.PACKAGE_DIRECTORY + "/"):
        return False
    if member.suffix == common.SOURCE_SUFFIX:
        return "__pycache__" not in member.parts
    return member.suffix in common.STATIC_SUFFIXES and name.startswith(
        common.STATIC_DIRECTORY + "/"
    )


def artifact_bytes(artifact: Path, digest: str) -> bytes:
    """Read one artifact, or refuse it. The gate every install passes through.

    Four questions, in the order that makes a "no" cheap: is the digest one
    somebody wrote down, is the file within bounds, are the bytes the ones the
    digest names, and is what is inside the file only portable application code
    and static assets that hold the boundary.
    """
    if not common._DIGEST.fullmatch(digest):
        raise common.LifecycleError("an explicit SHA-256 is required")
    if artifact.stat().st_size > common.MAX_ARTIFACT:
        raise common.LifecycleError("artifact exceeds size bound")
    raw = artifact.read_bytes()
    if sha256(raw).hexdigest() != digest:
        raise common.LifecycleError("artifact digest mismatch")
    check_artifact(raw)
    return raw


def artifact_members(raw: bytes) -> dict[str, bytes]:
    """Every member of an artifact, once the file itself has been vouched for.

    Traversal, duplication, symlinks, non-code members and both size bounds are
    decided here, so no caller unpacks anything this function has not read.
    """
    members: dict[str, bytes] = {}
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            if (
                entry.filename in members
                or name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or name.parts[0] != common.PACKAGE_DIRECTORY
                or "\\" in entry.filename
            ):
                raise common.LifecycleError("unsafe artifact member")
            mode = entry.external_attr >> 16
            if (
                stat.S_ISLNK(mode)
                or stat.S_IFMT(mode) not in common._ALLOWED_MEMBER_TYPES
            ):
                raise common.LifecycleError("artifact contains nonregular member")
            if entry.is_dir():
                continue
            if not packable(entry.filename):
                raise common.LifecycleError("artifact contains non-code member")
            expanded += entry.file_size
            if expanded > common.MAX_ARTIFACT or len(members) >= common.MAX_MEMBERS:
                raise common.LifecycleError("expanded artifact exceeds bound")
            members[entry.filename] = archive.read(entry)
    if not set(members) >= common.REQUIRED_MEMBERS:
        raise common.LifecycleError("artifact lacks runtime entry points")
    return members


def check_artifact(raw: bytes) -> dict[str, bytes]:
    """Hold the portable boundary against the packaged bytes themselves.

    The working tree is checked by ``tests/test_workbench_foundations.py``;
    this checks the file that will actually be copied to the Pi. They are not
    the same question. An artifact built from a different revision, or from a
    tree with an extra module in it, is exactly the case where "the checkout
    was fine" says nothing useful.
    """
    members = artifact_members(raw)
    report = check_sources(
        {
            name[: -len(common.SOURCE_SUFFIX)].replace("/", "."): payload.decode(
                "utf-8"
            )
            for name, payload in members.items()
            if name.endswith(common.SOURCE_SUFFIX)
        },
        package=common.PACKAGE_DIRECTORY,
    )
    if (
        report.forbidden_imports
        or report.third_party_imports
        or report.unsupported_syntax
    ):
        raise common.LifecycleError(
            "artifact breaks the portable boundary: " + "; ".join(report.problems())
        )
    return members


def normalize(members: dict[str, bytes], *, timestamp: int) -> bytes:
    """Build one artifact whose bytes depend only on its content and a commit.

    ``git archive`` already produces a stable tree, but its zip carries the
    packer's choices; rebuilding here fixes the member order, the mode and the
    timestamp, so two packs of one revision on two machines produce one digest.
    A digest that moved with the machine would make "pinned" mean nothing.
    """
    stamp = time.gmtime(timestamp)[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(members):
            entry = zipfile.ZipInfo(name, date_time=stamp)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, members[name])
    return buffer.getvalue()


def pack(artifact: Path, revision: str, *, repository: Path | None = None) -> str:
    """Build a production artifact from a commit. Returns its SHA-256.

    From a commit, never from the working tree: an artifact packed from
    uncommitted files cannot be rebuilt, and the digest it is installed under
    would name bytes nobody can produce again.
    """
    if not common._REVISION.fullmatch(revision):
        raise common.LifecycleError("pack requires a full commit revision")
    root = repository or Path(__file__).resolve().parents[1]
    committed_at = int(
        common.command(["git", "show", "-s", "--format=%ct", revision], cwd=root)
        .stdout.decode()
        .strip()
    )
    raw = common.command(
        [
            "git",
            "archive",
            "--format=zip",
            revision,
            common.PACKAGE_DIRECTORY,
        ],
        cwd=root,
    ).stdout
    members: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise common.LifecycleError("the package tree contains a symlink")
            if not entry.is_dir() and packable(entry.filename):
                members[entry.filename] = archive.read(entry)
    payload = normalize(members, timestamp=committed_at)
    digest = sha256(payload).hexdigest()
    atomic(artifact, payload)
    # Packed content is checked by the installer's own gate, here, rather than
    # by a weaker one: what ships is what an install would accept.
    artifact_bytes(artifact, digest)
    return digest


def unpack(raw: bytes, destination: Path) -> None:
    """Write a verified artifact into a fresh private directory."""
    destination.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    for name, payload in check_artifact(raw).items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        atomic(path, payload)
