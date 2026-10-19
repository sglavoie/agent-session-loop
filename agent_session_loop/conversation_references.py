"""Conversation references."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import conversation_validation as validation
from .clock import utc_iso_now
from .harnesses import (
    ACCESS_PROFILE_NAMES,
    HARNESS_NAMES,
    AccessProfileName,
    HarnessName,
)
from .workspaces import WorkspaceBinding


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
        object.__setattr__(
            self, "plan_dir", validation._validate_plan_dir(self.plan_dir)
        )
        object.__setattr__(
            self,
            "session_id",
            validation._validate_session_id(self.session_id),
        )
        object.__setattr__(
            self,
            "harness",
            validation._narrow(self.harness, HARNESS_NAMES, "harness"),
        )
        if not isinstance(self.workspace, WorkspaceBinding):
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "workspace must be a WorkspaceBinding value",
            )
        object.__setattr__(
            self,
            "access_profile",
            validation._narrow(
                self.access_profile, ACCESS_PROFILE_NAMES, "access_profile"
            ),
        )
        object.__setattr__(
            self,
            "scope_aliases",
            validation._validate_scope_aliases(self.scope_aliases),
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
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "other must be a ConversationBinding value",
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
    outcome: validation.AttemptOutcome
    recorded_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            validation._require_text(self.run_id, field="run_id", limit=200),
        )
        object.__setattr__(self, "turn", validation._require_turn(self.turn))
        object.__setattr__(
            self,
            "outcome",
            validation._narrow(self.outcome, validation.ATTEMPT_OUTCOMES, "outcome"),
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
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            f"prior attempt must be an object; got {payload!r}",
        )
    return AttemptReference(
        run_id=validation._require_text(
            payload.get("run_id"), field="run_id", limit=200
        ),
        turn=validation._require_turn(payload.get("turn")),
        outcome=validation._narrow(
            payload.get("outcome"), validation.ATTEMPT_OUTCOMES, "outcome"
        ),
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
    version: int = validation.CONVERSATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.binding, ConversationBinding):
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "binding must be a ConversationBinding value",
            )
        object.__setattr__(
            self,
            "native_id",
            validation.validate_native_id(self.native_id),
        )
        attempts = tuple(self.prior_attempts)
        if len(attempts) > validation.MAX_PRIOR_ATTEMPTS:
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "prior_attempts must hold at most "
                f"{validation.MAX_PRIOR_ATTEMPTS} entries; "
                f"got {len(attempts)}",
            )
        if any(not isinstance(attempt, AttemptReference) for attempt in attempts):
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "prior_attempts entries must be AttemptReference values",
            )
        object.__setattr__(self, "prior_attempts", attempts)
        recorded_at = self.recorded_at if isinstance(self.recorded_at, str) else ""
        object.__setattr__(self, "recorded_at", recorded_at.strip() or utc_iso_now())
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version not in validation.SUPPORTED_CONVERSATION_SCHEMA_VERSIONS
        ):
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "version must be one of "
                f"{sorted(validation.SUPPORTED_CONVERSATION_SCHEMA_VERSIONS)}; "
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
            raise validation.ConversationError(
                validation.ERROR_INVALID_REFERENCE,
                "attempt must be an AttemptReference value",
            )
        attempts = (*self.prior_attempts, attempt)[-validation.MAX_PRIOR_ATTEMPTS :]
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
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            f"reference must be an object; got {payload!r}",
        )
    raw_attempts = payload.get("prior_attempts", ())
    if isinstance(raw_attempts, str) or not isinstance(raw_attempts, Sequence):
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            f"prior_attempts must be a sequence; got {raw_attempts!r}",
        )
    raw_aliases = payload.get("scope_aliases", ())
    if isinstance(raw_aliases, str) or not isinstance(raw_aliases, Sequence):
        raise validation.ConversationError(
            validation.ERROR_INVALID_RECORD,
            f"scope_aliases must be a sequence; got {raw_aliases!r}",
        )
    binding = ConversationBinding(
        plan_dir=validation._validate_plan_dir(payload.get("plan_dir")),
        session_id=validation._validate_session_id(payload.get("session_id")),
        harness=validation._narrow(payload.get("harness"), HARNESS_NAMES, "harness"),
        workspace=validation._workspace_binding(
            payload.get("workspace_root"), payload.get("registry_digest")
        ),
        access_profile=validation._narrow(
            payload.get("access_profile"), ACCESS_PROFILE_NAMES, "access_profile"
        ),
        scope_aliases=tuple(raw_aliases),
    )
    return ConversationReference(
        binding=binding,
        native_id=validation.validate_native_id(payload.get("native_id")),
        prior_attempts=tuple(parse_attempt_reference(entry) for entry in raw_attempts),
        recorded_at=payload.get("recorded_at", ""),
        version=payload.get("version", validation.CONVERSATION_SCHEMA_VERSION),
    )
