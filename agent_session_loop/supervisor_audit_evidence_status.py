"""The status section of the audit evidence bundle and its mailbox ask joins."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import events, mailbox_vocabulary
from .supervisor_audit_evidence_collector import _Collector, _projection
from .supervisor_audit_evidence_constants import (
    _STATUS_ACTION_REQUIRED_SUPERSEDED,
    _STATUS_STATE_KEYS,
    SUPERSEDED_BY_CONSUMED_RULING,
)
from .supervisor_audit_evidence_helpers import _moment


def _mailbox_asks(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every question in the run's mailbox slice, with how far it got.

    Mirrors :meth:`mailbox_snapshot.MailboxView.delivery_state_for`: a question is
    answered once a reply or a resolution targets it, and answered *and
    consumed* once a receipt names that closure. Only the consumed state
    proves the answer reached the run rather than sitting unread.
    """
    closures: dict[str, Mapping[str, Any]] = {}
    receipted: set[str] = set()
    for record in records:
        kind = record.get("kind")
        target = record.get("to")
        if kind in mailbox_vocabulary.CLOSURE_KINDS and target is not None:
            closures.setdefault(str(target), record)
        elif (
            kind == mailbox_vocabulary.KIND_RECEIPT
            and record.get("closure_id") is not None
        ):
            receipted.add(str(record["closure_id"]))
    asks: list[dict[str, Any]] = []
    for record in records:
        if (
            record.get("kind") != mailbox_vocabulary.KIND_QUESTION
            or record.get("id") is None
        ):
            continue
        closure = closures.get(str(record["id"]))
        closure_id = None if closure is None else closure.get("id")
        asks.append(
            {
                "id": str(record["id"]),
                "session_id": record.get("session_id"),
                "turn_index": record.get("turn_index"),
                "closure_id": closure_id,
                "closure_action": None if closure is None else closure.get("action"),
                "closed_at": None if closure is None else closure.get("created_at"),
                "consumed": closure_id is not None and str(closure_id) in receipted,
            }
        )
    return asks


def _raised_by_same_turn(ask: Mapping[str, Any], signal: Mapping[str, Any]) -> bool:
    """Whether ``ask`` was raised by the turn that emitted ``signal``.

    Both identifiers are required on both sides. A signal that names no turn
    cannot be shown to correspond to any particular question, and the whole
    point of the comparison is to avoid retiring one turn's ask on another
    turn's answer.
    """
    for key in ("session_id", "turn_index"):
        expected = signal.get(key)
        actual = ask.get(key)
        if expected is None or actual is None or str(expected) != str(actual):
            return False
    return True


def _status_section(
    collector: _Collector,
    state_payload: Mapping[str, Any],
    mailbox_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The terminal state projection, with an answered ask retired.

    ``action_required`` is the current unresolved intervention, not the run's
    history — the reading ``turn_execution_signals.clear_live_action_required``
    already enforces on live state, where starting another turn or finishing
    unpaused proves the signal was answered. Neither proof is available to a
    run that stopped *on* the signal, so a paused run reaches the bundle still
    advertising an ask a human has already ruled on, and an auditor cannot tell
    it from a run genuinely waiting on one.

    A ruling the run consumed is the third proof. When every question the
    signal's own turn raised has been answered and receipted, and at least one
    of those rulings is no older than the signal itself, the projection is
    retired and the record it replaced is kept beside it. ``action_required``
    of ``null`` next to a standing ``paused`` then reads as what it is: a run
    merely paused, not one still asking. The ordering requirement is what keeps
    an earlier, unrelated exchange in the same turn from retiring a signal
    raised after it; without a ruling that postdates the signal, nothing shows
    a human ever saw it.
    """
    status = _projection(collector, state_payload, _STATUS_STATE_KEYS)
    signal = status.get("action_required")
    if not isinstance(signal, Mapping):
        return status
    if signal.get("reason") not in events.LIVE_ACTION_REQUIRED_REASONS:
        return status
    raised_at = _moment(signal.get("created_at"))
    if raised_at is None:
        return status
    asks = [
        ask
        for ask in _mailbox_asks(mailbox_records)
        if _raised_by_same_turn(ask, signal)
    ]
    if not asks or not all(ask["consumed"] for ask in asks):
        return status
    answered_after = [
        ask
        for ask in asks
        if (closed_at := _moment(ask["closed_at"])) is not None
        and closed_at >= raised_at
    ]
    if not answered_after:
        return status
    status["action_required"] = None
    status[_STATUS_ACTION_REQUIRED_SUPERSEDED] = {
        "superseded_by": SUPERSEDED_BY_CONSUMED_RULING,
        "questions": asks,
        "record": dict(signal),
    }
    return status
