"""NDJSON stdio bridge over ASL's active/actionable run projection.

The bridge is the authoritative boundary between ASL and a standalone
supervision client. Protocol 2 is UTF-8 NDJSON on stdio: the server writes a
``hello`` frame before reading anything, every request carries a correlation
``id`` answered by exactly one response, and unsolicited events carry a
monotonic ``seq``. Stdout is protocol-only; diagnostics go to stderr.

Protocol 2 is not a negotiation. The hello names the contract set both ends
must already agree on, and a client that does not recognize it has no
compatible way to continue: there is no protocol-1 parser left to fall back to.
Reads stay available across a mismatch so a stale cockpit can still explain
itself, but every mutation is gated behind ``session.handshake``, which accepts
only an exact contract-set match against a snapshot digest the bridge still
holds.

Five properties are structural rather than incidental:

- **Observation never writes.** Every state read goes through the explicitly
  non-reclaiming paths (``reclaim=False``), so a snapshot is a function of the
  corpus and not of the order in which the bridge happened to poll it.
- **Question bodies never enter a snapshot.** Rows carry question identity,
  counts, and timing only. The prose an operator answers is fetched deliberately
  through ``question.get`` and never rides along with a projection.
- **Every mutation goes through the shared service layer.** The handlers below
  own request parsing and the operation model; :mod:`.intervention` owns what is
  legal, so the bridge cannot drift from the CLI or the Workbench HTTP API.
- **Reply text never becomes protocol or diagnostic state.** It is passed
  straight to ASL's own durable records and is absent from every response,
  event, operation receipt, and stderr line the bridge can emit.
- **A mutation is authorized against state the client has actually seen.** The
  handshake pins a snapshot digest; the action token pins the plan. Both must
  still hold when the request lands, so an operator can never act on a row that
  moved between the projection and the click.
"""

from __future__ import annotations

import errno
import hashlib
import json
import select
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from asl_workbench import actions as action_rules
from asl_workbench import controls as control_rules
from asl_workbench import operations as operation_rules

from . import (
    __version__,
    control_queue,
    events,
    intervention,
    mailbox,
    plans,
    redirect_admission,
    redirect_cli,
    redirect_recovery,
    redirect_store,
    state,
    workbench,
    workbench_actions,
    workbench_controls,
    workbench_operations,
)
from .agent_mux_projection import CONTRACT_SET_ID

PROTOCOL_VERSION = 2

# A frame ceiling only helps if both ends agree on it before parsing, so it is
# advertised in the hello and enforced on read and on write.
MAX_FRAME_BYTES = 1024 * 1024

POLL_INTERVAL_SECONDS = 1.0

# How long a failed projection is owed before it is attempted again. A failure
# leaves the last successful projection's deadline in the past, so without a
# deadline of its own the loop selects with timeout zero and retries the same
# failing walk immediately, burning a core instead of waiting.
PROJECTION_RETRY_INTERVAL_SECONDS = 1.0

# How often the Workbench listener checks whether it has been asked to stop.
# It is well below the client's shutdown grace because the client terminates a
# bridge that has not exited by then, and the default half second is long
# enough to turn every clean exit into a kill.
WORKBENCH_POLL_INTERVAL_SECONDS = 0.02

# How long a stopping bridge waits for an in-flight projection to notice. A
# build cannot be interrupted mid-walk, so this is bounded by the corpus cost
# rather than by the client's shutdown grace; the thread is a daemon thread, so
# exceeding it costs an unjoined thread rather than a hung exit.
WORKER_JOIN_TIMEOUT_SECONDS = 0.1

# Ordered so a client can compare capability lists byte for byte across
# restarts. Later protocol work appends; it never reorders or removes.
#
# This is the *candidate* order. The advertised ``CAPABILITIES`` is derived at
# the bottom of this module by intersecting it with the methods that actually
# got registered, so a name can never reach a hello without a handler behind it.
#
# Protocol 2 splits the single protocol-1 capability list into three closed
# vocabularies. Mixing methods and events in one array made "can I call this?"
# and "will I be sent this?" indistinguishable to a client, and left the
# mutation subset — the one that needs a handshake — implicit.
METHOD_HANDSHAKE = "session.handshake"

METHOD_REDIRECT_STATUS = "run.redirect_status"
METHOD_RECOVER_CONVERSATION = "run.recover_conversation"

_METHOD_ORDER: tuple[str, ...] = (
    METHOD_HANDSHAKE,
    "runs.snapshot",
    "question.get",
    "question.reply",
    "question.resolve",
    "run.resume",
    "run.stop_after_current",
    "run.abort",
    "workbench.ensure",
    "run.redirect",
    METHOD_REDIRECT_STATUS,
    METHOD_RECOVER_CONVERSATION,
)

# The methods that change ASL state. Each one is refused until a handshake has
# matched the contract set against a snapshot digest the bridge still holds.
#
# ``run.redirect`` is here and its two companions are not, and the split is the
# point: submitting a correction appends to the redirect journal, while reading
# where a correction got to and reading which recovery a pause is waiting on
# both write nothing. A read that a stale client can still make is what lets an
# incompatible cockpit explain a stuck run instead of guessing at it.
MUTATION_METHODS: tuple[str, ...] = (
    "question.reply",
    "question.resolve",
    "run.resume",
    "run.stop_after_current",
    "run.abort",
    "run.redirect",
)

EVENT_RUNS_CHANGED = "runs.changed"
EVENT_OPERATION_UPDATED = "operation.updated"
EVENT_NAMES: tuple[str, ...] = (EVENT_RUNS_CHANGED, EVENT_OPERATION_UPDATED)

# Rich discovery is the intervention queue, not the plan list: healthy
# ``ready``, ``recent``, and ``complete`` plans stay in Plans and Workbench.
ACTIVE_PRIMARY_STATES: tuple[workbench.PrimaryState, ...] = (
    "paused-for-reply",
    "failed",
    "stale",
    "invalid",
    "verifying",
    "running",
)

Phase = str
PHASE_AWAITING_REPLY = "awaiting-reply"
PHASE_BY_STATE: dict[workbench.PrimaryState, Phase] = {
    "paused-for-reply": PHASE_AWAITING_REPLY,
    "failed": "failed",
    "stale": "stale",
    "invalid": "invalid",
    "verifying": "verifying",
    "running": "running",
}
PHASES: tuple[Phase, ...] = (
    PHASE_AWAITING_REPLY,
    "failed",
    "stale",
    "invalid",
    "verifying",
    "running",
)

CONTEXT_KIND_PAUSE = "pause"
CONTEXT_KIND_WAITING = "waiting"
CONTEXT_KINDS: tuple[str, ...] = (CONTEXT_KIND_PAUSE, CONTEXT_KIND_WAITING)

ERROR_MALFORMED_JSON = "malformed-json"
ERROR_INVALID_REQUEST = "invalid-request"
ERROR_UNSUPPORTED_PROTOCOL = "unsupported-protocol"
ERROR_UNKNOWN_METHOD = "unknown-method"
ERROR_FRAME_TOO_LARGE = "frame-too-large"
ERROR_INTERNAL = "internal-error"
ERROR_MUTATION_FAILED = "mutation-failed"
ERROR_WORKBENCH_UNAVAILABLE = "workbench-unavailable"

# Protocol 2 appends exactly two codes and removes none, so a client that
# understands the protocol-1 vocabulary still understands every refusal it
# shares with protocol 2.
#
# The two are deliberately different kinds of failure. A contract-set mismatch
# is a build-time disagreement: retrying cannot fix it, so it is reported
# non-retryable and the client is expected to degrade to read-only rather than
# loop. A stale snapshot is a race the client can and should resolve by
# refreshing and handshaking again.
ERROR_CONTRACT_SET_MISMATCH = "contract-set-mismatch"
ERROR_STALE_SNAPSHOT = "stale-snapshot"

ERROR_CODES: tuple[str, ...] = (
    ERROR_MALFORMED_JSON,
    ERROR_INVALID_REQUEST,
    ERROR_UNSUPPORTED_PROTOCOL,
    ERROR_UNKNOWN_METHOD,
    ERROR_FRAME_TOO_LARGE,
    ERROR_INTERNAL,
    ERROR_MUTATION_FAILED,
    ERROR_WORKBENCH_UNAVAILABLE,
    # Refusal reasons the service layer owns. They are listed here so a client
    # sees one error vocabulary rather than two.
    intervention.REASON_INVALID_PLAN,
    intervention.REASON_NO_ACTIVE_RUN,
    intervention.REASON_RUN_MISMATCH,
    intervention.REASON_NOT_PAUSED,
    intervention.REASON_NO_OPEN_QUESTION,
    intervention.REASON_QUESTION_MISMATCH,
    intervention.REASON_STALE_ACTION_TOKEN,
    intervention.REASON_ACTION_NOT_PERMITTED,
    intervention.REASON_INVALID_ACTION,
    intervention.REASON_EMPTY_TEXT,
    intervention.REASON_BODY_TOO_LARGE,
    intervention.REASON_MISSING_QUESTION_BODY,
    ERROR_CONTRACT_SET_MISMATCH,
    ERROR_STALE_SNAPSHOT,
)

OPERATION_ACCEPTED = "accepted"
OPERATION_CONSUMED = "consumed"
OPERATION_REJECTED = "rejected"
OPERATION_EXPIRED = "expired"
OPERATION_SUPERSEDED = "superseded"
OPERATION_STATUSES: tuple[str, ...] = (
    OPERATION_ACCEPTED,
    OPERATION_CONSUMED,
    OPERATION_REJECTED,
    OPERATION_EXPIRED,
    OPERATION_SUPERSEDED,
)
OPERATION_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        OPERATION_CONSUMED,
        OPERATION_REJECTED,
        OPERATION_EXPIRED,
        OPERATION_SUPERSEDED,
    }
)

# A receipt window, not a journal: it exists so a client that reconnects within
# a few minutes learns the fate of what it submitted, and it is deliberately
# small enough that a long-lived bridge cannot accumulate operator history.
#
# Redirect operations deliberately do not live here. Their receipt is the
# redirect journal, which survives a bridge restart, an eviction, and the TTL
# below; projecting them out of this cache instead would make "where did my
# correction go?" answerable only by the process that happened to take it.
MAX_OPERATIONS = 64
OPERATION_TTL_SECONDS = 900.0

# Redirect vocabularies, aliased rather than restated. Each one is owned by the
# module that produces it, so a value this protocol can carry is exactly a value
# ASL can produce — the alternative is a fourth list that drifts.
REDIRECT_STATUSES: tuple[str, ...] = redirect_recovery.OPERATION_STATUSES
REDIRECT_STAGES: tuple[str, ...] = redirect_cli.STAGES
REDIRECT_OUTCOMES: tuple[str, ...] = redirect_admission.OUTCOMES
OWNER_LIVENESS_VALUES: tuple[str, ...] = (
    redirect_recovery.OWNER_LIVE,
    redirect_recovery.OWNER_GONE,
    redirect_recovery.OWNER_UNKNOWN,
)

# Why a correction was refused or settled, from both sides of the same boundary:
# admission refuses before anything is persisted, the journal settles after, and
# `submitted` is the third answer — persisted, unsettled, and inside its TTL,
# which is neither. A client renders one vocabulary; ordered admission-first and
# deduplicated so the union is stable under any of the three lists growing.
REDIRECT_REASONS: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *redirect_admission.REFUSAL_REASONS,
            *redirect_store.SETTLEMENT_REASONS,
            redirect_store.STATE_SUBMITTED,
        )
    )
)

# How many durable redirect operations one projection may carry. The journal is
# append-only and unbounded, so the projection needs a ceiling or a long-lived
# plan eventually produces a frame no client can read. Aliased rather than
# restated: two surfaces with two windows would disagree about which
# corrections exist.
MAX_REDIRECT_OPERATIONS = control_rules.MAX_REDIRECT_OPERATIONS

# Deep-link focus keys the Workbench front end recognizes. A request may name
# any of them; the default lands on the panel the operator is about to act in.
WORKBENCH_FOCUS_REPLY = "reply"
WORKBENCH_FOCUS_VERIFICATION = "verification"
WORKBENCH_FOCUS_ACTIVE = "active"
WORKBENCH_FOCUS_EVENTS = "events"
WORKBENCH_FOCUS_VALUES: tuple[str, ...] = (
    WORKBENCH_FOCUS_REPLY,
    WORKBENCH_FOCUS_VERIFICATION,
    WORKBENCH_FOCUS_ACTIVE,
    WORKBENCH_FOCUS_EVENTS,
)

# Per-field bounds run first so a single corrupt plan cannot consume the whole
# frame budget, and the aggregate row bound runs second so a corrupt *corpus*
# cannot either. Both are needed: neither one implies the other.
MAX_TEXT_CHARS = 400
MAX_PATH_CHARS = 1024
MAX_ID_CHARS = 128
MAX_WARNINGS = 8
MAX_QUESTION_IDS = 16
MAX_ROWS = 200


class BridgeError(Exception):
    """A protocol-level failure carrying a stable, client-visible code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": _bounded_text(self.message),
            "retryable": self.retryable,
            "details": self.details,
        }


def _bounded_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _bounded_path(value: Any) -> str | None:
    if isinstance(value, Path):
        value = str(value)
    return _bounded_text(value, limit=MAX_PATH_CHARS)


def _bounded_id(value: Any) -> str | None:
    return _bounded_text(value, limit=MAX_ID_CHARS)


def _bounded_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bounded_strings(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    bounded = []
    for value in values:
        text = _bounded_text(value)
        if text is not None:
            bounded.append(text)
        if len(bounded) >= limit:
            break
    return bounded


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def row_id(plan_dir: str, run_id: str | None) -> str:
    """Return the stable identity of one projected run.

    A plan directory can be re-run, and a run id is absent before the first
    run, so neither alone identifies a row across polls. Hashing the pair
    avoids inventing a delimiter that a plan path could itself contain.
    """
    material = json.dumps([plan_dir, run_id], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _session_identifier(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("session_id", "session"):
        identifier = _bounded_id(payload.get(key))
        if identifier is not None:
            return identifier
    return None


def _question_projection(
    plan_dir: Path,
    runner_state: Mapping[str, Any],
    *,
    state_dir: str | Path | None,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Project body-free question metadata plus the unbounded open-question ids.

    Deliberately derived from persisted runner state and mailbox records rather
    than from ``qna.load_qna_context``: that loader reads the last-message
    artifact into memory, and the cheapest way to guarantee a body never
    reaches a snapshot is never to load one.

    The unbounded ids are returned alongside the projection because the row's
    action token must fingerprint exactly what the mutation path re-derives.
    Feeding it the display-bounded list instead would desynchronize every token
    on a plan holding more than ``MAX_QUESTION_IDS`` open questions.
    """
    waiting = runner_state.get("waiting_for_reply")
    waiting = waiting if isinstance(waiting, Mapping) else None

    unanswered: tuple[mailbox.MailboxMessage, ...] = ()
    mailbox_view: mailbox.MailboxView | None = None
    try:
        mailbox_view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
        run_id = runner_state.get("run_id")
        unanswered = tuple(
            message
            for message in mailbox_view.unanswered_questions
            if isinstance(run_id, str) and run_id and message.run_id == run_id
        )
    except OSError, ValueError, mailbox.MailboxError:
        unanswered = ()
    open_ids = tuple(
        message.id
        for message in unanswered
        if isinstance(message.id, str) and message.id
    )
    context_kind, selected_question_id = intervention.question_context(
        runner_state, open_ids
    )
    active_turn = runner_state.get("active_turn")
    if mailbox_view is not None and isinstance(active_turn, Mapping):
        run_id = runner_state.get("run_id")
        session_id = _session_identifier(active_turn)
        turn_index = _bounded_int(active_turn.get("turn_index"))
        if (
            isinstance(run_id, str)
            and run_id
            and session_id is not None
            and turn_index is not None
        ):
            active_wait = mailbox_view.active_worker_wait(
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
            )
            if active_wait is not None and active_wait.to in open_ids:
                # ``input_wait_active`` is a heartbeat observation.  A
                # connector poll can land between heartbeats even while the
                # worker remains blocked in ``asl ask --wait``.  The paired
                # mailbox wait is durable for exactly that blocked interval,
                # so it is the authoritative tie-breaker for the same live
                # run/session/turn.
                context_kind = CONTEXT_KIND_WAITING
                selected_question_id = active_wait.to
    if context_kind is None:
        return None, open_ids

    question_id = _bounded_id(selected_question_id)

    question_ids = [
        identifier
        for identifier in (_bounded_id(message.id) for message in unanswered)
        if identifier is not None
    ][:MAX_QUESTION_IDS]
    selected = next(
        (message for message in unanswered if message.id == question_id), None
    )
    question_id = _bounded_id(selected.id) if selected is not None else None

    waiting_status = _bounded_text(runner_state.get("waiting_for_reply_status"))
    if context_kind == CONTEXT_KIND_WAITING and waiting_status is None:
        waiting_status = state.WAITING_FOR_REPLY_STATUS_LIVE

    projection = {
        "context_kind": context_kind,
        "question_id": question_id,
        "unanswered_count": len(unanswered),
        "question_ids": question_ids,
        "asked_at": _bounded_text(selected.created_at)
        if selected is not None
        else None,
        "session_id": _bounded_id(selected.session_id)
        if selected is not None
        else None,
        "turn_index": _bounded_int(selected.turn_index)
        if selected is not None
        else None,
        "waiting_status": waiting_status,
        "hard_deadline": (
            _bounded_text(waiting.get("hard_deadline")) if waiting else None
        ),
        "unsupervised_ceiling_seconds": (
            _bounded_int(waiting.get("unsupervised_ceiling_seconds"))
            if waiting
            else None
        ),
        "last_lease_activity_at": (
            _bounded_text(waiting.get("last_lease_activity_at")) if waiting else None
        ),
    }
    return projection, open_ids


def _phase(
    primary_state: workbench.PrimaryState, question: Mapping[str, Any] | None
) -> Phase:
    if (
        primary_state == "running"
        and question is not None
        and question.get("context_kind") == CONTEXT_KIND_WAITING
    ):
        return PHASE_AWAITING_REPLY
    return PHASE_BY_STATE[primary_state]


def project_row(
    summary: workbench.WorkbenchSummary,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Project one active/actionable Workbench summary into a snapshot row.

    Free-form summary prose is never copied. ``concise_reason``, ``detail``,
    ``pause_message``, ``last_message_preview``, and ``searchable_text`` all
    quote the harness's own words on at least one classification branch, so the
    row's human text is drawn only from ASL's fixed activity vocabulary.

    The row also carries the actions this run currently permits and the token
    that proves the caller saw this exact state, so a client never has to guess
    which buttons are live and a mutation built from a stale row is refused
    rather than misapplied.
    """
    plan_dir = str(summary.plan_dir)
    lock_info = state.inspect_lock_info(summary.plan_dir, state_dir)
    state_path = (
        state.plan_state_dir(summary.plan_dir, state_dir) / events.STATE_FILE_NAME
    )
    runner_state = state._runner_state_payload(state_path, lock_info=lock_info)
    question, open_ids = _question_projection(
        summary.plan_dir, runner_state, state_dir=state_dir
    )
    facts = intervention.build_run_facts(
        summary.plan_dir,
        runner_state=runner_state,
        lock_active=bool(lock_info is not None and lock_info.active),
        unanswered_ids=open_ids,
        active_wait_question_id=(
            question.get("question_id")
            if question is not None
            and question.get("context_kind") == CONTEXT_KIND_WAITING
            else None
        ),
    )
    active = (
        runner_state.get("active_verification")
        if summary.primary_state == "verifying"
        else runner_state.get("active_turn")
    )
    session_id = _session_identifier(active) or _bounded_id(
        summary.next_pending_session_id
    )
    if question is not None and question["context_kind"] == CONTEXT_KIND_WAITING:
        session_id = question["session_id"]
    window_id = _bounded_int(runner_state.get("kitty_window_id"))

    return {
        "row_id": row_id(plan_dir, summary.run_id),
        "plan_dir": _bounded_path(plan_dir),
        "label": _bounded_text(summary.label),
        "run_id": _bounded_id(summary.run_id),
        "session_id": session_id,
        "primary_state": summary.primary_state,
        "urgency_rank": summary.urgency_rank,
        "urgency_group": summary.urgency_group,
        "recommended_action": summary.recommended_action,
        "activity_label": _bounded_text(summary.activity_label),
        "phase": _phase(summary.primary_state, question),
        "harness": _bounded_id(summary.harness),
        "workspace_root": _bounded_path(summary.workspace_root),
        "registry_digest": _bounded_id(summary.registry_digest),
        "repository_count": summary.repository_count,
        "window": {"kitty_window_id": window_id} if window_id is not None else None,
        "run_started_at": _bounded_text(runner_state.get("run_started_at")),
        "run_finished_at": _bounded_text(runner_state.get("run_finished_at")),
        "last_activity_at": _bounded_text(summary.last_activity_at),
        "last_activity_source": summary.last_activity_source,
        "pause_reason": _bounded_text(summary.pause_reason),
        "question": question,
        "actions": list(intervention.permitted_actions(facts)),
        "disabled_reasons": intervention.disabled_reasons(facts),
        "action_token": intervention.action_token(facts),
        "warnings": _bounded_strings(summary.warnings, limit=MAX_WARNINGS),
    }


def _row_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Order rows exactly as the Workbench queue orders its default view.

    Urgency tier first, freshest activity within a tier, then label; the plan
    directory breaks the remaining ties so the projection is total rather than
    merely stable under one particular input order.
    """
    activity = _epoch(row.get("last_activity_at"))
    plan_dir = row.get("plan_dir") or ""
    label = (row.get("label") or plan_dir).lower()
    return (
        row.get("urgency_rank", 0),
        activity is None,
        -(activity or 0.0),
        label,
        plan_dir,
    )


def _fits_frame(payload: Mapping[str, Any]) -> bool:
    return len(encode_frame(payload)) <= MAX_FRAME_BYTES


def _snapshot_selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Reserve bounded space for unanswered questions, then live work."""
    activity = _epoch(row.get("last_activity_at")) or _epoch(row.get("run_started_at"))
    question = row.get("question")
    waiting_for_reply = bool(
        isinstance(question, Mapping)
        and question.get("question_id")
        and question.get("unanswered_count")
    )
    return (
        not waiting_for_reply,
        row.get("primary_state") not in {"running", "verifying"},
        activity is None,
        -(activity or 0.0),
        *_row_sort_key(row),
    )


def build_runs_snapshot(
    *,
    state_dir: str | Path | None = None,
    plan_roots: Iterable[str | Path] = (),
    include_default_dirs: bool = True,
) -> dict[str, Any]:
    """Build the deterministic active/actionable projection of every plan."""
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    try:
        discovery = workbench.discover_plans(
            state_root=state_dir,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )
    except (workbench.WorkbenchError, OSError, ValueError) as error:
        discovery = workbench.DiscoveryResult(warnings=(f"discovery failed: {error}",))
    warnings.extend(discovery.warnings)

    for discovered in discovery.plans:
        try:
            summary = workbench.build_workbench_summary(
                discovered, state_dir=state_dir, reclaim=False
            )
            if summary.primary_state not in ACTIVE_PRIMARY_STATES:
                continue
            rows.append(project_row(summary, state_dir=state_dir))
        except (plans.PlanError, OSError, ValueError) as error:
            # A plan that vanishes or turns unreadable mid-scan is news, not a
            # reason to drop every other row in the queue.
            warnings.append(
                f"plan projection failed for {discovered.plan_dir}: {error}"
            )

    # Urgency ordering puts even undated invalid plans ahead of live work.
    # Select the bounded working set separately so a backlog cannot crowd out
    # questions awaiting a person or the live runs this client observes.
    rows.sort(key=_snapshot_selection_key)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    bounded_warnings = _bounded_strings(warnings, limit=MAX_WARNINGS)

    snapshot: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        # Carried on the snapshot as well as the hello so a cached or replayed
        # projection is self-describing: a client that persists rows can tell
        # whether they were produced by the contract set it speaks today.
        "contract_set_id": CONTRACT_SET_ID,
        "rows": rows,
        "row_count": len(rows),
        "warnings": bounded_warnings,
        "truncated": truncated,
    }
    # The cadence block is attached when the snapshot is served rather than
    # built, so the shrink loop budgets for it here or a full frame would grow
    # past the ceiling on its way out.
    probe = {
        "digest": "0" * 64,
        "projection": dict.fromkeys(PROJECTION_KEYS, 99999.999),
    }
    while snapshot["rows"] and not _fits_frame(snapshot | probe):
        snapshot["rows"] = snapshot["rows"][:-1]
        snapshot["row_count"] = len(snapshot["rows"])
        snapshot["truncated"] = True
    snapshot["rows"].sort(key=_row_sort_key)
    snapshot["digest"] = snapshot_digest(snapshot)
    return snapshot


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    """Digest the parts of a snapshot a client can observe changing.

    Excludes ``digest`` itself and the ``projection`` cadence block, which is
    measured rather than observed: including it would make every walk whose
    duration jittered by a millisecond look like a corpus change. What remains
    is wall-clock free, so an idle corpus produces one digest forever and
    therefore no events.
    """
    material = {
        key: value
        for key, value in snapshot.items()
        if key not in {"digest", "projection"}
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def encode_frame(payload: Mapping[str, Any]) -> bytes:
    """Encode one NDJSON frame, newline included.

    ``sort_keys`` makes the bytes a function of the payload alone, which is
    what lets a client compare frames and what makes the tests deterministic.
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def hello_payload() -> dict[str, Any]:
    """The server-first frame, carrying the identity both ends must already share.

    ``contract_set_id`` is the whole compatibility story: it is the digest of
    the canonical corpus that defines these frames, the lifecycle hook payload,
    and the projection, so one string pins every component version at once.
    Listing the component schema versions separately would let the two drift
    apart, and the frame is closed against unknown properties precisely so that
    cannot happen.
    """
    return {
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "asl_version": __version__,
        "contract_set_id": CONTRACT_SET_ID,
        "capabilities": list(CAPABILITIES),
        "events": list(EVENT_NAMES),
        "mutation_methods": list(MUTATION_METHODS),
        "handshake_required": True,
        "max_frame_bytes": MAX_FRAME_BYTES,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
    }


def _response(request_id: str | None, result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "response",
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": dict(result),
    }


def _error_response(request_id: str | None, error: BridgeError) -> dict[str, Any]:
    return {
        "type": "response",
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "ok": False,
        "error": error.payload(),
    }


@dataclass
class Operation:
    """One accepted mutation, described by identifiers rather than by content.

    A mutation is durable the moment ASL's own record lands, which is strictly
    earlier than the moment the runner acts on it. The operation is what lets a
    client say "submitted" immediately and "consumed" later without inventing a
    second source of truth: every terminal status below is settled by observing
    canonical ASL data, never by the bridge remembering what it intended.
    """

    operation_id: str
    method: str
    plan_dir: str
    row_id: str
    accepted_at: float
    status: str = OPERATION_ACCEPTED
    run_id: str | None = None
    question_id: str | None = None
    closure_id: str | None = None
    command_id: str | None = None
    detail: str | None = None
    #: How far into the plan's event log this operation has already looked for
    #: proof its reply was read. Set to the log's size *before* the mutation, so
    #: a record already there cannot be mistaken for this one's, and advanced by
    #: every look so a growing log is read once rather than from the top. Kept
    #: in memory rather than persisted: a mark is meaningless to the process
    #: that did not take it, and the durable journal reconciles by timestamp.
    witness_offset: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in OPERATION_TERMINAL_STATUSES

    def settle(self, status: str, detail: str | None = None) -> None:
        self.status = status
        self.detail = detail

    def payload(self) -> dict[str, Any]:
        return {
            "operation_id": _bounded_id(self.operation_id),
            "method": self.method,
            "status": self.status,
            "plan_dir": _bounded_path(self.plan_dir),
            "row_id": self.row_id,
            "run_id": _bounded_id(self.run_id),
            "question_id": _bounded_id(self.question_id),
            "closure_id": _bounded_id(self.closure_id),
            "command_id": _bounded_id(self.command_id),
            "detail": _bounded_text(self.detail),
        }


class WorkbenchLifecycle:
    """One lazily started, bridge-owned Workbench server.

    Started on demand because most bridge sessions never need a browser, bound
    to ``127.0.0.1`` on an ephemeral port so it is unreachable off the host and
    cannot collide with an operator's own ``asl workbench serve``, and reused
    across requests so repeated links do not accumulate listeners.
    """

    host = "127.0.0.1"

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        plan_roots: Sequence[str | Path] = (),
        include_default_dirs: bool = True,
        server_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.state_dir = state_dir
        self.plan_roots = tuple(plan_roots)
        self.include_default_dirs = include_default_dirs
        self._server_factory = server_factory
        self._lock = threading.Lock()
        self._server: Any | None = None
        self._thread: threading.Thread | None = None

    def _factory(self) -> Callable[..., Any]:
        if self._server_factory is not None:
            return self._server_factory
        # Imported here so a bridge that is never asked for a browser never
        # pays for the HTTP stack.
        from . import workbench_server

        return workbench_server.create_server

    def ensure(self) -> Any:
        """Return the running server, starting exactly one on first request."""
        with self._lock:
            if self._server is not None:
                return self._server
            try:
                server = self._factory()(
                    host=self.host,
                    port=0,
                    state_dir=self.state_dir,
                    plan_roots=self.plan_roots,
                    include_default_dirs=self.include_default_dirs,
                )
            except (OSError, ValueError) as error:
                raise BridgeError(
                    ERROR_WORKBENCH_UNAVAILABLE,
                    f"workbench server could not start: {error}",
                    retryable=True,
                ) from error
            thread = threading.Thread(
                target=lambda: server.serve_forever(
                    poll_interval=WORKBENCH_POLL_INTERVAL_SECONDS
                ),
                name="asl-bridge-workbench",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            return server

    def url(self, plan_dir: str, focus: str) -> str:
        """Return a tokenized deep link into one plan's detail view.

        The token rides in the query string because the fragment is never sent
        to the server, and the plan selection rides in the fragment because the
        Workbench front end routes on it.
        """
        server = self.ensure()
        query = urllib.parse.urlencode({"token": server.token})
        fragment = urllib.parse.urlencode({"dir": plan_dir, "focus": focus})
        return (
            f"http://{server.bound_host}:{server.bound_port}/?{query}#plan?{fragment}"
        )

    def stop(self) -> None:
        """Shut the server down with the bridge that started it."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)


class ProjectionPending(Exception):
    """Raised when no projection has been published yet.

    A cold bridge has nothing truthful to serve: an empty snapshot would claim
    an empty corpus and re-enable actions against rows it has never read. The
    caller turns this into a retryable refusal instead, which a client answers
    by asking again — and which, unlike waiting for the walk, leaves the single
    protocol loop free to frame every request behind it.
    """


class ProjectionWorker:
    """Builds the corpus projection off the protocol loop, on a fixed cadence.

    A projection walks every discovered plan, so its cost scales with the
    corpus: several hundred plans take seconds. Building it on the loop that
    frames requests meant the server spent most of its life inside a build
    rather than inside ``select``, and owing the next build one whole interval
    *after* the last one finished made the real start-to-start cadence the sum
    of the build and the interval. A ten-second walk therefore observed change
    every twenty seconds while the handshake advertised one.

    Here the build runs on its own thread and the next one is owed ``interval``
    after the last one *started*, so change observation begins on the declared
    cadence and only a corpus that cannot be walked within it slips — by the
    overrun, never by a second full build.
    """

    def __init__(
        self,
        build: Callable[[], dict[str, Any]],
        *,
        interval: float = POLL_INTERVAL_SECONDS,
        retry_interval: float = PROJECTION_RETRY_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._build = build
        self._interval = interval
        self._retry_interval = retry_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._published: dict[str, Any] | None = None
        self._published_at: float | None = None
        self._started_at: float | None = None
        self._build_seconds = 0.0
        self._observed_interval = 0.0
        self._failures = 0
        self._last_failure: str | None = None

    # ----------------------------------------------------------------
    # Lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            thread = threading.Thread(
                target=self._run, name="asl-bridge-projection", daemon=True
            )
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stopped.set()
        with self._settled:
            thread, self._thread = self._thread, None
            self._settled.notify_all()
        if thread is not None:
            thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)

    def _run(self) -> None:
        while not self._stopped.is_set():
            # A build that overruns its interval owes no wait at all, so the
            # cadence degrades to the corpus cost instead of to twice it.
            self._stopped.wait(self.cycle())

    def cycle(self) -> float:
        """Run one projection; return the seconds owed before the next start.

        Separate from :meth:`_run` so the cadence is testable without a thread
        and without real time.
        """
        started = self._clock()
        try:
            snapshot = self._build()
        except Exception as error:  # noqa: BLE001 - one bad walk must not end the loop
            self._record(started, failure=error)
            # An overrunning *successful* walk owes no wait because it published
            # something; an overrunning failed one published nothing, so
            # retrying it at once only spends the same cost again.
            return max(self._retry_interval, started + self._interval - self._clock())
        self._record(started, snapshot=snapshot)
        return max(0.0, started + self._interval - self._clock())

    def _record(
        self,
        started: float,
        *,
        snapshot: dict[str, Any] | None = None,
        failure: BaseException | None = None,
    ) -> None:
        finished = self._clock()
        with self._settled:
            if failure is not None:
                # Timing describes the projection being served, and a failed
                # walk serves nothing: the last successful one stays published,
                # so replacing its cost or its cadence here would describe one
                # walk with another walk's numbers.
                self._failures += 1
                self._last_failure = str(failure)
                self._settled.notify_all()
                return
            if self._started_at is not None:
                self._observed_interval = max(0.0, started - self._started_at)
            self._started_at = started
            self._build_seconds = max(0.0, finished - started)
            self._published = snapshot
            self._published_at = finished
            self._last_failure = None
            self._settled.notify_all()

    # ----------------------------------------------------------------
    # Observation

    def latest(self) -> dict[str, Any]:
        """Return the most recently published projection without building one.

        This never waits, which is the whole point: a request is answered from
        the last walk while the next one runs beside it. Before the first walk
        lands there is nothing truthful to serve, and waiting for it would block
        the single protocol loop — the cold snapshot and every frame behind it —
        for the whole corpus walk. The caller refuses retryably instead, so the
        client re-asks while the loop keeps framing.
        """
        with self._settled:
            if self._published is not None:
                return self._published
            raise ProjectionPending(
                self._last_failure or "the first corpus projection is still in flight"
            )

    def cadence(self) -> dict[str, Any]:
        """Describe how fresh the published projection can honestly claim to be."""
        with self._settled:
            age = 0.0
            if self._published_at is not None:
                age = max(0.0, self._clock() - self._published_at)
            return projection_cadence(
                interval=self._interval,
                observed=self._observed_interval,
                build_seconds=self._build_seconds,
                age=age,
                failures=self._failures,
            )


def projection_cadence(
    *,
    interval: float,
    observed: float,
    build_seconds: float,
    age: float,
    failures: int,
) -> dict[str, Any]:
    """The `projection` block a snapshot carries so a client can label staleness.

    ``interval_seconds`` is the declared floor, the same number the handshake
    advertises. Everything else is measured, because on a corpus the floor
    cannot buy, the declared interval is the one number a client must not
    believe: it is what made a twenty-second observation cadence look like a
    one-second one.
    """
    observed = max(interval, observed, build_seconds)
    return {
        "interval_seconds": _round_seconds(interval),
        "observed_interval_seconds": _round_seconds(observed),
        "build_seconds": _round_seconds(build_seconds),
        "age_seconds": _round_seconds(age),
        # Worst case for any row in this snapshot: read at the start of a walk
        # that took ``build_seconds``, aged ``age_seconds`` since that walk
        # landed, and not replaced for another whole observed interval.
        "staleness_bound_seconds": _round_seconds(build_seconds + age + observed),
        "failed_projections": failures,
    }


def _round_seconds(value: float) -> float:
    """Round to milliseconds so a duration cannot carry float noise on the wire."""
    return round(float(value), 3)


# Derived rather than written out, so the frame-budget probe below cannot
# describe a block the server does not actually send.
PROJECTION_KEYS: tuple[str, ...] = tuple(
    projection_cadence(
        interval=0.0, observed=0.0, build_seconds=0.0, age=0.0, failures=0
    )
)


# ---------------------------------------------------------------------------
# Redirect projection
#
# Everything below reads the redirect journal and derives the same stage
# `asl control redirect-status` prints. None of it reaches
# `redirect_store.read_feedback`, which is the only function that returns an
# operator's words, so a correction's text cannot enter a frame by accident:
# the bridge never holds it in the first place.
# ---------------------------------------------------------------------------


def _redirect_target(value: Any) -> dict[str, Any] | None:
    """Project one bound turn, or ``None`` where the journal recorded none."""
    if not isinstance(value, Mapping):
        return None
    return {
        "run_id": _bounded_id(value.get("run_id")),
        "session_id": _bounded_id(value.get("session_id")),
        "turn_index": _bounded_int(value.get("turn_index")),
        "harness": _bounded_id(value.get("harness")),
    }


def bound_redirect_operation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Bound one already-projected redirect operation into a frame field.

    Takes the projection every reader of this journal shares rather than the
    journal record, so the stage a bridge client renders and the stage an
    operator reads out of ``asl control redirect-status`` are the same
    derivation of the same facts rather than two implementations of one rule.
    Only the bounds and the closed key set below are the protocol's own.
    """
    return {
        "operation_id": _bounded_id(payload["operation_id"]),
        "status": payload["status"],
        "stage": payload["stage"],
        "stage_description": _bounded_text(payload["stage_description"]),
        "journal_state": payload["journal_state"],
        "reason": _bounded_text(payload["reason"]),
        "target": _redirect_target(payload["target"]),
        "continuation": _redirect_target(payload["continuation"]),
        "continuation_recovered": bool(payload["continuation_recovered"]),
        # Digest and byte count, never the words. This pair is what lets a
        # client prove *which* correction a receipt describes without the
        # protocol ever carrying one.
        "feedback_digest": _bounded_id(payload["feedback_digest"]),
        "feedback_bytes": _bounded_int(payload["feedback_bytes"]),
        "created_at": _bounded_text(payload["created_at"]),
        "expires_at": _bounded_text(payload["expires_at"]),
    }


def _project_redirect_operations(
    payloads: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Bound a window of already-projected operations, newest last.

    The window itself is the shared service's, truncated from the *front* when
    the journal outgrows it: the oldest corrections on a long-lived plan are the
    settled ones, and an operator asking where a correction got to is asking
    about a recent one.
    """
    return [bound_redirect_operation(payload) for payload in payloads]


def _conversation_evidence(
    evidence: redirect_recovery.ConversationEvidence,
) -> dict[str, Any]:
    """Project what the conversation ledger can still say about this plan.

    Carried by both redirect reads because the two recoveries can otherwise
    contradict each other: a ledger that cannot be trusted already forces a
    fresh-conversation pause, so a correction waiting on the turn that pause
    prevented has nowhere to land, and a client that saw only the redirect half
    would report a stuck correction as the problem rather than the symptom.
    """
    return {
        "readable": evidence.readable,
        "trustworthy": evidence.trustworthy,
        "usable": evidence.usable,
        "records": evidence.records,
        "unreadable_lines": evidence.unreadable_lines,
        "detail": _bounded_text(evidence.detail),
    }


def _bridge_error(error: intervention.InterventionError) -> BridgeError:
    """Re-raise a service refusal in the protocol's own error vocabulary."""
    return BridgeError(
        error.reason,
        error.message,
        retryable=error.retryable,
        details=error.details,
    )


def _control_details(details: Mapping[str, Any]) -> dict[str, Any]:
    """Bound a refusal's details to values a versioned frame can carry.

    Numbers travel as text because a client renders these rather than computing
    with them, and because the shared service states a bound once for every
    caller: a ceiling reads the same here as it does in a browser, where a raw
    float and a string of one would not.
    """
    bounded: dict[str, Any] = {}
    for key, value in details.items():
        if value is None or isinstance(value, bool):
            bounded[key] = value
        else:
            text = value if isinstance(value, str) else str(value)
            bounded[key] = _bounded_text(text)
    return bounded


def _control_error(error: workbench_controls.ControlRefused) -> BridgeError:
    """Re-raise a shared control refusal in the protocol's own vocabulary.

    The service refuses in terms of what a client should *do* -- rewrite the
    request, retry it, or reconcile an id it already holds -- and this is where
    that becomes one of the protocol's codes. The mapping is deliberately
    lossless in one direction only: a durable reason the closed enum has no name
    for travels in ``details.reason``, so a client keeps the specific answer
    without this surface widening a versioned contract to carry it.
    """
    if isinstance(error, workbench_controls.ControlQueueRefused):
        return BridgeError(
            ERROR_MUTATION_FAILED,
            f"control command could not be queued: {error.queue_error}",
            retryable=True,
        )
    if isinstance(error, workbench_controls.RedirectSubmitFailed):
        # Split on which layer refused rather than on whether the body was at
        # fault: "submitted" and "persisted" are this protocol's two sentences
        # for a correction that did not land, and admission is the only layer
        # that refuses one before the journal is ever opened.
        if error.source == control_rules.SOURCE_ADMISSION:
            return BridgeError(
                ERROR_INVALID_REQUEST,
                error.submitted_message,
                details={"reason": error.reason},
            )
        return BridgeError(
            ERROR_MUTATION_FAILED,
            error.persisted_message,
            retryable=True,
            details=(
                {"reason": error.reason}
                if error.source == control_rules.SOURCE_JOURNAL
                else {}
            ),
        )
    if error.scope == control_rules.SCOPE_UNAVAILABLE:
        # A fact about this machine rather than about the request: an unreadable
        # plan or an unreadable journal. Retryable, and never answered as though
        # the plan simply held nothing.
        return BridgeError(
            ERROR_INTERNAL,
            error.message,
            retryable=True,
            details=_control_details(error.details),
        )
    code = error.reason if error.reason in ERROR_CODES else ERROR_INVALID_REQUEST
    details = dict(error.details)
    if code != error.reason:
        details = {"reason": error.reason, **details}
    if error.plan_dir is not None:
        details = {"plan_dir": _bounded_path(error.plan_dir), **details}
    return BridgeError(
        code,
        error.message,
        retryable=error.retryable,
        details=_control_details(details),
    )


@dataclass
class BridgeSession:
    """Protocol state for one connected client.

    Split from the transport so the projection, the framing, and the error
    vocabulary are all testable without a subprocess or a real pipe.
    """

    snapshot_builder: Callable[[], dict[str, Any]]
    state_dir: str | Path | None = None
    workbench_lifecycle: WorkbenchLifecycle | None = None
    clock: Callable[[], float] = time.monotonic
    poll_interval: float = POLL_INTERVAL_SECONDS
    projection_retry_interval: float = PROJECTION_RETRY_INTERVAL_SECONDS
    projection_cadence: Callable[[], dict[str, Any]] | None = None
    seq: int = 0
    last_digest: str | None = None
    # The mutation gate. ``handshake_id`` records that a client proved it speaks
    # this contract set; ``handshake_digest`` records the snapshot that proof
    # was made against, and is cleared rather than advanced when the corpus
    # moves, so a handshake authorizes exactly the projection it was shown.
    handshake_id: str | None = None
    handshake_digest: str | None = None
    handshake_count: int = 0
    operations: OrderedDict[str, Operation] = field(
        default_factory=OrderedDict, repr=False
    )
    _last_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    _last_snapshot_at: float | None = field(default=None, repr=False)
    _last_build_seconds: float = field(default=0.0, repr=False)
    _retry_at: float | None = field(default=None, repr=False)

    def hello(self) -> dict[str, Any]:
        return hello_payload()

    def handle_frame(self, raw: bytes) -> dict[str, Any] | None:
        """Answer one request frame; never raise for client-caused failures."""
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return _error_response(
                None,
                BridgeError(ERROR_MALFORMED_JSON, f"frame is not valid JSON: {error}"),
            )
        if not isinstance(decoded, Mapping):
            return _error_response(
                None,
                BridgeError(ERROR_INVALID_REQUEST, "frame is not a JSON object"),
            )

        raw_id = decoded.get("id")
        request_id = raw_id if isinstance(raw_id, str) and raw_id else None
        try:
            return _response(request_id, self._dispatch(decoded, request_id))
        except BridgeError as error:
            return _error_response(request_id, error)

    def _dispatch(
        self, request: Mapping[str, Any], request_id: str | None
    ) -> dict[str, Any]:
        if request.get("type") != "request":
            raise BridgeError(
                ERROR_INVALID_REQUEST,
                "frame type must be 'request'",
                details={"type": _bounded_text(request.get("type"))},
            )
        protocol = request.get("protocol")
        if protocol != PROTOCOL_VERSION:
            raise BridgeError(
                ERROR_UNSUPPORTED_PROTOCOL,
                f"protocol {protocol!r} is not supported",
                details={"supported": str(PROTOCOL_VERSION)},
            )
        if request_id is None:
            raise BridgeError(
                ERROR_INVALID_REQUEST, "request id must be a nonempty string"
            )
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise BridgeError(
                ERROR_INVALID_REQUEST, "request method must be a nonempty string"
            )
        params = request.get("params", {})
        if not isinstance(params, Mapping):
            raise BridgeError(ERROR_INVALID_REQUEST, "request params must be an object")

        handler = METHOD_HANDLERS.get(method)
        if handler is None:
            raise BridgeError(
                ERROR_UNKNOWN_METHOD,
                f"unknown method {method!r}",
                details={"method": _bounded_text(method) or ""},
            )
        # Enforced here rather than in each handler so a method added to
        # MUTATION_METHODS is gated by construction instead of by remembering.
        if method in MUTATION_METHODS:
            self._require_mutations_enabled(method)
        return handler(self, params)

    def projection_interval(self) -> float:
        """How long a projection stays authoritative, and how often to poll.

        A projection is only worth repeating if it can finish appreciably
        sooner than it costs. Scaling the interval by the last build keeps the
        server under half duty on a corpus where one projection takes ten
        seconds, instead of starting the next one before the last has landed
        and never answering a request again.
        """
        return max(self.poll_interval, self._last_build_seconds)

    def next_projection_at(self) -> float:
        """When the next projection is owed, counted from the last one.

        Counting from the projection rather than from the poll is what stops a
        client request from being immediately followed by a poll that rebuilds
        the same corpus, which on an expensive one doubles the work and blocks
        the next request behind it.

        A failure owes its own deadline, because the deadline of the last
        *successful* projection is already in the past by then and would put
        the loop into a zero-timeout retry spin.
        """
        if self._retry_at is not None:
            return self._retry_at
        if self._last_snapshot_at is None:
            return self.clock() + self.poll_interval
        return self._last_snapshot_at + self.projection_interval()

    def select_timeout(self) -> float:
        """How long the protocol loop may wait for a frame before reprojecting."""
        return max(0.0, self.next_projection_at() - self.clock())

    def _project(self) -> dict[str, Any]:
        started = self.clock()
        try:
            snapshot = self.snapshot_builder()
        except BaseException:
            self._defer_projection()
            raise
        finished = self.clock()
        self._last_build_seconds = max(0.0, finished - started)
        self._last_snapshot = snapshot
        self._last_snapshot_at = finished
        self._retry_at = None
        return snapshot

    def _defer_projection(self) -> None:
        """Owe the next projection a bounded nonzero delay after a failure."""
        deadline = self.clock() + self.projection_retry_interval
        self._retry_at = (
            deadline if self._retry_at is None else max(self._retry_at, deadline)
        )

    def cadence(self) -> dict[str, Any]:
        """The cadence block served with a snapshot.

        Delegated to the projector when there is one, because only the thread
        that runs the walks knows what they actually cost; a session building
        inline answers from its own last walk instead.
        """
        if self.projection_cadence is not None:
            return self.projection_cadence()
        age = 0.0
        if self._last_snapshot_at is not None:
            age = max(0.0, self.clock() - self._last_snapshot_at)
        return projection_cadence(
            interval=self.poll_interval,
            observed=self.projection_interval(),
            build_seconds=self._last_build_seconds,
            age=age,
            failures=0,
        )

    def _current_snapshot(self) -> dict[str, Any]:
        """Return the projection this session would serve right now.

        Shared by ``runs.snapshot`` and by the mutation gate so both answer from
        one definition of "current": a handshake can never be validated against
        a projection the client was never eligible to be shown.
        """
        if self._last_snapshot is not None and self.clock() < self.next_projection_at():
            return self._last_snapshot
        try:
            snapshot = self._project()
        except ProjectionPending as error:
            # Refusing is what keeps a slow first walk from blocking the loop:
            # the client re-asks, and meanwhile every other method still answers.
            raise BridgeError(
                ERROR_INTERNAL,
                f"no projection is available yet: {error}",
                retryable=True,
            ) from error
        except Exception as error:  # noqa: BLE001 - one plan must not end the loop
            raise BridgeError(
                ERROR_INTERNAL,
                f"snapshot projection failed: {error}",
                retryable=True,
            ) from error
        self.last_digest = snapshot["digest"]
        return snapshot

    def runs_snapshot(self) -> dict[str, Any]:
        # The cadence block is attached per response rather than per walk: the
        # projection is shared and cached, but how stale it is by now is not.
        return self._current_snapshot() | {"projection": self.cadence()}

    # ----------------------------------------------------------------
    # Compatibility handshake

    def session_handshake(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Prove contract-set agreement and pin the snapshot mutations may act on.

        Deliberately not a negotiation. The bridge implements exactly one
        contract set, so a client naming another is told so once, non-retryably,
        and keeps its read-only access; there is nothing to fall back to.
        """
        offered_contract_set = _require_str(params, "contract_set_id")
        if offered_contract_set != CONTRACT_SET_ID:
            # The message is a fixed string rather than an interpolation of the
            # client's value: the reason a client renders must be stable across
            # runs, and echoing an unvalidated identifier back into a UI is how
            # a compatibility notice turns into an injection surface.
            raise BridgeError(
                ERROR_CONTRACT_SET_MISMATCH,
                "client contract set is not the one this bridge implements",
                retryable=False,
                details={"contract_set_id": CONTRACT_SET_ID},
            )
        offered_digest = _optional_str(params, "snapshot_digest")
        current = str(self._current_snapshot()["digest"])
        enabled = offered_digest == current
        self.handshake_count += 1
        self.handshake_id = (
            "hs-"
            + hashlib.sha256(f"{self.handshake_count}:{current}".encode()).hexdigest()[
                :8
            ]
        )
        self.handshake_digest = current if enabled else None
        return {
            "contract_set_id": CONTRACT_SET_ID,
            "handshake_id": self.handshake_id,
            "mutations_enabled": enabled,
            "snapshot_digest": current,
        }

    def _require_mutations_enabled(self, method: str) -> None:
        """Refuse a mutation that no live handshake authorizes.

        Complementary to the action token rather than redundant with it: the
        token proves the *plan* has not moved since the client read it, and this
        proves the client read it from a projection produced by the contract set
        the bridge implements and still holds.
        """
        if self.handshake_id is None:
            raise BridgeError(
                ERROR_CONTRACT_SET_MISMATCH,
                "mutations require a completed session.handshake",
                retryable=False,
                details={"contract_set_id": CONTRACT_SET_ID, "method": method},
            )
        current = str(self._current_snapshot()["digest"])
        if self.handshake_digest != current:
            raise BridgeError(
                ERROR_STALE_SNAPSHOT,
                "the handshake snapshot is no longer current; "
                "refresh the snapshot and handshake again",
                retryable=True,
                details={"snapshot_digest": current},
            )

    def poll(self) -> dict[str, Any] | None:
        """Return a ``runs.changed`` event, or ``None`` when nothing changed."""
        try:
            snapshot = self._project()
        except Exception:  # noqa: BLE001 - _project already owes the retry delay
            return None
        if snapshot["digest"] == self.last_digest:
            return None
        self.last_digest = snapshot["digest"]
        self.seq += 1
        return {
            "type": "event",
            "protocol": PROTOCOL_VERSION,
            "seq": self.seq,
            "event": EVENT_RUNS_CHANGED,
            "data": {
                "digest": snapshot["digest"],
                "row_count": snapshot["row_count"],
            },
        }

    # ----------------------------------------------------------------
    # Mutation plumbing

    def _facts(self, params: Mapping[str, Any]) -> intervention.RunFacts:
        """Re-read the plan now, so validation never trusts a client's copy."""
        plan_dir = _require_str(params, "plan_dir")
        try:
            return intervention.inspect_run(plan_dir, state_dir=self.state_dir)
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        except (OSError, ValueError) as error:
            raise BridgeError(
                ERROR_INTERNAL,
                f"plan state could not be read: {error}",
                retryable=True,
            ) from error

    def _authorize(
        self, params: Mapping[str, Any], action: str
    ) -> intervention.RunFacts:
        """Re-read and validate immediately before the caller mutates anything.

        Bridge callers are held to strict active-run and exact-run matching even
        where a CLI would accept the command for a runner that has not started
        yet: a supervision client acts on a row it was shown, so a request that
        no longer matches what it was shown is a bug to surface, not an
        instruction to queue.
        """
        facts = self._facts(params)
        if not facts.run_active and action != intervention.ACTION_RESUME:
            raise BridgeError(
                intervention.REASON_NO_ACTIVE_RUN,
                intervention.DISABLED_NO_ACTIVE_RUN,
                details={"plan_dir": _bounded_path(facts.plan_dir)},
            )
        try:
            intervention.validate_action(
                facts,
                action,
                token=params.get("action_token"),
                question_id=_optional_str(params, "question_id"),
                run_id=_optional_str(params, "run_id"),
            )
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        return facts

    @staticmethod
    def _selected_question(
        params: Mapping[str, Any], facts: intervention.RunFacts
    ) -> str:
        """Return the question id the caller explicitly authorized against.

        Read after ``_authorize`` from the same facts it validated, so the
        closure is appended against exactly the id the operator selected rather
        than against whichever question the runner state calls primary.
        """
        selected = intervention.selected_question_id(
            facts, _optional_str(params, "question_id")
        )
        assert selected is not None  # validate_action proved this
        return selected

    def _replay(self, params: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
        """Return the operation id and, when already accepted, its receipt.

        Replaying is what makes a client retry safe *before* ASL's own state has
        moved: once it has moved, the action token refuses the retry instead.
        """
        operation_id = _require_str(params, "operation_id")
        existing = self.operations.get(operation_id)
        if existing is not None:
            return operation_id, existing.payload()
        return operation_id, None

    def _accept(
        self,
        operation_id: str,
        method: str,
        facts: intervention.RunFacts,
        *,
        question_id: str | None = None,
        closure_id: str | None = None,
        command_id: str | None = None,
        witness_offset: int = 0,
    ) -> dict[str, Any]:
        operation = Operation(
            operation_id=operation_id,
            method=method,
            plan_dir=str(facts.plan_dir),
            row_id=row_id(str(facts.plan_dir), facts.run_id),
            accepted_at=self.clock(),
            run_id=facts.run_id,
            question_id=question_id,
            closure_id=closure_id,
            command_id=command_id,
            witness_offset=witness_offset,
        )
        self.operations[operation_id] = operation
        self._evict()
        return operation.payload()

    def _events_mark(self, plan_dir: Path) -> int:
        """Where this plan's event log ends right now, in bytes.

        Taken *before* a reply is delivered, because everything already in the
        log answers something else. A log that cannot be measured marks zero
        and is read from the top, where the ``since``-free scan's own filters --
        origin, question id -- are all that stand between an old record and a
        wrong ``consumed``. That is why the mark is taken early rather than
        defended later.
        """
        try:
            return (
                workbench_operations.events_path_for_plan(plan_dir, self.state_dir)
                .stat()
                .st_size
            )
        except OSError:
            return 0

    def _perform(
        self,
        facts: intervention.RunFacts,
        params: Mapping[str, Any],
        *,
        action: str,
        operation_id: str,
        text: str,
        question_id: str | None = None,
    ) -> workbench_actions.ActionOutcome:
        """Run one mutation through the shared action, in bridge vocabulary.

        The bridge's own receipt table is not dropped in favour of the journal:
        the two answer different questions. ``self.operations`` is what this
        session tells *this* client about work it is watching; the journal is
        what keeps a retry that outlived this session -- a reconnect to another
        process, a supervisor resending after a lost response -- from becoming a
        second spawn or a second answer.
        """
        try:
            return workbench_actions.perform(
                facts.plan_dir,
                action=action,
                operation_id=operation_id,
                state_dir=self.state_dir,
                reply_text=text,
                action_token=_optional_str(params, "action_token"),
                question_id=question_id,
                run_id=_optional_str(params, "run_id"),
            )
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        except workbench.LaunchError as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"{action} could not be started: {error.message}",
                details={"reason": error.reason},
            ) from error
        except operation_rules.OperationError as error:
            # The journal's refusals are finer-grained than the protocol's
            # error codes, which are pinned by the contract set. The reason
            # rides in `details` rather than being minted as a new code a
            # connector would not recognise.
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                error.message,
                details={"reason": error.reason, **error.details},
            ) from error
        except workbench_actions.ActionRefused as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                error.message,
                details={"reason": error.reason},
            ) from error
        except (OSError, ValueError, mailbox.MailboxError) as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"{action} could not be completed: {error}",
                retryable=True,
            ) from error

    def _expire(self) -> list[Operation]:
        """Settle operations whose outcome never arrived within the window.

        Separate from eviction so an expiry is always something the client is
        told about, rather than a record that quietly disappears.
        """
        now = self.clock()
        expired = [
            operation
            for operation in self.operations.values()
            if not operation.terminal
            and now - operation.accepted_at > OPERATION_TTL_SECONDS
        ]
        for operation in expired:
            operation.settle(
                OPERATION_EXPIRED, "no outcome observed before the receipt window"
            )
        return expired

    def _evict(self) -> None:
        """Keep the receipt window bounded without ever forgetting live work.

        Count-based eviction only ever drops settled operations; anything still
        awaiting an outcome leaves the window through the TTL alone, so a burst
        of requests cannot evict an in-flight resume and let it spawn twice.
        """
        while len(self.operations) > MAX_OPERATIONS:
            evictable = next(
                (
                    key
                    for key, operation in self.operations.items()
                    if operation.terminal
                ),
                None,
            )
            if evictable is None:
                return
            del self.operations[evictable]

    # ----------------------------------------------------------------
    # Methods

    def question_get(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return the body of one selected question, live or synthetic."""
        facts = self._facts(params)
        try:
            question_id, context_kind, body = intervention.question_body(
                facts.plan_dir,
                question_id=_optional_str(params, "question_id"),
                state_dir=self.state_dir,
            )
            body_bytes = intervention.enforce_body_bound(body)
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            "question_id": _bounded_id(question_id),
            "context_kind": context_kind,
            "body": body,
            "body_bytes": body_bytes,
            "action_token": intervention.action_token(facts),
        }

    def question_reply(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer the live question, returning a receipt rather than the text."""
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        facts = self._authorize(params, intervention.ACTION_REPLY)
        text = params.get("text")
        question_id = self._selected_question(params, facts)
        mark = self._events_mark(facts.plan_dir)
        outcome = self._perform(
            facts,
            params,
            action=action_rules.ACTION_REPLY,
            operation_id=operation_id,
            text=text if isinstance(text, str) else "",
            question_id=question_id,
        )
        return self._accept(
            operation_id,
            intervention.ACTION_REPLY,
            facts,
            question_id=question_id,
            closure_id=outcome.closure_id,
            witness_offset=mark,
        )

    def question_resolve(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Close the live question answerlessly, continuing or pausing the run."""
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        facts = self._authorize(params, intervention.ACTION_RESOLVE)
        assert facts.run_id is not None
        question_id = self._selected_question(params, facts)
        try:
            outcome = intervention.append_question_resolution(
                facts.plan_dir,
                question_id,
                _require_str(params, "action"),
                expected_run_id=facts.run_id,
                note=_optional_str(params, "note"),
                state_dir=self.state_dir,
            )
        except intervention.InterventionError as error:
            raise _bridge_error(error) from error
        except (OSError, ValueError, mailbox.MailboxError) as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"resolution could not be appended: {error}",
                retryable=True,
            ) from error
        return self._accept(
            operation_id,
            intervention.ACTION_RESOLVE,
            facts,
            question_id=question_id,
            closure_id=outcome.closure_id,
        )

    def _control(
        self, params: Mapping[str, Any], kind: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Queue one control through the service the Workbench also goes through.

        Only the ledger around it is this protocol's: replay out of
        ``self.operations``, and the acceptance row a client settles against.
        The authorization, the read, the append, and the wakeup are the shared
        ones, held to ``AUTHORIZATION_STRICT`` because a supervision client acts
        on a row it was shown.
        """
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        try:
            outcome = workbench_controls.dispatch_control(
                _require_str(params, "plan_dir"),
                kind,
                state_dir=self.state_dir,
                command_id=operation_id,
                authorization=control_rules.AUTHORIZATION_STRICT,
                action_token=params.get("action_token"),
                run_id=_optional_str(params, "run_id"),
                **kwargs,
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        except (OSError, ValueError) as error:
            raise BridgeError(
                ERROR_MUTATION_FAILED,
                f"control command could not be queued: {error}",
                retryable=True,
            ) from error
        facts = outcome.activity.facts
        assert facts is not None  # a strict dispatch refuses an unreadable plan
        action = control_rules.asl_action_for(kind)
        assert action is not None  # and refuses a control with no action
        # The operation id *is* the command id, so the runner's own delivery
        # receipt settles this operation without any bridge-side bookkeeping.
        return self._accept(operation_id, action, facts, command_id=operation_id)

    def run_stop_after_current(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the runner to finish the current session and then stop."""
        return self._control(params, control_rules.CONTROL_STOP_AFTER_CURRENT)

    def run_abort(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Ask the runner to abort the in-flight turn under ASL's default grace."""
        return self._control(
            params,
            control_rules.CONTROL_ABORT,
            grace_seconds=control_queue.DEFAULT_ABORT_GRACE_SECONDS,
        )

    def run_resume(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Spawn ``asl run --resume`` for a paused plan, streaming the reply."""
        operation_id, replay = self._replay(params)
        if replay is not None:
            return replay
        # Authorization is the last read before the spawn, so the fingerprint a
        # resume is checked against is the one the child will actually find.
        facts = self._authorize(params, intervention.ACTION_RESUME)
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise BridgeError(
                intervention.REASON_EMPTY_TEXT,
                "reply text must contain non-whitespace content",
            )
        mark = self._events_mark(facts.plan_dir)
        self._perform(
            facts,
            params,
            action=action_rules.ACTION_RESUME,
            operation_id=operation_id,
            text=text,
            question_id=facts.question_id,
        )
        return self._accept(
            operation_id,
            intervention.ACTION_RESUME,
            facts,
            question_id=facts.question_id,
            witness_offset=mark,
        )

    def workbench_ensure(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return a tokenized Workbench deep link for one plan."""
        if self.workbench_lifecycle is None:
            raise BridgeError(
                ERROR_WORKBENCH_UNAVAILABLE,
                "this bridge session cannot host a workbench server",
            )
        facts = self._facts(params)
        focus = _optional_str(params, "focus")
        if focus is None:
            focus = (
                WORKBENCH_FOCUS_REPLY
                if facts.context_kind is not None
                else WORKBENCH_FOCUS_ACTIVE
            )
        elif focus not in WORKBENCH_FOCUS_VALUES:
            raise BridgeError(
                ERROR_INVALID_REQUEST,
                f"unsupported workbench focus {focus!r}",
                details={"supported": list(WORKBENCH_FOCUS_VALUES)},
            )
        url = self.workbench_lifecycle.url(str(facts.plan_dir), focus)
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "focus": focus,
            "url": url,
        }

    # ----------------------------------------------------------------
    # Redirect

    def _redirect_wait_seconds(self, params: Mapping[str, Any]) -> float:
        """Bound how long one request may hold the protocol loop.

        The bridge answers one frame at a time, so a wait a client chooses is a
        wait every other client on this session pays. Both the default and the
        ceiling come from the shared service, which takes them from admission
        rather than restating them: they are the same promise measured from the
        other end.
        """
        try:
            return workbench_controls.wait_seconds(
                params.get("wait_seconds"), label="params.wait_seconds"
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error

    @staticmethod
    def _redirect_payload(
        submission: workbench_controls.RedirectSubmission,
    ) -> dict[str, Any]:
        """Answer a redirect request from the journal rather than from memory.

        Every field here is read back out of the durable record by the shared
        service, so the status a client is told and the status the next reader
        of this journal computes are one projection of the same bytes. What
        stays the protocol's own is the naming and the bounds.
        """
        facts = submission.facts
        operation = submission.operation_payload()
        return {
            # Null exactly when nothing was persisted. Echoing the client's own
            # id for a refusal that never reached the journal would invite it to
            # ask after a correction that does not exist.
            "operation_id": _bounded_id(submission.operation_id),
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            # Null on a replay: this call submitted nothing, so it has no
            # submission outcome to report and `operation` carries the truth.
            "outcome": submission.outcome,
            "reason": submission.reason,
            "target": _redirect_target(submission.target),
            "waited_seconds": _round_seconds(submission.waited_seconds),
            "replayed": submission.replayed,
            "operation": (
                bound_redirect_operation(operation) if operation is not None else None
            ),
            "journal_complete": submission.journal_complete,
        }

    def run_redirect(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Submit one urgent correction against the turn this run is on now.

        The submission itself is the one both surfaces make. Replay is served
        out of the redirect journal before the freshness proof is checked, and
        that ordering is the whole reason this method can be retried: a
        correction that landed moves the plan, which moves the action token,
        which would make the retry of a request whose answer was lost in transit
        look like a stale second correction. Asking the journal first makes the
        id the client already holds the answer.

        What is *not* short-circuited is the request itself. ``text`` and any
        ``expected_target`` are compared against the record that id already
        names, because a retry is only a retry when it carries the same
        correction. Reporting different words as a successful replay would tell
        a client its second correction landed when the worker was handed the
        first.

        Held to ``AUTHORIZATION_STRICT``, which is what distinguishes this
        caller from a local operator: the plan is re-read immediately before the
        append, so the fingerprint the correction is checked against is the one
        the journal is about to be written under.
        """
        try:
            submission = workbench_controls.submit_correction(
                _require_str(params, "plan_dir"),
                operation_id=_require_str(params, "operation_id"),
                text=params.get("text"),
                state_dir=self.state_dir,
                expected_target_raw=params.get("expected_target"),
                wait_seconds_raw=params.get("wait_seconds"),
                authorization=control_rules.AUTHORIZATION_STRICT,
                action_token=params.get("action_token"),
                run_id=_optional_str(params, "run_id"),
                target_label="params.expected_target",
                wait_label="params.wait_seconds",
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        return self._redirect_payload(submission)

    def run_redirect_status(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Project this plan's durable redirect state, writing nothing.

        A read, and available without a handshake, because the client most in
        need of it is the one that just lost its own record of what it
        submitted — including a client whose contract set no longer matches.
        """
        wanted = _optional_id_list(params, "operation_ids")
        try:
            view = workbench_controls.status_view(
                _require_str(params, "plan_dir"),
                state_dir=self.state_dir,
                operation_ids=tuple(wanted),
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        facts = view.facts
        report = view.report
        live_target = view.live_target
        operations = _project_redirect_operations(view.operation_payloads())
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            "journal_path": _bounded_path(report.journal_path),
            "owner": report.owner.liveness,
            "owner_run_id": _bounded_id(report.owner.run_id),
            "live_target": _redirect_target(
                live_target.to_payload() if live_target is not None else None
            ),
            "operations": operations,
            "uncertain_operation_ids": [
                operation["operation_id"]
                for operation in operations
                if operation["stage"] == redirect_cli.STAGE_UNCERTAIN
            ][:MAX_REDIRECT_OPERATIONS],
            # `complete` is the honesty flag, and `truncated` is its projection
            # counterpart: a torn journal means a record may be missing from the
            # source, and a full window means one is missing from this frame.
            "journal_complete": report.complete,
            "torn_tail": report.torn_tail,
            "unreadable_lines": report.unreadable_lines,
            "truncated": view.truncated,
            "checked_at": _bounded_text(report.checked_at),
            "conversations": _conversation_evidence(report.conversations),
            "action_token": intervention.action_token(facts),
        }

    def run_recover_conversation(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Report which recovery a pause is waiting on, and how to authorize it.

        A read, and deliberately only a read. Performing the recovery here would
        persist the operator's one-use authorization in the one place it must
        never live: a request that can be replayed.

        The pause-to-decision mapping and both commands are derived from
        :mod:`.intervention` and :mod:`.redirect_cli` rather than re-assembled,
        so this projection, a recovery card in the Workbench, and ``asl control
        recover-conversation`` cannot hand an operator three different answers
        about the same pause.
        """
        try:
            view = workbench_controls.recovery_view(
                _require_str(params, "plan_dir"), state_dir=self.state_dir
            )
        except workbench_controls.ControlRefused as error:
            raise _control_error(error) from error
        facts = view.facts
        classified = view.decision
        authorization = _bounded_text(view.authorization_command)
        report = view.report
        return {
            "plan_dir": _bounded_path(facts.plan_dir),
            "row_id": row_id(str(facts.plan_dir), facts.run_id),
            "run_id": _bounded_id(facts.run_id),
            "recovery_required": classified.required,
            # The pause reason is reported whether or not it maps to a decision.
            # "paused, and no manual recovery is what it is waiting on" is an
            # answer; an unexplained null is a second question.
            "pause_reason": _bounded_text(classified.pause_reason),
            "decision": classified.decision,
            "auto_resume": classified.auto_resume,
            "authorized_by": classified.authorized_by,
            "inspect_command": _bounded_text(view.inspect_command),
            "authorization_command": authorization,
            # Both describe the authorization command, so both are false and
            # null where there is none. `predecessor-not-stopped` has no
            # authorization at all, and telling an operator its (absent)
            # authorization is one-use would read as an invitation to find it.
            "one_use": view.one_use,
            "note": _bounded_text(view.note),
            "conversations": _conversation_evidence(report.conversations),
            # The corrections a fresh start would still carry. Reported here so
            # an operator authorizing one can see what it inherits rather than
            # discovering it in the successor's first prompt.
            "retained_operations": _project_redirect_operations(
                view.retained_payloads()
            ),
        }

    # ----------------------------------------------------------------
    # Operation outcomes

    def poll_operations(self) -> list[dict[str, Any]]:
        """Settle accepted operations against ASL's own records.

        Every outcome below is read back out of the corpus — a mailbox receipt,
        a run-control delivery event, a paused flag — so a client learns what
        actually happened rather than what the bridge hoped would.
        """
        settled: list[Operation] = self._expire()
        for operation in list(self.operations.values()):
            if operation.terminal:
                continue
            try:
                self._settle(operation)
            except OSError, ValueError:
                continue
            if operation.terminal:
                settled.append(operation)
        self._evict()
        return [self._operation_event(operation) for operation in settled]

    def _settle(self, operation: Operation) -> None:
        if operation.terminal:
            return
        plan_dir = Path(operation.plan_dir)
        if operation.closure_id is not None:
            receipt = intervention.closure_receipt(
                plan_dir, operation.closure_id, state_dir=self.state_dir
            )
            if receipt is not None:
                operation.settle(OPERATION_CONSUMED, "runner recorded a receipt")
            return
        if operation.command_id is not None:
            if intervention.command_observed(
                plan_dir, operation.command_id, state_dir=self.state_dir
            ):
                operation.settle(OPERATION_CONSUMED, "runner observed the command")
                return
            queued = operation.command_id in intervention.pending_command_ids(
                plan_dir, state_dir=self.state_dir
            )
            if queued:
                return
            # Drained without a receipt is only conclusive once the run is gone:
            # otherwise the runner has read the line and not yet logged it.
            if not intervention.inspect_run(
                plan_dir, state_dir=self.state_dir
            ).run_active:
                operation.settle(
                    OPERATION_REJECTED, "run ended before the command was observed"
                )
            return
        if operation.method == intervention.ACTION_RESUME:
            self._settle_resume(operation, plan_dir)

    def _settle_resume(self, operation: Operation, plan_dir: Path) -> None:
        """Settle a resume on the reply being read, not on the pause lifting.

        This used to read "the plan is no longer paused, therefore the reply was
        consumed". That is a fact about the plan and not about the operation:
        any run starting on it lifts the pause -- one an operator launched from
        the Workbench a second earlier, a retry, a scheduled run -- so the rule
        credited this reply with whatever happened next. The Companion grew its
        own resume witness precisely because of that answer, and this is that
        witness, moved here so both surfaces have it.

        What settles it now is run-bound: a run appended ``reply_provided``
        naming ``stdin`` as the origin after this operation took its mark. What
        does *not* settle it is the pause lifting, which now leaves the
        operation ``accepted`` until the witness or the TTL says otherwise --
        "nobody can say" rather than a claim.
        """
        evidence = self._witness(operation)
        if evidence.consumed:
            # Deliberately not required to match `operation.run_id`: a resume
            # starts a *new* process with a new run id, so the paused run's id
            # is the one id the consuming run cannot have.
            operation.settle(OPERATION_CONSUMED, "a run recorded reading the reply")
            return
        facts = intervention.inspect_run(plan_dir, state_dir=self.state_dir)
        if facts.paused and facts.run_id != operation.run_id:
            operation.settle(
                OPERATION_SUPERSEDED, "a different run now holds this plan"
            )

    def _witness(self, operation: Operation) -> action_rules.ConsumptionEvidence:
        """Look once at the plan's event log for proof this reply was read.

        Bounded and forward-only: each look reads at most one chunk from the
        mark and advances the mark by the whole records it read, so following a
        busy run costs the same on the hundredth poll as on the first and a
        record still being written is never half-parsed.
        """
        origin = action_rules.consuming_origin(
            action_rules.METHOD_FOR_ACTION[action_rules.ACTION_RESUME]
        )
        assert origin is not None  # a resume bears a reply
        path = workbench_operations.events_path_for_plan(
            Path(operation.plan_dir), self.state_dir
        )
        try:
            with path.open("rb") as handle:
                handle.seek(operation.witness_offset)
                chunk = handle.read(action_rules.MAX_WITNESS_BYTES_PER_LOOK)
        except OSError:
            return action_rules.ConsumptionEvidence(readable=False)
        evidence, stride = action_rules.scan_replies(chunk, origin=origin)
        operation.witness_offset += stride
        return evidence

    def _operation_event(self, operation: Operation) -> dict[str, Any]:
        self.seq += 1
        return {
            "type": "event",
            "protocol": PROTOCOL_VERSION,
            "seq": self.seq,
            "event": EVENT_OPERATION_UPDATED,
            "data": operation.payload(),
        }


def _require_str(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise BridgeError(
            ERROR_INVALID_REQUEST, f"params.{key} must be a nonempty string"
        )
    return value


def _optional_str(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BridgeError(
            ERROR_INVALID_REQUEST, f"params.{key} must be a nonempty string when set"
        )
    return value


def _optional_id_list(params: Mapping[str, Any], key: str) -> frozenset[str]:
    """Read an optional filter of operation ids, bounded like every other list.

    An empty result means *no filter*, not *nothing matched*, which is why an
    explicitly empty array is refused rather than silently read as "all": a
    client that filtered its way down to zero ids and got the whole journal back
    would be told the opposite of what it asked.
    """
    value = params.get(key)
    if value is None:
        return frozenset()
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not value
        or len(value) > MAX_REDIRECT_OPERATIONS
    ):
        raise BridgeError(
            ERROR_INVALID_REQUEST,
            f"params.{key} must be a nonempty array of at most "
            f"{MAX_REDIRECT_OPERATIONS} strings when set",
        )
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_ID_CHARS:
            raise BridgeError(
                ERROR_INVALID_REQUEST,
                f"params.{key} entries must be nonempty strings of at most "
                f"{MAX_ID_CHARS} characters",
            )
    return frozenset(value)


# Registered last so the derived capability list below can only advertise names
# that already have a handler behind them.
METHOD_HANDLERS: dict[
    str, Callable[[BridgeSession, Mapping[str, Any]], dict[str, Any]]
] = {
    METHOD_HANDSHAKE: BridgeSession.session_handshake,
    "runs.snapshot": lambda session, params: session.runs_snapshot(),
    "question.get": BridgeSession.question_get,
    intervention.ACTION_REPLY: BridgeSession.question_reply,
    intervention.ACTION_RESOLVE: BridgeSession.question_resolve,
    intervention.ACTION_RESUME: BridgeSession.run_resume,
    intervention.ACTION_STOP_AFTER_CURRENT: BridgeSession.run_stop_after_current,
    intervention.ACTION_ABORT: BridgeSession.run_abort,
    intervention.ACTION_WORKBENCH: BridgeSession.workbench_ensure,
    intervention.ACTION_REDIRECT: BridgeSession.run_redirect,
    METHOD_REDIRECT_STATUS: BridgeSession.run_redirect_status,
    METHOD_RECOVER_CONVERSATION: BridgeSession.run_recover_conversation,
}

CAPABILITIES: tuple[str, ...] = tuple(
    name for name in _METHOD_ORDER if name in METHOD_HANDLERS
)


class _FrameTooLarge(Exception):
    """One inbound line exceeded the frame ceiling and has been drained."""


def read_frame(reader: BinaryIO, *, limit: int = MAX_FRAME_BYTES) -> bytes | None:
    """Read one bounded NDJSON frame, or ``None`` at end of stream.

    Reads ``limit + 2`` so a frame of exactly ``limit`` payload bytes plus its
    newline is accepted and anything longer is provably over budget without
    ever buffering it.
    """
    chunk = reader.readline(limit + 2)
    if not chunk:
        return None
    if len(chunk) > limit + 1:
        while chunk and not chunk.endswith(b"\n"):
            chunk = reader.readline(limit + 2)
        raise _FrameTooLarge
    return chunk.rstrip(b"\r\n")


def serve_stdio(
    *,
    reader: BinaryIO,
    writer: TextIO,
    diagnostics: TextIO,
    state_dir: str | Path | None = None,
    plan_roots: Sequence[str | Path] = (),
    include_default_dirs: bool = True,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the bridge server loop until the client closes the stream."""

    def build() -> dict[str, Any]:
        return build_runs_snapshot(
            state_dir=state_dir,
            plan_roots=plan_roots,
            include_default_dirs=include_default_dirs,
        )

    lifecycle = WorkbenchLifecycle(
        state_dir=state_dir,
        plan_roots=plan_roots,
        include_default_dirs=include_default_dirs,
    )
    worker = ProjectionWorker(build, interval=poll_interval, clock=clock)
    session = BridgeSession(
        # The session reads the published projection instead of walking the
        # corpus itself, so neither a snapshot response nor the next frame ever
        # waits on a build.
        snapshot_builder=worker.latest,
        state_dir=state_dir,
        workbench_lifecycle=lifecycle,
        clock=clock,
        poll_interval=poll_interval,
        projection_cadence=worker.cadence,
    )

    def emit(payload: Mapping[str, Any]) -> bool:
        frame = encode_frame(payload)
        if len(frame) > MAX_FRAME_BYTES:
            print(
                f"agent-session-loop: dropping oversize {payload.get('type')} frame "
                f"({len(frame)} bytes)",
                file=diagnostics,
                flush=True,
            )
            return True
        try:
            writer.write(frame.decode("utf-8"))
            writer.flush()
        except BrokenPipeError:
            return False
        except OSError as error:
            if error.errno in {errno.EPIPE, errno.ESHUTDOWN}:
                return False
            raise
        return True

    def loop() -> int:
        if not emit(session.hello()):
            return 0
        worker.start()

        while True:
            timeout = session.select_timeout()
            try:
                ready, _, _ = select.select([reader], [], [], timeout)
            except OSError, ValueError:
                return 0
            if ready:
                try:
                    raw = read_frame(reader)
                except _FrameTooLarge:
                    if not emit(
                        _error_response(
                            None,
                            BridgeError(
                                ERROR_FRAME_TOO_LARGE,
                                "request frame exceeded the protocol frame ceiling",
                                details={"max_frame_bytes": str(MAX_FRAME_BYTES)},
                            ),
                        )
                    ):
                        return 0
                    continue
                except OSError:
                    return 0
                if raw is None:
                    return 0
                if raw:
                    response = session.handle_frame(raw)
                    if response is not None and not emit(response):
                        return 0
            if clock() >= session.next_projection_at():
                event = session.poll()
                if event is not None and not emit(event):
                    return 0
                for update in session.poll_operations():
                    if not emit(update):
                        return 0

    try:
        return loop()
    finally:
        # Both background threads belong to this process, so they die with it
        # rather than outliving the client they were started for.
        worker.stop()
        lifecycle.stop()


def bridge_serve_from_args(args: Any) -> int:
    """Serve protocol 2 on stdio for ``asl bridge serve``.

    There is no protocol-1 path left to select. A client pinned to the retired
    version fails here, before a byte of protocol is written, rather than
    discovering the mismatch halfway through a session.
    """
    requested = getattr(args, "protocol", PROTOCOL_VERSION)
    if requested != PROTOCOL_VERSION:
        print(
            f"agent-session-loop: unsupported bridge protocol {requested}; "
            f"this build speaks protocol {PROTOCOL_VERSION} "
            f"(contract set {CONTRACT_SET_ID})",
            file=sys.stderr,
        )
        return 2
    return serve_stdio(
        reader=sys.stdin.buffer,
        writer=sys.stdout,
        diagnostics=sys.stderr,
        state_dir=getattr(args, "state_dir", None),
        plan_roots=tuple(getattr(args, "plan_root", None) or ()),
    )
