# ruff: noqa: F401, F403, F405, I001
"""What a turn's completion claim has to survive, asserted at the boundary.

The behavioural coverage of the guards themselves already lives where it always
has — ``tests/test_asl_run_handoff_status.py`` drives real runs into the plan-artifact
and mailbox refusals, ``tests/test_asl_review_*.py`` and
``tests/test_asl_review_lifecycle_*.py`` own the review-mutation guard's run-level
accounting, and ``tests/test_asl_progress.py`` owns what an operator reads out
of ``progress.md``. What is asserted here is the contract those modules cannot
state, because they exercise the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* the **verify → progress → ledgers** ordering that :func:`_append_progress_entry`
  is the middle step of, pinned against the loop that implements it rather than
  restated in prose — a journal row written before verification would record
  work nobody checked, and a ledger synced before the journal would describe a
  session the journal never got;
* every durable write this boundary makes, named per function, and every
  result-file write taken under the run's one mutex — the guards write a plan
  file, a journal row, a turn result and two events, which is more filesystem
  reach than any other extracted module has, so "which function may write what"
  has to be a checked list rather than a convention;
* the two seams the loop still binds are structural, so drift in either is
  silent: ``handoff_correction._append_handoff_note`` has to keep matching
  :class:`handoff_guards.HandoffNoteAppender`, which stays a Protocol because
  ``handoff_correction`` imports this module and so cannot be imported back,
  and ``session_verification._reinspect_after_revert`` has to keep matching
  ``run_termination.SnapshotReinspector``; and
* neither seam is ever defaulted, so no path can silently skip the note or the
  post-revert re-read.

The seam and ordering groups are why this file exists. A note appender whose
signature drifted still type-checks at call sites that pass it by keyword, and
a revert that skipped its re-read publishes a snapshot describing the plan as
it was *before* the guard put the session back to pending — which is exactly
the record an operator reaches for to find out what the guard did.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_correction,
    handoff_guards,
    harnesses,
    plans_content_hash,
    plans_errors,
    plans_handoff,
    plans_models,
    plans_progress,
    plans_snapshot,
    repository_intent,
    run_persistence,
    run_termination,
    runner,
    session_verification,
    turn_execution_commit,
    turn_settlement,
)
from tests.support import assert_no_runner_import, called_names, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_session_path_in_snapshot",
    "_append_progress_entry",
    "_revert_session_status_to_pending",
    "_mailbox_completion_guard_evidence",
    "_enforce_mailbox_completion_guard",
    "_read_review_verdict",
    "_enforce_review_mutation_guard",
    "_terminating_result_disposition",
    "_enforce_plan_artifact_guard",
    "_capture_workspace_write_guard_snapshot",
    "_capture_legacy_implementation_snapshot",
    "_legacy_unobserved_repositories",
)

#: Every call this boundary can make that outlives the process: a file write, a
#: journal append, a turn-result write, and the two events a refusal emits.
#: Anything a guard learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when a
#: new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
        "mkdir",
        "record_turn_rejected",
        "replace",
        "unlink",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
        "_pause_run",
    }
)

#: Which module-level function makes which durable write. Stated per function
#: rather than per module because the interesting failure is not "this module
#: writes" -- it plainly does -- but a *guard* learning to append to the journal,
#: or the journal append learning to rewrite a session file.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_append_progress_entry": frozenset({"append_progress_entry"}),
    "_revert_session_status_to_pending": frozenset({"write_text"}),
    "_enforce_mailbox_completion_guard": frozenset(
        {"write_json_file", "record_turn_rejected", "_pause_run"}
    ),
    "_enforce_review_mutation_guard": frozenset(
        {"write_json_file", "record_turn_rejected", "_pause_run"}
    ),
    "_enforce_plan_artifact_guard": frozenset(
        {"write_json_file", "record_turn_rejected", "_pause_run"}
    ),
}


class _RecordingAppender:
    """A ``HandoffNoteAppender`` that records instead of rewriting."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        lines: list[str],
        *,
        handoff_index: int,
        section_end: int,
        note: str,
    ) -> list[str]:
        self.calls.append(
            {
                "handoff_index": handoff_index,
                "section_end": section_end,
                "note": note,
            }
        )
        return [*lines, f"- **Note**: {note}"]


class _PlanCase(unittest.TestCase):
    """Shared scaffolding: a real two-session plan on disk."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.plan_dir = self.tmp_dir / "plan"
        write_plan(self.plan_dir, ("complete", "pending"))
        self.appender = _RecordingAppender()

    def snapshot(self) -> plans_models.PlanSnapshot:
        return plans_snapshot.inspect_plan(self.plan_dir)

    def session_path(self, session_id: str) -> Path:
        return self.plan_dir / f"session-{session_id}.md"


__all__ = (
    "ast",
    "importlib",
    "inspect",
    "json",
    "tempfile",
    "unittest",
    "Path",
    "Any",
    "mock",
    "events",
    "handoff_correction",
    "handoff_guards",
    "harnesses",
    "plans_content_hash",
    "plans_errors",
    "plans_handoff",
    "plans_models",
    "plans_progress",
    "plans_snapshot",
    "repository_intent",
    "run_persistence",
    "run_termination",
    "runner",
    "session_verification",
    "turn_execution_commit",
    "turn_settlement",
    "assert_no_runner_import",
    "called_names",
    "write_plan",
    "MOVED_SYMBOLS",
    "DURABLE_WRITE_NAMES",
    "DURABLE_WRITES",
    "_RecordingAppender",
    "_PlanCase",
)
