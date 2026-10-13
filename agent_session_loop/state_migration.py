"""Relocating a plan's operational state between state roots."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import agent_mux_projection, artifacts, conversations, events, plans
from .state import (
    LOCK_FILE_NAME,
    RUNS_DIR_NAME,
    STATE_MIGRATED_EVENT,
    _dump_state_bytes,
    _nonempty_str,
    plan_state_dir,
)
from .state_locks import _read_lock_path
from .state_migration_notes import (
    _conversation_note,
    _redirect_note,
    _survey_redirect_journal,
)
from .state_plan_refs import (
    _iter_result_paths,
    _read_json_object_safe,
    _recorded_content_hash,
)
from .state_schema_upgrade import (
    _MIGRATION_PATH_FIELDS,
    MigrationError,
    MigrationReport,
    _upgrade_state_object,
    state_schema_version,
    upgrade_plan_state_schema,
)


def migrate_plan_state(
    plan_dir: str | Path,
    *,
    from_plan_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    apply: bool = False,
) -> MigrationReport:
    """Relocate an orphaned state directory from an old plan path to a new one.

    SHA-of-path keying means moving a plan directory strands its operational
    state under the hash of its old path. This renames that state directory to
    the hash of the current path and rewrites the absolute path fields inside
    ``state.json`` so ``asl status`` and resume work again. Historical per-turn
    ``result.json`` paths are intentionally left untouched and reported via
    ``unmigrated_artifacts_note``. The default is a dry-run; ``apply`` performs
    the rename, rewrite, and ``state_migrated`` event append.

    ``from_plan_dir=None`` selects the schema upgrade instead: see
    :func:`upgrade_plan_state_schema`. With a source plan named, the relocated
    record is opportunistically upgraded in the same rewrite — it is being
    written anyway, and leaving it a version behind would only require a second
    command. That upgrade is best-effort here: a legacy record too thin to
    project keeps its old version and is reported, because a stranded state
    directory's paths are worth restoring whether or not it can carry a
    projection.

    Refuses (in both dry-run and apply, before touching either directory) on a
    source plan directory that still exists on disk (its state is not orphaned),
    an active source lock, an already-occupied destination, a target that is not
    a valid plan, a source state that does not record the requested old plan, or
    a source whose content hash is incompatible with the target plan. The
    content check prefers ``final_snapshot.content_sha256`` and falls back to
    ``initial_snapshot.content_sha256`` only for legacy state.
    """
    if from_plan_dir is None:
        return upgrade_plan_state_schema(plan_dir, state_dir=state_dir, apply=apply)
    # Inspecting the target validates it is a real plan (raising PlanError,
    # surfaced as ``malformed-plan``) and yields the content hash the source
    # state must be compatible with.
    target_snapshot = plans.inspect_plan(plan_dir)
    new_plan_dir = target_snapshot.plan_dir
    old_plan_dir = plans.canonicalize_path(from_plan_dir)
    old_state_dir = plan_state_dir(old_plan_dir, state_dir)
    new_state_dir = plan_state_dir(new_plan_dir, state_dir)

    if old_state_dir == new_state_dir:
        raise MigrationError(
            "noop-migration",
            f"source and destination state directories are identical: {old_state_dir}",
            {"state_dir": str(old_state_dir)},
        )

    old_state_path = old_state_dir / events.STATE_FILE_NAME
    if not old_state_dir.is_dir() or not old_state_path.is_file():
        raise MigrationError(
            "no-source-state",
            f"no source state to migrate at {old_state_dir}",
            {
                "from_state_dir": str(old_state_dir),
                "from_state_path": str(old_state_path),
            },
        )

    # Refuse when the source plan directory still exists on disk: its state is
    # not orphaned, so migrating would steal a live plan's operational state.
    # Discovery already skips still-existing source plans; this keeps the
    # mutating API consistent with read-only discovery. Two identical plans
    # otherwise satisfy the identity and content checks below, and --apply would
    # rename the source's state dir out from under it.
    try:
        source_plan_exists = old_plan_dir.is_dir()
    except OSError:
        source_plan_exists = False
    if source_plan_exists:
        raise MigrationError(
            "source-plan-exists",
            f"source plan directory still exists and is not orphaned: {old_plan_dir}",
            {"from_plan_dir": str(old_plan_dir)},
        )

    # Refuse to migrate a live run out from under itself.
    old_lock = _read_lock_path(old_state_dir / LOCK_FILE_NAME, old_plan_dir)
    if old_lock is not None and old_lock.active:
        raise MigrationError(
            "active-lock",
            f"source plan is locked by pid {old_lock.pid} at {old_lock.path}",
            {"lock_path": str(old_lock.path), "pid": old_lock.pid},
        )

    if new_state_dir.exists():
        raise MigrationError(
            "destination-occupied",
            f"destination state directory already exists: {new_state_dir}",
            {"state_dir": str(new_state_dir)},
        )

    state_obj = _read_json_object_safe(old_state_path)
    if state_obj is None:
        raise MigrationError(
            "malformed-source-state",
            f"source state file is not a JSON object: {old_state_path}",
            {"from_state_path": str(old_state_path)},
        )

    # Confirm the source state truly belongs to the requested old plan, so a
    # mistaken --from cannot attach unrelated operational state to this target.
    recorded_plan_dir = _nonempty_str(state_obj.get("plan_dir"))
    recorded_matches = False
    if recorded_plan_dir is not None:
        try:
            recorded_matches = (
                plans.canonicalize_path(recorded_plan_dir) == old_plan_dir
            )
        except OSError, ValueError:
            recorded_matches = False
    if not recorded_matches:
        raise MigrationError(
            "source-plan-mismatch",
            "source state records plan directory "
            f"{recorded_plan_dir!r}, not the requested {old_plan_dir}",
            {
                "recorded_plan_dir": recorded_plan_dir,
                "from_plan_dir": str(old_plan_dir),
            },
        )

    # The same identity check for the conversation ledger, which is plan-owned
    # operational state sitting beside state.json rather than inside it. A ledger
    # naming some other plan is not this plan's, and relocating it would attach
    # another plan's conversations — and their resumable native ids — to the
    # target. Unreadable lines cannot be attributed to any plan, so they are
    # reported rather than treated as foreign: refusing on them would strand the
    # whole state directory over one truncated append.
    foreign_plan_dirs = tuple(
        recorded
        for recorded in conversations.recorded_plan_dirs(old_state_dir)
        if recorded != str(old_plan_dir)
    )
    if foreign_plan_dirs:
        raise MigrationError(
            "conversation-plan-mismatch",
            "source conversation ledger records plan directories "
            f"{', '.join(foreign_plan_dirs)}, not the requested {old_plan_dir}",
            {
                "recorded_plan_dirs": list(foreign_plan_dirs),
                "from_plan_dir": str(old_plan_dir),
            },
        )

    # Confirm the source content belongs to the target plan (final snapshot
    # preferred, initial snapshot as the legacy fallback).
    recorded_hash = _recorded_content_hash(state_obj)
    if recorded_hash != target_snapshot.content_sha256:
        raise MigrationError(
            "content-mismatch",
            "source state content hash "
            f"{recorded_hash!r} does not match target plan content hash "
            f"{target_snapshot.content_sha256}",
            {
                "recorded_content_sha256": recorded_hash,
                "target_content_sha256": target_snapshot.content_sha256,
            },
        )

    # Count historical per-turn result.json artifacts; their embedded paths
    # point at the old plan directory and are intentionally left unrewritten.
    result_artifacts = sum(1 for _ in _iter_result_paths(old_state_dir / RUNS_DIR_NAME))
    unmigrated_note = None
    if result_artifacts:
        plural = "" if result_artifacts == 1 else "s"
        unmigrated_note = (
            f"{result_artifacts} per-turn result.json artifact{plural} under "
            f"runs/ retain references to the old plan directory "
            f"{old_plan_dir}; they are historical and were not rewritten."
        )

    rewritten_fields = tuple(
        field for field in _MIGRATION_PATH_FIELDS if field in state_obj
    )

    # The relocated record is rewritten anyway, so bring its schema forward in
    # the same write rather than leaving a second command to run. Best-effort:
    # an unrecognised version or a record too thin to project keeps its version
    # and is reported, because restoring a stranded state directory's paths is
    # worth doing either way.
    version_before = state_schema_version(state_obj)
    current_version = agent_mux_projection.STATE_SCHEMA_VERSION
    upgradable = (
        version_before is None
        or version_before in agent_mux_projection.UPGRADABLE_STATE_SCHEMA_VERSIONS
    )

    # Surveyed before either branch, and before anything is renamed: an
    # unreadable redirect journal is a refusal rather than a relocation with a
    # missing count, on the same terms as the conversation ledger beside it.
    # The journal is the only durable record of a correction, so moving a state
    # directory whose journal cannot be read would relocate a promise nothing
    # can later account for.
    redirect_survey = _survey_redirect_journal(old_state_dir)

    if not apply:
        try:
            preview_conversations = conversations.relocate_records(
                old_state_dir,
                from_plan_dir=old_plan_dir,
                to_plan_dir=new_plan_dir,
                apply=False,
            )
        except conversations.ConversationError as error:
            raise MigrationError(
                "conversation-ledger-error",
                f"source conversation ledger could not be read: {error}",
                {"from_state_dir": str(old_state_dir)},
            ) from error
        note: str | None = None
        upgraded_preview = False
        projection_preview = False
        if version_before == current_version:
            note = f"state is already at schema version {current_version}"
            projection_preview = isinstance(
                state_obj.get(agent_mux_projection.PROJECTION_KEY), dict
            )
        elif not upgradable:
            note = (
                f"state records schema version {state_obj.get('version')!r}, which "
                "this build cannot upgrade; relocation leaves it unchanged"
            )
        else:
            # Discarding the upgraded object: the dry-run only needs to know
            # whether the record carries enough identity to be projected.
            projection_preview = _upgrade_state_object(
                state_obj, now=datetime.now(UTC)
            )[1]
            upgraded_preview = projection_preview
            shown = "absent" if version_before is None else version_before
            note = (
                f"schema version {shown} → {current_version}"
                if projection_preview
                else (
                    f"state records no run identity, so no projection can be built; "
                    f"it stays at schema version {shown}"
                )
            )
        return MigrationReport(
            plan_dir=str(new_plan_dir),
            from_plan_dir=str(old_plan_dir),
            state_dir=str(new_state_dir),
            from_state_dir=str(old_state_dir),
            applied=False,
            rewritten_fields=rewritten_fields,
            result_artifacts=result_artifacts,
            unmigrated_artifacts_note=unmigrated_note,
            schema_version_before=version_before,
            schema_version_after=(
                current_version if upgraded_preview else version_before
            ),
            schema_upgraded=upgraded_preview,
            projection_written=projection_preview,
            schema_note=note,
            conversation_records=preview_conversations.records,
            conversation_records_rewritten=preview_conversations.rewritten,
            conversation_note=_conversation_note(preview_conversations),
            redirect_requests=redirect_survey.requests,
            redirect_requests_unfinished=redirect_survey.unfinished,
            redirect_note=_redirect_note(redirect_survey, applied=False),
        )

    new_state_dir.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old_state_dir, new_state_dir)

    # Repoint the conversation ledger after the rename, so the rewrite lands in
    # the directory the plan will actually read from. The ledger moved with the
    # directory; only the absolute plan paths inside its records are stale.
    # Doing it before the rename would be worse on failure: a rewritten ledger
    # left in the old directory names the *new* plan, which the identity gate
    # above would then read as foreign and refuse on the operator's retry.
    try:
        relocated_conversations = conversations.relocate_records(
            new_state_dir,
            from_plan_dir=old_plan_dir,
            to_plan_dir=new_plan_dir,
            apply=True,
        )
    except conversations.ConversationError as error:
        raise MigrationError(
            "conversation-ledger-error",
            "state directory was relocated but its conversation ledger could "
            f"not be repointed: {error}",
            {"state_dir": str(new_state_dir), "relocated": True},
        ) from error

    new_state_path = new_state_dir / events.STATE_FILE_NAME
    new_events_path = new_state_dir / events.EVENTS_FILE_NAME
    migrated = _read_json_object_safe(new_state_path)
    if migrated is None:
        migrated = state_obj
    field_targets = {
        "plan_dir": str(new_plan_dir),
        "state_dir": str(new_state_dir),
        "state_path": str(new_state_path),
        "events_path": str(new_events_path),
        "event_path": str(new_events_path),
        "lock_path": str(new_state_dir / LOCK_FILE_NAME),
    }
    for field, value in field_targets.items():
        if field in migrated:
            migrated[field] = value

    # Project after the path rewrite so the projection names where the state
    # now lives, not where it was stranded.
    schema_note: str | None = None
    projection_written = isinstance(
        migrated.get(agent_mux_projection.PROJECTION_KEY), dict
    )
    schema_upgraded = False
    version_after = version_before
    if version_before == current_version:
        schema_note = f"state was already at schema version {current_version}"
    elif not upgradable:
        schema_note = (
            f"state records schema version {migrated.get('version')!r}, which this "
            "build cannot upgrade; it was relocated unchanged"
        )
    else:
        migrated, projection_written = _upgrade_state_object(
            migrated, now=datetime.now(UTC)
        )
        shown = "absent" if version_before is None else version_before
        if projection_written:
            schema_upgraded = True
            version_after = current_version
            schema_note = f"schema version {shown} → {current_version}"
        else:
            schema_note = (
                "state records no run identity, so no projection could be built; "
                f"it stays at schema version {shown}"
            )

    artifacts.atomic_write_bytes(
        new_state_path, _dump_state_bytes(migrated), durable=True
    )

    migrated_details: dict[str, Any] = {
        "from_plan_dir": str(old_plan_dir),
        "to_plan_dir": str(new_plan_dir),
        "from_state_dir": str(old_state_dir),
        "to_state_dir": str(new_state_dir),
    }
    if schema_upgraded:
        migrated_details["from_schema_version"] = version_before
        migrated_details["to_schema_version"] = current_version
        migrated_details["projection_generation"] = migrated[
            agent_mux_projection.PROJECTION_KEY
        ]["projection_generation"]
    if redirect_survey.requests or redirect_survey.unreadable_lines:
        migrated_details["redirect_requests"] = redirect_survey.requests
        migrated_details["redirect_requests_unfinished"] = redirect_survey.unfinished
        migrated_details["redirect_requests_unreadable"] = (
            redirect_survey.unreadable_lines
        )
    if relocated_conversations.records or relocated_conversations.rejected:
        migrated_details["conversation_records"] = relocated_conversations.records
        migrated_details["conversation_records_rewritten"] = (
            relocated_conversations.rewritten
        )
        migrated_details["conversation_records_unreadable"] = (
            relocated_conversations.rejected
        )
    events.append_event(
        new_events_path,
        events.Event(STATE_MIGRATED_EVENT, migrated_details),
    )

    return MigrationReport(
        plan_dir=str(new_plan_dir),
        from_plan_dir=str(old_plan_dir),
        state_dir=str(new_state_dir),
        from_state_dir=str(old_state_dir),
        applied=True,
        rewritten_fields=rewritten_fields,
        result_artifacts=result_artifacts,
        unmigrated_artifacts_note=unmigrated_note,
        schema_version_before=version_before,
        schema_version_after=version_after,
        schema_upgraded=schema_upgraded,
        projection_written=projection_written,
        schema_note=schema_note,
        conversation_records=relocated_conversations.records,
        conversation_records_rewritten=relocated_conversations.rewritten,
        conversation_note=_conversation_note(relocated_conversations),
        redirect_requests=redirect_survey.requests,
        redirect_requests_unfinished=redirect_survey.unfinished,
        redirect_note=_redirect_note(redirect_survey, applied=True),
    )
