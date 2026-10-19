"""Durable conversation continuity across an interrupted harness turn.

A turn that is cancelled mid-flight has to be *continued*, not restarted, and
the only thing that can continue it is the harness's own conversation. That
native identifier is therefore a durable fact about the plan, not a detail of
the run that happened to learn it, and this module is where it is written down.

The storage rules follow from what a crash, a race, or a replaced run may do to
that fact:

* **It outlives the run that learned it.** ``state.json`` is replaced wholesale
  by the next run of the same plan, so a conversation reference kept there would
  be destroyed by the very restart it exists to survive. References live in
  ``conversations.jsonl`` beside it -- plan-owned operational state, keyed by the
  plan session rather than by the run.
* **Nothing is ever rewritten.** Opening, confirming, and retiring a
  conversation are three appended records, taken under an exclusive ``flock``
  and flushed through a platform durability barrier before the lock is released.
  The ledger is the history, not the current opinion. The one sanctioned rewrite
  is :func:`relocate_records`, which state-directory migration uses to follow a
  plan that moved, and it preserves line order and unreadable lines exactly.
* **Every physical line has a sequence, including the bad ones.** Sequences are
  positional, so a malformed or crash-truncated line keeps its place in history
  instead of being deleted to make the file parse.
* **Selection is exact or it is a refusal.** :func:`select_conversation` answers
  for one ``(session_id, harness)`` key and then *checks* every binding the
  stored reference carries -- plan, workspace, registry digest, scope, access
  profile. A mismatch is reported as :data:`SELECTION_INCOMPATIBLE`; it never
  falls through to a different conversation, to the newest one, or to a fresh
  one. There is deliberately no "latest" lookup in this module's API.
* **Unproven references are refused too.** The native identifier is persisted
  *before* work is submitted, so a failure before work starts leaves an
  ``opened`` record naming a conversation that may hold nothing. Selection
  reports that as :data:`SELECTION_UNCONFIRMED` rather than resuming into it.
* **Corruption fails closed for the whole ledger.** An unreadable line cannot be
  attributed to a key, so it may be the retirement or the supersession of the
  one being asked about. Any rejected line makes selection report
  :data:`SELECTION_CORRUPT` for every key. That is recoverable -- the operator's
  explicit, one-use fresh-conversation authorization is the way out -- and the
  alternative, skipping the bad line and answering from an older record, is
  exactly the silent selection of a stale conversation this module exists to
  prevent.
* **Only bounded metadata is stored.** Records carry identifiers, bindings, and
  timestamps. Feedback text, prompts, and transcripts never reach this ledger;
  :class:`ContinuationIntent` carries operation *identifiers* for the same
  reason.
* **The fresh-conversation override is never durable.** It lives only on a
  :class:`ContinuationIntent`, which has no serialization at all. Nothing here
  can write it down, so nothing can rehydrate it into a later run.

Invariants later sessions must preserve
---------------------------------------

1. A conversation reference is written before the work that uses it is
   submitted, and confirmed only once the harness has accepted work on it.
2. ``(session_id, harness)`` selects at most one live conversation per plan.
   Binding fields are *checked*, never used to widen the search.
3. Every non-``resumable`` selection state requires user input. No caller may
   turn one into a silent fresh conversation without an explicit,
   operator-supplied authorization carried on the intent.
4. A retired conversation is never selected again, and retirement is a new
   record rather than a deletion.
5. This module imports nothing that imports :mod:`agent_session_loop.state`, so
   state-directory migration can depend on it rather than restate its format.
   Its public entry points take the plan *state directory*, not a plan
   directory, which is what keeps that direction one-way.


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .conversation_capabilities import (
    AVAILABLE_CAPABILITY_DETAIL as AVAILABLE_CAPABILITY_DETAIL,
)
from .conversation_capabilities import (
    STAGED_CAPABILITY_DETAIL as STAGED_CAPABILITY_DETAIL,
)
from .conversation_capabilities import (
    CapabilityReport as CapabilityReport,
)
from .conversation_capabilities import (
    ContinuationIntent as ContinuationIntent,
)
from .conversation_capabilities import (
    HarnessCapability as HarnessCapability,
)
from .conversation_capabilities import (
    capability_report as capability_report,
)
from .conversation_capabilities import (
    fresh_intent as fresh_intent,
)
from .conversation_capabilities import (
    parse_capability_report as parse_capability_report,
)
from .conversation_capabilities import (
    resume_intent as resume_intent,
)
from .conversation_ledger_models import (
    ConversationLedger as ConversationLedger,
)
from .conversation_ledger_models import (
    ConversationRecord as ConversationRecord,
)
from .conversation_ledger_models import (
    ConversationSelection as ConversationSelection,
)
from .conversation_ledger_models import (
    RejectedLine as RejectedLine,
)
from .conversation_references import (
    AttemptReference as AttemptReference,
)
from .conversation_references import (
    ConversationBinding as ConversationBinding,
)
from .conversation_references import (
    ConversationReference as ConversationReference,
)
from .conversation_references import (
    parse_attempt_reference as parse_attempt_reference,
)
from .conversation_references import (
    parse_conversation_reference as parse_conversation_reference,
)
from .conversation_relocation import (
    RelocationReport as RelocationReport,
)
from .conversation_relocation import (
    recorded_plan_dirs as recorded_plan_dirs,
)
from .conversation_relocation import (
    relocate_records as relocate_records,
)
from .conversation_selection import (
    confirm_conversation as confirm_conversation,
)
from .conversation_selection import (
    open_conversation as open_conversation,
)
from .conversation_selection import (
    retire_completed_session as retire_completed_session,
)
from .conversation_selection import (
    retire_conversation as retire_conversation,
)
from .conversation_selection import (
    select_conversation as select_conversation,
)
from .conversation_storage import (
    _append_lock as _append_lock,
)
from .conversation_storage import (
    _append_record as _append_record,
)
from .conversation_storage import (
    _encode_record as _encode_record,
)
from .conversation_storage import (
    _parse_line as _parse_line,
)
from .conversation_storage import (
    _physical_lines as _physical_lines,
)
from .conversation_storage import (
    _write_record as _write_record,
)
from .conversation_storage import (
    conversation_ledger_path as conversation_ledger_path,
)
from .conversation_storage import (
    conversation_lock_path as conversation_lock_path,
)
from .conversation_storage import (
    read_conversations as read_conversations,
)
from .conversation_validation import (
    ATTEMPT_OUTCOMES as ATTEMPT_OUTCOMES,
)
from .conversation_validation import (
    CAPABILITY_STATES as CAPABILITY_STATES,
)
from .conversation_validation import (
    CONTINUATION_MODES as CONTINUATION_MODES,
)
from .conversation_validation import (
    CONVERSATION_KINDS as CONVERSATION_KINDS,
)
from .conversation_validation import (
    CONVERSATION_LEDGER_FILE_NAME as CONVERSATION_LEDGER_FILE_NAME,
)
from .conversation_validation import (
    CONVERSATION_LOCK_FILE_NAME as CONVERSATION_LOCK_FILE_NAME,
)
from .conversation_validation import (
    CONVERSATION_SCHEMA_VERSION as CONVERSATION_SCHEMA_VERSION,
)
from .conversation_validation import (
    ERROR_FRESH_NOT_AUTHORIZED as ERROR_FRESH_NOT_AUTHORIZED,
)
from .conversation_validation import (
    ERROR_INVALID_RECORD as ERROR_INVALID_RECORD,
)
from .conversation_validation import (
    ERROR_INVALID_REFERENCE as ERROR_INVALID_REFERENCE,
)
from .conversation_validation import (
    ERROR_LEDGER_FULL as ERROR_LEDGER_FULL,
)
from .conversation_validation import (
    ERROR_LEDGER_WRITE as ERROR_LEDGER_WRITE,
)
from .conversation_validation import (
    ERROR_RECORD_TOO_LARGE as ERROR_RECORD_TOO_LARGE,
)
from .conversation_validation import (
    ERROR_UNKNOWN_CONVERSATION as ERROR_UNKNOWN_CONVERSATION,
)
from .conversation_validation import (
    KIND_CONVERSATION_CONFIRMED as KIND_CONVERSATION_CONFIRMED,
)
from .conversation_validation import (
    KIND_CONVERSATION_OPENED as KIND_CONVERSATION_OPENED,
)
from .conversation_validation import (
    KIND_CONVERSATION_RETIRED as KIND_CONVERSATION_RETIRED,
)
from .conversation_validation import (
    MAX_CONVERSATION_LEDGER_BYTES as MAX_CONVERSATION_LEDGER_BYTES,
)
from .conversation_validation import (
    MAX_CONVERSATION_RECORD_BYTES as MAX_CONVERSATION_RECORD_BYTES,
)
from .conversation_validation import (
    MAX_DETAIL_CHARS as MAX_DETAIL_CHARS,
)
from .conversation_validation import (
    MAX_INTERRUPTED_OPERATIONS as MAX_INTERRUPTED_OPERATIONS,
)
from .conversation_validation import (
    MAX_NATIVE_ID_CHARS as MAX_NATIVE_ID_CHARS,
)
from .conversation_validation import (
    MAX_OPERATION_ID_CHARS as MAX_OPERATION_ID_CHARS,
)
from .conversation_validation import (
    MAX_PLAN_DIR_CHARS as MAX_PLAN_DIR_CHARS,
)
from .conversation_validation import (
    MAX_PRIOR_ATTEMPTS as MAX_PRIOR_ATTEMPTS,
)
from .conversation_validation import (
    MAX_REJECTED_RAW_CHARS as MAX_REJECTED_RAW_CHARS,
)
from .conversation_validation import (
    MAX_SCOPE_ALIASES as MAX_SCOPE_ALIASES,
)
from .conversation_validation import (
    NATIVE_ID_RE as NATIVE_ID_RE,
)
from .conversation_validation import (
    OPERATION_ID_RE as OPERATION_ID_RE,
)
from .conversation_validation import (
    REJECT_EMPTY_LINE as REJECT_EMPTY_LINE,
)
from .conversation_validation import (
    REJECT_INVALID_JSON as REJECT_INVALID_JSON,
)
from .conversation_validation import (
    REJECT_INVALID_RECORD as REJECT_INVALID_RECORD,
)
from .conversation_validation import (
    REJECT_NOT_OBJECT as REJECT_NOT_OBJECT,
)
from .conversation_validation import (
    REJECT_RECORD_TOO_LARGE as REJECT_RECORD_TOO_LARGE,
)
from .conversation_validation import (
    REJECT_UNKNOWN_KIND as REJECT_UNKNOWN_KIND,
)
from .conversation_validation import (
    REJECT_UNREADABLE_LEDGER as REJECT_UNREADABLE_LEDGER,
)
from .conversation_validation import (
    REJECT_UNSUPPORTED_VERSION as REJECT_UNSUPPORTED_VERSION,
)
from .conversation_validation import (
    REJECTION_REASONS as REJECTION_REASONS,
)
from .conversation_validation import (
    RETIREMENT_REASONS as RETIREMENT_REASONS,
)
from .conversation_validation import (
    SELECTION_ABSENT as SELECTION_ABSENT,
)
from .conversation_validation import (
    SELECTION_CORRUPT as SELECTION_CORRUPT,
)
from .conversation_validation import (
    SELECTION_INCOMPATIBLE as SELECTION_INCOMPATIBLE,
)
from .conversation_validation import (
    SELECTION_RESUMABLE as SELECTION_RESUMABLE,
)
from .conversation_validation import (
    SELECTION_RETIRED as SELECTION_RETIRED,
)
from .conversation_validation import (
    SELECTION_STATES as SELECTION_STATES,
)
from .conversation_validation import (
    SELECTION_UNCONFIRMED as SELECTION_UNCONFIRMED,
)
from .conversation_validation import (
    SUPPORTED_CONVERSATION_SCHEMA_VERSIONS as SUPPORTED_CONVERSATION_SCHEMA_VERSIONS,
)
from .conversation_validation import (
    AttemptOutcome as AttemptOutcome,
)
from .conversation_validation import (
    CapabilityState as CapabilityState,
)
from .conversation_validation import (
    ContinuationMode as ContinuationMode,
)
from .conversation_validation import (
    ConversationError as ConversationError,
)
from .conversation_validation import (
    ConversationKind as ConversationKind,
)
from .conversation_validation import (
    RetirementReason as RetirementReason,
)
from .conversation_validation import (
    SelectionState as SelectionState,
)
from .conversation_validation import (
    _narrow as _narrow,
)
from .conversation_validation import (
    _require_text as _require_text,
)
from .conversation_validation import (
    _require_turn as _require_turn,
)
from .conversation_validation import (
    _validate_plan_dir as _validate_plan_dir,
)
from .conversation_validation import (
    _validate_scope_aliases as _validate_scope_aliases,
)
from .conversation_validation import (
    _validate_session_id as _validate_session_id,
)
from .conversation_validation import (
    _workspace_binding as _workspace_binding,
)
from .conversation_validation import (
    validate_native_id as validate_native_id,
)
from .conversation_validation import (
    validate_operation_id as validate_operation_id,
)
from .harnesses import HARNESS_NAMES as HARNESS_NAMES
