"""The remote provider: the Pi answering the shared routes from what it kept.

:mod:`asl_workbench.http` owns the routes, the roles, the framing rules, and
the envelope. This module owns one of the two things a mode is allowed to
differ in -- execution -- and it executes very differently from the Mac,
because it is not standing on the plans it is describing:

* **A retained observation answers what it can.** Health, the summary, the
  queue, and one plan's row come out of :class:`~asl_workbench.store.
  WorkbenchStore`, which holds what a connector published and nothing else.
  Each of those answers carries the freshness of the observation it was built
  from, because an answer about a run that is a minute old is a different
  answer from one about a run as it is now, and a caller that cannot tell them
  apart will act on the wrong one.

* **A detail read is a bounded round trip.** The twelve artifact routes have no
  local file to open, so they ask the Mac through
  :class:`~asl_workbench.remote_reads.DetailReadClient` and report what came
  back, including the four ways nothing came back at all. Each outcome gets its
  own status: an offline link, a pairing that never granted ``detail``, a read
  that timed out, and a read ASL itself refused are four different problems
  with four different fixes, and folding them into one 503 would hide which.

* **A mutation is journalled here and carried out there.** Admission is
  :func:`~asl_workbench.operations.admit`, the same gate a local click passes,
  so a resubmitted operation id is one operation on both sides. What goes down
  the link is validated with :func:`~asl_workbench.remote_actions.
  parse_submission` -- the far end's own parser -- before the row is written,
  so a submission this service would journal is one the connector will accept.
  The answer is ``202``: the operator is told the mutation was admitted, never
  that it happened.

* **Everything else says so, in advance.** The registry, the archive, and the
  redirect journal live on the Mac and are not published, so
  :meth:`RemoteProvider.unavailable` names each one with the reason a frontend
  can show. ``GET /api/capabilities`` reports the same sentences before a
  control is ever clicked.

Prose never lands here. A reply and a correction arrive as a ``reply_ref`` and
the single-use token that redeems it, exactly as
:mod:`asl_workbench.remote_actions` requires, and the body is fetched on the
Mac at the moment of use. That is the one place the remote request shape
differs from the local one, and it differs because the alternative is storing
an operator's words on a machine that has no business holding them.

Like the rest of the package this module reads no clock and touches no socket:
the moment is an argument and the two lanes are protocols the caller supplies.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from . import overview
from .freshness import aggregate_completeness, aggregate_freshness
from .http import (
    COMMAND_NAME,
    MODE_REMOTE,
    OP_ARCHIVE_DELETE,
    OP_ARCHIVE_LIST,
    OP_ARCHIVE_RESTORE,
    OP_ARCHIVE_SETTINGS,
    OP_ARTIFACTS,
    OP_COMMITS,
    OP_CONTROL_ABORT,
    OP_CONTROL_REDIRECT,
    OP_CONTROL_SELECT_HARNESS,
    OP_CONTROL_STOP_AFTER_CURRENT,
    OP_CONVERSATION_RECOVERY,
    OP_EVENTS,
    OP_FINAL_REVIEW,
    OP_HEALTH,
    OP_HISTORY,
    OP_LAST_MESSAGE,
    OP_LAUNCH,
    OP_LOGS,
    OP_PLAN,
    OP_PLAN_ARCHIVE,
    OP_PLANS_ARCHIVE,
    OP_PROGRESS,
    OP_QUESTIONS,
    OP_QUEUE,
    OP_RECOVERY,
    OP_REDIRECT_STATUS,
    OP_REGISTRY,
    OP_REGISTRY_PIN,
    OP_REGISTRY_UNPIN,
    OP_REPLY,
    OP_RESULT,
    OP_STATE,
    OP_SUMMARY,
    OP_UNLOCK,
    OP_VERIFICATION,
    Call,
    HttpRefusal,
    Response,
    error_payload,
    invalid_request,
    unavailable_from,
)
from .observation import COMPLETENESS_COMPLETE
from .operations import (
    MAX_OPERATION_ID_CHARS,
    METHOD_ABORT,
    METHOD_QUESTION_REPLY,
    METHOD_REDIRECT,
    METHOD_REPLY,
    METHOD_SELECT_HARNESS,
    METHOD_STOP_AFTER_CURRENT,
    METHOD_UNLOCK,
    OperationError,
    OperationTarget,
    admit,
    export_operation,
    method_scope,
    operation_fingerprint,
    require_method,
)
from .remote_actions import (
    MAX_DELIVERY_WINDOW_SECONDS,
    MutationRequest,
    RemoteActionError,
    parse_submission,
)
from .remote_reads import (
    OUTCOME_ANSWERED,
    OUTCOME_CANCELLED,
    OUTCOME_OFFLINE,
    OUTCOME_REFUSED,
    OUTCOME_TIMEOUT,
    OUTCOME_UNAVAILABLE,
    READ_OUTCOMES,
    ReadAnswer,
)
from .store import DEFAULT_READ_LIMIT, MAX_READ_LIMIT, WorkbenchStore
from .transport import MAX_DETAIL_BYTES, MAX_DETAIL_ENTRIES, REQUEST_MUTATE

__all__ = [
    "DETAIL_OPERATIONS",
    "MUTATION_METHODS",
    "READ_STATUSES",
    "UNAVAILABLE_REASONS",
    "DetailLane",
    "RemoteProvider",
    "SubmissionLane",
]


# ---------------------------------------------------------------------------
# What this mode can and cannot do
# ---------------------------------------------------------------------------

#: The routes that become a bounded read on the Mac, and the detail kind each
#: one asks for. Keyed by operation rather than by path so the mapping survives
#: a route being renamed, and valued with
#: :data:`~asl_workbench.details.DETAIL_KINDS` words so the request that goes
#: out is one the far end already understands.
DETAIL_OPERATIONS: Mapping[str, str] = {
    OP_ARTIFACTS: "artifacts",
    OP_STATE: "state",
    OP_EVENTS: "events",
    OP_LOGS: "logs",
    OP_LAST_MESSAGE: "last-message",
    OP_RESULT: "result",
    OP_VERIFICATION: "verification",
    OP_COMMITS: "commits",
    OP_PROGRESS: "progress",
    OP_FINAL_REVIEW: "final-review",
    OP_QUESTIONS: "questions",
    OP_RECOVERY: "recovery",
}

#: The mutations this mode will admit, and the method each submits. Every one
#: of them is in :data:`~asl_workbench.remote_actions.DELIVERABLE_METHODS`;
#: :func:`~asl_workbench.remote_actions.parse_submission` re-checks that on the
#: composed payload, so a method added here that the lane will not carry is
#: refused at submission rather than accepted and then dropped.
#:
#: Every one of them also names a run. That is not a coincidence and not a
#: preference: the service journal's ``run_key`` column is ``NOT NULL``, which
#: is the Companion's submission contract made durable, so an operation with no
#: run to name has nowhere to be recorded. The two deliverable methods that
#: name a plan instead -- launching and archiving -- are declared unavailable in
#: :data:`UNAVAILABLE_REASONS` rather than journalled against an invented run.
MUTATION_METHODS: Mapping[str, str] = {
    OP_REPLY: METHOD_REPLY,
    OP_CONTROL_STOP_AFTER_CURRENT: METHOD_STOP_AFTER_CURRENT,
    OP_CONTROL_ABORT: METHOD_ABORT,
    OP_CONTROL_SELECT_HARNESS: METHOD_SELECT_HARNESS,
    OP_CONTROL_REDIRECT: METHOD_REDIRECT,
    OP_UNLOCK: METHOD_UNLOCK,
}

#: The methods whose fingerprint is narrowed by something other than the plan.
#: Named per method rather than guessed from whichever selector happens to be
#: present: reusing an operation id against a different question is a conflict,
#: and it can only be recognised as one if both submissions narrow the same way.
#:
#: Only ``question_id`` appears, and the journal's columns are why. A
#: resubmission is compared against the row that was written, so a fingerprint
#: narrowed on a field the row does not keep could never be recomputed from it,
#: and every retry of such a method would read as a conflicting reuse. The
#: journal keeps ``question_id``; it keeps no harness and no session id. So
#: those methods narrow by plan, and reusing one id for two harness choices is
#: read as the retry it almost always is rather than as a refusal nobody can
#: act on.
_SELECTORS: Mapping[str, str] = {
    METHOD_REPLY: "question_id",
    METHOD_QUESTION_REPLY: "question_id",
}

#: Why each operation the Pi cannot carry out is one it cannot carry out.
#: Operator-facing, because ``GET /api/capabilities`` shows these to whoever is
#: looking at a greyed-out control.
UNAVAILABLE_REASONS: Mapping[str, str] = {
    OP_LAUNCH: (
        "starting a run is done on the Mac: a launch names a plan rather than "
        "a run, and the service journal records mutations against a run"
    ),
    OP_PLAN_ARCHIVE: (
        "archiving moves the plan's own directory, which only the Mac holds"
    ),
    OP_REGISTRY: (
        "the registry is the Mac's own file and is not published over the link"
    ),
    OP_REGISTRY_PIN: (
        "pinning edits the Mac's registry, which this service never holds"
    ),
    OP_REGISTRY_UNPIN: (
        "unpinning edits the Mac's registry, which this service never holds"
    ),
    OP_PLANS_ARCHIVE: (
        "bulk archiving is not carried over the link; archive one plan at a time"
    ),
    OP_ARCHIVE_LIST: "the archive lives on the Mac and is not published",
    OP_ARCHIVE_RESTORE: "restoring reads the Mac's archive directly",
    OP_ARCHIVE_DELETE: "purging the archive is done where the archive is",
    OP_ARCHIVE_SETTINGS: (
        "retention is a registry setting, and the registry stays on the Mac"
    ),
    OP_REDIRECT_STATUS: (
        "a correction's progress is reported through the operation journal, "
        "not through the Mac's redirect route"
    ),
    OP_CONVERSATION_RECOVERY: (
        "recovering a conversation needs the harness session files, which stay "
        "on the machine that wrote them"
    ),
}

_DEFAULT_UNAVAILABLE = "the remote provider does not carry out this operation"

#: The status and reason each read outcome answers with. Four of the six are
#: failures and each keeps its own code, because "the link is down", "this
#: pairing may not read details", "the Mac took too long", and "the Mac said
#: no" call for four different things from whoever is reading.
READ_STATUSES: Mapping[str, tuple[int, str]] = {
    OUTCOME_ANSWERED: (200, "ok"),
    OUTCOME_REFUSED: (409, "detail-refused"),
    OUTCOME_OFFLINE: (503, "link-offline"),
    OUTCOME_UNAVAILABLE: (501, "detail-unavailable"),
    OUTCOME_TIMEOUT: (504, "detail-timeout"),
    OUTCOME_CANCELLED: (503, "detail-cancelled"),
}

_UNMAPPED_OUTCOMES = tuple(
    sorted(outcome for outcome in READ_OUTCOMES if outcome not in READ_STATUSES)
)
if _UNMAPPED_OUTCOMES:  # pragma: no cover - a registration mistake, not a state
    raise RuntimeError(
        "asl_workbench.service has no status for read outcomes: "
        + ", ".join(_UNMAPPED_OUTCOMES)
    )


# ---------------------------------------------------------------------------
# The two lanes this provider borrows
# ---------------------------------------------------------------------------


class DetailLane(Protocol):
    """Asking the Mac to read one artifact, and waiting for the answer.

    The waiting is the caller's, not this module's: a service loop holds the
    channel and knows when a frame arrived, and putting a wait in here would
    put a socket in a package that deliberately has none.
    """

    def fetch(
        self,
        *,
        detail: str,
        plan_key: str,
        run_key: str | None = None,
        artifact: str | None = None,
        stream: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        max_bytes: int | None = None,
        now: float,
    ) -> ReadAnswer:
        """The answer, material or not. Never raises for an ordinary refusal."""


class SubmissionLane(Protocol):
    """Handing one validated ``operation.submit`` down to the connector."""

    def hand_down(self, payload: Mapping[str, Any], *, now: float) -> None:
        """Queue the submission for the next acknowledgement frame."""


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class RemoteProvider:
    """Execution for :data:`~asl_workbench.http.MODE_REMOTE`.

    Built with the store it reads and, optionally, the two lanes it needs to
    reach the Mac. A lane left out is not a broken provider: the operations
    that would have used it become unavailable with a reason, which is the
    honest state of a service whose connector has not paired yet.
    """

    mode = MODE_REMOTE

    def __init__(
        self,
        store: WorkbenchStore,
        *,
        clock: Callable[[], float],
        connector_id: str,
        reads: DetailLane | None = None,
        mutations: SubmissionLane | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._connector_id = connector_id
        self._reads = reads
        self._mutations = mutations

    # -- What it can do -----------------------------------------------------

    def unavailable(self, operation: str) -> str | None:
        supported = set(_STORE_OPERATIONS)
        if self._reads is not None:
            supported.update(DETAIL_OPERATIONS)
        if self._mutations is not None:
            supported.update(MUTATION_METHODS)
        reasons = dict(UNAVAILABLE_REASONS)
        if self._reads is None:
            for operation_name in DETAIL_OPERATIONS:
                reasons.setdefault(operation_name, _NO_READ_LANE)
        if self._mutations is None:
            for operation_name in MUTATION_METHODS:
                reasons.setdefault(operation_name, _NO_SUBMISSION_LANE)
        return unavailable_from(
            supported, reasons=reasons, default=_DEFAULT_UNAVAILABLE
        )(operation)

    # -- Dispatch -----------------------------------------------------------

    def invoke(self, call: Call) -> Response:
        operation = call.operation
        if operation in DETAIL_OPERATIONS:
            return self._detail(call, DETAIL_OPERATIONS[operation])
        if operation in MUTATION_METHODS:
            return self._submit(call, MUTATION_METHODS[operation])
        handler = _STORE_OPERATIONS.get(operation)
        if handler is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(
                503,
                "operation-unavailable",
                _DEFAULT_UNAVAILABLE,
                details={"operation": operation, "mode": self.mode},
            )
        return handler(self, call)

    # -- Retained observations ----------------------------------------------

    def _handle_health(self, call: Call) -> Response:
        """Liveness, and nothing a stranger could not learn by watching the port.

        This is the one public route, so the answer names the service's own
        state and never the state of anything it has observed.
        """
        del call
        healthy = self._store.healthy()
        return Response.success(
            200 if healthy else 503,
            reason="ok" if healthy else "unavailable",
            status="ok" if healthy else "unavailable",
        )

    def _handle_summary(self, call: Call) -> Response:
        """Counts and freshness, in the shape the local mode answers with.

        The numbers come from a different place than the Mac's do -- retained
        observations rather than a filesystem scan -- and the *shape* is
        deliberately the same, because a summary pane should not have to know
        which mode it is pointed at to read the answer.
        """
        del call
        now = self._clock()
        counts = self._store.counts(now=now)
        statuses = self._store.connector_statuses(now=now)
        by_state: dict[str, int] = {}
        for row in self._store.runs(now=now, limit=MAX_READ_LIMIT, include_absent=True):
            state = str(row.get("primary_state") or "unknown")
            by_state[state] = by_state.get(state, 0) + 1
        completeness = aggregate_completeness(
            [status.completeness for status in statuses]
        )
        observed = [
            status.last_snapshot_at
            for status in statuses
            if status.last_snapshot_at is not None
        ]
        generations = [
            status.source_generation
            for status in statuses
            if status.source_generation is not None
        ]
        return Response.success(
            reason="ok",
            counts={"plans": counts.plans, "by_state": by_state},
            freshness={
                "generation": max(generations) if generations else None,
                "complete": completeness == COMPLETENESS_COMPLETE,
                "total": counts.plans,
                # Unlike the Mac, this answer is about something that was
                # observed rather than something being looked at, so staleness
                # is a fact it has to carry rather than one it can rule out.
                "stale": not statuses or any(status.degraded for status in statuses),
                "observed_at": max(observed) if observed else None,
            },
        )

    def _handle_queue(self, call: Call) -> Response:
        """The retained catalog, one page, newest generation first.

        The rows are catalog entries rather than the Mac's plan summaries, and
        they address a plan by ``plan_key``: this service has never seen a
        filesystem path and publishing one would be publishing a guess. The
        freshness fields alongside them are what tells a reader how much of the
        catalog this page is a part of.
        """
        now = self._clock()
        filters = overview.parse_filters(call, default_limit=DEFAULT_READ_LIMIT)
        connector_id = call.optional_query("connector_id")
        include_absent = call.optional_query("include_absent") == "1"
        narrowing: dict[str, Any] = {
            "connector_id": connector_id,
            "project": filters.project,
            "search": filters.search,
            "harness": filters.harness,
            "include_absent": include_absent,
        }
        entries = self._store.catalog(
            now=now,
            states=filters.states,
            offset=filters.offset,
            limit=filters.limit,
            **narrowing,
        )
        # Three reads of one narrowing, and the order is the contract: the
        # page is cut from the filtered set, the total counts that same set,
        # and the chip histogram counts it with the chip left out. None of the
        # three is derived from ``entries``, because a page cannot say how
        # large the set it came from was.
        matched = self._store.catalog_matched(states=filters.states, **narrowing)
        counts = overview.counts_from_states(self._store.catalog_states(**narrowing))
        statuses = self._store.connector_statuses(now=now)
        totals = self._store.counts(now=now)
        catalog_completeness = aggregate_completeness(
            [status.catalog.completeness for status in statuses]
        )
        return Response.success(
            reason="ok",
            plans=entries,
            observed_at=now,
            freshness=aggregate_freshness(
                [status.freshness.label for status in statuses]
            ),
            # Two answers to one question, because they are asked by different
            # readers: ``catalog_complete`` is the yes/no this route has always
            # published, and ``catalog_completeness`` is the word that
            # distinguishes "this observation covered part of the catalog" from
            # "this observation did not say". A banner cannot write the second
            # sentence from a boolean.
            catalog_complete=(catalog_completeness == COMPLETENESS_COMPLETE),
            catalog_completeness=catalog_completeness,
            catalog_matched=matched,
            catalog_total=totals.plans,
            counts=counts,
            filters=filters.as_document(),
            truncated=filters.offset + len(entries) < matched,
            projects=self._store.catalog_projects(include_absent=include_absent),
            harnesses=self._store.catalog_harnesses(include_absent=include_absent),
            connectors=[status.as_document() for status in statuses],
            incidents=self._store.incident_diagnostics(),
            # Two fields the local mode answers with that have no remote
            # meaning, reported as absent rather than omitted: a frontend
            # reading them should see "there is no such thing here", not a
            # missing key it has to guess about.
            registry_warnings=[],
            state_root=None,
        )

    def _handle_history(self, call: Call) -> Response:
        """Runs that ended inside the retention window, newest first.

        The Companion's history pane, answered from the endings the connector
        published. It is narrowed by the same words the queue is, so switching
        a project filter does not silently leave the history describing a
        different project than the plan list above it.
        """
        now = self._clock()
        filters = overview.parse_filters(call, default_limit=DEFAULT_READ_LIMIT)
        entries = self._store.history(
            now=now,
            project=filters.project,
            search=filters.search,
            harness=filters.harness,
            outcome=call.optional_query("outcome"),
            limit=filters.limit,
        )
        statuses = self._store.connector_statuses(now=now)
        return Response.success(
            reason="ok",
            entries=entries,
            observed_at=now,
            # History on the Pi is a retained import, so it carries the same
            # completeness the observation it arrived with did. The Mac reads
            # its own event files and answers ``complete`` from the window it
            # actually walked; the field means the same thing in both.
            complete=(
                aggregate_completeness([status.completeness for status in statuses])
                == COMPLETENESS_COMPLETE
            ),
            incidents=self._store.incident_diagnostics(),
            warnings=[],
        )

    def _handle_plan(self, call: Call) -> Response:
        """One retained catalog entry and the runs observed against it."""
        now = self._clock()
        plan_key = _plan_key(call)
        entries = [
            entry
            for entry in self._store.catalog(
                now=now, include_absent=True, limit=MAX_READ_LIMIT
            )
            if str(entry.get("plan_key")) == plan_key
        ]
        if not entries:
            raise HttpRefusal(
                404,
                "not-found",
                "no observation of this plan has been retained",
                details={"plan_key": plan_key},
            )
        # Narrowed by the store rather than here: `plan_key` is a storage
        # column the cockpit export deliberately drops, so a row that has
        # reached this scope no longer says which plan it belongs to and any
        # filter written over it would match nothing.
        runs = self._store.runs(
            now=now, plan_key=plan_key, limit=MAX_READ_LIMIT, include_absent=True
        )
        return Response.success(
            reason="ok",
            plan=entries[0],
            runs=runs,
            observed_at=now,
            outstanding_operation=self._store.outstanding_operation(plan_key),
        )

    # -- Bounded reads over the link ----------------------------------------

    def _detail(self, call: Call, detail: str) -> Response:
        """Ask the Mac for one artifact window and report what came back.

        The bounds are the shared ones: a caller cannot ask the Pi for a larger
        page than the Mac would have served locally, because the ceilings are
        :mod:`asl_workbench.transport`'s and both ends read them from there.
        """
        lane = self._reads
        if lane is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(503, "operation-unavailable", _NO_READ_LANE)
        now = self._clock()
        answer = lane.fetch(
            detail=detail,
            plan_key=_plan_key(call),
            run_key=call.optional_query("run_key"),
            artifact=call.optional_query("artifact"),
            stream=call.optional_query("stream"),
            offset=call.optional_int_query("offset"),
            limit=(
                None
                if call.optional_query("limit") is None
                else call.bounded_int(
                    "limit", default=MAX_DETAIL_ENTRIES, maximum=MAX_DETAIL_ENTRIES
                )
            ),
            max_bytes=(
                None
                if call.optional_query("max_bytes") is None
                else call.bounded_int(
                    "max_bytes", default=MAX_DETAIL_BYTES, maximum=MAX_DETAIL_BYTES
                )
            ),
            now=now,
        )
        return _read_response(answer)

    # -- Mutations ----------------------------------------------------------

    def _submit(self, call: Call, method: str) -> Response:
        """Admit one mutation, journal it, and hand it down. Never carry it out.

        Three steps in an order that matters. The payload is validated by the
        far end's own parser first, so nothing is journalled that the connector
        would then refuse. Admission runs second, against the journal, so a
        resubmitted operation id is the first submission's row rather than a
        second mutation. The hand-down is last, because a command that left
        before its row was written is a command whose existence a restart
        cannot discover.
        """
        lane = self._mutations
        if lane is None:  # pragma: no cover - `unavailable` refused it first
            raise HttpRefusal(503, "operation-unavailable", _NO_SUBMISSION_LANE)
        now = self._clock()
        request = self._parse(call, method, now=now)
        target = OperationTarget(
            plan_key=request.plan_key, selector=_selector(request, method)
        )
        recorded = [
            _rehydrate(record)
            for record in (
                self._store.operation(request.operation_id),
                self._store.outstanding_operation(request.plan_key),
            )
            if record is not None
        ]
        admission = admit(
            recorded,
            operation_id=request.operation_id,
            method=method,
            target=target,
            now=now,
            deliver_by=request.deliver_by,
            command_id=request.command_id,
            run_key=request.run_key,
            question_id=request.question_id,
        )
        if admission.refused:
            raise admission.error()
        if admission.replayed:
            # The first submission's own row, handed back untouched. Handing it
            # down again would be a second delivery of one command.
            record = admission.record or {}
            return _accepted(record, replay=True)
        record = self._store.record_operation(
            admission.record or {}, connector_id=self._connector_id, received_at=now
        )
        lane.hand_down(_submission(request, method), now=now)
        return _accepted(record, replay=False)

    def _parse(self, call: Call, method: str, *, now: float) -> MutationRequest:
        """Compose the submission and let the far end's parser judge it.

        Validating with :func:`~asl_workbench.remote_actions.parse_submission`
        rather than with a second set of checks here is the point: whatever the
        connector will refuse on arrival is refused now, while the operator is
        still looking at the answer.
        """
        operation_id = call.require_string("operation_id")
        if len(operation_id) > MAX_OPERATION_ID_CHARS:
            raise invalid_request(
                f"field 'operation_id' exceeds {MAX_OPERATION_ID_CHARS} characters"
            )
        # `run_key` is optional to the far end's parser and required here. The
        # journal column is `NOT NULL`, so a submission without one would be
        # admitted, handed down, and then fail at the insert -- a mutation on
        # its way out with no durable record that it left. Refusing it names the
        # missing field while the operator can still supply it.
        target: dict[str, Any] = {
            "plan_key": call.require_string("plan_key"),
            "run_key": call.require_string("run_key"),
        }
        for name in ("run_id", "question_id", "session_id"):
            value = call.optional_string(name)
            if value is not None:
                target[name] = value
        payload: dict[str, Any] = {
            "request": REQUEST_MUTATE,
            "method": method,
            "operation_id": operation_id,
            "target": target,
        }
        for name in ("reply_ref", "action_token", "bridge_token", "harness", "reason"):
            value = call.optional_string(name)
            if value is not None:
                payload[name] = value
        try:
            return parse_submission(
                payload, now=now, window=MAX_DELIVERY_WINDOW_SECONDS
            )
        except RemoteActionError as error:
            raise HttpRefusal(
                400,
                "invalid-submission",
                error.message,
                details={"detail_code": error.detail_code, "method": method},
            ) from error


#: The operations answered out of the retained store alone. Held as a table for
#: the same reason the local provider holds one: a route with no entry here is
#: unavailable rather than a dispatch that fails at request time.
_STORE_OPERATIONS: Mapping[str, Callable[[RemoteProvider, Call], Response]] = {
    OP_HEALTH: RemoteProvider._handle_health,
    OP_SUMMARY: RemoteProvider._handle_summary,
    OP_QUEUE: RemoteProvider._handle_queue,
    OP_HISTORY: RemoteProvider._handle_history,
    OP_PLAN: RemoteProvider._handle_plan,
}

_NO_READ_LANE = "no connector is paired, so no artifact can be read right now"
_NO_SUBMISSION_LANE = (
    "no connector is paired, so there is nothing to carry a mutation out"
)


# ---------------------------------------------------------------------------
# Shared shaping
# ---------------------------------------------------------------------------


def _plan_key(call: Call) -> str:
    """The plan a remote request is about, refusing a path it cannot resolve.

    ``plan_dir`` is how the local surface addresses a plan and it is refused
    here rather than ignored: this service has never seen the Mac's filesystem,
    so a path it accepted would be a path it could only guess at.
    """
    if call.optional_query("plan_key") is None and call.optional_query("plan_dir"):
        raise invalid_request(
            "the remote surface addresses plans by 'plan_key'; 'plan_dir' names "
            "a path only the Mac can resolve"
        )
    return call.require_query("plan_key")


def _read_response(answer: ReadAnswer) -> Response:
    """One read answer as an envelope, material or not.

    An unanswered read is an error envelope rather than an empty success. "The
    Mac never answered" and "the artifact is empty" are different facts, and a
    panel that renders them the same way tells an operator a run produced
    nothing when the truth is that nobody asked it.
    """
    status, reason = READ_STATUSES[answer.outcome]
    if answer.answered:
        return Response.success(
            status,
            reason=reason,
            detail=answer.detail,
            plan_key=answer.plan_key,
            run_key=answer.run_key,
            content=answer.content,
            entries=[dict(entry) for entry in answer.entries],
            complete=answer.complete,
            truncated=answer.truncated,
            offset=answer.offset,
            next_offset=answer.next_offset,
            total=answer.total,
            warnings=list(answer.warnings),
            observed_at=answer.observed_at,
        )
    return Response.json(
        status,
        error_payload(
            COMMAND_NAME,
            reason=reason,
            error=answer.note or reason,
            paused=False,
            # The answer without its body: enough to say what was asked and
            # what became of it, and nothing that was read.
            details=answer.identity(),
        ),
    )


def _selector(request: MutationRequest, method: str) -> str:
    """What narrows this mutation's fingerprint beyond the plan it acts on."""
    name = _SELECTORS.get(method)
    if name is None:
        return ""
    return str(getattr(request, name, None) or "")


def _rehydrate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Give a journal row back the two derived fields the table does not store.

    ``fingerprint`` and ``scope`` are computed from the method and the target,
    and the schema keeps neither -- it keeps the parts they are computed from.
    :func:`~asl_workbench.operations.admit` compares on both, so a row read back
    without them answers "different target" to every resubmission, which would
    turn each retry into a conflict and each outstanding mutation into one the
    contention check skips. Recomputing here keeps that derivation in one place
    rather than adding two columns that could disagree with it.
    """
    rehydrated = dict(record)
    method = str(record.get("method", ""))
    try:
        resolved = require_method(method)
    except OperationError:  # pragma: no cover - a row this service wrote
        return rehydrated
    name = _SELECTORS.get(resolved)
    target = OperationTarget(
        plan_key=str(record.get("plan_key", "")),
        selector=str(record.get(name, "") or "") if name else "",
    )
    rehydrated["fingerprint"] = operation_fingerprint(resolved, target)
    rehydrated["scope"] = method_scope(resolved)
    return rehydrated


def _submission(request: MutationRequest, method: str) -> dict[str, Any]:
    """The ``operation.submit`` body, rebuilt from what the parser accepted."""
    payload: dict[str, Any] = {
        "request": REQUEST_MUTATE,
        "method": method,
        "operation_id": request.operation_id,
        "target": request.target(),
    }
    for name in ("reply_ref", "action_token", "bridge_token", "harness", "reason"):
        value = getattr(request, name, None)
        if value:
            payload[name] = value
    return payload


def _accepted(record: Mapping[str, Any], *, replay: bool) -> Response:
    """``202`` and the journal row: admitted, which is not the same as done."""
    return Response.success(
        202,
        reason="accepted",
        operation=export_operation(record),
        replay=replay,
    )
