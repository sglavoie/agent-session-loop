"""History consume."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

from . import history_checkpoints as checkpoints
from . import history_entries as entries_module
from . import history_outcomes as outcomes
from . import history_primitives as primitives

# -- Reading ----------------------------------------------------------------


@dataclass(frozen=True)
class SourceRead:
    """The outcome of interpreting one pass over one event file."""

    checkpoint: checkpoints.SourceCheckpoint
    entries: tuple[entries_module.HistoryEntry, ...] = ()
    questions: tuple[entries_module.QuestionEntry, ...] = ()
    restarted: bool = False
    malformed: int = 0
    duplicates: int = 0
    unattributed: int = 0
    expired: int = 0
    undatable: int = 0
    unsupported: int = 0
    oversized: int = 0
    deferred_bytes: int = 0
    read_bytes: int = 0
    unreadable: bool = False
    unresolved: bool = False


def consume_records(
    payload: bytes,
    *,
    checkpoint: checkpoints.SourceCheckpoint,
    source_key: str,
    window_start: float,
    facts: outcomes.PlanFacts = outcomes.NO_PLAN_FACTS,
) -> SourceRead:
    """Interpret a block of whole records, advancing ``checkpoint`` as it goes.

    ``payload`` must already have been trimmed to whole records by
    :func:`complete_records`. The returned checkpoint is the one that *would*
    consume them; the caller persists it only after the entries have been
    delivered.
    """
    entries: dict[str, entries_module.HistoryEntry] = {}
    questions: dict[str, entries_module.QuestionEntry] = {}
    malformed = duplicates = unattributed = expired = undatable = unsupported = 0
    oversized = 0
    records = checkpoint.records
    unresolved = False

    def delivered_already(entry_id: str) -> bool:
        return (
            entry_id in entries
            or entry_id in questions
            or entry_id in checkpoint.delivered
        )

    for line in payload.split(b"\n"):
        if not line.strip():
            continue
        # A cheap substring test before any parse. Turn records dominate the
        # stream and carry harness output; not decoding them is both the
        # performance decision and a privacy one.
        if not any(probe in line for probe in primitives.EVENT_NAME_PROBES):
            continue
        if len(line) > primitives.MAX_RECORD_BYTES:
            # A record this size is a transcript, not a run's ending: the
            # compact terminal payload is a few hundred bytes of scalars.
            # Stepped over unparsed so a corpus cannot make this process decode
            # a multi-megabyte harness turn that merely mentions the event name.
            oversized += 1
            continue
        try:
            record = json.loads(line, parse_constant=primitives.refuse_non_finite)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(record, Mapping):
            malformed += 1
            continue
        name = record.get("name")
        if name not in primitives.IMPORTED_EVENT_NAMES:
            continue
        records += 1
        try:
            version = primitives.event_schema_version(record)
        except ValueError:
            malformed += 1
            continue
        if version not in primitives.SUPPORTED_EVENT_SCHEMA_VERSIONS:
            unsupported += 1
            continue
        body = record.get("payload")
        if not isinstance(body, Mapping):
            malformed += 1
            continue

        if name == primitives.RUN_STARTED_EVENT:
            run_id = primitives._nonempty(body.get("run_id"))
            if run_id is None:
                unattributed += 1
                continue
            checkpoint = checkpoint.remember(
                checkpoints.OpenRun(
                    run_id=run_id,
                    plan_dir=primitives._nonempty(body.get("plan_dir")),
                    harness=primitives._nonempty(body.get("harness")),
                    model=primitives._nonempty(body.get("model")),
                    started_at=primitives.epoch(body.get("run_started_at"))
                    or primitives.epoch(record.get("created_at")),
                )
            )
            continue

        if name == primitives.MAILBOX_EVENT:
            action = body.get("action")
            if action not in (
                primitives.MAILBOX_ACTION_WAIT_STARTED,
                primitives.MAILBOX_ACTION_WAIT_CLEARED,
            ):
                continue
            question_id = primitives._nonempty(body.get("question_id"))
            if question_id is None:
                unattributed += 1
                continue
            run_id = primitives._nonempty(body.get("run_id"))
            asked_at = primitives.epoch(body.get("started_at"))
            session = primitives._session_id(body.get("session_id"))
            if action == primitives.MAILBOX_ACTION_WAIT_STARTED:
                checkpoint = checkpoint.remember_question(
                    checkpoints.OpenQuestion(
                        question_id=question_id,
                        run_id=run_id,
                        session_id=session,
                        asked_at=asked_at or primitives.epoch(record.get("created_at")),
                    )
                )
                continue
            recorded_at = primitives.epoch(record.get("created_at"))
            if recorded_at is None:
                undatable += 1
                continue
            opened = checkpoint.find_question(question_id)
            checkpoint = checkpoint.forget_question(question_id)
            if recorded_at < window_start:
                expired += 1
                continue
            plan_dir = _resolve_plan_dir(
                checkpoint,
                run_id=run_id or (opened.run_id if opened else None),
                declared=primitives._nonempty(body.get("plan_dir")),
                facts=facts,
            )
            if plan_dir is None:
                unresolved = True
                continue
            question = entries_module.project_question(
                source_key=source_key,
                plan_dir=plan_dir,
                question_id=question_id,
                run_id=run_id or (opened.run_id if opened else None),
                session_id=session or (opened.session_id if opened else None),
                asked_at=asked_at or (opened.asked_at if opened else None),
                resolved_at=recorded_at,
                recorded_at=recorded_at,
                outcome=outcomes.question_outcome(body.get("reason")),
            )
            if delivered_already(question.entry_id):
                duplicates += 1
                continue
            questions[question.entry_id] = question
            checkpoint = replace(checkpoint, plan_dir=plan_dir)
            continue

        run_id = primitives._nonempty(body.get("run_id"))
        if run_id is None:
            # Attaching this to whichever run was last seen starting is how a
            # rapid rerun inherits the previous run's ending. Dropped.
            unattributed += 1
            continue
        recorded_at = primitives.epoch(record.get("created_at")) or primitives.epoch(
            body.get("run_finished_at")
        )
        if recorded_at is None:
            undatable += 1
            continue
        if recorded_at < window_start:
            expired += 1
            continue

        started = checkpoint.find(run_id)
        plan_dir = _resolve_plan_dir(
            checkpoint,
            run_id=run_id,
            declared=primitives._nonempty(body.get("plan_dir")),
            facts=facts,
        )
        if plan_dir is None:
            unresolved = True
            continue

        # A wait this run never cleared is history, not live state: the run has
        # ended, so nothing will ever answer it. Recorded before the run's own
        # entry so the two travel in the same batch.
        for stranded in checkpoint.questions_for_run(run_id):
            checkpoint = checkpoint.forget_question(stranded.question_id)
            abandoned = entries_module.project_question(
                source_key=source_key,
                plan_dir=plan_dir,
                question_id=stranded.question_id,
                run_id=run_id,
                session_id=stranded.session_id,
                asked_at=stranded.asked_at,
                resolved_at=None,
                recorded_at=recorded_at,
                outcome=primitives.QUESTION_OUTCOME_UNRESOLVED,
            )
            if delivered_already(abandoned.entry_id):
                duplicates += 1
                continue
            questions[abandoned.entry_id] = abandoned

        entry = entries_module.project_entry(
            body,
            source_key=source_key,
            plan_dir=plan_dir,
            facts=facts,
            started=started,
            recorded_at=recorded_at,
            version=version,
        )
        if delivered_already(entry.entry_id):
            # The same run's ending seen twice: appended twice by ASL, or
            # re-read after a file replacement. The first record wins, which is
            # the rule ASL's own terminal-evidence reader follows.
            duplicates += 1
            continue
        entries[entry.entry_id] = entry
        checkpoint = replace(checkpoint, plan_dir=plan_dir)

    checkpoint = replace(checkpoint, records=records)
    checkpoint = checkpoint.record_delivered((*entries, *questions))
    return SourceRead(
        checkpoint=checkpoint,
        entries=tuple(entries.values()),
        questions=tuple(questions.values()),
        malformed=malformed,
        duplicates=duplicates,
        unattributed=unattributed,
        expired=expired,
        undatable=undatable,
        unsupported=unsupported,
        oversized=oversized,
        unresolved=unresolved,
    )


def _resolve_plan_dir(
    checkpoint: checkpoints.SourceCheckpoint,
    *,
    run_id: str | None,
    declared: str | None,
    facts: outcomes.PlanFacts,
) -> str | None:
    """The plan directory a record belongs to, by the only four ways to know it.

    The remembered ``run_started`` first, because it is the record that named
    the plan for this run specifically. Then whatever the record itself
    declared, then the last plan this source resolved, then the plan's own
    ``state.json``. Nothing below that: a record whose plan cannot be named is
    dropped and the pass says so, because attributing it to a neighbour is how
    one plan's history acquires another plan's runs.
    """
    started = checkpoint.find(run_id)
    return (
        (started.plan_dir if started else None)
        or declared
        or checkpoint.plan_dir
        or facts.plan_dir
    )
