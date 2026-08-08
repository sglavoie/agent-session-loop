"""Immutable domain types and validation for multi-repository workspaces.

This module deliberately has no plan-markdown or runner dependencies.  It is
the shared boundary for turning an ordered repository declaration into a
credential-free, filesystem-verified workspace binding.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import urllib.parse


REPOSITORY_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
REGISTRY_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SCP_ORIGIN_RE = re.compile(
    r"^(?:[^/@:\s]+@)?(?P<host>\[[^\]]+\]|[^/:@\s]+):(?P<path>.+)$"
)
_URL_ORIGIN_SCHEMES = frozenset({"git", "http", "https", "ssh"})
_DEFAULT_ORIGIN_PORTS = {"git": 9418, "http": 80, "https": 443, "ssh": 22}


class WorkspaceError(ValueError):
    """Raised when a workspace declaration or binding is unsafe or invalid."""


def validate_repository_alias(alias: str) -> str:
    """Return ``alias`` when it is canonical and safe for exact lookup."""
    if not isinstance(alias, str) or REPOSITORY_ALIAS_RE.fullmatch(alias) is None:
        raise WorkspaceError(
            "repository alias must match [A-Za-z][A-Za-z0-9_-]*"
        )
    return alias


def normalize_repository_path(path: str | PurePosixPath) -> str:
    """Normalize a portable workspace-relative repository path.

    Absolute paths, parent traversal, Windows separators/drives, and a path
    that denotes the workspace itself are rejected.  Symlinks are checked
    separately when a registry is resolved against a real workspace.
    """
    value = os.fspath(path)
    if not isinstance(value, str) or not value or value != value.strip():
        raise WorkspaceError("repository path must be a non-empty relative path")
    if "\\" in value or PureWindowsPath(value).is_absolute():
        raise WorkspaceError("repository path must use relative POSIX syntax")
    pure = PurePosixPath(value)
    if pure.is_absolute():
        raise WorkspaceError("repository path must be relative to the workspace")
    if ".." in pure.parts:
        raise WorkspaceError("repository path must not contain '..' segments")
    normalized = pure.as_posix().rstrip("/")
    if normalized in {"", "."}:
        raise WorkspaceError("repository path must identify a workspace descendant")
    return normalized


def canonicalize_origin(origin: str) -> str:
    """Return a credential-free, transport-neutral Git origin identity.

    SSH scp syntax and the common ``ssh``, ``git``, ``http``, and ``https``
    URL transports all canonicalize to ``host/path``.  User information is
    intentionally discarded and never included in validation errors.
    """
    if not isinstance(origin, str) or not origin or origin != origin.strip():
        raise WorkspaceError("repository origin must be a non-empty Git URL")
    if any(character.isspace() or ord(character) < 32 for character in origin):
        raise WorkspaceError("repository origin contains invalid characters")

    if "://" in origin:
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme.lower() not in _URL_ORIGIN_SCHEMES:
            raise WorkspaceError("repository origin uses an unsupported transport")
        if parsed.query or parsed.fragment or parsed.hostname is None:
            raise WorkspaceError("repository origin must identify a host and path")
        try:
            port = parsed.port
        except ValueError as error:
            raise WorkspaceError(
                "repository origin has an invalid host or port"
            ) from error
        scheme = parsed.scheme.lower()
        host = _canonical_origin_host(parsed.hostname)
        if port is not None and port != _DEFAULT_ORIGIN_PORTS[scheme]:
            host = f"{host}:{port}"
        path = parsed.path
    else:
        match = _SCP_ORIGIN_RE.fullmatch(origin)
        if match is None:
            raise WorkspaceError("repository origin must be an absolute Git URL")
        host = _canonical_origin_host(match.group("host").strip("[]"))
        path = match.group("path")

    decoded_path = urllib.parse.unquote(path).replace("\\", "/")
    if any(character.isspace() or ord(character) < 32 for character in decoded_path):
        raise WorkspaceError("repository origin path contains invalid characters")
    parts = tuple(part for part in decoded_path.split("/") if part)
    if not parts or any(part in {".", ".."} for part in parts):
        raise WorkspaceError(
            "repository origin must contain a canonical repository path"
        )
    final = parts[-1]
    if final.lower().endswith(".git"):
        final = final[:-4]
    if not final:
        raise WorkspaceError("repository origin must name a repository")
    canonical_path = "/".join((*parts[:-1], final))
    return f"{host}/{canonical_path}"


def origins_equal(first: str, second: str) -> bool:
    """Compare Git origins without transport or credentials."""
    return canonicalize_origin(first) == canonicalize_origin(second)


def _canonical_origin_host(host: str) -> str:
    try:
        canonical = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise WorkspaceError("repository origin has an invalid host") from error
    if not canonical or "/" in canonical or "\\" in canonical:
        raise WorkspaceError("repository origin has an invalid host")
    return canonical


@dataclass(frozen=True)
class Repository:
    """One registered repository with normalized, credential-free fields."""

    alias: str
    path: str
    origin: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "alias", validate_repository_alias(self.alias))
        object.__setattr__(self, "path", normalize_repository_path(self.path))
        object.__setattr__(self, "origin", canonicalize_origin(self.origin))

    def to_dict(self) -> dict[str, str]:
        return {"alias": self.alias, "path": self.path, "origin": self.origin}


def _path_contains(parent: str, child: str) -> bool:
    parent_parts = PurePosixPath(parent).parts
    child_parts = PurePosixPath(child).parts
    return (
        len(parent_parts) < len(child_parts)
        and child_parts[: len(parent_parts)] == parent_parts
    )


@dataclass(frozen=True)
class RepositoryRegistry:
    """An ordered repository registry with exact, case-sensitive aliases."""

    repositories: tuple[Repository, ...]

    def __post_init__(self) -> None:
        repositories = tuple(self.repositories)
        if not repositories:
            raise WorkspaceError("repository registry must not be empty")
        if any(not isinstance(repository, Repository) for repository in repositories):
            raise WorkspaceError(
                "repository registry entries must be Repository values"
            )

        aliases: set[str] = set()
        folded_aliases: set[str] = set()
        for repository in repositories:
            folded = repository.alias.casefold()
            if repository.alias in aliases:
                raise WorkspaceError(f"duplicate repository alias: {repository.alias}")
            if folded in folded_aliases:
                raise WorkspaceError(
                    "repository aliases must not collide case-insensitively"
                )
            aliases.add(repository.alias)
            folded_aliases.add(folded)

        for index, repository in enumerate(repositories):
            for other in repositories[index + 1 :]:
                if _path_contains(repository.path, other.path) or _path_contains(
                    other.path, repository.path
                ):
                    raise WorkspaceError(
                        "repository paths must not overlap: "
                        f"{repository.alias} and {other.alias}"
                    )
                if repository.path == other.path:
                    raise WorkspaceError(
                        "repository paths must be unique: "
                        f"{repository.alias} and {other.alias}"
                    )
        object.__setattr__(self, "repositories", repositories)

    def __iter__(self) -> Iterator[Repository]:
        return iter(self.repositories)

    def __len__(self) -> int:
        return len(self.repositories)

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(repository.alias for repository in self.repositories)

    def get(self, alias: str) -> Repository:
        validate_repository_alias(alias)
        for repository in self.repositories:
            if repository.alias == alias:
                return repository
        raise WorkspaceError(f"unknown repository alias: {alias}")

    @property
    def digest(self) -> str:
        payload = [repository.to_dict() for repository in self.repositories]
        encoded = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


def canonicalize_filesystem_path(path: str | Path) -> Path:
    """Return a real absolute path without requiring the target to exist."""
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(path)))))


def is_descendant(
    path: str | Path, parent: str | Path, *, allow_equal: bool = False
) -> bool:
    """Return whether ``path`` resolves beneath ``parent`` after symlinks."""
    canonical_path = canonicalize_filesystem_path(path)
    canonical_parent = canonicalize_filesystem_path(parent)
    try:
        relative = canonical_path.relative_to(canonical_parent)
    except ValueError:
        return False
    return allow_equal or bool(relative.parts)


@dataclass(frozen=True)
class ResolvedRepository:
    """A registered repository verified against the local filesystem and Git."""

    repository: Repository
    root: Path
    origin: str

    def __post_init__(self) -> None:
        if not isinstance(self.repository, Repository):
            raise WorkspaceError("resolved repository requires a Repository value")
        object.__setattr__(self, "root", canonicalize_filesystem_path(self.root))
        canonical_origin = (
            self.origin
            if self.origin == self.repository.origin
            else canonicalize_origin(self.origin)
        )
        if canonical_origin != self.repository.origin:
            raise WorkspaceError("resolved repository origin does not match registry")
        object.__setattr__(self, "origin", canonical_origin)

    @property
    def alias(self) -> str:
        return self.repository.alias


@dataclass(frozen=True)
class ResolvedWorkspace:
    """A canonical workspace root and its ordered, verified repositories."""

    root: Path
    repositories: tuple[ResolvedRepository, ...]
    registry: RepositoryRegistry = field(init=False)

    def __post_init__(self) -> None:
        root = canonicalize_filesystem_path(self.root)
        repositories = tuple(self.repositories)
        registry = RepositoryRegistry(
            tuple(repository.repository for repository in repositories)
        )
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "repositories", repositories)
        object.__setattr__(self, "registry", registry)

    def get(self, alias: str) -> ResolvedRepository:
        self.registry.get(alias)
        for repository in self.repositories:
            if repository.alias == alias:
                return repository
        raise AssertionError("resolved workspace registry is inconsistent")

    @property
    def binding(self) -> WorkspaceBinding:
        return WorkspaceBinding(
            workspace_root=str(self.root), registry_digest=self.registry.digest
        )


@dataclass(frozen=True)
class WorkspaceBinding:
    """Durable identity binding for a resolved workspace and registry."""

    workspace_root: str
    registry_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_root, str) or not Path(
            self.workspace_root
        ).is_absolute():
            raise WorkspaceError("workspace root must be a canonical absolute path")
        canonical = str(canonicalize_filesystem_path(self.workspace_root))
        if self.workspace_root != canonical:
            raise WorkspaceError(
                "workspace root must be a canonical absolute path"
            )
        if (
            not isinstance(self.registry_digest, str)
            or REGISTRY_DIGEST_RE.fullmatch(self.registry_digest) is None
        ):
            raise WorkspaceError("registry digest must be a lowercase SHA-256 digest")


def _git_stdout(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as error:
        raise WorkspaceError("git executable not found on PATH") from error
    except OSError as error:
        raise WorkspaceError("could not inspect repository with Git") from error
    if completed.returncode != 0:
        raise WorkspaceError("Git repository inspection failed")
    return completed.stdout.strip()


def resolve_workspace(
    workspace_root: str | Path, registry: RepositoryRegistry
) -> ResolvedWorkspace:
    """Resolve and verify every repository in ``registry`` without networking."""
    if not isinstance(registry, RepositoryRegistry):
        raise WorkspaceError("workspace resolution requires a RepositoryRegistry")
    root = canonicalize_filesystem_path(workspace_root)
    if not root.is_dir():
        raise WorkspaceError("workspace root must be an existing directory")

    resolved: list[ResolvedRepository] = []
    for repository in registry:
        candidate = canonicalize_filesystem_path(root / repository.path)
        if not is_descendant(candidate, root):
            raise WorkspaceError(
                f"repository {repository.alias} resolves outside the workspace"
            )
        if not candidate.is_dir():
            raise WorkspaceError(
                f"repository {repository.alias} must be an existing directory"
            )
        git_root = canonicalize_filesystem_path(
            _git_stdout(candidate, "rev-parse", "--show-toplevel")
        )
        if git_root != candidate:
            raise WorkspaceError(
                f"repository {repository.alias} path is not an exact Git top-level"
            )
        try:
            actual_origin = canonicalize_origin(
                _git_stdout(candidate, "remote", "get-url", "origin")
            )
        except WorkspaceError as error:
            raise WorkspaceError(
                f"repository {repository.alias} has no valid origin remote"
            ) from error
        if actual_origin != repository.origin:
            raise WorkspaceError(
                f"repository {repository.alias} origin does not match its "
                "registry entry"
            )
        resolved.append(
            ResolvedRepository(
                repository=repository, root=candidate, origin=actual_origin
            )
        )

    for index, repository in enumerate(resolved):
        for other in resolved[index + 1 :]:
            if is_descendant(
                repository.root, other.root, allow_equal=True
            ) or is_descendant(other.root, repository.root, allow_equal=True):
                raise WorkspaceError(
                    "resolved repository roots must not overlap: "
                    f"{repository.alias} and {other.alias}"
                )
    return ResolvedWorkspace(root=root, repositories=tuple(resolved))
