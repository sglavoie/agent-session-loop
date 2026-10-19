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
    plans_files,
    redirect_admission,
    redirect_cli,
    redirect_store_vocab,
    state,
    workbench_actions,
    workbench_archive,
    workbench_catalog,
    workbench_controls,
    workbench_details,
    workbench_history,
    workbench_launch,
    workbench_registry,
    workbench_registry_models,
    workbench_summary_model,
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
MAX_LAUNCH_ID_CHARS = redirect_store_vocab.REDIRECT_MAX_OPERATION_ID_CHARS


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
        list[workbench_summary_model.WorkbenchSummary]
        | tuple[workbench_summary_model.WorkbenchSummary, ...]
    ),
) -> list[workbench_summary_model.WorkbenchSummary]:
    """Sort plans oldest-activity-first; plans with no activity sort oldest.

    Complete plans are ordered by ``completed_at`` so cleanup surfaces the
    longest-finished plans first; every other state falls back to
    ``last_activity_at``. Missing timestamps sort oldest either way.
    """

    def sort_key(summary: workbench_summary_model.WorkbenchSummary) -> tuple[int, str]:
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
    payload: _JSONDict, summary: workbench_summary_model.WorkbenchSummary
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
    :meth:`asl_workbench.store_workbench.WorkbenchStore.history` joins them on from
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
    payload: _JSONDict, summary: workbench_summary_model.WorkbenchSummary
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


def _coerce_commit_mode(value: Any) -> workbench_registry_models.CommitMode | None:
    """Narrow a request's ``commit_mode`` to the vocabulary, or refuse it.

    Unlike the harness names -- which the launch path resolves against a
    registry -- ``commit_mode`` is a two-value closed set, so an unrecognized
    value is a malformed request and nothing else. Refusing it here reports it
    as one instead of letting it travel to the launch as an invalid profile.
    """
    text = _coerce_optional_str(value)
    if text is None:
        return None
    mode = next(
        (mode for mode in workbench_registry_models.COMMIT_MODES if mode == text), None
    )
    if mode is None:
        raise invalid_request(
            "field 'commit_mode' must be one of "
            + ", ".join(repr(each) for each in workbench_registry_models.COMMIT_MODES)
        )
    return mode


def coerce_profile_payload(raw: Any) -> workbench_registry_models.LaunchProfile | None:
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
    return workbench_registry_models.LaunchProfile(
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


class _LocalProviderOperations:
    """State supplied by :class:`LocalProvider` to its operation mixins."""

    config: LocalConfig
    launches: LaunchBook


# The operation mixins import this shared vocabulary with ``*``.  Keeping an
# explicit surface preserves the runtime facade and gives the type checker the
# same view of those names.
# fmt: off
__all__ = ("threading", "time", "Callable", "Mapping", "Sequence", "Path", "Any", "Protocol", "workbench_action_rules", "catalog", "freshness", "observation", "overview", "portable_controls", "detail_rules", "Window", "MODE_LOCAL", "Call", "HttpRefusal", "Response", "invalid_request", "unavailable_from", "HTTP_OPERATIONS", "OperationError", "intervention", "plans_files", "redirect_admission", "redirect_cli", "redirect_store_vocab", "state", "workbench_actions", "workbench_archive", "workbench_catalog", "workbench_controls", "workbench_details", "workbench_history", "workbench_launch", "workbench_registry", "workbench_registry_models", "workbench_summary_model", "_JSONDict", "MAX_LAUNCH_RECEIPTS", "MAX_LAUNCH_ID_CHARS", "LocalConfig", "LaunchBook", "_REFUSAL_STATUS", "_INTERVENTION_REQUEST_REASONS", "_intervention_status", "_refusal_status", "_ARCHIVE_BLOCKING_STATES", "sort_summaries_oldest_first", "_augment_archivable", "_augment_catalog_labels", "_queue_rows", "_plan_labels", "_history_newest_first", "_history_record", "_augment_catalog_keys", "_coerce_optional_str", "_coerce_optional_int", "_coerce_max_review_fix_cycles", "_coerce_optional_bool", "_coerce_commit_mode", "coerce_profile_payload", "_LocalProviderOperations")
# fmt: on


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------
