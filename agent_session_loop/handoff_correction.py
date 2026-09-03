"""The one bounded second chance a finished-but-unparsable handoff gets.

A worker can do a session's whole job and still leave a plan the runner cannot
read, because the ``Files modified`` line is the only part of a handoff whose
grammar the plan parser enforces. Without this boundary that turn ends as
``malformed-plan`` on a plan still claiming the session is ``complete``: the
work is on disk, the claim is unwithdrawn, and a relaunch has nothing to select.
This module is the layer that tells those two cases apart — a typo a worker can
be told how to fix, versus a plan that is broken for some other reason — and
repairs the first without ever discarding what the turn produced.

It owns three repairs:

* **The bounded correction turn.** :func:`_correct_malformed_handoff` runs only
  when the parse failure names *this* session's file.
  :func:`_classify_mechanical_handoff_defect` re-reads the handoff with registry
  validation switched off (the defect being classified is exactly what that
  validation refuses) and returns a :class:`_HandoffDefect` only for the three
  shapes a worker can correct inside its own Handoff block — a missing
  ``Files modified`` entry, the ``(none yet)`` placeholder left in place, and an
  entry that will not parse as ``Alias:path``.
  :func:`_handoff_correction_attempt` counts the run's own prior corrections for
  this session out of the event log, so the budget is
  ``MAX_HANDOFF_CORRECTION_ATTEMPTS`` per session per run rather than per
  process, and :func:`_handoff_correction_note` renders the instruction the
  reverted session file carries into the next turn.
* **The uninspectable-handoff reopen.** :func:`_reopen_uninspectable_handoff`
  covers everything the bounded correction does not. A defect with no mechanical
  classification used to pause on a plan that still said ``complete``; here the
  pre-turn session identity is returned to ``pending`` with
  :func:`_uninspectable_handoff_note` recording the parse failure, so the pause
  reports a plan that parses again and says whether the defect is repairable
  rather than only that the plan would not parse.
* **The paused-status repair.** :func:`_repair_paused_handoff_status` handles the
  worker that emits a structured ``RUNNER_PAUSE`` *and* writes ``paused`` as its
  status. The plan vocabulary has no such value, so the generic invalid-status
  branch would terminate the run as ``unknown-status`` and discard the reason the
  worker gave. Only the status line is wrong, so it is returned to ``pending``
  with :func:`_paused_handoff_status_note` and the structured pause keeps the
  run's cause. ``unknown-status`` stays the fallback for a pause naming no known
  reason and for any invalid status outside the selected session.

:func:`_append_handoff_note` is the note-insertion primitive all three repairs
render through, and the concrete implementation behind the
:class:`handoff_guards.HandoffNoteAppender` Protocol every other guard in the
package takes as a seam. It is a pure line-list transform: it finds or creates
the ``- **Notes**`` bullet inside the handoff section's bounds and returns the
rewritten lines, and it never touches the filesystem, which is what keeps
``handoff_guards._revert_session_status_to_pending`` the single writer of a
session file.

Durable records this boundary writes: the ``handoff_correction`` block of the
turn result and the refreshed ``final_snapshot``, both under the run's mutex;
one ``turn-handoff-correction`` event per armed correction turn; and one
``session-reopened`` event per paused-status repair. It rewrites no session file
itself — every one of the three repairs delegates that write to
``handoff_guards._revert_session_status_to_pending``, which owns it at its own
boundary. ``tests/test_asl_handoff_correction.py`` pins the write sites per
function, so a fourth writer appearing here is a test failure rather than a
review catch.

Nothing here imports the runner. The event path, the payload, the persistence
handle, the plan snapshots, the snapshot cache, the resolved workspace, the run
id, the turn index and the turn's paths all enter as parameters, and every
collaborator is read at its true owner. This module needs no Protocol seam of
its own: it is the last reader in the chain, importing ``handoff_guards`` for
the revert and ``session_verification`` for the post-revert re-read rather than
being imported by either. That direction is also why
``handoff_guards.HandoffNoteAppender`` stays a Protocol after this session —
both of its users import this module's transitive dependencies, so importing
:func:`_append_handoff_note` back would close a cycle.

Invariants that survive the move:

* **The work is never discarded, only the claim.** All three repairs rewrite the
  status line and append a note; not one of them touches the files the turn
  produced, and the correction note says so in as many words, so the next turn
  fixes one line instead of redoing the session.
* **The correction is bounded and the exhausted case still reports.**
  :func:`_correct_malformed_handoff` returns its evidence even when the budget is
  spent or the revert fails, so the pause that follows says what was tried
  rather than only that the plan would not parse. ``exhausted`` is set on every
  such exit, including the one where the revert restored a plan that still will
  not parse.
* **A revert that does not restore a readable plan arms nothing.** The
  correction re-reads the plan after reverting and, if that read still fails,
  records ``revert_plan_error`` and launches no correction turn — something
  other than this handoff is broken and a second turn would not converge.
* **Review artifacts outlive a malformed handoff.** ``final-review.md`` and
  ``final-review.json`` have their own validator, so
  :func:`_preserved_review_artifact_evidence` digests them and records the
  verdict *before* the session is re-run. The correction turn is then
  accountable for leaving them intact, and its note says to leave them alone.
* **A repair only fires on its own session's defect.** Both the correction and
  the reopen require the plan error to name the selected session's file, and the
  paused-status repair requires the invalid-status pause to name exactly that one
  session. A plan broken elsewhere is left to the branch that owns it.
* **A repairable defect is not a promise.** ``_reopen_uninspectable_handoff``
  reports ``repairable`` as false once the bounded correction has already failed
  once; reopening past that point is for the operator's benefit, not a claim
  that a rerun would converge.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    handoff_guards,
    plans,
    review,
    run_inputs,
    run_persistence,
    session_verification,
    turn_execution,
    workspaces,
)

# Post-turn handoff correction control. A completed handoff whose ``Files
# modified`` line is mechanically malformed — missing, still carrying the
# pending placeholder, or holding an entry that is not ``Alias:path`` — stops
# the whole plan from parsing, which used to end the run in a ``malformed-plan``
# pause even though the defect is one line the worker can rewrite. The runner
# instead reverts that session to ``pending`` once, names the defect in the
# Handoff note, and re-runs it. One attempt is the whole budget: a worker that
# reproduces the same unparsable handoff after being told exactly what is wrong
# is not converging, and every further turn costs a full harness launch.
MAX_HANDOFF_CORRECTION_ATTEMPTS = 1


HANDOFF_DEFECT_MISSING_FILES = "missing-files-modified"
HANDOFF_DEFECT_PENDING_PLACEHOLDER = "pending-files-modified-placeholder"
HANDOFF_DEFECT_UNQUALIFIED_ENTRY = "unqualified-files-modified-entry"


@dataclass(frozen=True)
class _HandoffDefect:
    """A ``Files modified`` defect the worker can repair by rewriting one line."""

    code: str
    detail: str
    instruction: str


def _classify_mechanical_handoff_defect(
    snapshot: plans.PlanSnapshot,
    session_id: str,
    *,
    registry_aliases: Sequence[str],
) -> _HandoffDefect | None:
    """Name the ``Files modified`` defect that left a completed handoff unparsable.

    Reads the handoff with registry validation switched off, because the defect
    being classified is exactly what that validation refuses. Only shapes the
    worker can correct inside its own Handoff block are returned: an entry that
    parses but names a repository outside the session's Writes is a scope
    violation rather than a typo, and keeps the pause it earns.
    """
    session_status = next(
        (item for item in snapshot.sessions if item.id == session_id),
        None,
    )
    if session_status is None:
        return None
    try:
        handoff = plans.read_session_handoff(
            session_status.session, repository_registry=None
        )
    except plans.PlanError:
        return None
    if handoff.status != "complete":
        return None
    aliases = ", ".join(registry_aliases)
    inline = handoff.files_modified[0].strip() if handoff.files_modified else ""
    if not inline:
        return _HandoffDefect(
            code=HANDOFF_DEFECT_MISSING_FILES,
            detail="the completed handoff carries no Files modified entry",
            instruction=(
                "list every file you changed on the Files modified line as "
                f"comma-separated Alias:path entries (aliases: {aliases}), or "
                "write (none) if the session changed no files"
            ),
        )
    if inline == "(none yet)":
        return _HandoffDefect(
            code=HANDOFF_DEFECT_PENDING_PLACEHOLDER,
            detail="the completed handoff still reads (none yet)",
            instruction=(
                "replace (none yet) on the Files modified line with the "
                "Alias:path entries you actually changed "
                f"(aliases: {aliases}), or with (none)"
            ),
        )
    if inline == "(none)":
        return None
    for entry in (part.strip() for part in inline.split(",")):
        try:
            plans.parse_qualified_path(entry, registry_aliases)
        except ValueError as error:
            return _HandoffDefect(
                code=HANDOFF_DEFECT_UNQUALIFIED_ENTRY,
                detail=f"Files modified entry {entry!r} is invalid: {error}",
                instruction=(
                    f"rewrite the Files modified entry {entry!r} as "
                    f"Alias:path using one of the declared aliases ({aliases})"
                ),
            )
    return None


def _preserved_review_artifact_evidence(plan_dir: Path) -> dict[str, Any] | None:
    """Digest the review artifacts a malformed handoff says nothing about.

    ``final-review.md`` and ``final-review.json`` are separate files with their
    own validator, so a handoff that fails to parse is no reason to discard
    them. Recording their digests and verdict before the session is re-run
    makes the correction turn accountable for leaving them intact.
    """
    output_path = review.final_review_output_path(plan_dir)
    artifact_path = review.final_review_artifact_path(plan_dir)
    if not output_path.is_file() and not artifact_path.is_file():
        return None
    evidence: dict[str, Any] = {}
    if output_path.is_file():
        evidence["review_output_path"] = str(output_path)
        with contextlib.suppress(OSError):
            evidence["review_output_sha256"] = review.final_review_output_sha256(
                plan_dir
            )
    if artifact_path.is_file():
        evidence["review_artifact_path"] = str(artifact_path)
        with contextlib.suppress(OSError):
            evidence["review_artifact_sha256"] = review.final_review_artifact_sha256(
                plan_dir
            )
    verdict = review.load_review_verdict(plan_dir)
    evidence["review_verdict_ok"] = bool(verdict.get("ok"))
    if verdict.get("ok"):
        evidence["review_outcome"] = verdict.get("outcome")
        evidence["review_total_findings"] = verdict.get("total_findings")
    else:
        evidence["review_verdict_reason"] = verdict.get("reason")
    return evidence


def _handoff_correction_attempt(event_path: str | Path, session_id: str) -> int:
    """Return the 1-indexed attempt number the next correction would be."""
    try:
        text = Path(event_path).read_text(encoding="utf-8")
    except OSError:
        return 1
    priors = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("name") != events.TURN_HANDOFF_CORRECTION_EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        recorded = payload.get("session_id", payload.get("session"))
        if str(recorded) != str(session_id):
            continue
        priors += 1
    return priors + 1


def _handoff_correction_note(
    defect: _HandoffDefect, *, preserved_review: Mapping[str, Any] | None
) -> str:
    note = (
        f"handoff correction required: {defect.detail}. The plan could not be "
        f"inspected with this handoff in place. To fix it, {defect.instruction}. "
        "Change nothing else: the session's work is already done and its status "
        "was reverted to pending only so this line can be corrected."
    )
    if preserved_review is None:
        return note
    return (
        f"{note} The review artifacts in this plan directory are valid on their "
        "own terms and were preserved; leave them exactly as they are."
    )


def _correct_malformed_handoff(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    plan_cache: plans.PlanSnapshotCache,
    before: plans.PlanSnapshot,
    workspace: workspaces.ResolvedWorkspace | None,
    run_id: str,
    turn_index: int,
    session_id: str,
    plan_error: str | None,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
) -> tuple[plans.PlanSnapshot | None, dict[str, Any] | None]:
    """Give a mechanically malformed handoff one bounded chance to be corrected.

    Returns the re-inspected snapshot when a correction turn was armed, and the
    evidence describing the defect whenever one was recognized — including the
    exhausted case, so the pause that follows says what was tried instead of
    only that the plan would not parse.
    """
    if workspace is None:
        return None, None
    session_path = handoff_guards._session_path_in_snapshot(before, session_id)
    if session_path is None or plan_error is None:
        return None, None
    if session_path.name not in plan_error:
        return None, None
    defect = _classify_mechanical_handoff_defect(
        before, session_id, registry_aliases=workspace.registry.aliases
    )
    if defect is None:
        return None, None
    attempt = _handoff_correction_attempt(event_path, session_id)
    preserved_review = _preserved_review_artifact_evidence(before.plan_dir)
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "turn_index": turn_index,
        **plans.session_identity_payload(session_id),
        "defect": defect.code,
        "detail": defect.detail,
        "plan_error": plan_error,
        "attempt": attempt,
        "max_attempts": MAX_HANDOFF_CORRECTION_ATTEMPTS,
        "session_path": str(session_path),
    }
    if preserved_review is not None:
        evidence["preserved_review_artifacts"] = preserved_review
    if attempt > MAX_HANDOFF_CORRECTION_ATTEMPTS:
        evidence["exhausted"] = True
        return None, evidence
    reverted = handoff_guards._revert_session_status_to_pending(
        before,
        session_id,
        failure_note=_handoff_correction_note(
            defect, preserved_review=preserved_review
        ),
        append_handoff_note=_append_handoff_note,
    )
    if reverted is None:
        evidence["exhausted"] = True
        return None, evidence
    try:
        corrected = plan_cache.get_fresh(before.plan_dir)
    except plans.PlanError as error:
        # The revert did not restore a readable plan, so something other than
        # this handoff is also broken and no correction turn can be launched.
        evidence["exhausted"] = True
        evidence["revert_plan_error"] = str(error)
        return None, evidence
    evidence["exhausted"] = False
    with persistence.lock:
        turn_payload["handoff_correction"] = evidence
        durability.write_json_file(paths["result"], turn_payload)
        payload["final_snapshot"] = plans.snapshot_payload(corrected)
        persistence.append_event(
            event_path, events.TURN_HANDOFF_CORRECTION_EVENT, evidence
        )
        persistence.write_state(payload)
    return corrected, evidence


def _uninspectable_handoff_note(plan_error: str) -> str:
    return (
        "the plan could not be inspected with this completed handoff in place: "
        f"{plan_error}. The status was reset to pending so the handoff can be "
        "rewritten; the session's work is already done, so repair the reported "
        "defect and change nothing else."
    )


def _reopen_uninspectable_handoff(
    *,
    plan_cache: plans.PlanSnapshotCache,
    before: plans.PlanSnapshot,
    session_id: str,
    plan_error: str | None,
    correction_exhausted: bool,
) -> tuple[plans.PlanSnapshot | None, dict[str, Any] | None]:
    """Return a completed handoff the plan cannot be read with to pending.

    The bounded correction turn covers the ``Files modified`` shapes a worker
    can be told how to fix. Everything else used to pause on a plan still
    claiming the session is complete, which leaves nothing a relaunch could
    pick up. Resetting the pre-turn session identity to pending and recording
    the parse failure as a repair note leaves a plan that parses again and a
    session a later turn can rewrite, so the pause reports whether the defect
    is repairable rather than only that the plan would not parse.
    """
    session_path = handoff_guards._session_path_in_snapshot(before, session_id)
    if session_path is None or plan_error is None:
        return None, None
    if session_path.name not in plan_error:
        # The plan is unreadable for a reason this session's handoff does not
        # own, so reopening it repairs nothing.
        return None, None
    session_status = next(
        (item for item in before.sessions if item.id == session_id),
        None,
    )
    if session_status is None:
        return None, None
    try:
        handoff = plans.read_session_handoff(
            session_status.session, repository_registry=None
        )
    except plans.PlanError:
        return None, None
    if handoff.status != "complete":
        return None, None
    evidence: dict[str, Any] = {
        **plans.session_identity_payload(session_id),
        "session_path": str(session_path),
        "plan_error": plan_error,
        "reopened": False,
        "repairable": False,
    }
    reverted = handoff_guards._revert_session_status_to_pending(
        before,
        session_id,
        failure_note=_uninspectable_handoff_note(plan_error),
        append_handoff_note=_append_handoff_note,
    )
    if reverted is None:
        return None, evidence
    evidence["reopened"] = True
    try:
        reinspected = plan_cache.get_fresh(before.plan_dir)
    except plans.PlanError as error:
        evidence["reinspect_error"] = str(error)
        return None, evidence
    # A defect the bounded correction already failed to fix once is reopened
    # for the operator's benefit, not as a promise that a rerun would converge.
    evidence["repairable"] = not correction_exhausted
    return reinspected, evidence


def _paused_handoff_status_note(pause_reason: str) -> str:
    return (
        f"the runner recorded this turn's structured pause ({pause_reason}) and "
        "reset the handoff status to pending, because a session that stopped "
        "without finishing is pending; there is no paused status value. Leave "
        "the status pending and answer the pause instead."
    )


def _repair_paused_handoff_status(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    plan_cache: plans.PlanSnapshotCache,
    after: plans.PlanSnapshot,
    session_id: str,
    turn_index: int,
    runner_pause: object,
) -> tuple[plans.PlanSnapshot, dict[str, Any] | None]:
    """Repair the selected session's handoff when its turn paused on purpose.

    A worker that emits a structured ``RUNNER_PAUSE`` and also writes a status
    the plan vocabulary has no value for (``paused``) is judged by the generic
    invalid-status branch first, which terminates the run as ``unknown-status``
    and discards the reason the worker gave. The status line is the only thing
    wrong, so it is returned to pending here and the structured pause keeps the
    run's cause. ``unknown-status`` stays the fallback: a pause message naming
    no known reason, and any invalid status outside the selected session, are
    both left to it.
    """
    if runner_pause is None:
        return after, None
    pause_reason = turn_execution.runner_pause_reason(str(runner_pause))
    if pause_reason == "runner-pause":
        return after, None
    invalid = run_inputs._invalid_status_pause(after)
    if invalid is None:
        return after, None
    sessions = invalid[2].get("sessions") or []
    if len(sessions) != 1 or sessions[0].get("id") != session_id:
        return after, None
    from_status = sessions[0].get("raw_value")
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=_paused_handoff_status_note(pause_reason),
        append_handoff_note=_append_handoff_note,
    )
    if reverted is None:
        return after, None
    repaired = session_verification._reinspect_after_revert(
        after.plan_dir, after, reverted, plan_cache
    )
    evidence: dict[str, Any] = {
        **plans.session_identity_payload(session_id),
        "session_path": str(reverted),
        "from_status": from_status,
        "to_status": "pending",
        "pause_reason": pause_reason,
    }
    persistence.append_event(
        event_path,
        events.SESSION_REOPENED_EVENT,
        {
            "run_id": payload.get("run_id"),
            "turn_index": turn_index,
            **plans.session_identity_payload(session_id),
            "from_status": from_status,
            "to_status": "pending",
            "guard": "plan-status-validity",
            "reason": pause_reason,
            **turn_execution.reopened_snapshot_identity(
                plans.snapshot_payload(repaired)
            ),
        },
    )
    return repaired, evidence


def _append_handoff_note(
    lines: list[str],
    *,
    handoff_index: int,
    section_end: int,
    note: str,
) -> list[str]:
    new_bullet = f"  - {note}"
    notes_index: int | None = None
    for index in range(handoff_index + 1, section_end):
        if lines[index].lstrip().startswith("- **Notes**"):
            notes_index = index
            break
    if notes_index is None:
        insert_at = section_end
        while insert_at - 1 > handoff_index and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        return lines[:insert_at] + ["- **Notes**:", new_bullet] + lines[insert_at:]
    insert_at = notes_index + 1
    while insert_at < section_end:
        candidate = lines[insert_at]
        stripped = candidate.lstrip()
        leading = candidate[: len(candidate) - len(stripped)]
        if len(leading) >= 2 and stripped.startswith("-"):
            insert_at += 1
            continue
        break
    return lines[:insert_at] + [new_bullet] + lines[insert_at:]
