"""The Mac provider: one operation carried out against ASL's own services.

This module is half of what ``workbench_server.py`` used to be. The half that
left is everything that was never local: routing, roles, request policy,
envelopes, static assets, and the parsing of every query parameter, all of
which now live in :mod:`asl_workbench.http` and are shared with the Pi.

What stays is the part that is genuinely Mac-specific:

* **Execution.** Each operation calls the ASL service that owns it --
  ``workbench_details``, ``workbench_archive``, ``workbench_catalog``,
  ``workbench_actions``, ``workbench_controls`` -- rather than reimplementing
  any of them. This module decides nothing about plans; it decides which
  service answers which operation.
* **Translation.** ASL's refusals are exceptions with their own vocabularies.
  Turning one into a status code is a statement about whose problem it is, and
  those statements are collected here so the same refusal cannot be rendered
  two ways by two routes.
* **Launch bookkeeping.** :class:`LaunchBook` is the in-memory, per-server
  record that keeps *this* process from spawning a second run for a plan while
  the first is still starting. It is deliberately not durable: the durable
  defence is the plan lock the children take, and the durable receipt is the
  operation journal ``workbench_actions`` writes.

Nothing here knows it is being reached over HTTP beyond the status codes, and
nothing here is reachable except through :class:`asl_workbench.http.Application`,
which has already decided the caller is the owner.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from asl_workbench import actions as workbench_action_rules
from asl_workbench import catalog, freshness, observation, overview
from asl_workbench import controls as portable_controls
from asl_workbench import details as detail_rules
from asl_workbench.details import Window
from asl_workbench.http import (
    MODE_LOCAL,
    Call,
    HttpRefusal,
    Response,
    invalid_request,
    unavailable_from,
)
from asl_workbench.http import OPERATIONS as HTTP_OPERATIONS
from asl_workbench.operations import OperationError

from . import (
    intervention,
    plans,
    redirect_admission,
    redirect_cli,
    redirect_store,
    state,
    workbench,
    workbench_actions,
    workbench_archive,
    workbench_catalog,
    workbench_controls,
    workbench_details,
    workbench_history,
)

_JSONDict = dict[str, Any]

#: How many launch receipts one server remembers. A receipt exists so a
#: browser that reconnected and resent its launch gets its own answer back
#: instead of a second process; nothing needs it once the operator has moved
#: on.
MAX_LAUNCH_RECEIPTS = 64

#: Same ceiling the redirect journal puts on an operation id, for the same
#: reason: an idempotency key is an identifier a client chooses, and an
#: unbounded one is an unbounded allocation on this side of the wire.
MAX_LAUNCH_ID_CHARS = redirect_store.REDIRECT_MAX_OPERATION_ID_CHARS


class LocalConfig(Protocol):
    """What the provider needs from the server that built it.

    Structural on purpose: the bind address, the token, and the socket belong
    to the transport, and a provider that imported the server to read a state
    directory would be a provider that could not be tested without one.
    """

    @property
    def state_dir(self) -> Path | None: ...

    @property
    def registry_path(self) -> Path | None: ...

    @property
    def plan_roots(self) -> tuple[str, ...]: ...

    @property
    def include_default_dirs(self) -> bool: ...


class LaunchBook:
    """What one server remembers about the runs it has started.

    Three records, each answering a different question a resent launch asks:
    which plan is mid-decision right now (:meth:`guard`), what answer an
    identical earlier request already got (:meth:`recorded`), and whether a
    process this server spawned is still alive (:meth:`pid`).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plan_locks: dict[str, threading.Lock] = {}
        self._receipts: dict[tuple[str, str], _JSONDict] = {}
        self._pids: dict[str, int] = {}

    def guard(self, plan_dir: Path) -> threading.Lock:
        """Return the lock that serializes launch decisions for one plan.

        A server answers requests on concurrent threads, so the ownership
        check and the spawn have to happen under one lock. Without it two
        reconnecting browser tabs can both read "nothing holds this plan" and
        both spawn a run, and the loser only finds out by dying on the lock.
        """
        key = str(plan_dir)
        with self._lock:
            guard = self._plan_locks.get(key)
            if guard is None:
                guard = threading.Lock()
                self._plan_locks[key] = guard
            return guard

    def recorded(self, plan_dir: Path, launch_id: str) -> _JSONDict | None:
        """Return the answer an earlier identical launch request already got."""
        with self._lock:
            recorded = self._receipts.get((str(plan_dir), launch_id))
            return dict(recorded) if recorded is not None else None

    def record(
        self, plan_dir: Path, launch_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Remember one launch so its request can be retried without repeating it."""
        with self._lock:
            while len(self._receipts) >= MAX_LAUNCH_RECEIPTS:
                # Oldest first: a dict preserves insertion order, and the
                # receipt a reconnecting browser asks for is a recent one.
                del self._receipts[next(iter(self._receipts))]
            self._receipts[(str(plan_dir), launch_id)] = dict(payload)

    def record_pid(self, plan_dir: Path, pid: int) -> None:
        """Remember the process this server spawned for one plan."""
        with self._lock:
            self._pids[str(plan_dir)] = pid

    def pid(self, plan_dir: Path) -> int | None:
        """Return the last process this server spawned for one plan, if any."""
        with self._lock:
            return self._pids.get(str(plan_dir))

    def forget_pid(self, plan_dir: Path) -> None:
        """Drop the recorded pid for a plan whose process has exited."""
        with self._lock:
            self._pids.pop(str(plan_dir), None)


# ---------------------------------------------------------------------------
# Refusal translation
# ---------------------------------------------------------------------------

#: The status each shared refusal scope answers with. Written as a table so the
#: mapping is a statement rather than a chain of conditionals: a control refused
#: by the shared service is refused for exactly one of these reasons, and each
#: reason has exactly one thing the client should do next.
_REFUSAL_STATUS: Mapping[str, int] = {
    # The body is wrong, and resending it unchanged cannot succeed.
    portable_controls.SCOPE_REQUEST: 400,
    # A durable record contradicts the request. Well formed, so not a 400: what
    # resolves it is reconciling the id, not fixing a field.
    portable_controls.SCOPE_CONFLICT: 409,
    # Retryable and unmistakably not a refusal: a journal this server cannot
    # read is a fact about the machine, and answering as though the plan held no
    # corrections would be the wrong kind of wrong.
    portable_controls.SCOPE_UNAVAILABLE: 503,
}

#: Service refusals that are facts about the request rather than about the run.
#: Resending one of these unchanged will never succeed, which is what separates
#: them from a stale token or an action the run does not currently permit.
_INTERVENTION_REQUEST_REASONS = frozenset(
    {
        intervention.REASON_INVALID_PLAN,
        intervention.REASON_INVALID_ACTION,
        intervention.REASON_EMPTY_TEXT,
    }
)


def _intervention_status(reason: str) -> int:
    """Map a shared-service refusal to the status that says whose problem it is.

    409 is the default on purpose: a stale action token, an action the run no
    longer permits, and a run that moved on are all resolved by re-reading the
    plan, not by rewriting the body, and 400 would tell a client to do the one
    thing that cannot help.
    """
    return 400 if reason in _INTERVENTION_REQUEST_REASONS else 409


def _refusal_status(error: workbench_controls.ControlRefused) -> int:
    """Map a shared refusal to the status that says whose problem it is."""
    fixed = _REFUSAL_STATUS.get(error.scope)
    if fixed is not None:
        return fixed
    if error.scope == portable_controls.SCOPE_INTERVENTION:
        return _intervention_status(error.reason)
    # A submission that was refused describes the correction; one that failed
    # describes this machine, and `retryable` is the service's own word for
    # which happened.
    return 500 if error.retryable else 400


# Plan states that hold an active operational lock; archiving one is blocked
# by the same active-lock guard the archive helper enforces.
_ARCHIVE_BLOCKING_STATES = frozenset({"verifying", "running"})


def sort_summaries_oldest_first(
    summaries: (
        list[workbench.WorkbenchSummary] | tuple[workbench.WorkbenchSummary, ...]
    ),
) -> list[workbench.WorkbenchSummary]:
    """Sort plans oldest-activity-first; plans with no activity sort oldest.

    Complete plans are ordered by ``completed_at`` so cleanup surfaces the
    longest-finished plans first; every other state falls back to
    ``last_activity_at``. Missing timestamps sort oldest either way.
    """

    def sort_key(summary: workbench.WorkbenchSummary) -> tuple[int, str]:
        if summary.primary_state == "complete":
            activity = summary.completed_at
        else:
            activity = summary.last_activity_at
        if not activity:
            # No recorded activity is treated as the oldest possible.
            return (0, "")
        return (1, activity)

    return sorted(summaries, key=sort_key)


def _augment_archivable(
    payload: _JSONDict, summary: workbench.WorkbenchSummary
) -> _JSONDict:
    """Add ``archivable`` / ``archive_blocker`` fields to a queue plan payload."""
    blocked = summary.primary_state in _ARCHIVE_BLOCKING_STATES
    payload["archivable"] = not blocked
    payload["archive_blocker"] = "active-lock" if blocked else None
    return payload


def _augment_catalog_labels(
    payload: _JSONDict, entry: catalog.CatalogEntry
) -> _JSONDict:
    """Add the naming a row is grouped and searched by in either mode.

    ``WorkbenchSummary`` has never carried a manifest title or a project, and
    the catalog projection has carried both since the consolidation began. The
    local queue publishes them here rather than growing a second derivation:
    project headings, multi-project membership and title search are supposed to
    read the same on a laptop and on a phone, and they only can if both rows
    were named by the same code.
    """
    payload["title"] = entry.title
    payload["projects"] = list(entry.projects)
    payload["project_group"] = entry.project_group
    payload["search_text"] = entry.search_text
    payload["activity_label"] = entry.activity_label
    return payload


def _queue_rows(scan: workbench_catalog.CatalogScan) -> list[_JSONDict]:
    """Every scanned plan as one queue row, named by the catalog projection.

    ``scan.summaries`` and ``scan.catalog.records`` come out of the same pass
    in the same order, which is what lets the Mac-side detail and the portable
    naming be joined without a second lookup keyed on something that could
    drift.
    """
    rows: list[_JSONDict] = []
    for summary, record in zip(scan.summaries, scan.catalog.records, strict=True):
        payload = _augment_catalog_keys(
            _augment_archivable(summary.to_payload(), summary), summary
        )
        rows.append(_augment_catalog_labels(payload, record.entry))
    return rows


def _plan_labels(scan: workbench_catalog.CatalogScan) -> dict[str, _JSONDict]:
    """Each plan key mapped to what a history row should call that plan."""
    return {
        record.entry.plan_key: {
            "plan_label": record.entry.display_label,
            "plan_title": record.entry.title,
            "projects": list(record.entry.projects),
        }
        for record in scan.catalog.records
    }


def _history_newest_first(entries: Sequence[Any]) -> list[Any]:
    """History entries in the order the remote store returns them.

    ``recorded_at DESC, entry_id ASC`` is the store's ``ORDER BY``; sorting the
    same way here is what lets one pane page either provider identically.
    """
    ordered = sorted(entries, key=lambda entry: str(entry.entry_id))
    ordered.sort(key=lambda entry: float(entry.recorded_at), reverse=True)
    return ordered


def _history_record(entry: Any, labels: Mapping[str, _JSONDict]) -> _JSONDict:
    """One ending, named the way the store names the same ending remotely.

    The portable entry knows its plan by digest and never by path, so the label
    and the title are joined on here from the catalog -- exactly as
    :meth:`asl_workbench.store.WorkbenchStore.history` joins them on from
    ``catalog_entry``. A plan the scan no longer sees keeps its ending and
    loses only its title, which is the right way round: the run happened.
    """
    record: _JSONDict = dict(entry.to_payload())
    naming = labels.get(str(record.get("plan_key")))
    if naming is None:
        return record
    record["plan_label"] = naming["plan_label"]
    record["plan_title"] = naming["plan_title"]
    projects = naming["projects"]
    if projects:
        # Joined rather than listed, because ``project`` is the one field the
        # history export carries and the store joins multi-project membership
        # into it the same way. One separator, so one reader can split it.
        record["project"] = " + ".join(projects)[: observation.MAX_LABEL_CHARS]
    return record


def _augment_catalog_keys(
    payload: _JSONDict, summary: workbench.WorkbenchSummary
) -> _JSONDict:
    """Add the catalog identities a row carries in either mode.

    The local queue addresses plans by path and does not need these to work.
    They are published anyway because the *links* do: an ntfy notification the
    Pi sent carries ``#run=<run_key>``, and opening one on the laptop has to
    land on the same plan it opens on a phone. Computing them here -- with the
    catalog's own functions, from the same inputs -- is what makes that one
    link instead of two that happen to look alike.
    """
    key = catalog.plan_key(str(summary.plan_dir))
    payload["plan_key"] = key
    payload["run_key"] = catalog.run_key(key, summary.run_id)
    return payload


# ---------------------------------------------------------------------------
# Launch profile coercion
# ---------------------------------------------------------------------------


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise invalid_request("profile fields must be strings when set")
    text = value.strip()
    return text or None


def _coerce_optional_int(value: Any, *, field_name: str = "timeout") -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_request(f"profile {field_name} must be an integer when set")
    return value


def _coerce_max_review_fix_cycles(value: Any) -> int | None:
    cap = _coerce_optional_int(value, field_name="max_review_fix_cycles")
    if cap is not None and cap < 0:
        raise invalid_request(
            "profile max_review_fix_cycles must be zero or a positive integer when set"
        )
    return cap


def _coerce_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise invalid_request("profile boolean fields must be booleans when set")
    return value


def _coerce_commit_mode(value: Any) -> workbench.CommitMode | None:
    """Narrow a request's ``commit_mode`` to the vocabulary, or refuse it.

    Unlike the harness names -- which the launch path resolves against a
    registry -- ``commit_mode`` is a two-value closed set, so an unrecognized
    value is a malformed request and nothing else. Refusing it here reports it
    as one instead of letting it travel to the launch as an invalid profile.
    """
    text = _coerce_optional_str(value)
    if text is None:
        return None
    mode = next((mode for mode in workbench.COMMIT_MODES if mode == text), None)
    if mode is None:
        raise invalid_request(
            "field 'commit_mode' must be one of "
            + ", ".join(repr(each) for each in workbench.COMMIT_MODES)
        )
    return mode


def coerce_profile_payload(raw: Any) -> workbench.LaunchProfile | None:
    """Build a launch profile from a request payload, or refuse the payload.

    The field names are :data:`asl_workbench.actions.LAUNCH_PROFILE_FIELDS`,
    which both surfaces already share; what stays here is the construction of
    the Mac dataclass they name, because the Pi forwards a profile without
    ever building one.
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise invalid_request("profile must be a JSON object when provided")
    return workbench.LaunchProfile(
        cwd=_coerce_optional_str(raw.get("cwd")),
        harness=_coerce_optional_str(raw.get("harness")),
        harness_model=_coerce_optional_str(raw.get("harness_model")),
        effort=_coerce_optional_str(raw.get("effort")),
        timeout=_coerce_optional_int(raw.get("timeout")),
        commit_mode=_coerce_commit_mode(raw.get("commit_mode")),
        review_harness=_coerce_optional_str(raw.get("review_harness")),
        review_harness_model=_coerce_optional_str(raw.get("review_harness_model")),
        max_review_fix_cycles=_coerce_max_review_fix_cycles(
            raw.get("max_review_fix_cycles")
        ),
        unsafe_bypass=_coerce_optional_bool(raw.get("unsafe_bypass")),
        allow_not_loop_ready=_coerce_optional_bool(raw.get("allow_not_loop_ready")),
        note=_coerce_optional_str(raw.get("note")),
    )


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class LocalProvider:
    """Carry out one Workbench operation against the Mac's own services.

    Every operation the application declares is supported here: this is the
    machine that owns the plans, so there is nothing for
    :meth:`unavailable` to decline. The method exists anyway because the
    frontend asks both modes the same question, and a local answer of "all of
    them" is a real answer rather than a missing one.
    """

    mode = MODE_LOCAL

    def __init__(
        self, config: LocalConfig, *, launches: LaunchBook | None = None
    ) -> None:
        self.config = config
        self.launches = launches if launches is not None else LaunchBook()

    # -- Capability reporting ----------------------------------------------

    def unavailable(self, operation: str) -> str | None:
        return _LOCAL_UNAVAILABLE(operation)

    # -- Dispatch -----------------------------------------------------------

    def invoke(self, call: Call) -> Response:
        handler = _HANDLERS.get(call.operation)
        if handler is None:  # pragma: no cover - the route table is the source
            raise HttpRefusal(
                500, "internal-error", f"no local handler for {call.operation!r}"
            )
        try:
            return handler(self, call)
        except workbench_archive.ArchiveRefused as error:
            # The status comes from the refusal rather than from a table kept
            # here, so a surface without status codes -- the CLI, the connector
            # -- translates the same refusal through its own vocabulary without
            # this provider having to agree with it first.
            raise HttpRefusal(
                error.status, error.reason, error.message, details=error.details
            ) from error
        except workbench_controls.ControlRefused as error:
            raise HttpRefusal(
                _refusal_status(error),
                error.reason,
                error.message,
                details=(
                    {"plan_dir": str(error.plan_dir), **error.details}
                    if error.plan_dir is not None
                    else dict(error.details)
                ),
                extra={"retryable": error.retryable},
            ) from error
        except intervention.InterventionError as error:
            # Reached only where a handler did not want to name the plan in the
            # refusal; `inspect_run` refuses an unreadable plan directory and
            # nothing else, which is a fact about the request.
            raise HttpRefusal(
                _intervention_status(error.reason),
                error.reason,
                error.message,
                details=dict(error.details),
                extra={"retryable": error.retryable},
            ) from error

    # -- Reads --------------------------------------------------------------

    def _registry(self) -> workbench.WorkbenchRegistry:
        try:
            return workbench.load_registry(self.config.registry_path)
        except workbench.WorkbenchError as error:
            raise HttpRefusal(500, "registry-unreadable", str(error)) from error

    def _open_plan(self, call: Call) -> workbench_details.PlanReader:
        """Open the requested plan for one detail read, reclaiming nothing.

        The single door every detail route goes through. Classification happens
        once per request and produces the scope that decides which files that
        request may reach, so no handler below resolves a path of its own.
        """
        return workbench_details.open_plan(
            call.require_query("plan_dir"), state_dir=self.config.state_dir
        )

    def _handle_health(self, call: Call) -> Response:
        del call
        return Response.success(reason="ok", status="ok")

    def _scan(
        self, registry: workbench.WorkbenchRegistry
    ) -> workbench_catalog.CatalogScan:
        # One non-reclaiming catalog scan answers the queue. Polling the queue
        # no longer empties expired archive entries or reclaims plan locks: a
        # read that repairs makes the answer a function of who looked and when.
        # Retention now runs only where an operator asked for it, on the
        # archive route; `observational-reads` in `asl_workbench.changes`
        # records the decision.
        return workbench_catalog.scan(
            state_dir=self.config.state_dir,
            registry=registry,
            plan_roots=self.config.plan_roots,
            include_default_dirs=self.config.include_default_dirs,
        )

    def _handle_summary(self, call: Call) -> Response:
        """Counts and freshness, and deliberately nothing else.

        The summary role exists so a caller who may not read a plan can still
        see that there are plans and whether the view is current. Every field
        below is a number or a flag; adding a title here would make the role a
        weaker owner rather than a different one.
        """
        del call
        scan = self._scan(self._registry())
        by_state: dict[str, int] = {}
        for summary in scan.summaries:
            by_state[summary.primary_state] = by_state.get(summary.primary_state, 0) + 1
        return Response.success(
            reason="ok",
            counts={"plans": len(scan.summaries), "by_state": by_state},
            freshness={
                "generation": scan.catalog.generation,
                "complete": scan.catalog.complete,
                "total": scan.catalog.total,
                # The Mac reads the filesystem it is asked about, so the answer
                # is as of now and there is no observation to be stale.
                "stale": False,
                "observed_at": None,
            },
        )

    def _handle_queue(self, call: Call) -> Response:
        """Every visible plan, narrowed and counted before it is paged.

        The Mac used to answer with the whole corpus and let the browser hide
        what did not match. That reads correctly right up until the catalog
        outgrows one page, at which point the browser is filtering a page and
        counting a page while describing a corpus. So the narrowing happens
        here now, in the same order the Pi's SQL does it --
        :func:`~asl_workbench.overview.select` is that order, shared -- and the
        page carries the size of the set it was cut from.
        """
        registry = self._registry()
        state_root = state.state_root(self.config.state_dir)
        scan = self._scan(registry)
        filters = overview.parse_filters(call)
        # The moment this answer describes, published because the eight-hour
        # window is measured against it. A browser reading its own clock would
        # measure a phone's idea of now against a laptop's idea of when a run
        # last moved, and the two are not the same number.
        observed_at = time.time()
        try:
            page = overview.select(_queue_rows(scan), filters)
        except overview.OverviewError as error:
            raise invalid_request(str(error)) from error
        return Response.success(
            reason="ok",
            plans=list(page.rows),
            observed_at=observed_at,
            registry_warnings=list(registry.warnings),
            discovery_warnings=list(scan.discovery.warnings),
            catalog_warnings=list(scan.projection_warnings),
            catalog_generation=scan.catalog.generation,
            catalog_complete=scan.catalog.complete,
            # The same fact in the shared vocabulary. A scan that did not cover
            # every root covered part of the catalog; the Mac is never in the
            # third case, because a filesystem it can read is a filesystem that
            # answers how much of itself it just showed.
            catalog_completeness=(
                observation.COMPLETENESS_COMPLETE
                if scan.catalog.complete
                else observation.COMPLETENESS_PARTIAL
            ),
            filters=filters.as_document(),
            # The Mac is standing on the plans it is describing, so every row
            # it publishes is current by construction. Saying so explicitly is
            # what lets one front end mark a *retained* row as retained
            # without having to know which provider answered.
            observation_status=overview.OBSERVATION_CURRENT,
            freshness=freshness.FRESHNESS_LIVE,
            # No connector, no incident engine: these two are the Pi's record
            # of what it could not confirm, and the honest local answer is
            # "there is no such thing here" rather than a missing key.
            connectors=[],
            incidents=[],
            state_root=str(state_root),
            state_root_exists=state_root.is_dir(),
            archive_retention_days=registry.archive_retention_days,
            **page.as_document(),
        )

    def _handle_history(self, call: Call) -> Response:
        """Runs that ended inside the retention window, newest first.

        Read from the event files themselves rather than from the publication
        the connector uploads, and read without advancing the connector's
        checkpoint: a pane that consumed endings would be a pane that stopped
        the Pi from ever seeing them.
        """
        filters = overview.parse_filters(call)
        outcome = call.optional_query("outcome")
        view = workbench_history.read_window(
            now=time.time(), state_dir=self.config.state_dir
        )
        labels = _plan_labels(self._scan(self._registry()))
        entries: list[_JSONDict] = []
        # ``view.entries`` arrives in the order the event files were read, so
        # the newest-first promise is this provider's to keep. Sorted before
        # the limit, or the limit would keep the oldest tail of the window.
        for entry in _history_newest_first(view.entries):
            record = _history_record(entry, labels)
            if outcome is not None and record.get("outcome") != outcome:
                continue
            if not overview.matches(record, filters, states=False):
                continue
            entries.append(observation.export_history_entry(record))
            if len(entries) >= filters.limit:
                break
        return Response.success(
            reason="ok",
            entries=entries,
            observed_at=view.window_end,
            complete=view.complete,
            incidents=[],
            warnings=list(view.warnings),
        )

    def _handle_plan(self, call: Call) -> Response:
        plan_dir = call.require_query("plan_dir")
        registry = self._registry()
        return Response.success(
            reason="ok",
            **workbench_details.read_plan(
                plan_dir, registry=registry, state_dir=self.config.state_dir
            ),
        )

    def _handle_recovery(self, call: Call) -> Response:
        return Response.success(
            reason="ok",
            **workbench_details.read_recovery(
                call.require_query("plan_dir"), state_dir=self.config.state_dir
            ),
        )

    def _handle_registry(self, call: Call) -> Response:
        del call
        return Response.success(reason="ok", registry=self._registry().to_payload())

    def _handle_artifacts(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_artifacts(self._open_plan(call))
        )

    def _handle_state(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_state(self._open_plan(call))
        )

    def _handle_events(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_events(
                reader,
                offset=call.non_negative_int("offset", default=0),
                limit=call.bounded_int(
                    "limit",
                    default=detail_rules.DEFAULT_EVENT_LIMIT,
                    maximum=detail_rules.MAX_EVENT_LIMIT,
                ),
                run_id=call.optional_query("run_id"),
            ),
        )

    def _handle_logs(self, call: Call) -> Response:
        reader = self._open_plan(call)
        # `artifact` is the named form; `stream` is its two-value alias and
        # `path` the compatibility form. Which of the three is acceptable, and
        # what each resolves to, is decided by the scope rather than here --
        # every one of them lands inside the selected plan or is refused.
        return Response.success(
            reason="ok",
            **workbench_details.read_logs(
                reader,
                stream=call.optional_query("stream"),
                artifact=call.optional_query("artifact"),
                path=call.optional_query("path"),
                window=Window(
                    offset=call.optional_int_query("offset"),
                    tail_lines=call.optional_int_query("tail_lines"),
                    max_bytes=call.bounded_int(
                        "max_bytes",
                        default=detail_rules.DEFAULT_LOG_BYTES,
                        maximum=detail_rules.MAX_READ_BYTES,
                    ),
                ),
            ),
        )

    def _handle_last_message(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_last_message(
                reader,
                max_bytes=call.bounded_int(
                    "max_bytes",
                    default=detail_rules.DEFAULT_LAST_MESSAGE_BYTES,
                    maximum=detail_rules.MAX_READ_BYTES,
                ),
            ),
        )

    def _handle_result(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_result(self._open_plan(call))
        )

    def _handle_verification(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_verification(self._open_plan(call))
        )

    def _handle_commits(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_commits(
                reader, run_id=call.optional_query("run_id")
            ),
        )

    def _handle_progress(self, call: Call) -> Response:
        return Response.success(
            reason="ok", **workbench_details.read_progress(self._open_plan(call))
        )

    def _handle_final_review(self, call: Call) -> Response:
        reader = self._open_plan(call)
        return Response.success(
            reason="ok",
            **workbench_details.read_final_review(
                reader,
                max_bytes=call.bounded_int(
                    "max_bytes",
                    default=detail_rules.DEFAULT_FINAL_REVIEW_BYTES,
                    maximum=detail_rules.MAX_READ_BYTES,
                ),
            ),
        )

    def _handle_questions(self, call: Call) -> Response:
        return Response.success(
            reason="ok",
            **workbench_details.read_questions(
                call.require_query("plan_dir"), state_dir=self.config.state_dir
            ),
        )

    def _handle_archive_list(self, call: Call) -> Response:
        """Report the archive and what retention makes of it. Delete nothing.

        This route used to run a maintenance pass first, so opening the archive
        tab was the act that permanently emptied expired entries -- an operator
        who went looking for something old destroyed it by looking. Now the
        retention window only marks candidates, and emptying them is
        ``POST /api/archive/delete`` with a typed count.
        """
        del call
        view = workbench_archive.review_archive(
            state_dir=self.config.state_dir, registry_path=self.config.registry_path
        )
        return Response.success(reason="ok", **view.to_payload())

    # -- Registry and archive mutations -------------------------------------

    def _handle_registry_pin(self, call: Call) -> Response:
        update = workbench_archive.pin_plan(
            call.require_string("plan_dir"),
            registry_path=self.config.registry_path,
            label=call.optional_string("label"),
            profile=coerce_profile_payload(call.body.get("profile")),
        )
        return Response.success(reason="ok", **update.to_payload())

    def _handle_registry_unpin(self, call: Call) -> Response:
        update = workbench_archive.unpin_plan(
            call.require_string("plan_dir"), registry_path=self.config.registry_path
        )
        return Response.success(reason="ok", **update.to_payload())

    def _handle_plan_archive(self, call: Call) -> Response:
        result = workbench_archive.archive_one(
            call.require_string("plan_dir"),
            call.require_string("confirm_plan_dir"),
            state_dir=self.config.state_dir,
            registry_path=self.config.registry_path,
            plan_roots=self.config.plan_roots,
            include_default_dirs=self.config.include_default_dirs,
        )
        return Response.success(reason="ok", **result.to_payload())

    def _handle_plans_archive(self, call: Call) -> Response:
        bulk = workbench_archive.archive_many(
            call.body.get("plan_dirs"),
            state_dir=self.config.state_dir,
            registry_path=self.config.registry_path,
            plan_roots=self.config.plan_roots,
            include_default_dirs=self.config.include_default_dirs,
        )
        return Response.success(reason="ok", **bulk.to_payload())

    def _handle_archive_restore(self, call: Call) -> Response:
        result = workbench_archive.restore_entry(
            call.require_string("archive_id"),
            state_dir=self.config.state_dir,
            registry_path=self.config.registry_path,
        )
        return Response.success(reason="ok", **result.to_payload())

    def _handle_archive_delete(self, call: Call) -> Response:
        """The one route that destroys, and the only one that ever did."""
        result = workbench_archive.purge_entries(
            call.body.get("archive_ids"),
            call.body.get("confirm_count"),
            state_dir=self.config.state_dir,
        )
        return Response.success(reason="ok", **result.to_payload())

    def _handle_archive_settings(self, call: Call) -> Response:
        update = workbench_archive.set_retention(
            call.body.get("archive_retention_days"),
            registry_path=self.config.registry_path,
        )
        return Response.success(
            reason="ok",
            **update.to_payload(),
            archive_retention_days=update.registry.archive_retention_days,
        )

    def _handle_unlock(self, call: Call) -> Response:
        plan_dir = call.require_string("plan_dir")
        result = workbench_archive.unlock(
            plan_dir,
            state_dir=self.config.state_dir,
            force=bool(call.body.get("force", False)),
        )
        return Response.success(
            reason="ok", **workbench_archive.unlock_payload(plan_dir, result)
        )

    # -- Spawning -----------------------------------------------------------

    def _handle_launch(self, call: Call) -> Response:
        return self._spawn_run(call, reply_text=None)

    def _handle_reply(self, call: Call) -> Response:
        # Read before the guard is taken so a request with no reply at all is
        # refused as malformed rather than counted as a launch attempt.
        return self._spawn_run(call, reply_text=call.require_string("reply_text"))

    def _spawn_run(self, call: Call, *, reply_text: str | None) -> Response:
        """Start one run for a plan, at most once per plan and per ``launch_id``.

        The two spawning operations share this path because they share the
        failure an operator actually hits: a browser that reconnects, or an
        impatient second click, resending a launch that already succeeded. Four
        things stand between that and a second process, in this order:

        1. Durable admission. ``workbench_actions`` records the submission in
           the plan's own operation journal under an exclusive lock, so a
           resent ``launch_id`` is recognised as the same submission even by a
           server that did not answer the first one -- and a submission that
           arrives while another is genuinely in flight is refused rather than
           spawned. This is also the write that makes an interrupted launch
           accountable afterwards, because dispatch is recorded before the
           process is started rather than after.
        2. ``launch_id`` replay. A resent request carrying the id of one that
           already ran gets that run's own answer back, so a retry is the same
           request rather than a second one.
        3. The ownership pre-check below, which reports a live run as a conflict
           instead of spawning a process that dies on the plan lock. The plan
           lock remains the authority on execution; admission only keeps a
           second attempt from being started in front of it.
        4. The per-plan guard, which makes 2 and 3 mean anything on a threaded
           server by serializing check-and-spawn for one plan.

        A client that supplies no ``launch_id`` gets a minted one, so its
        submission is still accounted for even though nothing can recognize a
        retry of it as a retry.

        All four live in ``workbench_actions``, which is also what the bridge
        calls. The two surfaces used to admit, spawn, and settle by their own
        arrangements; a browser click and a supervisor's command now differ
        only in how they arrived.
        """
        plan_dir = call.require_string("plan_dir")
        launch_id = call.bounded_id("launch_id", maximum=MAX_LAUNCH_ID_CHARS)
        profile = coerce_profile_payload(call.body.get("profile"))
        action = (
            workbench_action_rules.ACTION_LAUNCH
            if reply_text is None
            else workbench_action_rules.ACTION_RESUME
        )
        try:
            canonical = plans.canonicalize_path(plan_dir)
        except (OSError, ValueError) as error:
            raise HttpRefusal(
                400,
                workbench.LAUNCH_REASON_INVALID_PLAN,
                f"invalid plan_dir: {error}",
                details={"plan_dir": plan_dir},
            ) from error
        with self.launches.guard(canonical):
            try:
                outcome = workbench_actions.perform(
                    canonical,
                    action=action,
                    operation_id=launch_id,
                    reply_text=reply_text,
                    profile=profile,
                    state_dir=self.config.state_dir,
                    guard=self._launch_conflict,
                )
            except OperationError as error:
                raise HttpRefusal(
                    error.status, error.reason, error.message, details=error.details
                ) from error
            except intervention.InterventionError as error:
                raise HttpRefusal(
                    409, error.reason, error.message, details=dict(error.details)
                ) from error
            except workbench_actions.ActionRefused as error:
                raise HttpRefusal(
                    error.status,
                    error.reason,
                    error.message,
                    details={"plan_dir": str(canonical)},
                ) from error
            except workbench.LaunchError as error:
                raise HttpRefusal(
                    400, error.reason, error.message, details={"plan_dir": plan_dir}
                ) from error
            if outcome.replayed:
                return self._answer_prior_submission(outcome, canonical, launch_id)
            assert outcome.launch is not None  # a carried-out spawn has one
            payload = outcome.launch.to_payload()
            self.launches.record_pid(canonical, outcome.launch.pid)
            if launch_id is not None:
                self.launches.record(canonical, launch_id, payload)
        return Response.success(
            reason="ok",
            launch=payload,
            launch_id=launch_id,
            replayed=False,
            operation=outcome.operation,
        )

    def _answer_prior_submission(
        self,
        outcome: workbench_actions.ActionOutcome,
        plan_dir: Path,
        launch_id: str | None,
    ) -> Response:
        """Answer a submission the journal has already seen.

        A replay gets this server's own receipt when it happens to hold one,
        and the journal row otherwise. The distinction is worth keeping rather
        than collapsing: only the process that answered the first request can
        say what pid it started, so a cross-process replay reports *that the
        submission is already recorded* instead of inventing a launch payload
        for a process it never saw.
        """
        recorded = (
            self.launches.recorded(plan_dir, launch_id)
            if launch_id is not None
            else None
        )
        admission = outcome.admission
        return Response.success(
            reason="ok",
            launch=recorded,
            launch_id=launch_id,
            replayed=True,
            operation=(
                admission.to_payload() if admission is not None else outcome.operation
            ),
        )

    def _launch_conflict(self, facts: intervention.RunFacts) -> tuple[str, str] | None:
        """Name what already owns this plan, or ``None`` when nothing does.

        Two checks, because neither sees the other's window. Once a child has
        taken the plan lock, the shared service reports a live run. Before it
        has -- the seconds a reconnecting browser is most likely to resend in --
        nothing durable says so yet, and the pid this server spawned is the only
        evidence a launch is already under way.

        The facts are the ones the shared action already read, rather than a
        second reading of its own: a guard that answers about a different
        instant than the operation it guards is a guard with a gap in it.

        A paused run is deliberately not a conflict: resuming one is exactly
        what the reply operation is for.
        """
        plan_dir = facts.plan_dir
        if facts.run_active:
            return (
                "run-already-active",
                "a run already holds this plan"
                + (f": {facts.run_id}" if facts.run_id else ""),
            )
        pid = self.launches.pid(plan_dir)
        if pid is not None:
            if state.is_pid_running(pid):
                return (
                    "launch-already-in-flight",
                    f"this server already started a run for this plan (pid {pid})",
                )
            # Forget a process that has exited. Remembering it costs nothing
            # today and everything the day the operating system reuses its
            # number: a long-dead pid that reads as alive again would refuse a
            # launch that nothing is actually running.
            self.launches.forget_pid(plan_dir)
        return None

    # -- Controls -----------------------------------------------------------

    def _dispatch_control(
        self, kind: str, call: Call, **kwargs: Any
    ) -> workbench_controls.ControlOutcome:
        """Queue one control through the service both surfaces share.

        The service carries the same best-effort wakeup the CLI producer sends,
        so this surface is not the slower way to reach a live runner, and it
        reads the run the same way every other reader does, so the activity an
        abort reports here is not a second parse of the lock.
        """
        return workbench_controls.dispatch_control(
            call.require_string("plan_dir"),
            kind,
            state_dir=self.config.state_dir,
            command_id=call.optional_string("command_id"),
            **kwargs,
        )

    @staticmethod
    def _queued(outcome: workbench_controls.ControlOutcome) -> Response:
        """Answer a control whose only promise is that it was written down."""
        return Response.success(
            202,
            reason="accepted",
            accepted=True,
            queued_command=outcome.command.to_payload(),
            signal=outcome.dispatch.signal.to_payload(),
        )

    def _handle_control_stop_after_current(self, call: Call) -> Response:
        return self._queued(
            self._dispatch_control(portable_controls.CONTROL_STOP_AFTER_CURRENT, call)
        )

    def _handle_control_select_harness(self, call: Call) -> Response:
        return self._queued(
            self._dispatch_control(
                portable_controls.CONTROL_SELECT_HARNESS,
                call,
                harness=call.require_string("harness"),
            )
        )

    def _handle_control_abort(self, call: Call) -> Response:
        outcome = self._dispatch_control(
            portable_controls.CONTROL_ABORT,
            call,
            # Passed through untouched: ``append_command`` is the only validator
            # of grace and reason, so this surface cannot accept a window the
            # CLI would reject or reject one it would accept.
            grace_seconds=call.body.get("grace_seconds"),
            reason=call.body.get("reason"),
        )
        queued = outcome.command
        activity = outcome.activity
        return Response.success(
            202,
            reason="accepted",
            accepted=True,
            run_active=activity.runner_present,
            turn_active=activity.turn_active,
            grace_seconds=queued.grace_seconds,
            # The CLI calls this field `reason`; here the top-level `reason` is
            # the HTTP response status, so the abort's own reason keeps a
            # distinct name.
            abort_reason=queued.reason,
            note=activity.note,
            queued_command=queued.to_payload(),
            signal=outcome.dispatch.signal.to_payload(),
        )

    # -- Redirect -----------------------------------------------------------

    @staticmethod
    def _redirect_payload(
        submission: workbench_controls.RedirectSubmission,
    ) -> _JSONDict:
        """Name the shared submission's facts the way this API already does.

        Every field comes from the service, which re-read the journal after the
        submission rather than building an answer from the outcome in hand, so
        the status a browser renders and the status
        ``asl control redirect-status`` computes next are one projection of the
        same bytes.
        """
        return {
            # Null exactly when nothing was persisted. Echoing the client's own
            # id for a refusal that never reached the journal would invite it to
            # ask after a correction that does not exist.
            "operation_id": submission.operation_id,
            "plan_dir": str(submission.facts.plan_dir),
            "run_id": submission.facts.run_id,
            # Null on a replay: that call submitted nothing, so it has no
            # submission outcome to report and `operation` carries the truth.
            "outcome": submission.outcome,
            # The CLI and the bridge both call this `reason`; here the top-level
            # `reason` is the HTTP response status, so a refusal keeps its own
            # name.
            "refusal": submission.reason,
            "target": submission.target,
            "waited_seconds": round(submission.waited_seconds, 3),
            "replayed": submission.replayed,
            "operation": submission.operation_payload(),
            "journal_complete": submission.journal_complete,
        }

    def _handle_control_redirect(self, call: Call) -> Response:
        """Submit one urgent correction against the turn this run is on now.

        Replay is served out of the redirect journal before the freshness proof
        is checked, and that ordering is the whole reason a browser may retry
        this request. A correction that landed moves the plan, which moves the
        action token, which would make the retry of a request whose response was
        lost look like a stale second correction. Asking the journal first makes
        the ``operation_id`` the client already holds the answer.

        What is *not* short-circuited is the request itself. The body is read
        and compared against the record that id already names, because a retry
        is only a retry when it carries the same correction: an operator whose
        response was lost still has an editable textarea, and reporting their
        edited words as a successful replay of the words the worker actually got
        is the one falsehood this path exists to avoid.
        """
        try:
            submission = workbench_controls.submit_correction(
                call.require_string("plan_dir"),
                operation_id=call.require_string("operation_id"),
                text=call.body.get("text"),
                state_dir=self.config.state_dir,
                expected_target_raw=call.body.get("expected_target"),
                wait_seconds_raw=call.body.get("wait_seconds"),
                action_token=call.optional_string("action_token"),
                run_id=call.optional_string("run_id"),
            )
        except workbench_controls.RedirectConflict as error:
            # Nothing was written, and nothing about the recorded request
            # changed. 409 rather than 400 because the body is well formed:
            # what it contradicts is a durable record, and the client resolves
            # that by reconciling the id, not by fixing a field.
            raise HttpRefusal(
                409,
                error.reason,
                error.message,
                details={
                    "plan_dir": str(error.plan_dir),
                    "operation_id": error.operation_id,
                    "conflict": error.conflict,
                    "redirect": self._redirect_payload(error.submission),
                },
                extra={"retryable": False},
            ) from error
        receipt = self._redirect_payload(submission)
        if submission.replayed:
            # 200 rather than 202: this call accepted nothing, it recognized a
            # request that was already accepted once.
            return Response.success(reason="ok", redirect=receipt)
        if not submission.persisted:
            # Nothing reached the journal, so there is nothing to ask after
            # later. Reporting this as accepted is the one falsehood the whole
            # redirect path is built to avoid.
            raise HttpRefusal(
                409,
                submission.reason or redirect_admission.REFUSE_NO_ACTIVE_TURN,
                f"redirect was refused: {submission.reason}",
                details={
                    "plan_dir": str(submission.facts.plan_dir),
                    "redirect": receipt,
                },
                extra={
                    "retryable": submission.reason
                    in redirect_admission.UNAVAILABLE_REASONS
                },
            )
        return Response.success(
            202,
            reason="accepted",
            # Persisted, which is what this surface can promise. Whether the
            # runner took it is `redirect.operation.stage`, and an
            # `unavailable` or `uncertain` outcome says so there.
            accepted=submission.accepted,
            redirect=receipt,
        )

    def _handle_redirect_status(self, call: Call) -> Response:
        """Project this plan's durable redirect state, writing nothing.

        A read, and unauthenticated like every other local read, because the
        client most in need of it is the one that just lost its own record of
        what it submitted. Every field is derived through :mod:`.redirect_cli`,
        so the stage a browser renders and the stage an operator reads out of
        ``asl control redirect-status`` are one derivation of the same facts.
        """
        view = workbench_controls.status_view(
            call.require_query("plan_dir"),
            state_dir=self.config.state_dir,
            operation_ids=call.query_values("operation_id"),
        )
        facts = view.facts
        report = view.report
        live_target = view.live_target
        operations = view.operation_payloads()
        return Response.success(
            reason="ok",
            plan_dir=str(facts.plan_dir),
            run_id=facts.run_id,
            journal_path=report.journal_path,
            owner=report.owner.liveness,
            owner_run_id=report.owner.run_id,
            live_target=(live_target.to_payload() if live_target is not None else None),
            operations=operations,
            uncertain_operation_ids=[
                operation["operation_id"]
                for operation in operations
                if operation["stage"] == redirect_cli.STAGE_UNCERTAIN
            ],
            # `journal_complete` is the honesty flag and `truncated` is its
            # projection counterpart: a torn journal means a record may be
            # missing from the source, a full window that one is missing here.
            journal_complete=report.complete,
            torn_tail=report.torn_tail,
            unreadable_lines=report.unreadable_lines,
            truncated=view.truncated,
            checked_at=report.checked_at,
            conversations=report.conversations.to_payload(),
            # The freshness proof a redirect must carry back. Handed out by the
            # read so a composer never has to guess at one.
            action_token=intervention.action_token(facts),
            permitted_actions=list(intervention.permitted_actions(facts)),
            disabled_reasons=intervention.disabled_reasons(facts),
        )

    def _handle_conversation_recovery(self, call: Call) -> Response:
        """Report which recovery a pause is waiting on, and how to authorize it.

        A read, and deliberately only a read. Performing the recovery here would
        persist the operator's one-use authorization in the one place it must
        never live: a request a browser can replay. The pause-to-decision mapping
        and both commands come from :mod:`.intervention` and
        :mod:`.redirect_cli`, so this response, the recovery card, and
        ``asl control recover-conversation`` cannot hand an operator three
        different answers about the same pause.
        """
        view = workbench_controls.recovery_view(
            call.require_query("plan_dir"), state_dir=self.config.state_dir
        )
        classified = view.decision
        return Response.success(
            reason="ok",
            plan_dir=str(view.facts.plan_dir),
            run_id=view.facts.run_id,
            recovery_required=classified.required,
            # Reported whether or not it maps to a decision: "paused, and no
            # manual recovery is what it is waiting on" is an answer, while an
            # unexplained null is a second question.
            pause_reason=classified.pause_reason,
            decision=classified.decision,
            auto_resume=classified.auto_resume,
            authorized_by=classified.authorized_by,
            inspect_command=view.inspect_command,
            authorization_command=view.authorization_command,
            # Both describe the authorization command, so both are false and
            # null where there is none. `predecessor-not-stopped` has no
            # authorization at all, and telling an operator its absent
            # authorization is one-use would read as an invitation to find it.
            one_use=view.one_use,
            note=view.note,
            conversations=view.report.conversations.to_payload(),
            # The corrections a fresh start would still carry. Reported here so
            # an operator authorizing one sees what it inherits rather than
            # discovering it in the successor's first prompt.
            retained_operations=view.retained_payloads(),
        )


_HANDLERS: Mapping[str, Callable[[LocalProvider, Call], Response]] = {
    "health": LocalProvider._handle_health,
    "summary": LocalProvider._handle_summary,
    "queue": LocalProvider._handle_queue,
    "history": LocalProvider._handle_history,
    "plan": LocalProvider._handle_plan,
    "recovery": LocalProvider._handle_recovery,
    "registry": LocalProvider._handle_registry,
    "artifacts": LocalProvider._handle_artifacts,
    "state": LocalProvider._handle_state,
    "events": LocalProvider._handle_events,
    "logs": LocalProvider._handle_logs,
    "last-message": LocalProvider._handle_last_message,
    "result": LocalProvider._handle_result,
    "verification": LocalProvider._handle_verification,
    "commits": LocalProvider._handle_commits,
    "progress": LocalProvider._handle_progress,
    "final-review": LocalProvider._handle_final_review,
    "questions": LocalProvider._handle_questions,
    "redirect-status": LocalProvider._handle_redirect_status,
    "conversation-recovery": LocalProvider._handle_conversation_recovery,
    "archive-list": LocalProvider._handle_archive_list,
    "registry-pin": LocalProvider._handle_registry_pin,
    "registry-unpin": LocalProvider._handle_registry_unpin,
    "plan-archive": LocalProvider._handle_plan_archive,
    "plans-archive": LocalProvider._handle_plans_archive,
    "archive-restore": LocalProvider._handle_archive_restore,
    "archive-delete": LocalProvider._handle_archive_delete,
    "archive-settings": LocalProvider._handle_archive_settings,
    "launch": LocalProvider._handle_launch,
    "reply": LocalProvider._handle_reply,
    "control-stop-after-current": LocalProvider._handle_control_stop_after_current,
    "control-abort": LocalProvider._handle_control_abort,
    "control-select-harness": LocalProvider._handle_control_select_harness,
    "control-redirect": LocalProvider._handle_control_redirect,
    "unlock": LocalProvider._handle_unlock,
}

#: Every operation the application declares, minus the one it answers itself.
#: Built from the route table rather than restated, so a route added without a
#: handler here fails loudly at import instead of quietly at request time.
_LOCAL_UNAVAILABLE = unavailable_from(
    _HANDLERS,
    reasons={},
    default="this operation is not implemented by the local provider",
)

_MISSING_HANDLERS = tuple(
    sorted(
        operation
        for operation in HTTP_OPERATIONS
        if operation != "capabilities" and operation not in _HANDLERS
    )
)
if _MISSING_HANDLERS:  # pragma: no cover - a registration mistake, not a state
    raise RuntimeError(
        "workbench_local has no handler for: " + ", ".join(_MISSING_HANDLERS)
    )
