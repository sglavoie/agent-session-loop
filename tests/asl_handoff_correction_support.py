"""Boundary tests for `agent_session_loop.handoff_correction`.

The per-function behavioural coverage of this boundary already exists and stays
where it is: `tests/test_asl_turn_finalization.py` owns the defect
classification table, the preserved-artifact digests and the attempt counter,
and `tests/test_asl_run_handoff_status.py` drives a real malformed handoff through a
real run and asserts what an operator sees afterwards. Neither of them can cheaply
state the things this module owns, so they are stated here: the extraction
contract (runner-free, no alias left behind, exactly the symbols the session
moved), the per-function durable-write list, the import direction that keeps
`handoff_guards.HandoffNoteAppender` a Protocol rather than an import, and the
one property the session file names outright -- that a correction turn is armed
*without* disturbing the review artifacts the malformed handoff says nothing
about.

The correction turn is driven for real against a plan on disk, with the
persistence handle's state writes switched off and the events appended to a
temporary path. Nothing here reaches Git or `verification_run.run_verification`, so
nothing spawns a process, which is what keeps this module in
`PORTABLE_MODULES`; `test_the_correction_turn_spawns_no_process` asserts that
rather than assuming it.
"""

from __future__ import annotations

from pathlib import Path

from agent_session_loop import (
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_classify_mechanical_handoff_defect",
    "_preserved_review_artifact_evidence",
    "_handoff_correction_attempt",
    "_handoff_correction_note",
    "_correct_malformed_handoff",
    "_uninspectable_handoff_note",
    "_reopen_uninspectable_handoff",
    "_paused_handoff_status_note",
    "_repair_paused_handoff_status",
    "_append_handoff_note",
)

#: The class and the four constants that moved with them.
MOVED_CLASSES = ("_HandoffDefect",)
MOVED_CONSTANTS = (
    "MAX_HANDOFF_CORRECTION_ATTEMPTS",
    "HANDOFF_DEFECT_MISSING_FILES",
    "HANDOFF_DEFECT_PENDING_PLACEHOLDER",
    "HANDOFF_DEFECT_UNQUALIFIED_ENTRY",
)

#: Every call this boundary could make that outlives the process. Anything a
#: function here learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when a
#: new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
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

#: Which module-level function writes directly, and what it writes. The
#: session-file rewrite belongs to `handoff_guards`, which owns that write at
#: its own boundary and is the single writer of a `session-<id>.md`; what is
#: left here is the correction turn's own bookkeeping and the reopen event the
#: paused-status repair records on its own behalf. Stating it per function
#: rather than as a flat prohibition is what makes a *third* writer appearing
#: here a test failure instead of a review catch.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_correct_malformed_handoff": frozenset(
        {"write_json_file", "append_event", "write_state"}
    ),
    "_repair_paused_handoff_status": frozenset({"append_event"}),
}


def resolved_workspace(root: Path) -> workspaces.ResolvedWorkspace:
    """A registered workspace built from names, with no checkout behind it.

    Only ``registry.aliases`` is read by this boundary, so the repositories can
    be declared rather than cloned -- which is what keeps this module portable.
    """
    repositories = tuple(
        workspaces.ResolvedRepository(
            repository=workspaces.Repository(alias, path, origin),
            root=root / path,
            origin=origin,
        )
        for alias, path, origin in DEFAULT_MULTI_REPO_SPECS
    )
    return workspaces.ResolvedWorkspace(root=root, repositories=repositories)


def set_handoff(session_path: Path, *, status: str, files_modified: str) -> None:
    """Rewrite one session's handoff status and ``Files modified`` line."""
    lines = session_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        if line.startswith("- **Status**:"):
            rewritten.append(f"- **Status**: {status}")
        elif line.startswith("- **Files modified**:"):
            rewritten.append(f"- **Files modified**: {files_modified}")
        else:
            rewritten.append(line)
    session_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")
