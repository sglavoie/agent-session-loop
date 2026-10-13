"""Row and snapshot projection for the bridge.

Turns Workbench summaries into the bounded, body-free rows a snapshot carries,
including the redirect-journal projection. Nothing here writes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from . import (
    events,
    intervention,
    mailbox,
    mailbox_models,
    mailbox_snapshot,
    plans_errors,
    redirect_recovery,
    state,
    state_locks,
    state_payloads,
    workbench_discovery,
    workbench_registry_models,
    workbench_summary,
    workbench_summary_model,
)
from .agent_mux_projection import CONTRACT_SET_ID
from .bridge_protocol import (
    ACTIVE_PRIMARY_STATES,
    CONTEXT_KIND_WAITING,
    MAX_FRAME_BYTES,
    MAX_QUESTION_IDS,
    MAX_ROWS,
    MAX_WARNINGS,
    PHASE_AWAITING_REPLY,
    PHASE_BY_STATE,
    PROTOCOL_VERSION,
    Phase,
    _bounded_id,
    _bounded_int,
    _bounded_path,
    _bounded_strings,
    _bounded_text,
    _epoch,
    encode_frame,
)
from .bridge_workers import PROJECTION_KEYS


def row_id(plan_dir: str, run_id: str | None) -> str:
    """Return the stable identity of one projected run.

    A plan directory can be re-run, and a run id is absent before the first
    run, so neither alone identifies a row across polls. Hashing the pair
    avoids inventing a delimiter that a plan path could itself contain.
    """
    material = json.dumps([plan_dir, run_id], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _session_identifier(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("session_id", "session"):
        identifier = _bounded_id(payload.get(key))
        if identifier is not None:
            return identifier
    return None


def _question_projection(
    plan_dir: Path,
    runner_state: Mapping[str, Any],
    *,
    state_dir: str | Path | None,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Project body-free question metadata plus the unbounded open-question ids.

    Deliberately derived from persisted runner state and mailbox records rather
    than from ``qna.load_qna_context``: that loader reads the last-message
    artifact into memory, and the cheapest way to guarantee a body never
    reaches a snapshot is never to load one.

    The unbounded ids are returned alongside the projection because the row's
    action token must fingerprint exactly what the mutation path re-derives.
    Feeding it the display-bounded list instead would desynchronize every token
    on a plan holding more than ``MAX_QUESTION_IDS`` open questions.
    """
    waiting = runner_state.get("waiting_for_reply")
    waiting = waiting if isinstance(waiting, Mapping) else None

    unanswered: tuple[mailbox_models.MailboxMessage, ...] = ()
    mailbox_view: mailbox_snapshot.MailboxView | None = None
    try:
        mailbox_view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
        run_id = runner_state.get("run_id")
        unanswered = tuple(
            message
            for message in mailbox_view.unanswered_questions
            if isinstance(run_id, str) and run_id and message.run_id == run_id
        )
    except OSError, ValueError, mailbox_models.MailboxError:
        unanswered = ()
    open_ids = tuple(
        message.id
        for message in unanswered
        if isinstance(message.id, str) and message.id
    )
    context_kind, selected_question_id = intervention.question_context(
        runner_state, open_ids
    )
    active_turn = runner_state.get("active_turn")
    if mailbox_view is not None and isinstance(active_turn, Mapping):
        run_id = runner_state.get("run_id")
        session_id = _session_identifier(active_turn)
        turn_index = _bounded_int(active_turn.get("turn_index"))
        if (
            isinstance(run_id, str)
            and run_id
            and session_id is not None
            and turn_index is not None
        ):
            active_wait = mailbox_view.active_worker_wait(
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
            )
            if active_wait is not None and active_wait.to in open_ids:
                # ``input_wait_active`` is a heartbeat observation.  A
                # connector poll can land between heartbeats even while the
                # worker remains blocked in ``asl ask --wait``.  The paired
                # mailbox wait is durable for exactly that blocked interval,
                # so it is the authoritative tie-breaker for the same live
                # run/session/turn.
                context_kind = CONTEXT_KIND_WAITING
                selected_question_id = active_wait.to
    if context_kind is None:
        return None, open_ids

    question_id = _bounded_id(selected_question_id)

    question_ids = [
        identifier
        for identifier in (_bounded_id(message.id) for message in unanswered)
        if identifier is not None
    ][:MAX_QUESTION_IDS]
    selected = next(
        (message for message in unanswered if message.id == question_id), None
    )
    question_id = _bounded_id(selected.id) if selected is not None else None

    waiting_status = _bounded_text(runner_state.get("waiting_for_reply_status"))
    if context_kind == CONTEXT_KIND_WAITING and waiting_status is None:
        waiting_status = state_payloads.WAITING_FOR_REPLY_STATUS_LIVE

    projection = {
        "context_kind": context_kind,
        "question_id": question_id,
        "unanswered_count": len(unanswered),
        "question_ids": question_ids,
        "asked_at": _bounded_text(selected.created_at)
        if selected is not None
        else None,
        "session_id": _bounded_id(selected.session_id)
        if selected is not None
        else None,
        "turn_index": _bounded_int(selected.turn_index)
        if selected is not None
        else None,
        "waiting_status": waiting_status,
        "hard_deadline": (
            _bounded_text(waiting.get("hard_deadline")) if waiting else None
        ),
        "unsupervised_ceiling_seconds": (
            _bounded_int(waiting.get("unsupervised_ceiling_seconds"))
            if waiting
            else None
        ),
        "last_lease_activity_at": (
            _bounded_text(waiting.get("last_lease_activity_at")) if waiting else None
        ),
    }
    return projection, open_ids


def _phase(
    primary_state: workbench_summary_model.PrimaryState,
    question: Mapping[str, Any] | None,
) -> Phase:
    if (
        primary_state == "running"
        and question is not None
        and question.get("context_kind") == CONTEXT_KIND_WAITING
    ):
        return PHASE_AWAITING_REPLY
    return PHASE_BY_STATE[primary_state]


def project_row(
    summary: workbench_summary_model.WorkbenchSummary,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Project one active/actionable Workbench summary into a snapshot row.

    Free-form summary prose is never copied. ``concise_reason``, ``detail``,
    ``pause_message``, ``last_message_preview``, and ``searchable_text`` all
    quote the harness's own words on at least one classification branch, so the
    row's human text is drawn only from ASL's fixed activity vocabulary.

    The row also carries the actions this run currently permits and the token
    that proves the caller saw this exact state, so a client never has to guess
    which buttons are live and a mutation built from a stale row is refused
    rather than misapplied.
    """
    plan_dir = str(summary.plan_dir)
    lock_info = state_locks.inspect_lock_info(summary.plan_dir, state_dir)
    state_path = (
        state.plan_state_dir(summary.plan_dir, state_dir) / events.STATE_FILE_NAME
    )
    runner_state = state_payloads._runner_state_payload(state_path, lock_info=lock_info)
    question, open_ids = _question_projection(
        summary.plan_dir, runner_state, state_dir=state_dir
    )
    facts = intervention.build_run_facts(
        summary.plan_dir,
        runner_state=runner_state,
        lock_active=bool(lock_info is not None and lock_info.active),
        unanswered_ids=open_ids,
        active_wait_question_id=(
            question.get("question_id")
            if question is not None
            and question.get("context_kind") == CONTEXT_KIND_WAITING
            else None
        ),
    )
    active = (
        runner_state.get("active_verification")
        if summary.primary_state == "verifying"
        else runner_state.get("active_turn")
    )
    session_id = _session_identifier(active) or _bounded_id(
        summary.next_pending_session_id
    )
    if question is not None and question["context_kind"] == CONTEXT_KIND_WAITING:
        session_id = question["session_id"]
    window_id = _bounded_int(runner_state.get("kitty_window_id"))

    return {
        "row_id": row_id(plan_dir, summary.run_id),
        "plan_dir": _bounded_path(plan_dir),
        "label": _bounded_text(summary.label),
        "run_id": _bounded_id(summary.run_id),
        "session_id": session_id,
        "primary_state": summary.primary_state,
        "urgency_rank": summary.urgency_rank,
        "urgency_group": summary.urgency_group,
        "recommended_action": summary.recommended_action,
        "activity_label": _bounded_text(summary.activity_label),
        "phase": _phase(summary.primary_state, question),
        "harness": _bounded_id(summary.harness),
        "workspace_root": _bounded_path(summary.workspace_root),
        "registry_digest": _bounded_id(summary.registry_digest),
        "repository_count": summary.repository_count,
        "window": {"kitty_window_id": window_id} if window_id is not None else None,
        "run_started_at": _bounded_text(runner_state.get("run_started_at")),
        "run_finished_at": _bounded_text(runner_state.get("run_finished_at")),
        "last_activity_at": _bounded_text(summary.last_activity_at),
        "last_activity_source": summary.last_activity_source,
        "pause_reason": _bounded_text(summary.pause_reason),
        "question": question,
        "actions": list(intervention.permitted_actions(facts)),
        "disabled_reasons": intervention.disabled_reasons(facts),
        "action_token": intervention.action_token(facts),
        "warnings": _bounded_strings(summary.warnings, limit=MAX_WARNINGS),
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Order rows exactly as the Workbench queue orders its default view.

    Urgency tier first, freshest activity within a tier, then label; the plan
    directory breaks the remaining ties so the projection is total rather than
    merely stable under one particular input order.
    """
    activity = _epoch(row.get("last_activity_at"))
    plan_dir = row.get("plan_dir") or ""
    label = (row.get("label") or plan_dir).lower()
    return (
        row.get("urgency_rank", 0),
        activity is None,
        -(activity or 0.0),
        label,
        plan_dir,
    )


def _fits_frame(payload: Mapping[str, Any]) -> bool:
    return len(encode_frame(payload)) <= MAX_FRAME_BYTES


def _snapshot_selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Reserve bounded space for unanswered questions, then live work."""
    activity = _epoch(row.get("last_activity_at")) or _epoch(row.get("run_started_at"))
    question = row.get("question")
    waiting_for_reply = bool(
        isinstance(question, Mapping)
        and question.get("question_id")
        and question.get("unanswered_count")
    )
    return (
        not waiting_for_reply,
        row.get("primary_state") not in {"running", "verifying"},
        activity is None,
        -(activity or 0.0),
        *_row_sort_key(row),
    )


def build_runs_snapshot(
    *,
    state_dir: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> dict[str, Any]:
    """Build the deterministic active/actionable projection of every plan."""
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        discovery = workbench_discovery.discover_plans(
            state_root=state_dir,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )
    except (workbench_registry_models.WorkbenchError, OSError, ValueError) as error:
        discovery = workbench_registry_models.DiscoveryResult(
            warnings=(f"discovery failed: {error}",)
        )
    warnings.extend(discovery.warnings)

    for discovered in discovery.plans:
        try:
            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_dir, reclaim=False
            )
            if summary.primary_state not in ACTIVE_PRIMARY_STATES:
                continue
            rows.append(project_row(summary, state_dir=state_dir))
        except (plans_errors.PlanError, OSError, ValueError) as error:
            # A plan that vanishes or turns unreadable mid-scan is news, not a
            # reason to drop every other row in the queue.
            warnings.append(
                f"plan projection failed for {discovered.plan_dir}: {error}"
            )

    # Urgency ordering puts even undated invalid plans ahead of live work.
    # Select the bounded working set separately so a backlog cannot crowd out
    # questions awaiting a person or the live runs this client observes.
    rows.sort(key=_snapshot_selection_key)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    bounded_warnings = _bounded_strings(warnings, limit=MAX_WARNINGS)

    snapshot: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        # Carried on the snapshot as well as the hello so a cached or replayed
        # projection is self-describing: a client that persists rows can tell
        # whether they were produced by the contract set it speaks today.
        "contract_set_id": CONTRACT_SET_ID,
        "rows": rows,
        "row_count": len(rows),
        "warnings": bounded_warnings,
        "truncated": truncated,
    }
    # The cadence block is attached when the snapshot is served rather than
    # built, so the shrink loop budgets for it here or a full frame would grow
    # past the ceiling on its way out.
    probe = {
        "digest": "0" * 64,
        "projection": dict.fromkeys(PROJECTION_KEYS, 99999.999),
    }
    while snapshot["rows"] and not _fits_frame(snapshot | probe):
        snapshot["rows"] = snapshot["rows"][:-1]
        snapshot["row_count"] = len(snapshot["rows"])
        snapshot["truncated"] = True
    snapshot["rows"].sort(key=_row_sort_key)
    snapshot["digest"] = snapshot_digest(snapshot)
    return snapshot


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Digest the parts of a snapshot a client can observe changing.

    Excludes ``digest`` itself and the ``projection`` cadence block, which is
    measured rather than observed: including it would make every walk whose
    duration jittered by a millisecond look like a corpus change. What remains
    is wall-clock free, so an idle corpus produces one digest forever and
    therefore no events.
    """
    material = {
        key: value
        for key, value in snapshot.items()
        if key not in {"digest", "projection"}
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Redirect projection
#
# Everything below reads the redirect journal and derives the same stage
# `asl control redirect-status` prints. None of it reaches
# `redirect_store.read_feedback`, which is the only function that returns an
# operator's words, so a correction's text cannot enter a frame by accident:
# the bridge never holds it in the first place.
# ---------------------------------------------------------------------------


def _redirect_target(value: Any) -> dict[str, Any] | None:
    """Project one bound turn, or ``None`` where the journal recorded none."""
    if not isinstance(value, Mapping):
        return None
    return {
        "run_id": _bounded_id(value.get("run_id")),
        "session_id": _bounded_id(value.get("session_id")),
        "turn_index": _bounded_int(value.get("turn_index")),
        "harness": _bounded_id(value.get("harness")),
    }


def bound_redirect_operation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bound one already-projected redirect operation into a frame field.

    Takes the projection every reader of this journal shares rather than the
    journal record, so the stage a bridge client renders and the stage an
    operator reads out of ``asl control redirect-status`` are the same
    derivation of the same facts rather than two implementations of one rule.
    Only the bounds and the closed key set below are the protocol's own.
    """
    return {
        "operation_id": _bounded_id(payload["operation_id"]),
        "status": payload["status"],
        "stage": payload["stage"],
        "stage_description": _bounded_text(payload["stage_description"]),
        "journal_state": payload["journal_state"],
        "reason": _bounded_text(payload["reason"]),
        "target": _redirect_target(payload["target"]),
        "continuation": _redirect_target(payload["continuation"]),
        "continuation_recovered": bool(payload["continuation_recovered"]),
        # Digest and byte count, never the words. This pair is what lets a
        # client prove *which* correction a receipt describes without the
        # protocol ever carrying one.
        "feedback_digest": _bounded_id(payload["feedback_digest"]),
        "feedback_bytes": _bounded_int(payload["feedback_bytes"]),
        "created_at": _bounded_text(payload["created_at"]),
        "expires_at": _bounded_text(payload["expires_at"]),
    }


def _project_redirect_operations(
    payloads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bound a window of already-projected operations, newest last.

    The window itself is the shared service's, truncated from the *front* when
    the journal outgrows it: the oldest corrections on a long-lived plan are the
    settled ones, and an operator asking where a correction got to is asking
    about a recent one.
    """
    return [bound_redirect_operation(payload) for payload in payloads]


def _conversation_evidence(
    evidence: redirect_recovery.ConversationEvidence,
) -> dict[str, Any]:
    """Project what the conversation ledger can still say about this plan.

    Carried by both redirect reads because the two recoveries can otherwise
    contradict each other: a ledger that cannot be trusted already forces a
    fresh-conversation pause, so a correction waiting on the turn that pause
    prevented has nowhere to land, and a client that saw only the redirect half
    would report a stuck correction as the problem rather than the symptom.
    """
    return {
        "readable": evidence.readable,
        "trustworthy": evidence.trustworthy,
        "usable": evidence.usable,
        "records": evidence.records,
        "unreadable_lines": evidence.unreadable_lines,
        "detail": _bounded_text(evidence.detail),
    }
