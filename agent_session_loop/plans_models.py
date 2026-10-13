"""Frozen dataclasses describing parsed plan-directory state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .plans_session_ids import (
    SessionId,
)

StatusValue = Literal["pending", "complete", "empty", "unknown"]


ReadinessState = Literal["draft", "reviewed", "loop-ready", "running", "complete"]


READINESS_STATES: tuple[ReadinessState, ...] = (
    "draft",
    "reviewed",
    "loop-ready",
    "running",
    "complete",
)


@dataclass(frozen=True)
class SessionFile:
    """A canonical `session-<id>.md` file."""

    session_id: SessionId
    path: Path

    @property
    def id(self) -> str:
        return self.session_id.value

    @property
    def number(self) -> int:
        return self.session_id.as_int()

    @property
    def filename(self) -> str:
        return self.session_id.filename

    @property
    def expansion_filename(self) -> str:
        return self.session_id.expansion_filename

    @property
    def display_label(self) -> str:
        return self.session_id.display_label


@dataclass(frozen=True)
class SessionStatus:
    """Parsed handoff status for one session file."""

    session: SessionFile
    status: StatusValue
    raw_value: str
    line_number: int | None

    @property
    def session_id(self) -> SessionId:
        return self.session.session_id

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def number(self) -> int:
        return self.session.number

    @property
    def path(self) -> Path:
        return self.session.path

    @property
    def display_label(self) -> str:
        return self.session.display_label


@dataclass(frozen=True)
class SessionHandoff:
    """Parsed `## Handoff` block contents for one session file."""

    session: SessionFile
    status: StatusValue
    raw_value: str
    line_number: int | None
    files_modified: tuple[str, ...] = ()
    key_decisions: str | None = None
    blockers: str | None = None
    notes: str | None = None
    discovered_facts: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

    @property
    def session_id(self) -> SessionId:
        return self.session.session_id

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def number(self) -> int:
        return self.session.number

    @property
    def path(self) -> Path:
        return self.session.path

    @property
    def display_label(self) -> str:
        return self.session.display_label


@dataclass(frozen=True)
class ProgressEntry:
    """One rendered session row in the runner-managed `progress.md` journal."""

    session_id: SessionId
    status: StatusValue
    completed_at: str
    files_modified: tuple[str, ...] = ()
    key_decisions: str | None = None
    discovered_facts: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    blockers: str | None = None


@dataclass(frozen=True)
class PlanSnapshot:
    """Current durable state read from a split session plan directory."""

    plan_dir: Path
    context_path: Path
    manifest_path: Path
    plan_title: str
    sessions: tuple[SessionStatus, ...]
    content_sha256: str
    path_hash: str
    fingerprint: str

    @property
    def plan_hash(self) -> str:
        return self.fingerprint

    @property
    def next_pending(self) -> SessionStatus | None:
        return next(
            (session for session in self.sessions if session.status == "pending"), None
        )

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(session.id for session in self.sessions)

    @property
    def status_by_id(self) -> dict[str, StatusValue]:
        return {session.id: session.status for session in self.sessions}

    @property
    def status_by_number(self) -> dict[int, StatusValue]:
        return {
            session.number: session.status
            for session in self.sessions
            if session.session_id.is_integer
        }


@dataclass(frozen=True)
class ReadinessMetadata:
    """Readiness state recorded in a plan manifest."""

    state: ReadinessState
    readiness_sha256: str
    checked_at: str
