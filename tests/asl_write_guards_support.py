from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    guards,
    handoff_correction,
    handoff_guards,
    plans_errors,
    plans_session_scope,
    plans_snapshot,
    review_continuation,
    review_cycle,
    review_guard,
    run_loop_finalization,
    run_prompts,
    run_termination,
    runner,
    session_verification,
    turn_postflight,
    workspace_evidence,
    workspaces,
    write_guards,
    write_guards_legacy,
    write_guards_protected,
    write_guards_repair,
)
from tests.asl_workspace_evidence_support import _workspace
from tests.support import assert_no_runner_import, called_names, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_regenerate_stale_repair_child_scopes",
    "_pending_repair_child_continuation",
    "_enforce_workspace_write_guard",
    "_enforce_legacy_implementation_write_guard",
    "_protected_path_changed_paths",
    "_protected_path_review_changed_paths",
    "_enforce_protected_path_guard",
)

#: The module that owns each moved function and constant.
OWNERS: dict[str, Any] = {
    "_regenerate_stale_repair_child_scopes": write_guards_repair,
    "_pending_repair_child_continuation": write_guards_repair,
    "_enforce_workspace_write_guard": write_guards,
    "_enforce_legacy_implementation_write_guard": write_guards_legacy,
    "_protected_path_changed_paths": write_guards_protected,
    "_protected_path_review_changed_paths": write_guards_protected,
    "_enforce_protected_path_guard": write_guards_protected,
    "REPAIR_SCOPE_STALE_BLOCKER": write_guards_repair,
}
GUARD_MODULES = (
    write_guards,
    write_guards_legacy,
    write_guards_protected,
    write_guards_repair,
)

#: Moved with them, because its sole reader is
#: `_pending_repair_child_continuation`.
MOVED_CONSTANTS = ("REPAIR_SCOPE_STALE_BLOCKER",)

#: The three enforcement functions, which are the only ones that take seams and
#: the only ones that can refuse a turn.
ENFORCEMENT_FUNCTIONS = (
    "_enforce_workspace_write_guard",
    "_enforce_legacy_implementation_write_guard",
    "_enforce_protected_path_guard",
)

#: The collaborators this boundary does not own -- supplied by
#: ``handoff_correction._append_handoff_note`` and
#: ``session_verification._reinspect_after_revert`` -- and the names this
#: module receives them under. Neither may acquire a default: a refusal that
#: silently skipped the note or the re-read would leave a session `complete`
#: after the guard refused it.
SEAM_PARAMETERS = ("append_handoff_note", "reinspect")

#: Every call this boundary could make that outlives the process. Anything a
#: function here learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when
#: a new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "utime",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
    }
)

#: Which module-level function writes directly, and what it writes. Only the
#: turn result is written here: the session-file revert belongs to
#: `handoff_guards`, the scope widening to `workspace_evidence`, and the two
#: events to `turn_execution_commit` and `run_termination`, each of which owns that
#: write at its own boundary. Stating it per function rather than as a flat
#: prohibition is what makes a *fourth* writer appearing here a test failure
#: instead of a review catch.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_enforce_workspace_write_guard": frozenset({"write_json_file"}),
    "_enforce_legacy_implementation_write_guard": frozenset({"write_json_file"}),
    "_enforce_protected_path_guard": frozenset({"write_json_file"}),
}


def _resume(
    *,
    action: str = "run-repairs",
    eligible: bool = False,
    blocker: str | None = write_guards_repair.REPAIR_SCOPE_STALE_BLOCKER,
    gaps: tuple[dict[str, Any], ...] | None = ({"repair_session_id": "2"},),
) -> review_continuation.ReviewResume:
    """One review continuation projection, named only by what the gate reads."""
    return review_continuation.ReviewResume(
        source_cycle=1,
        review_session_id="3",
        source_disposition="findings",
        action=cast(Any, action),
        eligible=eligible,
        blocker=blocker,
        historical_materializations=1,
        minimum_required_cap=1,
        selected_cap=1,
        repair_scope_gaps=gaps,
    )


# ``import *`` skips underscore-prefixed names, so export every name explicitly.
__all__ = [name for name in globals() if not name.startswith("__")]
