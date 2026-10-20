"""Every git the run loop shells out to, and the refusals built on it.

The run loop makes claims about a repository it does not own: that the review
turn changed nothing outside its allowlist, that the worktree was clean before
auto-commit, that a generated repair child is not about to carry work it never
wrote. Each of those claims bottoms out in a ``git`` subprocess, and a claim is
only as honest as the process that produced it. This module owns that surface
so the loop is left deciding *what* to refuse rather than reimplementing *how*
git is invoked and how its failures are classified.

Four things live here:

* **The git shell.** :func:`_run_git` and :func:`_run_git_bytes` are the only
  entry points through which the run loop spawns ``git``; the spawn itself is
  :func:`agent_session_loop.git_subprocess.run_git_or_raise`. Both convert a
  missing executable or an OS-level failure into
  :class:`~agent_session_loop.run_errors.RunError`, so no caller above them
  ever handles a raw :class:`OSError`.
* **Repository fingerprinting.** The compatibility wrappers over
  :mod:`agent_session_loop.repository_evidence` that the review mutation guard
  captures before and after a turn, compares, and renders.
* **The allowlist.** :func:`_review_guard_excludes` resolves the review guard's
  exclusions from :data:`REVIEW_GUARD_EXCLUDE_ENV_VAR` and the CLI.
* **Commit preflight.** The clean-worktree gates the run takes before
  auto-commit, and the far narrower gate a generated repair child takes before
  its own owned commit.

Nothing here writes a durable record. Every function is a projection of the
filesystem, a parsed namespace, or a prior durable record into a value or an
exception; the run loop keeps the event log, the state payload, and the pause.

Nothing here imports the runner either. The one collaborator this module does
not own -- the projection that classifies a workspace comparison against a
session's write scope -- enters through the :class:`WorkspaceEvidenceProbe`
seam, which ``workspace_evidence`` satisfies at the single call site in
``turn_preparation``, and which is what lets the retained-scope branch of
commit preflight be typed without a cycle.

Four invariants survive the move and are load-bearing:

* **The allowlist is a refusal, not a filter.** An absolute path or a ``..``
  segment raises rather than being dropped, so an allowlist can never reach
  outside the repository and a typo fails the run instead of silently widening
  the guard's blind spot.
* **Fingerprint bytes are not decoded.** :func:`_run_git_bytes` exists because
  text mode translates ``\\r\\n`` and a lone ``\\r`` to ``\\n``, which collapses
  values differing only in those bytes onto one another and would report two
  distinct contents as unchanged.
* **A sanction requires proof.** :class:`_RetainedContentEvidence` reports
  ``available=False`` whenever the retained turn's own after-state could not be
  compared against the worktree now, and :func:`_repair_commit_preflight`
  sanctions nothing on that path. A pathname is not an origin: the bytes at a
  retained path now are whatever the last writer put there.
* **Preflight is fail-closed and runs before the harness.** Dirt the child did
  not author raises :class:`RepairCommitPreflightError` before a turn is
  launched, rather than riding along into a commit made under the repair's
  name.
"""

from __future__ import annotations

from .review_guard_commit import (
    _validate_commit_preflight as _validate_commit_preflight,
)
from .review_guard_commit import (
    commit_preflight_pause_details as commit_preflight_pause_details,
)
from .review_guard_config import (
    REVIEW_GUARD_EXCLUDE_ENV_VAR as REVIEW_GUARD_EXCLUDE_ENV_VAR,
)
from .review_guard_config import (
    _external_change_aliases as _external_change_aliases,
)
from .review_guard_config import (
    _review_guard_excludes as _review_guard_excludes,
)
from .review_guard_config import (
    _review_guard_include_ignored as _review_guard_include_ignored,
)
from .review_guard_config import (
    _validate_commit_at_end_scope as _validate_commit_at_end_scope,
)
from .review_guard_fingerprint import (
    _capture_review_repo_fingerprint as _capture_review_repo_fingerprint,
)
from .review_guard_fingerprint import (
    _default_excludes_file as _default_excludes_file,
)
from .review_guard_fingerprint import (
    _effective_excludes_file as _effective_excludes_file,
)
from .review_guard_fingerprint import (
    _index_flagged_paths as _index_flagged_paths,
)
from .review_guard_fingerprint import (
    _review_guard_changed_paths as _review_guard_changed_paths,
)
from .review_guard_fingerprint import (
    _review_guard_changed_summary as _review_guard_changed_summary,
)
from .review_guard_fingerprint import (
    _review_guard_unavailable_detail as _review_guard_unavailable_detail,
)
from .review_guard_fingerprint import (
    _review_repo_state as _review_repo_state,
)
from .review_guard_git import (
    _git_status_is_clean as _git_status_is_clean,
)
from .review_guard_git import (
    _porcelain_dirty_paths as _porcelain_dirty_paths,
)
from .review_guard_git import (
    _read_git_head as _read_git_head,
)
from .review_guard_git import (
    _run_git as _run_git,
)
from .review_guard_git import (
    _run_git_bytes as _run_git_bytes,
)
from .review_guard_git import (
    _validate_clean_git_status as _validate_clean_git_status,
)
from .review_guard_git import (
    _validate_git_worktree as _validate_git_worktree,
)
from .review_guard_repair import (
    _repair_commit_preflight as _repair_commit_preflight,
)
from .review_guard_retained import (
    RepairCommitPreflightError as RepairCommitPreflightError,
)
from .review_guard_retained import (
    WorkspaceEvidenceProbe as WorkspaceEvidenceProbe,
)
from .review_guard_retained import (
    _retained_turn_changed_paths as _retained_turn_changed_paths,
)
from .review_guard_retained import (
    _retained_turn_content_drift as _retained_turn_content_drift,
)
from .review_guard_retained import (
    _RetainedContentEvidence as _RetainedContentEvidence,
)
from .run_errors import RunError as RunError
