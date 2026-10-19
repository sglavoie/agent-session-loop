"""Canonical observations and incremental history, beside ASL's own vocabulary.

The Companion imports ASL's ``events.jsonl`` files into seven-day history. That
import is the only place in either application that knows what a run's *ending*
means -- and it lives in the other repository, mirroring ASL's constants from
memory and reading a corpus it does not own. This module is the interpretation
half of that code, moved next to the definitions it depends on, so a reason
added to ASL's vocabulary fails a test here rather than arriving on the Pi as an
unrecognised string.

Four rules are the content of the module. Each is code rather than a convention:

* **An outcome comes from a terminal record, never from an absence.**
  :func:`classify_outcome` names exactly one combination of recorded fields
  ``completed``; everything missing, unrecognised, or reconstructed lands on
  ``unknown`` or ``abandoned``. :func:`project_entry` then removes the last way
  a success could be invented: a ``completed`` record with no
  ``run_finished_at`` is downgraded, because the one outcome rendered as a
  success has to say when it succeeded.

* **Attribution is by run id, never by recency.** Two runs of one plan minutes
  apart append to the same file, so "the last start I saw" is not the run that
  just finished. :meth:`SourceCheckpoint.find` joins each terminal record to its
  own ``run_started``, and a record naming no run is dropped rather than
  attached to whichever run was open.

* **Nothing is consumed until it has been delivered.** :func:`consume_records`
  returns the checkpoint that *would* consume the bytes it read; the caller
  persists it only after the entries have been handed on. A crash in that gap
  re-reads the same bytes and re-derives byte-identical entries with the same
  ids, which an upsert absorbs -- whereas advancing first loses a run's ending
  permanently on one badly timed crash.

* **One projection feeds both consumers.** :func:`build_view` and
  :func:`batches` are two renderings of the same :class:`HistoryEntry` objects,
  not two derivations from the same bytes. Local presentation and connector
  publication therefore cannot disagree about what a run did, which they could
  and did while the Companion held its own copy of the interpretation.

Nothing here touches the filesystem. ``agent_session_loop.workbench_history``
discovers the state directories, reads the bytes, and persists the checkpoints;
this module is given bytes and parsed documents and returns records, which is
what keeps it loadable on a Pi that has no ASL corpus of its own.

Identity is not re-derived: :mod:`asl_workbench.catalog` already holds the
digest scheme, the label sanitiser, and the project naming, and history reuses
all three so an entry's ``plan_key`` is the same sixteen characters the catalog
publishes for the same plan.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .catalog import (
    digest_of as digest_of,
)
from .catalog import (
    plan_key as plan_key,
)
from .catalog import (
    run_key as run_key,
)
from .history_checkpoints import (
    OpenQuestion as OpenQuestion,
)
from .history_checkpoints import (
    OpenRun as OpenRun,
)
from .history_checkpoints import (
    SourceCheckpoint as SourceCheckpoint,
)
from .history_checkpoints import (
    checkpoint_document as checkpoint_document,
)
from .history_checkpoints import (
    checkpoints_from_document as checkpoints_from_document,
)
from .history_checkpoints import (
    restart_required as restart_required,
)
from .history_combine import (
    ImportResult as ImportResult,
)
from .history_combine import (
    _entry_order as _entry_order,
)
from .history_combine import (
    _question_order as _question_order,
)
from .history_combine import (
    combine as combine,
)
from .history_consume import (
    SourceRead as SourceRead,
)
from .history_consume import (
    _resolve_plan_dir as _resolve_plan_dir,
)
from .history_consume import (
    consume_records as consume_records,
)
from .history_entries import (
    HistoryEntry as HistoryEntry,
)
from .history_entries import (
    QuestionEntry as QuestionEntry,
)
from .history_entries import (
    _count_field as _count_field,
)
from .history_entries import (
    _flag as _flag,
)
from .history_entries import (
    entry_id_for as entry_id_for,
)
from .history_entries import (
    project_entry as project_entry,
)
from .history_entries import (
    project_question as project_question,
)
from .history_entries import (
    question_entry_id_for as question_entry_id_for,
)
from .history_entries import (
    question_key_for as question_key_for,
)
from .history_outcomes import (
    NO_PLAN_FACTS as NO_PLAN_FACTS,
)
from .history_outcomes import (
    PlanFacts as PlanFacts,
)
from .history_outcomes import (
    _count as _count,
)
from .history_outcomes import (
    _duration as _duration,
)
from .history_outcomes import (
    _review_outcome as _review_outcome,
)
from .history_outcomes import (
    _session_ids as _session_ids,
)
from .history_outcomes import (
    classify_outcome as classify_outcome,
)
from .history_outcomes import (
    pause_reason_code as pause_reason_code,
)
from .history_outcomes import (
    plan_facts_from_state as plan_facts_from_state,
)
from .history_outcomes import (
    question_outcome as question_outcome,
)
from .history_primitives import (
    _SESSION_ID_RE as _SESSION_ID_RE,
)
from .history_primitives import (
    ASL_CLEAN_STOP_REASONS as ASL_CLEAN_STOP_REASONS,
)
from .history_primitives import (
    ASL_OUTCOME_ABANDONED as ASL_OUTCOME_ABANDONED,
)
from .history_primitives import (
    ASL_REASON_ALL_COMPLETE as ASL_REASON_ALL_COMPLETE,
)
from .history_primitives import (
    CHECKPOINT_SCHEMA_VERSION as CHECKPOINT_SCHEMA_VERSION,
)
from .history_primitives import (
    EVENT_NAME_PROBES as EVENT_NAME_PROBES,
)
from .history_primitives import (
    EVENTS_FILE_NAME as EVENTS_FILE_NAME,
)
from .history_primitives import (
    HEAD_SAMPLE_BYTES as HEAD_SAMPLE_BYTES,
)
from .history_primitives import (
    HISTORY_OUTCOME_ABANDONED as HISTORY_OUTCOME_ABANDONED,
)
from .history_primitives import (
    HISTORY_OUTCOME_COMPLETED as HISTORY_OUTCOME_COMPLETED,
)
from .history_primitives import (
    HISTORY_OUTCOME_FAILED as HISTORY_OUTCOME_FAILED,
)
from .history_primitives import (
    HISTORY_OUTCOME_PAUSED as HISTORY_OUTCOME_PAUSED,
)
from .history_primitives import (
    HISTORY_OUTCOME_STOPPED as HISTORY_OUTCOME_STOPPED,
)
from .history_primitives import (
    HISTORY_OUTCOME_UNKNOWN as HISTORY_OUTCOME_UNKNOWN,
)
from .history_primitives import (
    HISTORY_OUTCOMES as HISTORY_OUTCOMES,
)
from .history_primitives import (
    HISTORY_SUCCESS_OUTCOMES as HISTORY_SUCCESS_OUTCOMES,
)
from .history_primitives import (
    IMPORTED_EVENT_NAMES as IMPORTED_EVENT_NAMES,
)
from .history_primitives import (
    MAILBOX_ACTION_WAIT_CLEARED as MAILBOX_ACTION_WAIT_CLEARED,
)
from .history_primitives import (
    MAILBOX_ACTION_WAIT_STARTED as MAILBOX_ACTION_WAIT_STARTED,
)
from .history_primitives import (
    MAILBOX_EVENT as MAILBOX_EVENT,
)
from .history_primitives import (
    MAX_COMPLETED_SESSION_IDS as MAX_COMPLETED_SESSION_IDS,
)
from .history_primitives import (
    MAX_DELIVERED_IDS as MAX_DELIVERED_IDS,
)
from .history_primitives import (
    MAX_HISTORY_ENTRIES_PER_BATCH as MAX_HISTORY_ENTRIES_PER_BATCH,
)
from .history_primitives import (
    MAX_OPEN_QUESTIONS as MAX_OPEN_QUESTIONS,
)
from .history_primitives import (
    MAX_OPEN_RUNS as MAX_OPEN_RUNS,
)
from .history_primitives import (
    MAX_OVERSIZED_SCAN_BYTES as MAX_OVERSIZED_SCAN_BYTES,
)
from .history_primitives import (
    MAX_READ_BYTES_PER_PASS as MAX_READ_BYTES_PER_PASS,
)
from .history_primitives import (
    MAX_RECORD_BYTES as MAX_RECORD_BYTES,
)
from .history_primitives import (
    MAX_STATE_BYTES as MAX_STATE_BYTES,
)
from .history_primitives import (
    MAX_WARNINGS as MAX_WARNINGS,
)
from .history_primitives import (
    PAUSE_REASON_OTHER as PAUSE_REASON_OTHER,
)
from .history_primitives import (
    PAUSE_REASONS as PAUSE_REASONS,
)
from .history_primitives import (
    QUESTION_OUTCOME_ANSWERED as QUESTION_OUTCOME_ANSWERED,
)
from .history_primitives import (
    QUESTION_OUTCOME_RESOLVED as QUESTION_OUTCOME_RESOLVED,
)
from .history_primitives import (
    QUESTION_OUTCOME_TIMED_OUT as QUESTION_OUTCOME_TIMED_OUT,
)
from .history_primitives import (
    QUESTION_OUTCOME_UNRESOLVED as QUESTION_OUTCOME_UNRESOLVED,
)
from .history_primitives import (
    QUESTION_OUTCOMES as QUESTION_OUTCOMES,
)
from .history_primitives import (
    REVIEW_OUTCOMES as REVIEW_OUTCOMES,
)
from .history_primitives import (
    RUN_FINISHED_EVENT as RUN_FINISHED_EVENT,
)
from .history_primitives import (
    RUN_STARTED_EVENT as RUN_STARTED_EVENT,
)
from .history_primitives import (
    STATE_FILE_NAME as STATE_FILE_NAME,
)
from .history_primitives import (
    SUPPORTED_EVENT_SCHEMA_VERSIONS as SUPPORTED_EVENT_SCHEMA_VERSIONS,
)
from .history_primitives import (
    WAIT_REASON_OUTCOMES as WAIT_REASON_OUTCOMES,
)
from .history_primitives import (
    HistoryError as HistoryError,
)
from .history_primitives import (
    _integer as _integer,
)
from .history_primitives import (
    _nonempty as _nonempty,
)
from .history_primitives import (
    _repository_count as _repository_count,
)
from .history_primitives import (
    _session_id as _session_id,
)
from .history_primitives import (
    complete_records as complete_records,
)
from .history_primitives import (
    epoch as epoch,
)
from .history_primitives import (
    event_schema_version as event_schema_version,
)
from .history_primitives import (
    head_digest as head_digest,
)
from .history_primitives import (
    refuse_non_finite as refuse_non_finite,
)
from .history_views import (
    HistoryBatch as HistoryBatch,
)
from .history_views import (
    HistoryView as HistoryView,
)
from .history_views import (
    PlanHistory as PlanHistory,
)
from .history_views import (
    batches as batches,
)
from .history_views import (
    build_view as build_view,
)
