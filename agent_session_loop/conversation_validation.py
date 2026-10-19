"""Conversation validation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .literals import as_literal
from .plans_files import canonicalize_path
from .plans_session_ids import is_valid_session_id
from .workspaces import WorkspaceBinding, WorkspaceError, validate_repository_alias

#: The plan-owned ledger and the ``flock`` target that serializes appends to it.
#: Both sit directly in the plan's state directory, beside ``state.json`` rather
#: than inside it: the next run replaces that file and must not replace these.
CONVERSATION_LEDGER_FILE_NAME = "conversations.jsonl"


CONVERSATION_LOCK_FILE_NAME = "conversations.lock"


#: Record envelope version. A record declaring anything outside
#: :data:`SUPPORTED_CONVERSATION_SCHEMA_VERSIONS` is rejected rather than guessed
#: at, including a *newer* version: a build that cannot read a record cannot know
#: whether it retired the conversation it names.
CONVERSATION_SCHEMA_VERSION = 1


SUPPORTED_CONVERSATION_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


#: Bounds. A conversation record is identifiers and bindings, so these sit far
#: above any honest record and far below anything that could hide a transcript.
MAX_CONVERSATION_RECORD_BYTES = 8 * 1024


MAX_CONVERSATION_LEDGER_BYTES = 4 * 1024 * 1024


MAX_NATIVE_ID_CHARS = 200


MAX_PLAN_DIR_CHARS = 4096


MAX_PRIOR_ATTEMPTS = 32


MAX_SCOPE_ALIASES = 64


MAX_INTERRUPTED_OPERATIONS = 32


MAX_OPERATION_ID_CHARS = 128


MAX_DETAIL_CHARS = 500


#: How much of an unreadable line is quoted back. Enough to recognise it, not
#: enough for a truncated transcript to reappear in a report.
MAX_REJECTED_RAW_CHARS = 500


#: What a native conversation identifier may contain. Deliberately narrow: the
#: value is handed to a harness as an argument and stored one per line in a JSONL
#: ledger, so whitespace, newlines, and shell metacharacters are refused at the
#: parse boundary rather than escaped at every use.
NATIVE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")


OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


ConversationKind = Literal[
    "conversation_opened",
    "conversation_confirmed",
    "conversation_retired",
]


KIND_CONVERSATION_OPENED: ConversationKind = "conversation_opened"


KIND_CONVERSATION_CONFIRMED: ConversationKind = "conversation_confirmed"


KIND_CONVERSATION_RETIRED: ConversationKind = "conversation_retired"


#: The lifecycle, in order. ``opened`` is written before work is submitted,
#: ``confirmed`` once the harness has accepted work on the conversation, and
#: ``retired`` when it may never be selected again.
CONVERSATION_KINDS: tuple[ConversationKind, ...] = (
    "conversation_opened",
    "conversation_confirmed",
    "conversation_retired",
)


RetirementReason = Literal[
    "session-completed",
    "superseded",
    "operator-discarded",
    "unusable",
]


#: Why a conversation may never be selected again. ``session-completed`` is the
#: ordinary one: a plan session that reached ``complete`` has no further turn to
#: continue, so its conversation is retired rather than left resumable for a
#: later, unrelated attempt at the same session id.
RETIREMENT_REASONS: tuple[RetirementReason, ...] = (
    "session-completed",
    "superseded",
    "operator-discarded",
    "unusable",
)


AttemptOutcome = Literal["interrupted", "failed", "superseded"]


#: How a prior attempt on this conversation ended. Recorded so the resumed worker
#: can be told what was interrupted rather than having to infer it.
ATTEMPT_OUTCOMES: tuple[AttemptOutcome, ...] = (
    "interrupted",
    "failed",
    "superseded",
)


ContinuationMode = Literal["resume", "fresh"]


CONTINUATION_MODES: tuple[ContinuationMode, ...] = ("resume", "fresh")


CapabilityState = Literal["available", "staged", "unsupported"]


#: What a harness can do about conversation continuity in this build. ``staged``
#: is the honest state for a harness whose persistence, lifecycle, and recovery
#: prerequisites have not all landed: the contract exists, the operator action
#: does not. ``unsupported`` means the harness cannot carry a conversation at
#: all, which no amount of later work changes.
CAPABILITY_STATES: tuple[CapabilityState, ...] = (
    "available",
    "staged",
    "unsupported",
)


SelectionState = Literal[
    "resumable",
    "absent",
    "unconfirmed",
    "incompatible",
    "retired",
    "corrupt",
]


SELECTION_RESUMABLE: SelectionState = "resumable"


SELECTION_ABSENT: SelectionState = "absent"


SELECTION_UNCONFIRMED: SelectionState = "unconfirmed"


SELECTION_INCOMPATIBLE: SelectionState = "incompatible"


SELECTION_RETIRED: SelectionState = "retired"


SELECTION_CORRUPT: SelectionState = "corrupt"


#: Every answer :func:`select_conversation` can give. Exactly one of them permits
#: continuation; the rest are refusals that require user input.
SELECTION_STATES: tuple[SelectionState, ...] = (
    "resumable",
    "absent",
    "unconfirmed",
    "incompatible",
    "retired",
    "corrupt",
)


REJECT_EMPTY_LINE = "empty-line"


REJECT_INVALID_JSON = "invalid-json"


REJECT_NOT_OBJECT = "not-object"


REJECT_UNSUPPORTED_VERSION = "unsupported-version"


REJECT_UNKNOWN_KIND = "unknown-kind"


REJECT_INVALID_RECORD = "invalid-record"


REJECT_RECORD_TOO_LARGE = "record-too-large"


REJECT_UNREADABLE_LEDGER = "unreadable-ledger"


#: Why one physical line could not be read, in a stable order for reporting.
REJECTION_REASONS: tuple[str, ...] = (
    REJECT_EMPTY_LINE,
    REJECT_INVALID_JSON,
    REJECT_NOT_OBJECT,
    REJECT_UNSUPPORTED_VERSION,
    REJECT_UNKNOWN_KIND,
    REJECT_INVALID_RECORD,
    REJECT_RECORD_TOO_LARGE,
    REJECT_UNREADABLE_LEDGER,
)


ERROR_INVALID_REFERENCE = "invalid-conversation-reference"


ERROR_INVALID_RECORD = "invalid-conversation-record"


ERROR_RECORD_TOO_LARGE = "conversation-record-too-large"


ERROR_LEDGER_FULL = "conversation-ledger-full"


ERROR_LEDGER_WRITE = "conversation-ledger-write-failed"


ERROR_UNKNOWN_CONVERSATION = "unknown-conversation"


ERROR_FRESH_NOT_AUTHORIZED = "fresh-conversation-not-authorized"


class ConversationError(Exception):
    """Raised when a conversation record cannot be built, read, or stored.

    Carries a closed ``reason`` and a details mapping, so a CLI or bridge
    response can name the failure without re-deriving it from the message.
    """

    def __init__(
        self, reason: str, message: str, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})


def _narrow[ValueT: str](
    value: object,
    allowed: Sequence[ValueT],
    field: str,
    *,
    reason: str = ERROR_INVALID_REFERENCE,
) -> ValueT:
    """Narrow ``value`` to ``allowed``, raising this module's error type.

    :func:`~agent_session_loop.literals.as_literal` builds ``error(message)``
    with one argument while :class:`ConversationError` needs a reason too, so the
    translation happens here rather than through a one-argument subclass. Every
    closed vocabulary in this module is narrowed through here, which is what lets
    a parser accept an arbitrary stored value without ever widening a field's
    declared type.
    """
    try:
        return as_literal(value, allowed, field)
    except ValueError as error:
        raise ConversationError(reason, str(error)) from error


def _require_text(
    value: object, *, field: str, limit: int, reason: str = ERROR_INVALID_REFERENCE
) -> str:
    if not isinstance(value, str):
        raise ConversationError(reason, f"{field} must be a string; got {value!r}")
    text = value.strip()
    if not text:
        raise ConversationError(reason, f"{field} must not be empty")
    if len(text) > limit:
        raise ConversationError(
            reason, f"{field} must be at most {limit} characters; got {len(text)}"
        )
    return text


def _require_turn(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConversationError(
            ERROR_INVALID_REFERENCE, f"turn must be an integer; got {value!r}"
        )
    if value < 0:
        raise ConversationError(
            ERROR_INVALID_REFERENCE, f"turn must not be negative; got {value}"
        )
    return value


def validate_native_id(value: object) -> str:
    """Return ``value`` as a storable native conversation identifier, or raise.

    The identifier is the harness's, so ASL neither invents nor normalises it
    beyond stripping surrounding whitespace: resuming works only against the
    exact string the harness reported.
    """
    text = _require_text(value, field="native_id", limit=MAX_NATIVE_ID_CHARS)
    if NATIVE_ID_RE.fullmatch(text) is None:
        raise ConversationError(
            ERROR_INVALID_REFERENCE,
            f"native_id must match {NATIVE_ID_RE.pattern}; got {text!r}",
            {"native_id": text},
        )
    return text


def validate_operation_id(value: object) -> str:
    """Return ``value`` as a storable operation identifier, or raise."""
    text = _require_text(value, field="operation_id", limit=MAX_OPERATION_ID_CHARS)
    if OPERATION_ID_RE.fullmatch(text) is None:
        raise ConversationError(
            ERROR_INVALID_REFERENCE,
            f"operation_id must match {OPERATION_ID_RE.pattern}; got {text!r}",
            {"operation_id": text},
        )
    return text


def _validate_session_id(value: object) -> str:
    text = _require_text(value, field="session_id", limit=MAX_NATIVE_ID_CHARS)
    if not is_valid_session_id(text):
        raise ConversationError(
            ERROR_INVALID_REFERENCE,
            f"session_id is not a canonical session id: {text!r}",
            {"session_id": text},
        )
    return text


def _validate_plan_dir(value: object) -> str:
    text = _require_text(value, field="plan_dir", limit=MAX_PLAN_DIR_CHARS)
    if not Path(text).is_absolute():
        raise ConversationError(
            ERROR_INVALID_REFERENCE,
            f"plan_dir must be an absolute path; got {text!r}",
            {"plan_dir": text},
        )
    return str(canonicalize_path(text))


def _validate_scope_aliases(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ConversationError(
            ERROR_INVALID_REFERENCE, f"scope_aliases must be a sequence; got {value!r}"
        )
    aliases = tuple(value)
    if len(aliases) > MAX_SCOPE_ALIASES:
        raise ConversationError(
            ERROR_INVALID_REFERENCE,
            f"scope_aliases must hold at most {MAX_SCOPE_ALIASES} entries; "
            f"got {len(aliases)}",
        )
    validated: list[str] = []
    for alias in aliases:
        if not isinstance(alias, str):
            raise ConversationError(
                ERROR_INVALID_REFERENCE,
                f"scope_aliases entries must be strings; got {alias!r}",
            )
        try:
            validate_repository_alias(alias)
        except WorkspaceError as error:
            raise ConversationError(
                ERROR_INVALID_REFERENCE, f"scope_aliases entry is invalid: {error}"
            ) from error
        validated.append(alias)
    if len(set(validated)) != len(validated):
        raise ConversationError(
            ERROR_INVALID_REFERENCE, "scope_aliases must not repeat an alias"
        )
    # Sorted, not source-ordered: the scope is a *set* of repositories, so two
    # callers spelling it in different orders must compare equal rather than look
    # like two different bindings.
    return tuple(sorted(validated))


def _workspace_binding(root: object, digest: object) -> WorkspaceBinding:
    if not isinstance(root, str) or not isinstance(digest, str):
        raise ConversationError(
            ERROR_INVALID_REFERENCE,
            "workspace requires string workspace_root and registry_digest",
        )
    try:
        return WorkspaceBinding(workspace_root=root, registry_digest=digest)
    except WorkspaceError as error:
        raise ConversationError(
            ERROR_INVALID_REFERENCE, f"workspace binding is invalid: {error}"
        ) from error
