"""Review guard allowlist, external-change aliases and commit-at-end scope."""

from __future__ import annotations

import argparse
import os
from pathlib import PurePosixPath

from . import plans_models, plans_payloads, verification_models
from .run_errors import RunError

# Repo-relative paths the review mutation guard ignores, on top of any
# ``--review-guard-exclude`` values. Tracked files the harness rewrites
# mid-turn (its own settings, caches) would otherwise revert a complete review
# turn.
REVIEW_GUARD_EXCLUDE_ENV_VAR = "ASL_REVIEW_GUARD_EXCLUDE"


def _review_guard_excludes(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve the review mutation guard's allowlist as sorted POSIX paths.

    Merges :data:`REVIEW_GUARD_EXCLUDE_ENV_VAR` (``os.pathsep``-separated) with
    every ``--review-guard-exclude`` value. Absolute paths and ``..`` segments
    raise :class:`RunError` so an allowlist can never reach outside the
    repository, and so a typo fails the run instead of silently widening the
    guard's blind spot.
    """
    raw: list[str] = []
    env_value = os.environ.get(REVIEW_GUARD_EXCLUDE_ENV_VAR)
    if env_value:
        raw.extend(env_value.split(os.pathsep))
    raw.extend(getattr(args, "review_guard_exclude", None) or ())

    excludes: set[str] = set()
    for entry in raw:
        candidate = entry.strip()
        if not candidate:
            continue
        pure = PurePosixPath(candidate)
        if pure.is_absolute():
            raise RunError(
                "--review-guard-exclude must be repo-relative, got "
                f"absolute path {candidate!r}"
            )
        if ".." in pure.parts:
            raise RunError(
                "--review-guard-exclude must not contain '..' segments, got "
                f"{candidate!r}"
            )
        normalized = pure.as_posix().rstrip("/")
        if normalized:
            excludes.add(normalized)
    return tuple(sorted(excludes))


def _review_guard_include_ignored(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "review_guard_include_ignored", False))


def _external_change_aliases(
    args: argparse.Namespace,
    context: verification_models.VerificationContext,
) -> frozenset[str]:
    """Resolve ``--allow-external-changes`` against the plan's registry.

    Only a registered workspace has aliases to name, and an alias the registry
    does not know is a typo that would otherwise leave the guard strict while
    the operator believes it relaxed, so both fail the run.
    """
    requested = frozenset(getattr(args, "allow_external_changes", None) or ())
    if not requested:
        return frozenset()
    if context.workspace is None:
        raise RunError(
            "--allow-external-changes requires a registered multi-repository plan"
        )
    unknown = sorted(requested - set(context.workspace.registry.aliases))
    if unknown:
        raise RunError(
            "--allow-external-changes names unregistered aliases: " + ", ".join(unknown)
        )
    return requested


def _validate_commit_at_end_scope(
    *,
    snapshot: plans_models.PlanSnapshot,
    max_sessions: int | None,
) -> None:
    if max_sessions is None:
        return
    pending_count = plans_payloads.status_counts(snapshot)["pending"]
    if max_sessions < pending_count:
        raise RunError(
            "--commit-at-end cannot be combined with --max-sessions when "
            "not all currently pending sessions can complete in one invocation"
        )
