"""Overseer context projections."""

from __future__ import annotations

from typing import Any

from . import (
    mailbox_snapshot,
    overseer_discovery,
)
from . import (
    overseer_context_models as models,
)
from . import (
    overseer_context_values as values,
)


def _project_status(
    record: dict[str, Any], *, max_summary_chars: int
) -> models._Projection:
    """The run's own record: what phase it is in, and what it waits on."""
    paused = bool(record.get("paused"))
    reason = values._first(record.get("pause_reason"), record.get("reason"))
    paused_at = values._first(record.get(overseer_discovery.PAUSED_AT_KEY))
    finished = values._first(record.get("run_finished_at"))
    if finished:
        phase = f"finished at {finished}"
    elif paused:
        phase = f"paused ({reason or 'no reason recorded'})"
        if paused_at:
            phase = f"{phase} at {paused_at}"
    else:
        phase = "running"

    clauses = [f"run {values._value(record.get('run_id'))} {phase}"]
    active = record.get("active_turn")
    if isinstance(active, dict):
        clauses.append(
            f"turn {values._value(active.get('turn_index'))} of "
            f"{values._value(active.get('session') or active.get('session_id'))} active"
        )
    if isinstance(record.get("waiting_for_reply"), dict):
        clauses.append("waiting for a reply")
    if isinstance(record.get("abandoned"), dict):
        clauses.append("recorded abandoned")

    fields = (
        ("run_id", record.get("run_id")),
        ("ok", record.get("ok")),
        ("paused", record.get("paused")),
        ("pause_reason", reason),
        ("paused_at", paused_at),
        ("run_started_at", record.get("run_started_at")),
        ("run_finished_at", record.get("run_finished_at")),
        ("active_turn", record.get("active_turn")),
        ("active_verification", record.get("active_verification")),
        ("active_finalization", record.get("active_finalization")),
        ("waiting_for_reply", record.get("waiting_for_reply")),
        ("pause_policy", record.get("pause_policy")),
        ("control", record.get("control")),
        ("abandoned", record.get("abandoned")),
    )
    return models._Projection(
        summary=values._clip("; ".join(clauses), max_summary_chars),
        detail=values._lines(fields),
        records=1,
        shown=1,
    )


def _project_run_config(
    record: dict[str, Any], *, max_records: int, max_summary_chars: int
) -> models._Projection:
    """The binding a run works under: workspace, registry, repositories."""
    repositories = record.get("repositories")
    entries = repositories if isinstance(repositories, list) else []
    aliases = [
        str(entry.get("alias"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("alias")
    ]
    run_config = record.get("run_config")
    config = run_config if isinstance(run_config, dict) else {}

    summary = (
        f"workspace {values._value(record.get('workspace_root'))} at registry "
        f"{values._value(record.get('registry_digest'))} with "
        f"{len(entries)} declared "
        f"{'repository' if len(entries) == 1 else 'repositories'}"
    )
    if aliases:
        summary = f"{summary} ({', '.join(aliases[:max_records])})"

    fields: list[tuple[str, Any]] = [
        ("workspace_root", record.get("workspace_root")),
        ("cwd", record.get("cwd")),
        ("registry_digest", record.get("registry_digest")),
        ("harness", record.get("harness")),
        ("review_harness", config.get("review_harness")),
        ("supervisor_audit", config.get("supervisor_audit")),
        ("dry_run", record.get("dry_run")),
        (
            overseer_discovery.DISCOVERY_VERSION_KEY,
            record.get(overseer_discovery.DISCOVERY_VERSION_KEY),
        ),
    ]
    shown = 0
    for entry in entries[:max_records]:
        if not isinstance(entry, dict):
            continue
        shown += 1
        fields.append(
            (
                f"repository[{shown - 1}]",
                {
                    "alias": entry.get("alias"),
                    "path": entry.get("path"),
                    "origin": entry.get("origin"),
                    "resolved_root": entry.get("resolved_root"),
                },
            )
        )
    return models._Projection(
        summary=values._clip(summary, max_summary_chars),
        detail=values._lines(fields),
        records=max(len(entries), 1),
        shown=max(shown, 1),
    )


def _project_questions(
    view: mailbox_snapshot.MailboxView,
    *,
    run_id: str,
    max_records: int,
    max_summary_chars: int,
) -> models._Projection:
    """The questions this run can still consume an answer for.

    ``open_questions`` rather than ``unanswered_questions``: a question whose
    owning run has ended blocks nothing and no reply can reach a worker, so
    reporting it as a live incident would send an operator to answer a mailbox
    nobody is reading.

    The identities follow the same cap as the summary. A run with more open
    questions than one summary names has more than one incident, and listing
    fifty identifiers to prove it would spend the context this boundary exists
    to protect; ``truncated`` is what says there are more.
    """
    questions = view.open_questions(run_id)
    if not questions:
        return models._Projection(
            summary="no question of this run is waiting on an answer",
            detail="",
            records=0,
            shown=0,
        )
    named = questions[:max_records]
    labels = []
    for message in named:
        waiting = view.active_wait_for(message.id) is not None
        labels.append(f"{message.id}{' (waiting)' if waiting else ''}")
    summary = (
        f"{len(questions)} open "
        f"{'question' if len(questions) == 1 else 'questions'}: "
        f"{', '.join(labels)}"
    )
    fields: list[tuple[str, Any]] = []
    for message in named:
        fields.append(
            (
                message.id,
                {
                    "created_at": message.created_at,
                    "session_id": message.session_id,
                    "turn_index": message.turn_index,
                    "delivery_mode": message.delivery_mode,
                    "delivery_state": view.delivery_state_for(message.id),
                    "waiting": view.active_wait_for(message.id) is not None,
                    "text": values._clip(message.text or "", 200),
                },
            )
        )
    return models._Projection(
        summary=values._clip(summary, max_summary_chars),
        detail=values._lines(fields),
        records=len(questions),
        shown=len(named),
        identities=tuple(message.id for message in named),
    )


def _project_supervision(
    view: mailbox_snapshot.MailboxView, *, max_records: int, max_summary_chars: int
) -> models._Projection:
    """What a human contributed to this plan, most recent last."""
    records = tuple(
        message for message in view.messages if message.kind in models.SUPERVISION_KINDS
    )
    if not records:
        return models._Projection(
            summary="no supervision record on this plan",
            detail="",
            records=0,
            shown=0,
        )
    named = records[-max_records:]
    counts: dict[str, int] = {}
    for message in records:
        counts[message.kind] = counts.get(message.kind, 0) + 1
    ended = view.latest_supervision_end
    summary = f"{len(records)} supervision records (" + ", ".join(
        f"{count} {kind}" for kind, count in sorted(counts.items())
    )
    summary = f"{summary})"
    if ended is not None:
        summary = (
            f"{summary}; last supervision ended {ended.created_at} "
            f"({values._value(ended.supervision_outcome)})"
        )
    fields: list[tuple[str, Any]] = []
    for message in named:
        fields.append(
            (
                f"{message.seq}:{message.kind}",
                {
                    "id": message.id,
                    "created_at": message.created_at,
                    "to": message.to,
                    "supervision_id": message.supervision_id,
                    "supervision_outcome": message.supervision_outcome,
                    "delivery_state": message.delivery_state,
                    "text": values._clip(message.text or "", 200),
                },
            )
        )
    return models._Projection(
        summary=values._clip(summary, max_summary_chars),
        detail=values._lines(fields),
        records=len(records),
        shown=len(named),
    )


def _project_verification(
    record: dict[str, Any], *, max_records: int, max_summary_chars: int
) -> models._Projection:
    """One turn's verification result, named checks first."""
    block = record.get("verification")
    if not isinstance(block, dict):
        return models._Projection(
            summary="this turn recorded no verification",
            detail="",
            records=0,
            shown=0,
        )
    failures = values._named(block.get("failures"))
    diagnostics = values._named(block.get("diagnostic_failures"))
    checks = values._named(block.get("checks"))
    passed = block.get("passed")
    verdict = "passed" if passed else ("failed" if passed is False else "unresolved")
    summary = (
        f"verification of {values._value(block.get('session'))} {verdict} "
        f"({values._value(block.get('reason'))}) at turn "
        f"{values._value(record.get('turn_index'))}"
    )
    if failures:
        summary = f"{summary}; failing: {', '.join(failures[:max_records])}"
    fields: list[tuple[str, Any]] = [
        ("session", block.get("session")),
        ("passed", passed),
        ("reason", block.get("reason")),
        ("error", block.get("error")),
        ("acceptance", block.get("acceptance")),
        ("recipe", block.get("recipe")),
        ("checks", checks[:max_records]),
        ("failures", failures[:max_records]),
        ("diagnostic_failures", diagnostics[:max_records]),
        (
            "skipped_checks",
            values._named(block.get("skipped_checks"))[:max_records],
        ),
    ]
    return models._Projection(
        summary=values._clip(summary, max_summary_chars),
        detail=values._lines(fields),
        records=max(len(checks), 1),
        shown=max(min(len(checks), max_records), 1),
    )


def _project_worker_changes(
    record: dict[str, Any], *, max_records: int, max_summary_chars: int
) -> models._Projection:
    """What the worker changed: a commit if there is one, session status if not.

    Two shapes because the run writes two. A blocked run has often changed the
    plan without reaching a commit, and reporting nothing for it would describe
    the artifact layout rather than the work.
    """
    if "commit_index" in record:
        paths = record.get("qualified_paths")
        qualified = [str(item) for item in paths] if isinstance(paths, list) else []
        summary = (
            f"commit {values._value(record.get('commit_sha'))} in "
            f"{values._value(record.get('repository_alias'))} "
            f"({values._value(record.get('status_result'))}) touching "
            f"{len(qualified)} {'path' if len(qualified) == 1 else 'paths'}"
        )
        fields: list[tuple[str, Any]] = [
            ("commit_index", record.get("commit_index")),
            ("scope", record.get("scope")),
            ("session", record.get("session_id") or record.get("session")),
            ("repository_alias", record.get("repository_alias")),
            ("status_result", record.get("status_result")),
            ("commit_sha", record.get("commit_sha")),
            ("head_before", record.get("head_before")),
            ("head_after", record.get("head_after")),
            ("clean_after", record.get("clean_after")),
            ("qualified_paths", qualified[:max_records]),
        ]
        return models._Projection(
            summary=values._clip(summary, max_summary_chars),
            detail=values._lines(fields),
            records=max(len(qualified), 1),
            shown=max(min(len(qualified), max_records), 1),
        )

    changes = record.get("status_changes")
    entries = [str(item) for item in changes] if isinstance(changes, list) else []
    if not entries:
        return models._Projection(
            summary="this run has committed nothing and changed no session status",
            detail="",
            records=0,
            shown=0,
        )
    summary = (
        f"turn {values._value(record.get('turn_index'))} of "
        f"{values._value(record.get('session_id') or record.get('session'))} changed "
        f"{len(entries)} session {'status' if len(entries) == 1 else 'statuses'}: "
        f"{', '.join(entries[:max_records])}"
    )
    fields = [
        ("turn_index", record.get("turn_index")),
        ("session", record.get("session_id") or record.get("session")),
        ("returncode", record.get("returncode")),
        ("timed_out", record.get("timed_out")),
        ("interrupted", record.get("interrupted")),
        ("status_result", record.get("status_result")),
        ("status_changes", entries[:max_records]),
        ("pending_completed", record.get("pending_completed_ids")),
    ]
    return models._Projection(
        summary=values._clip(summary, max_summary_chars),
        detail=values._lines(fields),
        records=len(entries),
        shown=min(len(entries), max_records),
    )
