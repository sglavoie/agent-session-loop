"""Remote service queries over retained observations and the connector read lane."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import overview_filters
from .freshness import aggregate_completeness, aggregate_freshness
from .http import (
    COMMAND_NAME,
    Call,
    HttpRefusal,
    Response,
    error_payload,
    invalid_request,
)
from .observation import COMPLETENESS_COMPLETE
from .operations import (
    METHOD_ARCHIVE_DELETE,
    METHOD_ARCHIVE_RESTORE,
    METHOD_ARCHIVE_SETTINGS,
    export_operation,
)
from .remote_reads import ReadAnswer
from .service_models import _NO_READ_LANE, READ_STATUSES, ServiceState
from .store_types import DEFAULT_READ_LIMIT, MAX_READ_LIMIT
from .transport import MAX_DETAIL_BYTES, MAX_DETAIL_ENTRIES


class ServiceQueries(ServiceState):
    """Retained observations and bounded connector reads."""

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
        # Counted by the database over every present plan, the same set
        # ``counts.plans`` counts, so the histogram sums to the total instead
        # of describing whichever rows fit under one read limit.
        by_state = self._store.catalog_states()
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
        filters = overview_filters.parse_filters(call, default_limit=DEFAULT_READ_LIMIT)
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
        counts = overview_filters.counts_from_states(
            self._store.catalog_states(**narrowing)
        )
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
            catalog_revision=self._store.catalog_revision(),
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
        filters = overview_filters.parse_filters(call, default_limit=DEFAULT_READ_LIMIT)
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
        operation_id = call.optional_query("operation_id")
        if operation_id is not None:
            record = self._store.operation(operation_id)
            if record is None or record.get("plan_key") != plan_key:
                raise HttpRefusal(404, "not-found", "operation not found for this plan")
            return Response.success(reason="ok", operation=export_operation(record))
        # Narrowed in SQL rather than over a bounded page: a low-urgency plan
        # sorts past any read limit, and filtering that page here would answer
        # "not found" for a plan the catalog holds.
        entries = self._store.catalog(
            now=now, include_absent=True, plan_key=plan_key, limit=MAX_READ_LIMIT
        )
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
            question_id=call.optional_query("question_id"),
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
        if detail == "questions" and answer.answered:
            rows = answer.entries or ()
            if len(rows) == 1 and isinstance(rows[0], Mapping):
                return Response.success(
                    reason="ok",
                    **dict(rows[0]),
                    complete=answer.complete,
                    observed_at=answer.observed_at,
                    warnings=list(answer.warnings),
                )
        return _read_response(answer)

    def _archive_list(self, call: Call) -> Response:
        """Read the Mac archive in bounded pages; retain none of its paths."""
        operation_id = call.optional_query("operation_id")
        if operation_id is not None:
            record = self._store.operation(operation_id)
            if record is None or record.get("method") not in (
                METHOD_ARCHIVE_RESTORE,
                METHOD_ARCHIVE_DELETE,
                METHOD_ARCHIVE_SETTINGS,
            ):
                raise HttpRefusal(404, "not-found", "archive operation not found")
            return Response.success(reason="ok", operation=export_operation(record))
        lane = self._reads
        if lane is None:
            raise HttpRefusal(503, "operation-unavailable", _NO_READ_LANE)
        entries: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {}
        offset = 0
        observed_at: float | None = None
        for _ in range(50):
            answer = lane.fetch(
                detail="archive", plan_key="", offset=offset, now=self._clock()
            )
            if not answer.answered:
                return _read_response(answer)
            rows = list(answer.entries)
            if not rows or "archive_metadata" not in rows[0]:
                raise HttpRefusal(
                    502, "invalid-archive-read", "the Mac returned no archive metadata"
                )
            page_meta = rows[0]["archive_metadata"]
            if not isinstance(page_meta, Mapping):
                raise HttpRefusal(
                    502,
                    "invalid-archive-read",
                    "the Mac returned malformed archive metadata",
                )
            if metadata and metadata != dict(page_meta):
                raise HttpRefusal(
                    409,
                    "archive-changed",
                    "the archive changed during this read; refresh it",
                )
            metadata = dict(page_meta)
            entries.extend(dict(row) for row in rows[1:])
            observed_at = answer.observed_at
            if answer.next_offset is None:
                break
            if answer.next_offset <= offset:
                raise HttpRefusal(
                    502, "invalid-archive-read", "archive pagination did not advance"
                )
            offset = answer.next_offset
        else:
            raise HttpRefusal(
                503, "archive-too-large", "the archive exceeds the bounded read window"
            )
        candidate_ids = [
            row["archive_id"]
            for row in entries
            if row.get("cleanup_state") == "eligible"
        ]
        return Response.success(
            reason="ok",
            entries=entries,
            archive_retention_days=metadata.get("archive_retention_days"),
            archive_cleanup={
                "candidate_ids": candidate_ids,
                "candidate_count": len(candidate_ids),
                "undated_count": metadata.get("undated_count", 0),
                "entry_count": len(entries),
                "retention_days": metadata.get("archive_retention_days"),
            },
            warnings=[],
            observed_at=observed_at,
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
