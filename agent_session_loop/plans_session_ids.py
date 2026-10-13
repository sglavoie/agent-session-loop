"""Hierarchical session identifiers and session filename parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

SESSION_ID_PATTERN = r"[1-9][0-9]*(?:\.[1-9][0-9]*)*"


SESSION_ID_RE = re.compile(rf"^{SESSION_ID_PATTERN}$")


SESSION_FILE_RE = re.compile(rf"^session-({SESSION_ID_PATTERN})\.md$")


SESSION_EXPANSION_FILE_RE = re.compile(rf"^session-({SESSION_ID_PATTERN})\.expand\.md$")


LOOSE_SESSION_FILE_RE = re.compile(r"^session-([0-9]+(?:\.[0-9]+)*)\.md$")


@dataclass(frozen=True, order=True)
class SessionId:
    """Canonical dotted session id sorted by numeric components."""

    components: tuple[int, ...]

    def __post_init__(self) -> None:
        components = tuple(self.components)
        if not components:
            raise ValueError("session id must contain at least one component")
        if any(
            type(component) is not int or component <= 0 for component in components
        ):
            raise ValueError("session id components must be positive integers")
        object.__setattr__(self, "components", components)

    @classmethod
    def parse(cls, value: int | str | SessionId) -> SessionId:
        if isinstance(value, SessionId):
            return value
        if type(value) is int:
            return cls((value,))
        if not isinstance(value, str):
            raise ValueError(f"invalid session id: {value!r}")

        text = value.strip()
        if SESSION_ID_RE.fullmatch(text) is None:
            raise ValueError(f"invalid session id: {value!r}")
        return cls(tuple(int(component) for component in text.split(".")))

    @property
    def value(self) -> str:
        return ".".join(str(component) for component in self.components)

    @property
    def is_integer(self) -> bool:
        return len(self.components) == 1

    @property
    def sort_key(self) -> tuple[int, ...]:
        return self.components

    @property
    def filename(self) -> str:
        return f"session-{self.value}.md"

    @property
    def expansion_filename(self) -> str:
        return f"session-{self.value}.expand.md"

    @property
    def display_label(self) -> str:
        return f"Session {self.value}"

    def as_int(self) -> int:
        if not self.is_integer:
            raise ValueError(f"nested session id has no integer alias: {self.value}")
        return self.components[0]

    def __str__(self) -> str:
        return self.value


def parse_session_id(value: int | str | SessionId) -> SessionId:
    """Parse and validate a canonical session id."""
    return SessionId.parse(value)


def is_valid_session_id(value: str) -> bool:
    return SESSION_ID_RE.fullmatch(value.strip()) is not None


def session_id_to_filename(value: int | str | SessionId) -> str:
    return parse_session_id(value).filename


def session_id_to_expansion_filename(value: int | str | SessionId) -> str:
    return parse_session_id(value).expansion_filename


def session_id_display_label(value: int | str | SessionId) -> str:
    return parse_session_id(value).display_label


def session_id_from_filename(filename: str) -> SessionId | None:
    match = SESSION_FILE_RE.fullmatch(filename)
    if match is None:
        return None
    return parse_session_id(match.group(1))


def session_id_from_expansion_filename(filename: str) -> SessionId | None:
    match = SESSION_EXPANSION_FILE_RE.fullmatch(filename)
    if match is None:
        return None
    return parse_session_id(match.group(1))


def _canonical_session_id_for_loose_filename(filename: str) -> SessionId | None:
    match = LOOSE_SESSION_FILE_RE.fullmatch(filename)
    if match is None:
        return None

    raw_id = match.group(1)
    try:
        components = tuple(int(component) for component in raw_id.split("."))
    except ValueError:
        return None
    if any(component <= 0 for component in components):
        return None

    canonical_id = SessionId(components)
    if canonical_id.value == raw_id:
        return None
    return canonical_id
