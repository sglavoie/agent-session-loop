# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Filesystem-safe resplit execution."""

import resplit_contracts as _contracts
import resplit_parsing as _parsing
import resplit_render as _render
import resplit_shared as _shared

for _module in (_shared, _parsing, _contracts, _render):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def resplit_session(
    plan_dir: str | Path,
    *,
    session_id: str | None = None,
    parts: int | None = None,
    child_specs: list[dict[str, Any]] | None = None,
    state_dir: str | Path | None = None,
    skill_prefix: str = "$",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resplit one pending session and return a JSON-serializable summary."""
    if child_specs is not None:
        child_specs = _normalize_child_specs(child_specs)
        parts = _validate_parts(len(child_specs))
    elif parts is not None:
        parts = _validate_parts(parts)

    snapshot = plans_snapshot.inspect_plan(plan_dir)
    parent_id = _select_parent(snapshot, session_id)
    parent_status = _status_by_id(snapshot).get(parent_id.value)
    if parent_status is None:
        raise ResplitError(
            "ambiguous-input", f"session {parent_id.value} does not exist"
        )

    parent_expansion_path = snapshot.plan_dir / parent_id.expansion_filename
    parent_details = _parse_parent_details(parent_status, parent_expansion_path)
    if parts is None:
        parts = _infer_parts(parent_details.body)
    parts = _validate_parts(parts)

    _validate_snapshot(snapshot, parent_id, parts)

    lock_acquired = False
    try:
        state_locks.acquire_lock(snapshot.plan_dir, state_dir, owner="session-resplit")
        lock_acquired = True
    except state.StateError as error:
        raise ResplitError("lock-contention", str(error)) from error

    try:
        timestamp = now or datetime.now(UTC)
        locked_snapshot = plans_snapshot.inspect_plan(snapshot.plan_dir)
        locked_parent_status = _validate_snapshot(locked_snapshot, parent_id, parts)
        parent_details = _parse_parent_details(
            locked_parent_status,
            locked_snapshot.plan_dir / parent_id.expansion_filename,
        )
        first_previous_session_file = (
            parent_details.prior_session_file
            or _preceding_session_file(locked_snapshot, parent_id)
        )
        children = _build_child_specs(
            parent_id,
            parent_details,
            parts,
            child_specs,
            first_previous_session_file,
        )

        archive_dir = authoring_transaction.create_archive_dir(
            locked_snapshot.plan_dir, parent_id, timestamp
        )
        archive_relative_path = archive_dir.relative_to(
            locked_snapshot.plan_dir
        ).as_posix()

        # Build every staged write before mutating anything on disk.
        transaction = authoring_transaction.PlanTransaction(locked_snapshot.plan_dir)
        for child in children:
            transaction.stage(
                child.filename,
                _render_child_session(
                    parent_id,
                    child,
                    archive_relative_path,
                    parent_details,
                    skill_prefix,
                ),
            )

        reference_updates: list[str] = []
        next_session = _next_session_after(locked_snapshot, parent_id)
        if next_session is not None:
            next_text, next_changed = (
                authoring_manifest.replace_previous_session_reference(
                    _read_text(next_session.path),
                    parent_id,
                    children[-1].session_id,
                )
            )
            if next_changed:
                transaction.stage(next_session.session.filename, next_text)
                reference_updates.append(next_session.session.filename)

        total_sessions = len(locked_snapshot.sessions) - 1 + len(children)
        ledger_updates: list[str] = []
        manifest_text, manifest_changed = _updated_manifest_text(
            locked_snapshot.manifest_path,
            parent_id,
            children,
            total_sessions,
            archive_relative_path,
        )
        if manifest_changed:
            transaction.stage("manifest.md", manifest_text)
            ledger_updates.append("manifest.md")
        context_text, context_changed = _updated_context_text(
            locked_snapshot.context_path, parent_id, children
        )
        if context_changed:
            transaction.stage("context.md", context_text)
            ledger_updates.append("context.md")

        archived_files, archive_moves = _archive_parent_files(
            [
                locked_parent_status.path,
                locked_snapshot.plan_dir / parent_id.expansion_filename,
            ],
            archive_dir,
        )

        try:
            transaction.commit()
        except plans_errors.PlanError:
            _rollback_archive(archive_moves, archive_dir)
            raise

        readiness = authoring_transaction.refresh_readiness_metadata(
            locked_snapshot.plan_dir, state="draft", now=timestamp
        )

        return {
            "ok": True,
            "plan_dir": str(locked_snapshot.plan_dir),
            "parent_id": parent_id.value,
            "children": [child.session_id.value for child in children],
            "child_files": [child.filename for child in children],
            "archive_dir": archive_relative_path,
            "archived_files": archived_files,
            "ledger_updates": ledger_updates,
            "reference_updates": reference_updates,
            "readiness": {
                "state": readiness.state,
                "readiness_sha256": readiness.readiness_sha256,
                "checked_at": readiness.checked_at,
            },
            "next_command": f"{skill_prefix}session-expand {locked_snapshot.plan_dir}/",
            "next_steps": _next_steps(str(locked_snapshot.plan_dir)),
        }
    finally:
        if lock_acquired:
            state_locks.release_lock(snapshot.plan_dir, state_dir)
