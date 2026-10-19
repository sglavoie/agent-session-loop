"""What a completed turn has to survive before its handoff is believed.

A worker's turn ends by claiming something: this session is complete, here is
the handoff. That claim is not taken at face value. Between the harness
returning and the run recording the work, three guards ask whether the claim
was earned -- whether a blocking mailbox question was really answered, whether
the review session touched only the plan directory, and whether the turn
rewrote a session artifact it was never given. Only a turn that clears all
three reaches the progress journal.

This module owns that stretch of the turn, and the two writes at either end of
it: :func:`_append_progress_entry`, the one durable append to the plan's
``progress.md``, and :func:`_revert_session_status_to_pending`, the one way a
session's ``complete`` claim is taken back.

Durable records this boundary writes:

* ``progress.md`` -- one row per verified-complete session, appended by
  :func:`_append_progress_entry`. The append is idempotent: a repeat call for
  a ``(session_id, completed_at)`` pair the journal already ends with is a
  no-op returning ``False``, so a resumed run cannot double-write the row it
  crashed after writing.
* ``session-<id>.md`` -- rewritten in place by
  :func:`_revert_session_status_to_pending`, which puts the session's
  ``- **Status**:`` line back to ``pending`` and appends a note under
  ``- **Notes**`` saying which guard refused it. Nothing else here rewrites a
  session file.
* ``turn_payload["mailbox_completion_guard"]``,
  ``turn_payload["review_mutation_guard"]``, ``turn_payload["review_repo_state"]``
  and ``turn_payload["plan_artifact_guard"]`` -- each guard's verdict, passed
  or failed, written to the turn's result file under the run's mutex.
* ``payload["final_snapshot"]`` -- refreshed from the re-read plan whenever a
  guard reverted a session, so the published snapshot never describes the plan
  as it was before the revert.
* One ``turn-rejected`` event and one ``run-paused`` event per failed guard,
  through ``turn_execution_commit.record_turn_rejected`` and
  ``run_termination._pause_run``.

Nothing here imports the runner. The event path, the payload, the persistence
handle, the plan snapshots, the snapshot cache and the resolved workspace all
enter as parameters. The two collaborators the caller supplies enter as seams:
the handoff-note appender, through :class:`HandoffNoteAppender`, which
``handoff_correction._append_handoff_note`` satisfies; and the post-revert plan
re-read, through ``run_termination.SnapshotReinspector``, which Session 15
declared for exactly this shape and
``session_verification._reinspect_after_revert`` satisfies.
Both are required keyword-only arguments, never defaulted, so a guard cannot
silently skip the note or the re-read by omitting them.

Invariants that survive the move:

* **Verify, then progress, then ledgers.** The runner's own docstring promises
  that ordering and :func:`_append_progress_entry` is the middle step of it.
  Only a verified-complete handoff reaches the journal, and the journal write
  happens before the derived status ledgers are re-synced, so a ledger can
  never describe a session the journal has not recorded.
* **A failed guard always reverts.** Leaving a session ``complete`` after a
  guard refused it would make the unguarded work permanent across a resume, so
  every refusal calls :func:`_revert_session_status_to_pending` before it
  pauses, and re-reads the plan afterwards rather than publishing the
  pre-revert snapshot.
* **The guard runs before the branch that would end the turn.**
  :func:`_terminating_result_disposition` names the result branch that would
  otherwise end the turn, and the plan-artifact guard runs first on those
  paths: a retryable pause or a mid-turn continue would otherwise relaunch
  against the tampered packet and adopt it as the new baseline.
* **A place you did not look is not evidence of no change.**
  :func:`_capture_legacy_implementation_snapshot` fingerprints exactly one
  repository, so :func:`_legacy_unobserved_repositories` re-derives every
  other repository the turn reached and reports it through the guard's
  fail-closed ``unavailable`` lane rather than letting silence read as a pass.
* **Registered repositories only.**
  :func:`_capture_workspace_write_guard_snapshot` fingerprints exactly the
  repositories the plan registered, so a concurrent run in a sibling worktree
  never appears as evidence about this turn.
"""

from __future__ import annotations

from collections import OrderedDict as OrderedDict
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import dataclass as dataclass
from pathlib import Path as Path
from typing import Any as Any
from typing import Protocol as Protocol

from . import durability as durability
from . import events as events
from . import harnesses as harnesses
from . import mailbox as mailbox
from . import mailbox_constants as mailbox_constants
from . import mailbox_cursors as mailbox_cursors
from . import mailbox_models as mailbox_models
from . import mailbox_vocabulary as mailbox_vocabulary
from . import plans_content_hash as plans_content_hash
from . import plans_errors as plans_errors
from . import plans_handoff as plans_handoff
from . import plans_models as plans_models
from . import plans_payloads as plans_payloads
from . import plans_progress as plans_progress
from . import plans_snapshot as plans_snapshot
from . import redirect_continuation as redirect_continuation
from . import repository_evidence as repository_evidence
from . import repository_intent as repository_intent
from . import review as review
from . import review_guard as review_guard
from . import run_persistence as run_persistence
from . import run_termination as run_termination
from . import turn_execution_commit as turn_execution_commit
from . import workspaces as workspaces
from .handoff_guard_artifacts import (
    _enforce_plan_artifact_guard as _enforce_plan_artifact_guard,
)
from .handoff_guard_artifacts import (
    _terminating_result_disposition as _terminating_result_disposition,
)
from .handoff_guard_mailbox import (
    _enforce_mailbox_completion_guard as _enforce_mailbox_completion_guard,
)
from .handoff_guard_mailbox import (
    _mailbox_completion_guard_evidence as _mailbox_completion_guard_evidence,
)
from .handoff_guard_review import (
    _enforce_review_mutation_guard as _enforce_review_mutation_guard,
)
from .handoff_guard_review import _read_review_verdict as _read_review_verdict
from .handoff_guard_sessions import HandoffNoteAppender as HandoffNoteAppender
from .handoff_guard_sessions import SessionRevert as SessionRevert
from .handoff_guard_sessions import _append_progress_entry as _append_progress_entry
from .handoff_guard_sessions import _reopened as _reopened
from .handoff_guard_sessions import (
    _revert_session_status_to_pending as _revert_session_status_to_pending,
)
from .handoff_guard_sessions import (
    _session_path_in_snapshot as _session_path_in_snapshot,
)
from .handoff_guard_sessions import session_status_reverter as session_status_reverter
from .handoff_guard_snapshots import (
    _capture_legacy_implementation_snapshot as _capture_legacy_implementation_snapshot,
)
from .handoff_guard_snapshots import (
    _capture_workspace_write_guard_snapshot as _capture_workspace_write_guard_snapshot,
)
from .handoff_guard_snapshots import (
    _legacy_unobserved_repositories as _legacy_unobserved_repositories,
)
