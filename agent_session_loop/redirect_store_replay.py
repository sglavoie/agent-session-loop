"""Replay: fold the journal's physical lines into requests and rejections.

Split out of the original ``redirect_store`` module; see
:mod:`agent_session_loop.redirect_store_vocab` for the journal's design.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import clock, literals
from .redirect_store_models import (
    RedirectJournalView,
    RedirectRequest,
    RedirectStoreError,
    RedirectTarget,
    RejectedRecord,
    _precedes_continuation,
    _recovers_continuation,
)
from .redirect_store_validation import feedback_digest, validate_target
from .redirect_store_vocab import (
    ANNOTATION_KINDS,
    KIND_CONTINUED,
    KIND_DELIVERED,
    KIND_EXPIRED,
    KIND_LAUNCHING,
    KIND_REJECTED,
    KIND_RELEASED,
    KIND_SUBMITTED,
    REASON_EXPIRED,
    RECORD_KINDS,
    REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION,
    REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION,
    REDIRECT_JOURNAL_SUPPORTED_VERSIONS,
    REJECT_ALREADY_ACKNOWLEDGED,
    REJECT_ALREADY_CARRIED,
    REJECT_ALREADY_DELIVERED,
    REJECT_ALREADY_LAUNCHING,
    REJECT_ALREADY_SETTLED,
    REJECT_CONTINUATION_MISMATCH,
    REJECT_DUPLICATE_ID,
    REJECT_EMPTY_LINE,
    REJECT_INVALID_CONTINUATION,
    REJECT_INVALID_FEEDBACK,
    REJECT_INVALID_JSON,
    REJECT_INVALID_REASON,
    REJECT_INVALID_TARGET,
    REJECT_INVALID_TIMESTAMP,
    REJECT_MISSING_ID,
    REJECT_NOT_ACCEPTED,
    REJECT_NOT_CARRIED,
    REJECT_NOT_OBJECT,
    REJECT_UNKNOWN_KIND,
    REJECT_UNKNOWN_REQUEST,
    REJECT_UNSUPPORTED_VERSION,
    REQUEST_STATES,
    SETTLEMENT_REASONS,
    STATE_SUBMITTED,
    SettlementReason,
)


def _physical_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


def _parse_target(payload: object) -> RedirectTarget | None:
    """Read one target out of a journal line's mapping, or ``None`` if malformed.

    Accepts ``object`` rather than a mapping because two of its callers read a
    nested value out of a line another process wrote: a ``continuation`` that is
    absent, null, or not an object is a line to reject, not a crash to raise.
    """
    if not isinstance(payload, Mapping):
        return None
    run_id = payload.get("run_id")
    session_id = payload.get("session_id")
    turn_index = payload.get("turn_index")
    harness = payload.get("harness")
    if harness is not None and not isinstance(harness, str):
        return None
    if not isinstance(run_id, str) or not isinstance(session_id, str):
        return None
    if isinstance(turn_index, bool) or not isinstance(turn_index, int):
        return None
    candidate = RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness or None,
    )
    try:
        return validate_target(candidate)
    except RedirectStoreError:
        return None


class _Replay:
    """Fold the journal's lines into requests and the lines that failed.

    A class rather than a closure-heavy function because every branch needs the
    same three pieces of mutable state, and a reducer whose rejection path can
    silently skip updating one of them is how a duplicate acceptance gets
    applied twice.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.requests: dict[str, RedirectRequest] = {}
        self.feedback: dict[str, str] = {}
        self.rejected: list[RejectedRecord] = []
        self.last_seq = 0
        self.size_bytes = 0
        self.torn_tail = False

    def reject(self, seq: int, reason: str, detail: str | None = None) -> None:
        self.rejected.append(RejectedRecord(seq=seq, reason=reason, detail=detail))

    def apply(self, seq: int, payload: Mapping[str, Any]) -> None:
        version = payload.get("version")
        # ``True`` equals 1, so membership alone would admit a boolean and hand
        # it to the legacy branch below. The version now decides what a line
        # asserts rather than only whether it is readable, which makes a value
        # that is not a version a line to refuse.
        if (
            isinstance(version, bool)
            or version not in REDIRECT_JOURNAL_SUPPORTED_VERSIONS
        ):
            self.reject(seq, REJECT_UNSUPPORTED_VERSION, str(version))
            return
        kind = payload.get("kind")
        if kind not in RECORD_KINDS:
            self.reject(seq, REJECT_UNKNOWN_KIND, str(kind))
            return
        operation_id = payload.get("id")
        if not isinstance(operation_id, str) or not operation_id:
            self.reject(seq, REJECT_MISSING_ID)
            return
        created_at = payload.get("created_at")
        if clock.parse_datetime(created_at) is None:
            self.reject(seq, REJECT_INVALID_TIMESTAMP, operation_id)
            return
        if kind == KIND_SUBMITTED:
            self._apply_submitted(seq, payload, operation_id, str(created_at))
            return
        if kind in ANNOTATION_KINDS:
            self._apply_annotation(
                seq,
                payload,
                operation_id,
                str(created_at),
                kind,
                # The line's own version, not the module's. What ``continued``
                # asserts changed between the two, and the writer's version is
                # the only record of which assertion this line was making.
                int(version),
            )
            return
        self._apply_settlement(seq, payload, operation_id, str(created_at), kind)

    def _apply_submitted(
        self,
        seq: int,
        payload: Mapping[str, Any],
        operation_id: str,
        created_at: str,
    ) -> None:
        if operation_id in self.requests:
            self.reject(seq, REJECT_DUPLICATE_ID, operation_id)
            return
        target = _parse_target(payload)
        if target is None:
            self.reject(seq, REJECT_INVALID_TARGET, operation_id)
            return
        expires_at = payload.get("expires_at")
        if clock.parse_datetime(expires_at) is None:
            self.reject(seq, REJECT_INVALID_TIMESTAMP, operation_id)
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            self.reject(seq, REJECT_INVALID_FEEDBACK, operation_id)
            return
        self.order.append(operation_id)
        self.feedback[operation_id] = text
        self.requests[operation_id] = RedirectRequest(
            operation_id=operation_id,
            target=target,
            state=STATE_SUBMITTED,
            created_at=created_at,
            expires_at=str(expires_at),
            feedback_bytes=len(text.encode("utf-8")),
            feedback_digest=feedback_digest(text),
            submitted_seq=seq,
        )

    def _apply_annotation(
        self,
        seq: int,
        payload: Mapping[str, Any],
        operation_id: str,
        created_at: str,
        kind: str,
        record_version: int,
    ) -> None:
        """Apply a carry annotation to an accepted request.

        ``continued`` creates the carry; ``launching``, ``delivered``,
        ``released`` and ``acknowledged`` all speak about a carry already on the
        record and are refused unless they name the one the journal currently
        holds.

        Every condition the appenders check is re-checked here, because a replay
        that trusted the file would let a hand-edited or racing line invent a
        receipt for an attempt that never carried the correction — and a receipt
        is the whole evidence that a worker read it. The same applies to
        ``delivered``: it is what stops a later reader from recovering words a
        worker is already acting on.

        ``record_version`` decides what a ``continued`` line claims.
        :data:`REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION` and below wrote it from
        a run that had already launched the worker, so the carry and the launch
        are the same line and both are applied from it. Through
        :data:`REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION` the writer had no
        ``launching`` line at all, so the carry is marked as one whose launch
        window was never declarable rather than one that was declared and did
        not happen.
        """
        existing = self.requests.get(operation_id)
        if existing is None:
            self.reject(seq, REJECT_UNKNOWN_REQUEST, operation_id)
            return
        continuation = _parse_target(payload.get("continuation"))
        if continuation is None:
            self.reject(seq, REJECT_INVALID_CONTINUATION, operation_id)
            return
        # The relaxed cross-run rule is reachable only from a line that says so.
        # An unmarked line is held to the ordinary ordering, so a hand-written
        # continuation cannot borrow recovery's latitude by omitting the marker.
        recovered = payload.get("recovered") is True
        admissible = (
            _recovers_continuation(existing.target, continuation)
            if recovered
            else _precedes_continuation(existing.target, continuation)
        )
        if not admissible:
            self.reject(seq, REJECT_INVALID_CONTINUATION, operation_id)
            return
        if kind == KIND_CONTINUED:
            if not existing.accepted:
                self.reject(
                    seq,
                    (
                        REJECT_ALREADY_SETTLED
                        if existing.terminal
                        else REJECT_NOT_ACCEPTED
                    ),
                    operation_id,
                )
                return
            if existing.carried:
                self.reject(seq, REJECT_ALREADY_CARRIED, operation_id)
                return
            carried = replace(
                existing,
                continuation=continuation,
                continued_at=created_at,
                continued_seq=seq,
                continuation_recovered=recovered,
                launching_at=None,
                launching_seq=None,
                launch_unrecorded=(
                    record_version <= REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION
                ),
            )
            if record_version <= REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION:
                # The writer of this line had no separate launch record to
                # append, so its absence proves nothing. Treating the carry as
                # the delivery it was meant as is what keeps an upgrade from
                # replaying a correction a worker already received.
                carried = replace(
                    carried,
                    delivered_at=created_at,
                    delivered_seq=seq,
                    delivered_legacy=True,
                )
            self.requests[operation_id] = carried
            return
        if not existing.carried:
            self.reject(seq, REJECT_NOT_CARRIED, operation_id)
            return
        if not existing.continues_attempt(continuation):
            self.reject(seq, REJECT_CONTINUATION_MISMATCH, operation_id)
            return
        if recovered != existing.continuation_recovered:
            # The annotation has to name delivery as the journal recorded it. A
            # line that agrees on the turn but disagrees on whether that turn
            # crossed a crash is describing a delivery that never happened.
            self.reject(seq, REJECT_CONTINUATION_MISMATCH, operation_id)
            return
        if kind == KIND_LAUNCHING:
            # The window can only be declared open once, and only while it is
            # still ahead of the prompt. A line claiming it after a delivery or
            # a receipt is describing a moment that has already passed.
            if existing.delivered:
                self.reject(seq, REJECT_ALREADY_DELIVERED, operation_id)
                return
            if existing.acknowledged:
                self.reject(seq, REJECT_ALREADY_ACKNOWLEDGED, operation_id)
                return
            if existing.launching:
                self.reject(seq, REJECT_ALREADY_LAUNCHING, operation_id)
                return
            self.requests[operation_id] = replace(
                existing,
                launching_at=created_at,
                launching_seq=seq,
            )
            return
        if kind == KIND_DELIVERED:
            if existing.delivered:
                self.reject(seq, REJECT_ALREADY_DELIVERED, operation_id)
                return
            self.requests[operation_id] = replace(
                existing,
                delivered_at=created_at,
                delivered_seq=seq,
            )
            return
        if kind == KIND_RELEASED:
            # Releasing is only ever the undo of a carry nothing saw. Once a
            # worker was launched with the words -- or receipted them -- the
            # attempt owns them, and withdrawing the carry here would hand the
            # same correction to a second worker. A declared-but-unfinished
            # launch window is not that: the attempt writing this line is the
            # one that declared it and is still alive to say the prompt never
            # went out, which is the only evidence that can close the window.
            if existing.delivered:
                self.reject(seq, REJECT_ALREADY_DELIVERED, operation_id)
                return
            if existing.acknowledged:
                self.reject(seq, REJECT_ALREADY_ACKNOWLEDGED, operation_id)
                return
            self.requests[operation_id] = replace(
                existing,
                continuation=None,
                continued_at=None,
                continued_seq=None,
                continuation_recovered=False,
                launching_at=None,
                launching_seq=None,
                launch_unrecorded=False,
                released_at=created_at,
                released_seq=seq,
                released_from=continuation,
            )
            return
        if existing.acknowledged:
            self.reject(seq, REJECT_ALREADY_ACKNOWLEDGED, operation_id)
            return
        self.requests[operation_id] = replace(
            existing,
            acknowledged_at=created_at,
            acknowledged_seq=seq,
        )

    def _apply_settlement(
        self,
        seq: int,
        payload: Mapping[str, Any],
        operation_id: str,
        created_at: str,
        kind: str,
    ) -> None:
        existing = self.requests.get(operation_id)
        if existing is None:
            self.reject(seq, REJECT_UNKNOWN_REQUEST, operation_id)
            return
        if existing.terminal:
            # First terminal record wins. A racing second acceptance is a line
            # that was not applied, never a request that changed its answer.
            self.reject(seq, REJECT_ALREADY_SETTLED, operation_id)
            return
        reason: SettlementReason | None = None
        if kind == KIND_REJECTED:
            try:
                reason = literals.as_literal(
                    payload.get("reason"),
                    SETTLEMENT_REASONS,
                    "reason",
                )
            except ValueError:
                self.reject(seq, REJECT_INVALID_REASON, operation_id)
                return
        elif kind == KIND_EXPIRED:
            reason = REASON_EXPIRED
        self.requests[operation_id] = replace(
            existing,
            state=literals.as_literal(kind, REQUEST_STATES, "kind"),
            settled_at=created_at,
            settled_seq=seq,
            reason=reason,
        )


def _replay(data: bytes) -> _Replay:
    replay = _Replay()
    lines = _physical_lines(data)
    replay.last_seq = len(lines)
    replay.size_bytes = len(data)
    replay.torn_tail = bool(data) and not data.endswith(b"\n")
    for seq, raw_bytes in enumerate(lines, start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        if not raw.strip():
            replay.reject(seq, REJECT_EMPTY_LINE)
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            # The message names a position, never the bytes at it.
            replay.reject(seq, REJECT_INVALID_JSON, error.msg)
            continue
        except RecursionError:
            # Nested past the decoder's depth: unreadable like any bad line, so
            # it is counted rather than allowed to fail every journal reader.
            replay.reject(seq, REJECT_INVALID_JSON, "nested too deeply")
            continue
        if not isinstance(payload, dict):
            replay.reject(seq, REJECT_NOT_OBJECT)
            continue
        replay.apply(seq, payload)
    return replay


def _view(replay: _Replay, journal_path: Path) -> RedirectJournalView:
    return RedirectJournalView(
        journal_path=journal_path,
        requests=tuple(replay.requests[key] for key in replay.order),
        rejected=tuple(replay.rejected),
        last_seq=replay.last_seq,
        size_bytes=replay.size_bytes,
        torn_tail=replay.torn_tail,
    )
