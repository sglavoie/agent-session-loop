# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Child session rendering and plan-file transaction helpers."""

import resplit_contracts as _contracts
import resplit_parsing as _parsing
import resplit_shared as _shared

for _module in (_shared, _parsing, _contracts):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
    )


def _child_body_lines(
    parent_id: plans_session_ids.SessionId,
    child: ChildSpec,
    archive_relative_path: str,
    parent_details: ParentDetails,
    skill_prefix: str,
) -> list[str]:
    lines = [
        "## Lineage",
        "",
        f"- **Split from**: `session-{parent_id.value}.md`",
        f"- **Parent title**: {parent_details.title}",
        f"- **Child session**: {child.session_id.value}",
        f"- **Parent archive**: `{archive_relative_path}`",
    ]
    if parent_details.had_expansion:
        lines.append(
            "- **Archived parent expansion**: historical context only; rerun "
            f"`{skill_prefix}session-expand` for current facts."
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## Parent Scope",
            "",
            "- Full parent instructions are archived at "
            f"`{archive_relative_path}/session-{parent_id.value}.md`.",
            "- Preserve the archived parent implementation intent while completing "
            "only this child scope.",
        ]
    )
    if parent_details.had_expansion:
        lines.append(
            "- Archived parent expansion notes are at "
            f"`{archive_relative_path}/{parent_id.expansion_filename}`; use them "
            f"as historical context until `{skill_prefix}session-expand` regenerates "
            "current notes."
        )

    lines.extend(
        [
            "",
            "## Child Scope",
            "",
            *child.instructions.strip().splitlines(),
        ]
    )
    return lines


def _render_child_session(
    parent_id: plans_session_ids.SessionId,
    child: ChildSpec,
    archive_relative_path: str,
    parent_details: ParentDetails,
    skill_prefix: str,
) -> str:
    return authoring_render.render_session_file(
        session_id=child.session_id,
        title=child.title,
        instruction_lines=_child_body_lines(
            parent_id, child, archive_relative_path, parent_details, skill_prefix
        ),
        is_first=False,
        previous_session_id=child.previous_session_id,
        contract=child.contract,
        scope=parent_details.scope,
        repository_registry=parent_details.registry,
    )


def _preceding_session_file(
    snapshot: plans_models.PlanSnapshot,
    parent_id: plans_session_ids.SessionId,
) -> str | None:
    previous: plans_models.SessionStatus | None = None
    for session in snapshot.sessions:
        if session.session_id == parent_id:
            return previous.session.filename if previous is not None else None
        previous = session
    return None


def _next_session_after(
    snapshot: plans_models.PlanSnapshot,
    parent_id: plans_session_ids.SessionId,
) -> plans_models.SessionStatus | None:
    for index, session in enumerate(snapshot.sessions):
        if session.session_id != parent_id:
            continue
        if index + 1 >= len(snapshot.sessions):
            return None
        return snapshot.sessions[index + 1]
    return None


def _append_resplit_notes(
    text: str,
    parent_id: plans_session_ids.SessionId,
    children: list[ChildSpec],
    archive_relative_path: str,
) -> str:
    child_files = ", ".join(child.filename for child in children)
    note = "\n".join(
        [
            "",
            "## Resplit Notes",
            "",
            f"- `session-{parent_id.value}.md` was split into {child_files}.",
            f"- Parent files were archived under `{archive_relative_path}`.",
            "- Rerun `session-expand` before continuing execution so expansion "
            "notes match the nested sessions.",
            "",
        ]
    )
    return text.rstrip() + "\n" + note


def _manifest_replacements(
    children: list[ChildSpec],
) -> tuple[authoring_manifest.ManifestSession, ...]:
    return tuple(
        authoring_manifest.ManifestSession(
            session_id=child.session_id, title=child.title, status="pending"
        )
        for child in children
    )


def _updated_manifest_text(
    manifest_path: Path,
    parent_id: plans_session_ids.SessionId,
    children: list[ChildSpec],
    total_sessions: int,
    archive_relative_path: str,
) -> tuple[str, bool]:
    text = _read_text(manifest_path)
    updated = authoring_manifest.replace_manifest_sessions_count(text, total_sessions)
    updated, tables_changed = authoring_manifest.replace_session_status_rows(
        updated, parent_id, _manifest_replacements(children)
    )
    updated = _append_resplit_notes(updated, parent_id, children, archive_relative_path)
    return updated, updated != text or tables_changed


def _updated_context_text(
    context_path: Path,
    parent_id: plans_session_ids.SessionId,
    children: list[ChildSpec],
) -> tuple[str, bool]:
    text = _read_text(context_path)
    return authoring_manifest.replace_session_status_rows(
        text, parent_id, _manifest_replacements(children)
    )


def _archive_parent_files(
    paths: list[Path], archive_dir: Path
) -> tuple[list[str], list[tuple[Path, Path]]]:
    archived_files: list[str] = []
    moves: list[tuple[Path, Path]] = []
    for path in paths:
        if not path.exists():
            continue
        destination = archive_dir / path.name
        path.rename(destination)
        archived_files.append(path.name)
        moves.append((path, destination))
    return archived_files, moves


def _rollback_archive(moves: list[tuple[Path, Path]], archive_dir: Path) -> None:
    for original, archived in moves:
        with contextlib.suppress(OSError):
            archived.rename(original)
    for directory in (archive_dir, archive_dir.parent):
        with contextlib.suppress(OSError):
            directory.rmdir()
