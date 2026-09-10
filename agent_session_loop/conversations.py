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
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import artifacts, durability
from .clock import utc_iso_now
from .harnesses import (
    ACCESS_PROFILE_NAMES,
    HARNESS_NAMES,
    AccessProfileName,
    HarnessName,
    continues_conversations,
)
from .literals import as_literal
from .plans import canonicalize_path, is_valid_session_id
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


@dataclass(frozen=True)
class ConversationBinding:
    """Everything a conversation is bound to, and nothing it merely used.

    A native conversation identifier is meaningless on its own: the same string
    may name a different conversation under a different harness, and the same
    conversation is the wrong one to continue once the workspace, the repository
    scope, or the access profile has changed underneath it. All of that is
    recorded here so a later lookup can be *checked* rather than trusted.
    """

    plan_dir: str
    session_id: str
    harness: HarnessName
    workspace: WorkspaceBinding
    access_profile: AccessProfileName
    scope_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_dir", _validate_plan_dir(self.plan_dir))
        object.__setattr__(self, "session_id", _validate_session_id(self.session_id))
        object.__setattr__(
            self, "harness", _narrow(self.harness, HARNESS_NAMES, "harness")
        )
        if not isinstance(self.workspace, WorkspaceBinding):
            raise ConversationError(
                ERROR_INVALID_REFERENCE, "workspace must be a WorkspaceBinding value"
            )
        object.__setattr__(
            self,
            "access_profile",
            _narrow(self.access_profile, ACCESS_PROFILE_NAMES, "access_profile"),
        )
        object.__setattr__(
            self, "scope_aliases", _validate_scope_aliases(self.scope_aliases)
        )

    @property
    def key(self) -> tuple[str, str]:
        """The ledger key: one live conversation per plan session and harness.

        Deliberately *not* widened by the workspace, scope, or access profile.
        Those are checked on lookup, so a changed binding is reported as
        incompatible instead of quietly opening a second conversation for the
        same session under the same harness.
        """
        return (self.session_id, self.harness)

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "session_id": self.session_id,
            "harness": self.harness,
            "workspace_root": self.workspace.workspace_root,
            "registry_digest": self.workspace.registry_digest,
            "access_profile": self.access_profile,
            "scope_aliases": list(self.scope_aliases),
        }

    def mismatches(self, other: ConversationBinding) -> tuple[str, ...]:
        """Name every bound field on which ``self`` and ``other`` differ.

        Returned in a stable order, so a refusal reads the same way twice, and as
        field names rather than values, so the caller decides what an operator is
        shown.
        """
        if not isinstance(other, ConversationBinding):
            raise ConversationError(
                ERROR_INVALID_REFERENCE, "other must be a ConversationBinding value"
            )
        comparisons: tuple[tuple[str, object, object], ...] = (
            ("plan_dir", self.plan_dir, other.plan_dir),
            ("session_id", self.session_id, other.session_id),
            ("harness", self.harness, other.harness),
            (
                "workspace_root",
                self.workspace.workspace_root,
                other.workspace.workspace_root,
            ),
            (
                "registry_digest",
                self.workspace.registry_digest,
                other.workspace.registry_digest,
            ),
            ("access_profile", self.access_profile, other.access_profile),
            ("scope_aliases", self.scope_aliases, other.scope_aliases),
        )
        return tuple(name for name, mine, theirs in comparisons if mine != theirs)


@dataclass(frozen=True)
class AttemptReference:
    """One previous attempt that ran on this conversation and did not finish.

    Kept so the resumed worker can be told what was interrupted. The record is
    identifiers and an outcome; the feedback that caused an interruption is never
    stored here.
    """

    run_id: str
    turn: int
    outcome: AttemptOutcome
    recorded_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "run_id", _require_text(self.run_id, field="run_id", limit=200)
        )
        object.__setattr__(self, "turn", _require_turn(self.turn))
        object.__setattr__(
            self, "outcome", _narrow(self.outcome, ATTEMPT_OUTCOMES, "outcome")
        )
        recorded_at = self.recorded_at if isinstance(self.recorded_at, str) else ""
        object.__setattr__(self, "recorded_at", recorded_at.strip() or utc_iso_now())

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "turn": self.turn,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
        }


def parse_attempt_reference(payload: object) -> AttemptReference:
    """Read one prior-attempt reference back from a stored payload."""
    if not isinstance(payload, Mapping):
        raise ConversationError(
            ERROR_INVALID_RECORD, f"prior attempt must be an object; got {payload!r}"
        )
    return AttemptReference(
        run_id=_require_text(payload.get("run_id"), field="run_id", limit=200),
        turn=_require_turn(payload.get("turn")),
        outcome=_narrow(payload.get("outcome"), ATTEMPT_OUTCOMES, "outcome"),
        recorded_at=payload.get("recorded_at", ""),
    )


@dataclass(frozen=True)
class ConversationReference:
    """The shared reference to one native harness conversation.

    This is the value every layer that continues a turn passes around: the CLI,
    the supervisor, the bridge, and the harness adapters name a conversation by
    this rather than by a bare string, so no code path can reach a harness
    holding an identifier whose bindings were never checked.
    """

    binding: ConversationBinding
    native_id: str
    prior_attempts: tuple[AttemptReference, ...] = ()
    recorded_at: str = ""
    version: int = CONVERSATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ConversationBinding):
            raise ConversationError(
                ERROR_INVALID_REFERENCE, "binding must be a ConversationBinding value"
            )
        object.__setattr__(self, "native_id", validate_native_id(self.native_id))
        attempts = tuple(self.prior_attempts)
        if len(attempts) > MAX_PRIOR_ATTEMPTS:
            raise ConversationError(
                ERROR_INVALID_REFERENCE,
                f"prior_attempts must hold at most {MAX_PRIOR_ATTEMPTS} entries; "
                f"got {len(attempts)}",
            )
        if any(not isinstance(attempt, AttemptReference) for attempt in attempts):
            raise ConversationError(
                ERROR_INVALID_REFERENCE,
                "prior_attempts entries must be AttemptReference values",
            )
        object.__setattr__(self, "prior_attempts", attempts)
        recorded_at = self.recorded_at if isinstance(self.recorded_at, str) else ""
        object.__setattr__(self, "recorded_at", recorded_at.strip() or utc_iso_now())
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version not in SUPPORTED_CONVERSATION_SCHEMA_VERSIONS
        ):
            raise ConversationError(
                ERROR_INVALID_REFERENCE,
                "version must be one of "
                f"{sorted(SUPPORTED_CONVERSATION_SCHEMA_VERSIONS)}; "
                f"got {self.version!r}",
            )

    @property
    def key(self) -> tuple[str, str]:
        return self.binding.key

    @property
    def locator(self) -> str:
        """A short, quotable name for this conversation in operator output."""
        return f"{self.binding.harness}:{self.native_id}"

    def to_payload(self) -> dict[str, Any]:
        """Render the reference as the versioned payload the ledger stores."""
        payload = self.binding.to_payload()
        payload.update(
            {
                "native_id": self.native_id,
                "prior_attempts": [
                    attempt.to_payload() for attempt in self.prior_attempts
                ],
                "recorded_at": self.recorded_at,
                "version": self.version,
            }
        )
        return payload

    def with_attempt(self, attempt: AttemptReference) -> ConversationReference:
        """Return a copy naming one more prior attempt, oldest first.

        At the bound, the *oldest* attempt is dropped rather than the append
        being refused: the newest interruptions are the ones a resumed worker has
        to be told about, and a long-running session must not become
        unrecordable.
        """
        if not isinstance(attempt, AttemptReference):
            raise ConversationError(
                ERROR_INVALID_REFERENCE, "attempt must be an AttemptReference value"
            )
        attempts = (*self.prior_attempts, attempt)[-MAX_PRIOR_ATTEMPTS:]
        return ConversationReference(
            binding=self.binding,
            native_id=self.native_id,
            prior_attempts=attempts,
            recorded_at=self.recorded_at,
            version=self.version,
        )


def parse_conversation_reference(payload: object) -> ConversationReference:
    """Read a conversation reference back from a stored payload.

    Every closed vocabulary is narrowed through
    :func:`~agent_session_loop.literals.as_literal` and every identifier through
    its validator, so a hand-edited or truncated record raises here rather than
    reaching a harness.
    """
    if not isinstance(payload, Mapping):
        raise ConversationError(
            ERROR_INVALID_RECORD, f"reference must be an object; got {payload!r}"
        )
    raw_attempts = payload.get("prior_attempts", ())
    if isinstance(raw_attempts, str) or not isinstance(raw_attempts, Sequence):
        raise ConversationError(
            ERROR_INVALID_RECORD,
            f"prior_attempts must be a sequence; got {raw_attempts!r}",
        )
    raw_aliases = payload.get("scope_aliases", ())
    if isinstance(raw_aliases, str) or not isinstance(raw_aliases, Sequence):
        raise ConversationError(
            ERROR_INVALID_RECORD,
            f"scope_aliases must be a sequence; got {raw_aliases!r}",
        )
    binding = ConversationBinding(
        plan_dir=_validate_plan_dir(payload.get("plan_dir")),
        session_id=_validate_session_id(payload.get("session_id")),
        harness=_narrow(payload.get("harness"), HARNESS_NAMES, "harness"),
        workspace=_workspace_binding(
            payload.get("workspace_root"), payload.get("registry_digest")
        ),
        access_profile=_narrow(
            payload.get("access_profile"), ACCESS_PROFILE_NAMES, "access_profile"
        ),
        scope_aliases=tuple(raw_aliases),
    )
    return ConversationReference(
        binding=binding,
        native_id=validate_native_id(payload.get("native_id")),
        prior_attempts=tuple(parse_attempt_reference(entry) for entry in raw_attempts),
        recorded_at=payload.get("recorded_at", ""),
        version=payload.get("version", CONVERSATION_SCHEMA_VERSION),
    )


@dataclass(frozen=True)
class ContinuationIntent:
    """What the next turn is being asked to do about a conversation.

    Deliberately has no serialization. The recovery policy says an operator's
    fresh-conversation authorization is explicit and one-use and must never be
    persisted or automatically rehydrated -- so the value that carries it is one
    this module cannot write down. There is no ``to_payload`` here, and the
    ledger's writers accept references and retirements only.

    ``interrupted_operations`` holds operation *identifiers*, never the feedback
    that caused an interruption: the resumed worker is told which operations were
    cut short and reads their content from the channel that owns it.
    """

    mode: ContinuationMode
    reference: ConversationReference | None = None
    interrupted_operations: tuple[str, ...] = ()
    fresh_authorized: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _narrow(self.mode, CONTINUATION_MODES, "mode"))
        operations = tuple(self.interrupted_operations)
        if len(operations) > MAX_INTERRUPTED_OPERATIONS:
            raise ConversationError(
                ERROR_INVALID_REFERENCE,
                "interrupted_operations must hold at most "
                f"{MAX_INTERRUPTED_OPERATIONS} entries; got {len(operations)}",
            )
        object.__setattr__(
            self,
            "interrupted_operations",
            tuple(validate_operation_id(entry) for entry in operations),
        )
        if self.mode == "resume":
            if not isinstance(self.reference, ConversationReference):
                raise ConversationError(
                    ERROR_UNKNOWN_CONVERSATION,
                    "a resume intent requires the conversation reference it continues",
                )
            if self.fresh_authorized:
                raise ConversationError(
                    ERROR_INVALID_REFERENCE,
                    "a resume intent must not carry a fresh-conversation authorization",
                )
            return
        if self.reference is not None:
            raise ConversationError(
                ERROR_INVALID_REFERENCE,
                "a fresh intent must not name a conversation to continue",
            )
        if not self.fresh_authorized:
            raise ConversationError(
                ERROR_FRESH_NOT_AUTHORIZED,
                "starting a fresh conversation requires an explicit, one-use "
                "operator authorization",
            )

    @property
    def resumes(self) -> bool:
        return self.mode == "resume"

    def describe(self) -> dict[str, Any]:
        """An observable, feedback-free projection for status and events.

        Not a serialization: it names the conversation and the interrupted
        operations for an operator, and it is never read back into an intent. The
        authorization flag is *reported* rather than stored, because a run that
        started fresh should say so even though nothing may rehydrate it.
        """
        return {
            "mode": self.mode,
            "conversation": None if self.reference is None else self.reference.locator,
            "interrupted_operations": list(self.interrupted_operations),
            "fresh_authorized": self.fresh_authorized,
        }


def resume_intent(
    reference: ConversationReference,
    *,
    interrupted_operations: Sequence[str] = (),
) -> ContinuationIntent:
    """Build the intent that continues ``reference``."""
    return ContinuationIntent(
        mode="resume",
        reference=reference,
        interrupted_operations=tuple(interrupted_operations),
    )


def fresh_intent(
    *,
    authorized: bool,
    interrupted_operations: Sequence[str] = (),
) -> ContinuationIntent:
    """Build the intent that starts a fresh conversation.

    ``authorized`` is the operator's explicit, one-use permission. Passing
    ``False`` raises rather than downgrading to a fresh start, which is the whole
    point: no code path may turn a missing or incompatible conversation into a
    silent new one.
    """
    return ContinuationIntent(
        mode="fresh",
        interrupted_operations=tuple(interrupted_operations),
        fresh_authorized=authorized,
    )


@dataclass(frozen=True)
class HarnessCapability:
    """What one harness can do about conversation continuity in this build."""

    harness: HarnessName
    state: CapabilityState
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "harness", _narrow(self.harness, HARNESS_NAMES, "harness")
        )
        object.__setattr__(
            self, "state", _narrow(self.state, CAPABILITY_STATES, "state")
        )
        if not isinstance(self.detail, str):
            raise ConversationError(
                ERROR_INVALID_RECORD, f"detail must be a string; got {self.detail!r}"
            )
        object.__setattr__(self, "detail", self.detail.strip()[:MAX_DETAIL_CHARS])

    @property
    def available(self) -> bool:
        return self.state == "available"

    def to_payload(self) -> dict[str, Any]:
        return {"harness": self.harness, "state": self.state, "detail": self.detail}


@dataclass(frozen=True)
class CapabilityReport:
    """Conversation-continuity capability for every harness, as one value.

    The report is what an operator surface asks before offering continuation. It
    names *every* harness, including the ones that cannot do it, so a caller
    iterating the report cannot silently omit a harness it forgot about.
    """

    capabilities: tuple[HarnessCapability, ...]
    version: int = CONVERSATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        capabilities = tuple(self.capabilities)
        if any(
            not isinstance(capability, HarnessCapability) for capability in capabilities
        ):
            raise ConversationError(
                ERROR_INVALID_RECORD,
                "capabilities entries must be HarnessCapability values",
            )
        named = sorted(capability.harness for capability in capabilities)
        if named != sorted(HARNESS_NAMES):
            raise ConversationError(
                ERROR_INVALID_RECORD,
                "a capability report must name every harness exactly once; "
                f"got {named}",
            )
        order = {name: index for index, name in enumerate(HARNESS_NAMES)}
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted(capabilities, key=lambda entry: order[entry.harness])),
        )
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version not in SUPPORTED_CONVERSATION_SCHEMA_VERSIONS
        ):
            raise ConversationError(
                ERROR_INVALID_RECORD,
                "version must be one of "
                f"{sorted(SUPPORTED_CONVERSATION_SCHEMA_VERSIONS)}; "
                f"got {self.version!r}",
            )

    def for_harness(self, name: str) -> HarnessCapability:
        harness = _narrow(name, HARNESS_NAMES, "harness")
        for capability in self.capabilities:
            if capability.harness == harness:
                return capability
        raise AssertionError("capability report is missing a validated harness")

    @property
    def available_harnesses(self) -> tuple[HarnessName, ...]:
        return tuple(
            capability.harness
            for capability in self.capabilities
            if capability.available
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "capabilities": [
                capability.to_payload() for capability in self.capabilities
            ],
        }


#: Why a harness is staged rather than available. The contracts, the durable
#: store, and the turn-lifecycle seam exist; *this* adapter does not report or
#: resume a native conversation through that seam yet, and an operator action
#: backed by incomplete recovery must not be offered for it.
STAGED_CAPABILITY_DETAIL = (
    "conversation contracts, durable storage, and the turn lifecycle seam are "
    "in place; this harness does not capture or resume a native conversation yet"
)

#: Why a harness whose adapter does capture and resume is available.
AVAILABLE_CAPABILITY_DETAIL = (
    "this harness reports the conversation each turn runs on and can start a "
    "turn on a conversation it is handed"
)


def capability_report() -> CapabilityReport:
    """Return this build's conversation-continuity capability for each harness.

    Derived from the adapters rather than declared here: a harness is
    ``available`` exactly when its adapter both reports the conversation a turn
    ran on and accepts one to resume, which is the same pair of flags
    ``HarnessAdapter.run`` enforces when it refuses a capture sink or a resume
    intent. One producer, so this report cannot promise recovery the turn
    boundary would refuse to perform. ``codex`` is ``available`` today; the
    rest are ``staged``.
    """
    return CapabilityReport(
        capabilities=tuple(
            HarnessCapability(
                harness=name,
                state="available" if continues_conversations(name) else "staged",
                detail=(
                    AVAILABLE_CAPABILITY_DETAIL
                    if continues_conversations(name)
                    else STAGED_CAPABILITY_DETAIL
                ),
            )
            for name in HARNESS_NAMES
        )
    )


def parse_capability_report(payload: object) -> CapabilityReport:
    """Read a capability report back from a stored or transported payload."""
    if not isinstance(payload, Mapping):
        raise ConversationError(
            ERROR_INVALID_RECORD,
            f"capability report must be an object; got {payload!r}",
        )
    entries = payload.get("capabilities", ())
    if isinstance(entries, str) or not isinstance(entries, Sequence):
        raise ConversationError(
            ERROR_INVALID_RECORD, f"capabilities must be a sequence; got {entries!r}"
        )
    capabilities: list[HarnessCapability] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ConversationError(
                ERROR_INVALID_RECORD, f"capability must be an object; got {entry!r}"
            )
        capabilities.append(
            HarnessCapability(
                harness=_narrow(
                    entry.get("harness"),
                    HARNESS_NAMES,
                    "harness",
                    reason=ERROR_INVALID_RECORD,
                ),
                state=_narrow(
                    entry.get("state"),
                    CAPABILITY_STATES,
                    "state",
                    reason=ERROR_INVALID_RECORD,
                ),
                detail=entry.get("detail", ""),
            )
        )
    return CapabilityReport(
        capabilities=tuple(capabilities),
        version=payload.get("version", CONVERSATION_SCHEMA_VERSION),
    )


@dataclass(frozen=True)
class ConversationRecord:
    """One readable line of the ledger, with the position it occupies.

    ``sequence`` is positional and one-based. A record returned by a writer
    carries the sequence its line actually took, so a caller can quote the exact
    line it appended.
    """

    sequence: int
    kind: ConversationKind
    reference: ConversationReference
    recorded_at: str
    retirement_reason: RetirementReason | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.reference.key

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": CONVERSATION_SCHEMA_VERSION,
            "kind": self.kind,
            "recorded_at": self.recorded_at,
            "reference": self.reference.to_payload(),
        }
        if self.retirement_reason is not None:
            payload["retirement_reason"] = self.retirement_reason
        return payload


@dataclass(frozen=True)
class RejectedLine:
    """One physical line that could not be read, and why.

    Keeps its positional sequence: the line stays part of the history rather than
    being dropped to make the file parse, and its presence is what makes
    corruption visible to selection.
    """

    sequence: int
    reason: str
    raw: str


@dataclass(frozen=True)
class ConversationLedger:
    """Everything one plan's ledger holds, readable lines and unreadable ones."""

    records: tuple[ConversationRecord, ...]
    rejected: tuple[RejectedLine, ...]

    @property
    def trustworthy(self) -> bool:
        """Whether every physical line was accounted for.

        An unreadable line cannot be attributed to a key, so it may be the
        retirement or the supersession of any conversation in the file. Nothing
        may be selected while one exists.
        """
        return not self.rejected

    def history(self, key: tuple[str, str]) -> tuple[ConversationRecord, ...]:
        """Every readable record for one ``(session_id, harness)`` key, in order."""
        return tuple(record for record in self.records if record.key == key)

    def to_payload(self) -> dict[str, Any]:
        return {
            "records": [record.to_payload() for record in self.records],
            "rejected": [
                {"sequence": line.sequence, "reason": line.reason, "raw": line.raw}
                for line in self.rejected
            ],
        }


@dataclass(frozen=True)
class ConversationSelection:
    """The answer to "may this turn continue a saved conversation?".

    Exactly one state permits continuation. Every other state is a refusal the
    recovery policy sends to the operator, so callers test :attr:`resumable`
    rather than testing for absence.
    """

    state: SelectionState
    reference: ConversationReference | None = None
    mismatched_fields: tuple[str, ...] = ()
    detail: str = ""

    @property
    def resumable(self) -> bool:
        return self.state == SELECTION_RESUMABLE

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "conversation": None if self.reference is None else self.reference.locator,
            "mismatched_fields": list(self.mismatched_fields),
            "detail": self.detail,
        }


def conversation_ledger_path(plan_state: str | Path) -> Path:
    """The conversation ledger inside one plan's state directory."""
    return Path(plan_state) / CONVERSATION_LEDGER_FILE_NAME


def conversation_lock_path(plan_state: str | Path) -> Path:
    """The ``flock`` target that serializes appends to that ledger."""
    return Path(plan_state) / CONVERSATION_LOCK_FILE_NAME


def _encode_record(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _physical_lines(data: bytes) -> list[bytes]:
    """Split ledger bytes into physical lines, keeping an unterminated tail.

    A crash-truncated final line has no newline. It is a line all the same -- it
    occupies a sequence and is reported as rejected -- so splitting must not
    discard it, and an empty element after a proper final newline must not invent
    one.
    """
    if not data:
        return []
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


@contextmanager
def _append_lock(plan_state: Path) -> Iterator[None]:
    """Hold the exclusive claim on writing this plan's ledger.

    A sidecar rather than the ledger file itself: :func:`relocate_records`
    replaces that path, so two callers holding it could be holding different
    files. ``flock`` is owned by the open file description, so this excludes
    threads as well as processes, and the kernel releases it if the holder dies
    mid-write.
    """
    plan_state.mkdir(parents=True, exist_ok=True)
    lock_path = conversation_lock_path(plan_state)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _append_record(plan_state: Path, payload: Mapping[str, Any]) -> int:
    """Append one prevalidated record as a single durable transaction.

    Returns the one-based sequence the appended line occupies.
    """
    encoded = _encode_record(payload)
    if len(encoded) > MAX_CONVERSATION_RECORD_BYTES:
        raise ConversationError(
            ERROR_RECORD_TOO_LARGE,
            f"conversation record is {len(encoded)} bytes, over the "
            f"{MAX_CONVERSATION_RECORD_BYTES}-byte limit",
        )
    ledger_path = conversation_ledger_path(plan_state)
    with _append_lock(plan_state):
        try:
            existing = ledger_path.read_bytes()
        except FileNotFoundError:
            existing = b""
        except OSError as error:
            raise ConversationError(
                ERROR_LEDGER_WRITE,
                f"could not read {ledger_path} before appending: {error}",
                {"path": str(ledger_path)},
            ) from error
        # Terminate an interrupted append rather than cutting it back: the
        # truncated bytes stay in history as a rejected line, which is what keeps
        # a crash visible instead of silently healed.
        separator = b"\n" if existing and not existing.endswith(b"\n") else b""
        chunk = separator + encoded
        if len(existing) + len(chunk) > MAX_CONVERSATION_LEDGER_BYTES:
            raise ConversationError(
                ERROR_LEDGER_FULL,
                f"append would exceed {MAX_CONVERSATION_LEDGER_BYTES} bytes",
                {"path": str(ledger_path)},
            )
        handle = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            original_size = os.fstat(handle).st_size
            try:
                written = os.write(handle, chunk)
                if written != len(chunk):
                    os.ftruncate(handle, original_size)
                    raise OSError(
                        f"short conversation append: wrote {written} of "
                        f"{len(chunk)} bytes"
                    )
                # The next turn's decision to resume rests on this line. Flush it
                # through the device cache before the lock is released, so a
                # sudden shutdown cannot lose a reference whose conversation the
                # harness has already created.
                durability.fullsync(handle)
            except OSError as error:
                with contextlib.suppress(OSError):
                    os.ftruncate(handle, original_size)
                raise ConversationError(
                    ERROR_LEDGER_WRITE,
                    f"could not append to {ledger_path}: {error}",
                    {"path": str(ledger_path)},
                ) from error
        finally:
            os.close(handle)
        return len(_physical_lines(existing + chunk))


def _parse_line(sequence: int, raw: bytes) -> ConversationRecord | RejectedLine:
    text = raw.decode("utf-8", errors="replace")
    preview = text[:MAX_REJECTED_RAW_CHARS]
    if not text.strip():
        return RejectedLine(sequence, REJECT_EMPTY_LINE, preview)
    if len(raw) > MAX_CONVERSATION_RECORD_BYTES:
        return RejectedLine(sequence, REJECT_RECORD_TOO_LARGE, preview)
    try:
        parsed = json.loads(text)
    except ValueError:
        return RejectedLine(sequence, REJECT_INVALID_JSON, preview)
    if not isinstance(parsed, dict):
        return RejectedLine(sequence, REJECT_NOT_OBJECT, preview)
    version = parsed.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in SUPPORTED_CONVERSATION_SCHEMA_VERSIONS
    ):
        return RejectedLine(sequence, REJECT_UNSUPPORTED_VERSION, preview)
    if parsed.get("kind") not in CONVERSATION_KINDS:
        return RejectedLine(sequence, REJECT_UNKNOWN_KIND, preview)
    try:
        kind = _narrow(parsed.get("kind"), CONVERSATION_KINDS, "kind")
        reference = parse_conversation_reference(parsed.get("reference"))
        raw_reason = parsed.get("retirement_reason")
        reason: RetirementReason | None = None
        if kind == KIND_CONVERSATION_RETIRED:
            reason = _narrow(raw_reason, RETIREMENT_REASONS, "retirement_reason")
        elif raw_reason is not None:
            raise ConversationError(
                ERROR_INVALID_RECORD,
                f"only a {KIND_CONVERSATION_RETIRED} record may carry a "
                "retirement reason",
            )
        recorded_at = parsed.get("recorded_at")
        if not isinstance(recorded_at, str) or not recorded_at.strip():
            raise ConversationError(
                ERROR_INVALID_RECORD, "record requires a recorded_at timestamp"
            )
    except ConversationError:
        return RejectedLine(sequence, REJECT_INVALID_RECORD, preview)
    return ConversationRecord(
        sequence=sequence,
        kind=kind,
        reference=reference,
        recorded_at=recorded_at,
        retirement_reason=reason,
    )


def read_conversations(plan_state: str | Path) -> ConversationLedger:
    """Read one plan's conversation ledger, readable lines and rejected ones.

    A plan whose state predates this feature has no ledger at all, and that is an
    empty result rather than an exception: absent conversation state is a
    recovery decision for the caller and never a crash. A ledger file that cannot
    be *opened* is different from one that is merely absent -- it is reported as a
    rejected line, so selection fails closed instead of reading an unreadable
    ledger as an empty one.
    """
    ledger_path = conversation_ledger_path(plan_state)
    try:
        data = ledger_path.read_bytes()
    except FileNotFoundError, NotADirectoryError:
        return ConversationLedger(records=(), rejected=())
    except OSError as error:
        return ConversationLedger(
            records=(),
            rejected=(RejectedLine(1, REJECT_UNREADABLE_LEDGER, f"{error}"),),
        )
    records: list[ConversationRecord] = []
    rejected: list[RejectedLine] = []
    for offset, raw in enumerate(_physical_lines(data)):
        parsed = _parse_line(offset + 1, raw)
        if isinstance(parsed, RejectedLine):
            rejected.append(parsed)
        else:
            records.append(parsed)
    return ConversationLedger(records=tuple(records), rejected=tuple(rejected))


def _write_record(
    plan_state: str | Path,
    *,
    kind: ConversationKind,
    reference: ConversationReference,
    retirement_reason: RetirementReason | None = None,
) -> ConversationRecord:
    if not isinstance(reference, ConversationReference):
        raise ConversationError(
            ERROR_INVALID_REFERENCE, "reference must be a ConversationReference value"
        )
    record = ConversationRecord(
        sequence=0,
        kind=kind,
        reference=reference,
        recorded_at=utc_iso_now(),
        retirement_reason=retirement_reason,
    )
    sequence = _append_record(Path(plan_state), record.to_payload())
    return ConversationRecord(
        sequence=sequence,
        kind=record.kind,
        reference=record.reference,
        recorded_at=record.recorded_at,
        retirement_reason=record.retirement_reason,
    )


def open_conversation(
    plan_state: str | Path, reference: ConversationReference
) -> ConversationRecord:
    """Record ``reference`` before the work that will use it is submitted.

    This is the write the continuity contract requires to happen *first*. It
    deliberately does not make the conversation resumable: until
    :func:`confirm_conversation` records that the harness accepted work on it,
    selection reports it as unconfirmed, so a failure between this write and the
    first submitted turn cannot resume into a conversation that may hold nothing.
    """
    return _write_record(plan_state, kind=KIND_CONVERSATION_OPENED, reference=reference)


def confirm_conversation(
    plan_state: str | Path, reference: ConversationReference
) -> ConversationRecord:
    """Record that the harness accepted work on ``reference``.

    Only a confirmed conversation is resumable. Confirming one this ledger never
    saw opened is refused: the reference would rest on nothing, and a typo in a
    native identifier would mint a resumable conversation out of thin air.
    """
    if not isinstance(reference, ConversationReference):
        raise ConversationError(
            ERROR_INVALID_REFERENCE, "reference must be a ConversationReference value"
        )
    opened = [
        record
        for record in read_conversations(plan_state).history(reference.key)
        if record.kind == KIND_CONVERSATION_OPENED
        and record.reference.native_id == reference.native_id
    ]
    if not opened:
        raise ConversationError(
            ERROR_UNKNOWN_CONVERSATION,
            f"no opened conversation to confirm for {reference.locator}",
            {
                "session_id": reference.binding.session_id,
                "harness": reference.binding.harness,
            },
        )
    return _write_record(
        plan_state, kind=KIND_CONVERSATION_CONFIRMED, reference=reference
    )


def retire_conversation(
    plan_state: str | Path, reference: ConversationReference, *, reason: str
) -> ConversationRecord:
    """Record that ``reference`` may never be selected again.

    Retirement is an append, not a deletion: the history of a completed session
    stays readable, and a run that has already read the ledger cannot be raced
    into resuming a conversation whose record vanished underneath it.
    """
    return _write_record(
        plan_state,
        kind=KIND_CONVERSATION_RETIRED,
        reference=reference,
        retirement_reason=_narrow(reason, RETIREMENT_REASONS, "retirement_reason"),
    )


def retire_completed_session(
    plan_state: str | Path, *, session_id: str, harness: str | None = None
) -> tuple[ConversationRecord, ...]:
    """Retire every live conversation belonging to a completed plan session.

    A session that reached ``complete`` has no turn left to continue, so leaving
    its conversation resumable would let a later, unrelated attempt at the same
    session id continue work that already finished. ``harness=None`` retires
    every harness's conversation for the session, which is what completion means;
    naming one retires only that harness's.

    A ledger that cannot be fully read is refused rather than partially retired:
    the unreadable line may name the very conversation that would be left behind.
    """
    ledger = read_conversations(plan_state)
    if not ledger.trustworthy:
        raise ConversationError(
            ERROR_INVALID_RECORD,
            "conversation ledger holds unreadable lines; refusing to retire "
            "against a partial history",
            {"rejected": len(ledger.rejected)},
        )
    wanted = _validate_session_id(session_id)
    selected: HarnessName | None = None
    if harness is not None:
        selected = _narrow(harness, HARNESS_NAMES, "harness")
    retired: list[ConversationRecord] = []
    for name in HARNESS_NAMES:
        if selected is not None and name != selected:
            continue
        history = ledger.history((wanted, name))
        if not history or history[-1].kind == KIND_CONVERSATION_RETIRED:
            continue
        retired.append(
            retire_conversation(
                plan_state, history[-1].reference, reason="session-completed"
            )
        )
    return tuple(retired)


def select_conversation(
    plan_state: str | Path, binding: ConversationBinding
) -> ConversationSelection:
    """Answer whether ``binding``'s next turn may continue a saved conversation.

    The lookup is by ``(session_id, harness)`` and nothing else, and the record it
    finds is then *checked* against every other bound field. There is no fallback:
    a mismatch, an unconfirmed record, a retirement, or an unreadable ledger each
    yield a refusal naming why, and none of them yields a different conversation.
    That is what makes an invalid reference unable to select another conversation,
    and what leaves the recovery decision with the operator rather than here.
    """
    if not isinstance(binding, ConversationBinding):
        raise ConversationError(
            ERROR_INVALID_REFERENCE, "binding must be a ConversationBinding value"
        )
    ledger = read_conversations(plan_state)
    if not ledger.trustworthy:
        return ConversationSelection(
            state=SELECTION_CORRUPT,
            detail=(
                f"{len(ledger.rejected)} unreadable ledger line(s); an unreadable "
                "line may retire or supersede any conversation, so none may be "
                "selected"
            ),
        )
    history = ledger.history(binding.key)
    if not history:
        return ConversationSelection(
            state=SELECTION_ABSENT,
            detail=(
                f"no conversation recorded for session {binding.session_id} under "
                f"{binding.harness}"
            ),
        )
    latest = history[-1]
    if latest.kind == KIND_CONVERSATION_RETIRED:
        return ConversationSelection(
            state=SELECTION_RETIRED,
            reference=latest.reference,
            detail=(
                f"conversation {latest.reference.locator} was retired "
                f"({latest.retirement_reason})"
            ),
        )
    mismatches = binding.mismatches(latest.reference.binding)
    if mismatches:
        return ConversationSelection(
            state=SELECTION_INCOMPATIBLE,
            reference=latest.reference,
            mismatched_fields=mismatches,
            detail=(
                f"conversation {latest.reference.locator} is bound to a different "
                f"{', '.join(mismatches)}"
            ),
        )
    if latest.kind != KIND_CONVERSATION_CONFIRMED:
        return ConversationSelection(
            state=SELECTION_UNCONFIRMED,
            reference=latest.reference,
            detail=(
                f"conversation {latest.reference.locator} was recorded but no work "
                "was ever accepted on it"
            ),
        )
    return ConversationSelection(state=SELECTION_RESUMABLE, reference=latest.reference)


@dataclass(frozen=True)
class RelocationReport:
    """What :func:`relocate_records` did, or would do, to one plan's ledger."""

    ledger_path: str
    records: int
    rewritten: int
    rejected: int
    applied: bool


def recorded_plan_dirs(plan_state: str | Path) -> tuple[str, ...]:
    """Every distinct plan directory the readable records name, sorted.

    State-directory migration validates against this: a ledger naming a plan
    other than the one being migrated is not this plan's operational state, and
    relocating it would attach another plan's conversations to the target.
    """
    ledger = read_conversations(plan_state)
    return tuple(
        sorted({record.reference.binding.plan_dir for record in ledger.records})
    )


def relocate_records(
    plan_state: str | Path,
    *,
    from_plan_dir: str | Path,
    to_plan_dir: str | Path,
    apply: bool = False,
) -> RelocationReport:
    """Point this ledger's records at a plan directory that moved.

    The one sanctioned rewrite of an append-only file, and the same exception
    ``state.json``'s path fields already are: a plan that moved has operational
    state whose absolute paths are stale, and leaving them stale strands the
    conversations rather than preserving them.

    Line order is preserved and every unreadable line is copied back unchanged,
    so migration neither reorders history nor silently repairs corruption it
    could not read in the first place. The rewrite goes through one atomic,
    durable replace, so a crash mid-migration leaves either the old ledger or the
    new one. ``apply=False`` reports what would change and writes nothing.
    """
    ledger_path = conversation_ledger_path(plan_state)
    old_plan = str(canonicalize_path(from_plan_dir))
    new_plan = str(canonicalize_path(to_plan_dir))
    try:
        data = ledger_path.read_bytes()
    except FileNotFoundError, NotADirectoryError:
        return RelocationReport(
            ledger_path=str(ledger_path),
            records=0,
            rewritten=0,
            rejected=0,
            applied=False,
        )
    except OSError as error:
        raise ConversationError(
            ERROR_LEDGER_WRITE,
            f"could not read {ledger_path} before relocating: {error}",
            {"path": str(ledger_path)},
        ) from error
    lines = _physical_lines(data)
    terminated = data.endswith(b"\n")
    records = 0
    rewritten = 0
    rejected = 0
    rendered: list[bytes] = []
    for offset, raw in enumerate(lines):
        parsed = _parse_line(offset + 1, raw)
        if isinstance(parsed, RejectedLine):
            rejected += 1
            rendered.append(raw + b"\n")
            continue
        records += 1
        if parsed.reference.binding.plan_dir != old_plan:
            rendered.append(raw + b"\n")
            continue
        payload = parsed.to_payload()
        payload["reference"]["plan_dir"] = new_plan
        rendered.append(_encode_record(payload))
        rewritten += 1
    # An unterminated final line stays unterminated: it is the signature of an
    # interrupted append, and healing it here would erase the evidence that the
    # next append is meant to preserve.
    if rendered and not terminated:
        rendered[-1] = rendered[-1].removesuffix(b"\n")
    if apply and rewritten:
        with _append_lock(Path(plan_state)):
            artifacts.atomic_write_bytes(ledger_path, b"".join(rendered), durable=True)
    return RelocationReport(
        ledger_path=str(ledger_path),
        records=records,
        rewritten=rewritten,
        rejected=rejected,
        applied=bool(apply and rewritten),
    )
