"""State schema versioning and in-place upgrade."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import agent_mux_projection, artifacts, events, plans_files
from .plans_errors import PlanNotFoundError
from .state import (
    LOCK_FILE_NAME,
    STATE_MIGRATED_EVENT,
    StateError,
    _dump_state_bytes,
    plan_state_dir,
)
from .state_locks import _read_lock_path
from .state_plan_refs import _read_json_object_safe

# state.json path fields the migration rewrites to point at the new state dir.
# ``event_path`` is the legacy duplicate of ``events_path``; both are rewritten
# so neither is left dangling at the old location. Historical per-turn
# ``result.json`` paths are intentionally excluded (see ``migrate_plan_state``).
_MIGRATION_PATH_FIELDS: tuple[str, ...] = (
    "plan_dir",
    "state_dir",
    "state_path",
    "events_path",
    "event_path",
    "lock_path",
)


class MigrationError(StateError):
    """Raised when a state-directory migration cannot proceed safely.

    Carries a machine-readable ``reason`` and structured ``details`` so the
    ``migrate-state`` CLI handler can surface an actionable JSON payload while
    ``except StateError`` guards keep working.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


#: ``migrate-state`` relocates an orphaned state directory to the current
#: plan's path hash, upgrades a state record to the current schema, or both.
MIGRATION_MODE_RELOCATE = "relocate"


MIGRATION_MODE_UPGRADE = "upgrade"


@dataclass(frozen=True)
class MigrationReport:
    """Outcome of migrating one plan's operational state directory."""

    plan_dir: str
    from_plan_dir: str | None
    state_dir: str
    from_state_dir: str
    applied: bool
    rewritten_fields: tuple[str, ...]
    result_artifacts: int
    unmigrated_artifacts_note: str | None
    mode: str = MIGRATION_MODE_RELOCATE
    schema_version_before: int | None = None
    schema_version_after: int | None = None
    schema_upgraded: bool = False
    projection_written: bool = False
    schema_note: str | None = None
    conversation_records: int = 0
    conversation_records_rewritten: int = 0
    conversation_note: str | None = None
    redirect_requests: int = 0
    redirect_requests_unfinished: int = 0
    redirect_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "migrate-state",
            "plan_dir": self.plan_dir,
            "from_plan_dir": self.from_plan_dir,
            "state_dir": self.state_dir,
            "from_state_dir": self.from_state_dir,
            "applied": self.applied,
            "rewritten_fields": list(self.rewritten_fields),
            "result_artifacts": self.result_artifacts,
            "unmigrated_artifacts_note": self.unmigrated_artifacts_note,
            "mode": self.mode,
            "schema_version_before": self.schema_version_before,
            "schema_version_after": self.schema_version_after,
            "schema_upgraded": self.schema_upgraded,
            "projection_written": self.projection_written,
            "schema_note": self.schema_note,
            "conversation_records": self.conversation_records,
            "conversation_records_rewritten": self.conversation_records_rewritten,
            "conversation_note": self.conversation_note,
            "redirect_requests": self.redirect_requests,
            "redirect_requests_unfinished": self.redirect_requests_unfinished,
            "redirect_note": self.redirect_note,
        }


def state_schema_version(state_obj: Mapping[str, Any]) -> int | None:
    """Return the declared state schema version.

    ``None`` means the record declares nothing, which the format has always
    read as v1, and ``-1`` stands for a ``version`` key this build cannot
    interpret at all — a future version, or a non-integer. The two are kept
    apart because the first is a record this build knows how to upgrade and the
    second is one it must refuse to touch.
    """
    if "version" not in state_obj:
        return None
    raw = state_obj["version"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        return -1
    return raw


def _upgrade_state_object(
    state_obj: dict[str, Any],
    *,
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    """Return ``(upgraded object, projection written)`` for one state record.

    The projection is built from the persisted state alone and never names a
    process: whatever ran this record's loop is not this migrator, and a
    ``(pid, start token)`` pair it cannot observe is not one it may assert. A
    paused or terminal record would clear the process anyway; a record that
    still reads as live gets a null process, which says "no live owner is
    claimed here" rather than making one up.
    """
    upgraded = dict(state_obj)
    upgraded.pop(agent_mux_projection.PROJECTION_KEY, None)
    projection = agent_mux_projection.attach_projection(
        upgraded,
        updated_at=now.isoformat(),
        detect_process=False,
    )
    if projection is None:
        return upgraded, False
    upgraded["version"] = agent_mux_projection.STATE_SCHEMA_VERSION
    return upgraded, True


def upgrade_plan_state_schema(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    apply: bool = False,
) -> MigrationReport:
    """Upgrade one plan's ``state.json`` to the current schema, in place.

    Reads an absent-version, v1, or v2 record and rewrites it as v3: the same
    evidence, plus the nested body-free ``agent_mux`` projection built from it.
    Nothing else in the record is touched — no turn, no snapshot, no pause
    evidence, no path — so a paused run stays paused with its pause details
    intact and a terminal run keeps the ending it recorded.

    Idempotent by construction: a record already at v3 is reported and left
    alone, so re-running is a no-op rather than a second generation bump.

    Refuses a record whose ``version`` this build cannot interpret, a record
    with too little identity to project, and — the case that matters most — a
    run whose lock is still live. An old process holds its state payload in
    memory and rewrites the whole file on its next transition, so upgrading
    underneath it would be silently reverted; worse, that process emits schema-1
    lifecycle hooks a v3 consumer will not recognise. Such a run is reported as
    incompatible and left for its operator to stop.
    """
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    plan_state = plan_state_dir(canonical_plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    if not plan_state.is_dir() or not state_path.is_file():
        # State outlives a deleted plan and can still be upgraded, so a missing
        # directory only explains the refusal when there is no state either; a
        # mistyped PLAN_DIR then reads as one, not as a plan that never ran.
        if not canonical_plan_dir.exists():
            raise PlanNotFoundError(
                f"plan directory does not exist: {canonical_plan_dir}"
            )
        raise MigrationError(
            "no-source-state",
            f"no state to upgrade at {plan_state}",
            {"state_dir": str(plan_state), "state_path": str(state_path)},
        )

    state_obj = _read_json_object_safe(state_path)
    if state_obj is None:
        raise MigrationError(
            "malformed-source-state",
            f"state file is not a JSON object: {state_path}",
            {"state_path": str(state_path)},
        )

    version = state_schema_version(state_obj)
    current = agent_mux_projection.STATE_SCHEMA_VERSION
    upgradable = version in agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS
    if version is not None and version != current and not upgradable:
        known = ", ".join(
            str(each) for each in agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS
        )
        raise MigrationError(
            "unknown-schema-version",
            f"state records schema version {state_obj['version']!r}, which this "
            f"build cannot upgrade; it understands absent, {known} and {current}",
            {"state_path": str(state_path), "schema_version": state_obj["version"]},
        )

    lock = _read_lock_path(plan_state / LOCK_FILE_NAME, canonical_plan_dir)

    def _report(
        *,
        applied: bool,
        version_after: int | None,
        upgraded: bool,
        projection_written: bool,
        note: str | None,
    ) -> MigrationReport:
        return MigrationReport(
            plan_dir=str(canonical_plan_dir),
            from_plan_dir=None,
            state_dir=str(plan_state),
            from_state_dir=str(plan_state),
            applied=applied,
            rewritten_fields=(),
            result_artifacts=0,
            unmigrated_artifacts_note=None,
            mode=MIGRATION_MODE_UPGRADE,
            schema_version_before=version,
            schema_version_after=version_after,
            schema_upgraded=upgraded,
            projection_written=projection_written,
            schema_note=note,
        )

    if version == current:
        # Already current. Reported rather than refused, and reported the same
        # way whether or not a run holds the lock: nothing here would change.
        return _report(
            applied=False,
            version_after=current,
            upgraded=False,
            projection_written=isinstance(
                state_obj.get(agent_mux_projection.PROJECTION_KEY), dict
            ),
            note=f"state is already at schema version {current}; nothing to upgrade",
        )

    # Spelled as one test rather than a precomputed flag: the three reads below
    # are only reachable when a lock exists, and only this form says so.
    if lock is not None and lock.active:
        raise MigrationError(
            "incompatible-active-run",
            f"plan is locked by pid {lock.pid} and its state is at schema "
            f"version {'absent' if version is None else version}, not {current}; "
            "that run predates this build's projection and cannot be upgraded "
            "underneath itself — stop it, then re-run this command",
            {
                "lock_path": str(lock.path),
                "pid": lock.pid,
                "schema_version": version,
                "expected_schema_version": current,
                "incompatible_active_run": True,
            },
        )

    upgraded_obj, projection_written = _upgrade_state_object(
        state_obj, now=datetime.now(UTC)
    )
    if not projection_written:
        raise MigrationError(
            "unprojectable-state",
            f"state at {state_path} records no run identity (run_id, plan_dir "
            "and run_started_at are all required), so no projection can be "
            f"built for it; it stays at schema version "
            f"{'absent' if version is None else version}",
            {"state_path": str(state_path), "schema_version": version},
        )

    if not apply:
        return _report(
            applied=False,
            version_after=current,
            upgraded=True,
            projection_written=True,
            note=(
                f"schema version {'absent' if version is None else version} → "
                f"{current}; re-run with --apply to write it"
            ),
        )

    artifacts.atomic_write_bytes(
        state_path, _dump_state_bytes(upgraded_obj), durable=True
    )
    events.append_event(
        plan_state / events.EVENTS_FILE_NAME,
        events.Event(
            STATE_MIGRATED_EVENT,
            {
                "plan_dir": str(canonical_plan_dir),
                "state_dir": str(plan_state),
                "from_schema_version": version,
                "to_schema_version": current,
                "projection_generation": upgraded_obj[
                    agent_mux_projection.PROJECTION_KEY
                ]["projection_generation"],
            },
        ),
    )
    return _report(
        applied=True,
        version_after=current,
        upgraded=True,
        projection_written=True,
        note=(f"schema version {'absent' if version is None else version} → {current}"),
    )
