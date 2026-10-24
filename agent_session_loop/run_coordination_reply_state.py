"""Rollback of paused Q&A state when a reply attempt fails before consumption."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from . import (
    commit_settlement_ledger,
    durability,
    events,
)

#: Events that record a commit, or an attempt at one, against a real Git
#: repository. Any of them appended during an unconsumed reply attempt means the
#: ledger now describes something that happened, so it must not be rolled back.
SIDE_EFFECT_EVENTS = frozenset(
    {
        events.COMMIT_STARTED_EVENT,
        events.COMMIT_FINISHED_EVENT,
        events.COMMIT_GROUP_STARTED_EVENT,
        events.COMMIT_GROUP_UPDATED_EVENT,
        events.COMMIT_GROUP_FINISHED_EVENT,
        events.PAUSED_WORK_COMMITTED_EVENT,
    }
)
#: ``state.json`` keys whose change during the attempt has the same meaning.
SIDE_EFFECT_STATE_KEYS = ("commit_groups", commit_settlement_ledger.COMMIT_FAILURES_KEY)
#: Payload key naming why a rollback was skipped, present only when it was.
ROLLBACK_SKIPPED_KEY = "reply_rollback_skipped"


def read_optional_bytes(path: Path) -> bytes | None:
    """Return ``path``'s bytes, or ``None`` when it does not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def restore_optional_bytes(path: Path, payload: bytes | None) -> None:
    """Put ``path`` back to ``payload``, removing it when that was absence."""
    if payload is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    # The bytes being replaced were written durably, so the restore must be as
    # well or a crash could resurrect the attempt it just rolled back.
    path.parent.mkdir(parents=True, exist_ok=True)
    durability.replace_bytes(path, payload, sync_file=True, sync_parent=True)


def _state_fields(payload: bytes | None) -> dict[str, Any]:
    state: Any = None
    if payload is not None:
        with contextlib.suppress(ValueError):
            state = json.loads(payload)
    if not isinstance(state, dict):
        state = {}
    return {key: state.get(key) for key in SIDE_EFFECT_STATE_KEYS}


def _has_new_entries(before: Any, after: Any) -> bool:
    """Whether ``after`` holds an entry ``before`` did not.

    A fresh run's payload carries over only some of the paused run's ledger, so
    an entry that disappeared says nothing; one that appeared or changed is a
    record this attempt wrote.
    """
    if not isinstance(after, list):
        return False
    previous = before if isinstance(before, list) else []
    return any(entry not in previous for entry in after)


def _appended_side_effect_events(
    before: bytes | None, after: bytes | None
) -> list[str]:
    if after is None:
        return []
    prefix = before or b""
    # A log that no longer starts with the snapshot was rewritten rather than
    # appended to; every line in it is then a candidate.
    appended = after[len(prefix) :] if after.startswith(prefix) else after
    names: list[str] = []
    for line in appended.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        name = record.get("name") if isinstance(record, dict) else None
        if name in SIDE_EFFECT_EVENTS and name not in names:
            names.append(name)
    return names


class ReplyStatePreserver:
    """Restore paused Q&A state when a reply attempt fails before consumption.

    A reply is delivered by replaying the paused run, which rewrites both
    ``state.json`` and ``events.jsonl`` before the harness has accepted
    anything. If the attempt dies short of consumption those two files describe
    a continuation that never happened, so the pre-attempt bytes are captured up
    front and put back unless :meth:`mark_consumed` says the reply really landed.

    The one exception is an attempt that did something real before consumption,
    such as settling an owed commit group at startup. Rolling that back would
    erase the only record of a commit that exists in Git, so the ledger is kept
    as written and the run stays paused on whatever it recorded; the operator
    resolves that and answers again.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        event_path: Path,
        enabled: bool,
    ) -> None:
        self._enabled = enabled
        self._consumed = False
        self._state_path = state_path
        self._event_path = event_path
        self._state_bytes = read_optional_bytes(state_path) if enabled else None
        self._event_bytes = read_optional_bytes(event_path) if enabled else None

    @property
    def consumed(self) -> bool:
        """Whether the reply reached the harness and must not be rolled back."""
        return self._consumed

    def mark_consumed(self) -> None:
        self._consumed = True

    def side_effect_evidence(self) -> list[str]:
        """Name what this attempt recorded that happened outside the ledger."""
        evidence = _appended_side_effect_events(
            self._event_bytes, read_optional_bytes(self._event_path)
        )
        before = _state_fields(self._state_bytes)
        after = _state_fields(read_optional_bytes(self._state_path))
        evidence.extend(
            f"state.{key}"
            for key in SIDE_EFFECT_STATE_KEYS
            if _has_new_entries(before[key], after[key])
        )
        return evidence

    def restore_if_unconsumed(self) -> dict[str, Any] | None:
        """Roll an unconsumed attempt back, or say why it was kept.

        Returns ``None`` when nothing needed doing or the rollback happened, and
        a record naming the evidence when a side effect kept the ledger as is.
        """
        if not self._enabled or self._consumed:
            return None
        evidence = self.side_effect_evidence()
        if evidence:
            return {
                "reason": "side-effect-before-consumption",
                "message": (
                    "the reply was not consumed, but the attempt recorded a "
                    "commit first, so its state was kept rather than rolled back"
                ),
                "evidence": evidence,
            }
        restore_optional_bytes(self._state_path, self._state_bytes)
        restore_optional_bytes(self._event_path, self._event_bytes)
        return None
