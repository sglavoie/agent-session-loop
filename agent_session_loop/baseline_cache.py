"""Baseline verification result cache for completion contracts.

Sessions frequently share the same verification recipe (for example the same
``python3 -m unittest ...`` invocation). Running those checks once and reusing
the result across sessions avoids redundant, expensive executions.

This module provides:

- :func:`normalize_recipe` — a deterministic, name-independent projection of a
  :class:`~agent_session_loop.contracts.VerifyRecipe` so equivalent recipes in
  different sessions hash to the same value (deduplication).
- :func:`repository_fingerprint` — the legacy git-aware fingerprint that changes
  when the working tree is dirty, staged, or has untracked files, so stale cache
  entries are invalidated.
- :func:`cache_key` / :func:`compute_cache_key` — a composite key over the
  normalized recipe, working directory, repository fingerprint, optional
  registry digest, executable identity, and relevant environment.
- :class:`BaselineCache` — a small JSON-backed store persisted to the derived
  ``baseline-results.json`` journal. The journal is *not* one of the files hashed
  by ``plan_content_sha256``/``stable_readiness_sha256`` (those hash only
  ``context.md``, ``manifest.md``, session files, and expansion files), so it is
  excluded from content and readiness hashes by construction.

The module is stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import contracts, durability
from .contracts import VerifyRecipe
from .git_subprocess import run_git
from .plans_files import canonicalize_path

#: Derived journal filename. Excluded from content/readiness hashes because the
#: hashers only walk context.md, manifest.md, session files, and expansion files.
BASELINE_RESULTS_FILENAME = "baseline-results.json"

#: On-disk schema version for :data:`BASELINE_RESULTS_FILENAME`.
BASELINE_CACHE_VERSION = 1

#: Environment variables that participate in the cache key by default. ``PATH``
#: changes executable resolution and is the most relevant to command checks.
DEFAULT_ENVIRONMENT_KEYS: tuple[str, ...] = ("PATH",)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_check(check: contracts.VerificationCheck) -> dict:
    """Return a name-independent projection of a single verification check.

    ``name`` is deliberately excluded so that two sessions running the same
    command under different check names still deduplicate to one recipe.
    """
    data: dict[str, object] = {
        "type": check.kind,
        "baseline_expectation": getattr(
            check, "baseline_expectation", contracts.DEFAULT_BASELINE_EXPECTATION
        ),
    }
    repo = getattr(check, "repo", None)
    if repo is not None:
        data["repo"] = repo
    if isinstance(check, contracts.CommandCheck):
        data["argv"] = list(check.argv)
        data["cwd"] = check.cwd
        data["timeout_seconds"] = contracts.baseline_check_budget_seconds(check)
    elif isinstance(check, contracts.FileExistsCheck):
        data["path"] = check.path
    elif isinstance(check, contracts.RegexInFileCheck):
        data["path"] = check.path
        data["pattern"] = check.pattern
    return data


def normalize_recipe(recipe: VerifyRecipe) -> str:
    """Return a deterministic, name-independent JSON projection of ``recipe``.

    Equivalent recipes (identical checks, possibly under different check names or
    session ids) produce identical strings, which is what lets the cache
    deduplicate executions across sessions.
    """
    payload = {"checks": [_normalized_check(check) for check in recipe.checks]}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = run_git(os.fspath(root), args, chdir=True)
    except OSError, ValueError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def repository_fingerprint(cwd: str | Path) -> str:
    """Return a fingerprint of the repository state rooted at ``cwd``.

    The fingerprint combines the current ``HEAD`` commit with a full porcelain
    status listing (including untracked files), so dirty working-tree changes,
    staged changes, and untracked files each change the fingerprint and thereby
    invalidate cache entries. Non-git directories fall back to a stable,
    path-derived marker.
    """
    root = canonicalize_path(cwd)
    head = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if head is None and status is None:
        return "non-git:" + _sha256("nogit\0" + os.fspath(root))
    digest = hashlib.sha256()
    digest.update((head or "no-head").encode("utf-8"))
    digest.update(b"\0")
    digest.update((status or "").encode("utf-8"))
    return digest.hexdigest()


def executable_identity(argv: object) -> str:
    """Return an identity string for a command's executable.

    The first argv element is resolved through ``PATH``; the resolved absolute
    path (or the raw program name when unresolved) identifies the executable so
    swapping interpreters invalidates the cache.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        return ""
    program = str(argv[0])
    resolved = shutil.which(program)
    return resolved or program


def _recipe_executable_identity(recipe: VerifyRecipe) -> str:
    for check in recipe.checks:
        if isinstance(check, contracts.CommandCheck):
            return executable_identity(list(check.argv))
    return ""


def environment_signature(
    env: Mapping[str, str] | None = None,
    keys: tuple[str, ...] = DEFAULT_ENVIRONMENT_KEYS,
) -> str:
    """Return a signature over the relevant subset of environment variables."""
    source = os.environ if env is None else env
    selected = {key: source.get(key, "") for key in keys}
    return _sha256(json.dumps(selected, sort_keys=True))


def cache_key(
    *,
    recipe: VerifyRecipe,
    cwd: str | Path,
    repo_fingerprint: str,
    executable: str,
    environment: str,
    registry_digest: str | None = None,
) -> str:
    """Return the composite cache key for a recipe execution."""
    payload = {
        "recipe": normalize_recipe(recipe),
        "cwd": os.fspath(canonicalize_path(cwd)),
        "repo": repo_fingerprint,
        "exe": executable,
        "env": environment,
    }
    # Omit the field for legacy plans so historical v1 keys remain byte-for-byte
    # stable. Registered workspaces include the ordered registry identity.
    if registry_digest is not None:
        payload["registry_digest"] = registry_digest
    material = json.dumps(payload, sort_keys=True)
    return _sha256(material)


def compute_cache_key(
    recipe: VerifyRecipe,
    cwd: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    environment_keys: tuple[str, ...] = DEFAULT_ENVIRONMENT_KEYS,
    registry_digest: str | None = None,
    aggregate_repository_fingerprint: str | None = None,
) -> str:
    """Compute the composite cache key from live repository/environment state."""
    return cache_key(
        recipe=recipe,
        cwd=cwd,
        repo_fingerprint=(
            aggregate_repository_fingerprint
            if aggregate_repository_fingerprint is not None
            else repository_fingerprint(cwd)
        ),
        executable=_recipe_executable_identity(recipe),
        environment=environment_signature(env, environment_keys),
        registry_digest=registry_digest,
    )


@dataclass
class BaselineRecord:
    """A single cached baseline execution result."""

    key: str
    recipe: str
    result: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "recipe": self.recipe, "result": self.result}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaselineRecord:
        return cls(
            key=str(data.get("key", "")),
            recipe=str(data.get("recipe", "")),
            result=dict(data.get("result", {})),
        )


class BaselineCache:
    """JSON-backed store persisted to the derived ``baseline-results.json``.

    Keyed by :func:`cache_key`, so identical recipes across sessions collapse to
    a single entry (deduplication). Loading fail-closes on an unrecognized schema
    version by starting empty, giving future readers a v1 guard.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, BaselineRecord] = {}

    @classmethod
    def load(cls, path: str | Path) -> BaselineCache:
        cache = cls(path)
        if not cache.path.exists():
            return cache
        try:
            data = json.loads(cache.path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return cache
        if not isinstance(data, dict) or data.get("version") != BASELINE_CACHE_VERSION:
            return cache
        entries = data.get("entries", {})
        if isinstance(entries, dict):
            for key, raw in entries.items():
                if isinstance(raw, dict):
                    cache._entries[str(key)] = BaselineRecord.from_dict(raw)
        return cache

    def get(self, key: str) -> BaselineRecord | None:
        return self._entries.get(key)

    def put(self, key: str, record: BaselineRecord) -> None:
        self._entries[key] = record

    def record(
        self, recipe: VerifyRecipe, cwd: str | Path, result: dict[str, Any]
    ) -> str:
        """Compute the key for ``recipe``/``cwd`` and store ``result`` under it.

        Returns the cache key. Identical recipes deduplicate to one entry.
        """
        key = compute_cache_key(recipe, cwd)
        self.put(
            key, BaselineRecord(key=key, recipe=normalize_recipe(recipe), result=result)
        )
        return key

    def entries(self) -> dict[str, BaselineRecord]:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": BASELINE_CACHE_VERSION,
            "entries": {key: record.to_dict() for key, record in self._entries.items()},
        }

    def save(self) -> None:
        """Atomically persist the cache to :attr:`path`."""
        text = json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        durability.replace_text(self.path, text)
