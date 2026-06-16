#!/usr/bin/env python3
"""Resplit one pending session into nested child sessions.

This helper performs the filesystem-safe parts of the session-resplit skill:
plan inspection, target validation, lock acquisition, archival, child file
creation, and derived ledger updates. The skill may provide child specs for
better semantic boundaries; otherwise the helper falls back to deterministic
phase-based child instructions.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_session_loop import plans, state  # noqa: E402


MIN_PARTS = 2
MAX_PARTS = 20
HANDOFF_HEADING = "## Handoff"
ARCHIVE_ROOT_NAME = "resplit-archive"


class ResplitError(Exception):
    """A recoverable resplit refusal with a stable reason."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ChildSpec:
    """One generated child session."""

    session_id: plans.SessionId
    title: str
    instructions: str
    required_prior_sessions: str
    previous_session_file: str | None
    owned_write_paths: str

    @property
    def filename(self) -> str:
        return self.session_id.filename


@dataclass(frozen=True)
class ParentDetails:
    """Parsed parent session content needed for child generation."""

    title: str
    body: str
    metadata: dict[str, str]
    had_expansion: bool
    prior_session_file: str | None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _write_text_exclusive(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as output:
        output.write(text)


def _session_sort_key(session_id: plans.SessionId) -> tuple[int, ...]:
    return session_id.sort_key


def _is_descendant(candidate: plans.SessionId, parent: plans.SessionId) -> bool:
    return (
        len(candidate.components) > len(parent.components)
        and candidate.components[: len(parent.components)] == parent.components
    )


def _child_id(parent: plans.SessionId, index: int) -> plans.SessionId:
    return plans.SessionId(parent.components + (index,))


def _validate_parts(value: int) -> int:
    if value < MIN_PARTS or value > MAX_PARTS:
        raise ResplitError(
            "ambiguous-input",
            f"child count must be between {MIN_PARTS} and {MAX_PARTS}: {value}",
        )
    return value


def _status_by_id(snapshot: plans.PlanSnapshot) -> dict[str, plans.SessionStatus]:
    return {session.id: session for session in snapshot.sessions}


def _select_parent(
    snapshot: plans.PlanSnapshot,
    requested_session_id: str | None,
) -> plans.SessionId:
    if requested_session_id is not None:
        try:
            return plans.parse_session_id(requested_session_id)
        except ValueError as error:
            raise ResplitError(
                "ambiguous-input", f"invalid session id: {requested_session_id}"
            ) from error

    next_pending = snapshot.next_pending
    if next_pending is None:
        raise ResplitError("blocked", "no pending session is available to resplit")
    return next_pending.session_id


def _validate_snapshot(
    snapshot: plans.PlanSnapshot,
    parent_id: plans.SessionId,
    parts: int,
    parallel_validation_text: str | None,
) -> plans.SessionStatus:
    for session in snapshot.sessions:
        if session.status == "unknown":
            raise ResplitError(
                "unknown-status",
                f"{session.session.filename} has unknown status {session.raw_value!r}",
            )
        if session.status == "empty":
            raise ResplitError(
                "malformed-plan",
                f"{session.session.filename} is missing a populated final handoff status",
            )

    parent_status = _status_by_id(snapshot).get(parent_id.value)
    if parent_status is None:
        raise ResplitError("ambiguous-input", f"session {parent_id.value} does not exist")
    if parent_status.status == "complete":
        raise ResplitError(
            "blocked", f"session {parent_id.value} is already complete"
        )
    if parent_status.status != "pending":
        raise ResplitError(
            "unknown-status",
            f"session {parent_id.value} has status {parent_status.raw_value!r}",
        )

    for session in snapshot.sessions:
        if _is_descendant(session.session_id, parent_id):
            raise ResplitError(
                "collision",
                f"descendant session already exists: {session.session.filename}",
            )

    for index in range(1, parts + 1):
        child = _child_id(parent_id, index)
        child_path = snapshot.plan_dir / child.filename
        child_expansion_path = snapshot.plan_dir / child.expansion_filename
        if child_path.exists():
            raise ResplitError("collision", f"child file already exists: {child.filename}")
        if child_expansion_path.exists():
            raise ResplitError(
                "collision",
                f"child expansion file already exists: {child.expansion_filename}",
            )

    if parallel_validation_text is not None:
        _validate_parallel_status(parent_id, parallel_validation_text)

    return parent_status


def _validate_parallel_status(parent_id: plans.SessionId, text: str) -> None:
    section = _parallel_validation_section(parent_id, text)
    if section is None:
        return

    marker_labels = {
        "validated classification",
        "run condition",
        "session-run markers",
    }
    for line in section.splitlines():
        match = re.match(r"^-\s+\*\*(.+?)\*\*:\s*(.*?)\s*$", line)
        if match is None:
            continue
        label = match.group(1).strip().lower()
        if label not in marker_labels:
            continue
        value = match.group(2).strip().lower()
        blockers = (
            "unsafe to run now",
            "ownership-conflicted",
            "ownership conflicted",
        )
        blocker = next((item for item in blockers if item in value), None)
        if blocker is None and re.search(r"(?<!not\s)\bblocked\b", value):
            blocker = "blocked"
        if blocker is not None:
            raise ResplitError(
                "blocked",
                f"parallel-validation.md marks session {parent_id.value} as {blocker}",
            )


def _parallel_validation_section(parent_id: plans.SessionId, text: str) -> str | None:
    pattern = re.compile(
        rf"^###\s+Session\s+{re.escape(parent_id.value)}(?::|\b).*$",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if match is None:
        return None

    next_match = re.search(r"^###\s+Session\s+\S+.*$", text[match.end() :], re.MULTILINE)
    if next_match is None:
        return text[match.start() :]
    return text[match.start() : match.end() + next_match.start()]


def _split_before_final_handoff(text: str, filename: str) -> str:
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return "\n".join(lines[:index]).rstrip() + "\n"
    raise ResplitError("malformed-plan", f"{filename} has no final handoff block")


def _extract_parent_title(parent_id: plans.SessionId, before_handoff: str) -> str:
    for line in before_handoff.splitlines():
        match = re.match(r"^#\s+Session\s+\S+\s*:\s*(.+?)\s*$", line)
        if match:
            return match.group(1)
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            title = match.group(1)
            return re.sub(rf"^Session\s+{re.escape(parent_id.value)}\s*:?\s*", "", title)
    return f"Session {parent_id.value}"


def _extract_metadata(before_handoff: str) -> dict[str, str]:
    lines = before_handoff.splitlines()
    metadata: dict[str, str] = {}
    in_metadata = False

    for line in lines:
        if line.strip() == "## Parallel Metadata":
            in_metadata = True
            continue
        if not in_metadata:
            continue
        if line.strip() == "---":
            break
        if line.startswith("## "):
            break
        match = re.match(r"^-\s+\*\*(.+?)\*\*:\s*(.*)\s*$", line)
        if match:
            metadata[match.group(1).strip().lower()] = match.group(2).strip()

    return metadata


def _extract_previous_session_file(before_handoff: str) -> str | None:
    match = re.search(
        r"> \*\*Previous session\*\*: Read the \*\*Handoff\*\* section "
        r"at the bottom of `([^`]+)`\.",
        before_handoff,
    )
    return match.group(1) if match else None


def _extract_instruction_body(before_handoff: str) -> str:
    lines = before_handoff.splitlines()
    output: list[str] = []
    index = 0

    if index < len(lines) and lines[index].startswith("# "):
        index += 1
    while index < len(lines) and (
        not lines[index].strip() or lines[index].lstrip().startswith(">")
    ):
        index += 1

    while index < len(lines):
        line = lines[index]
        if line.strip() == "## Parallel Metadata":
            index += 1
            while index < len(lines) and lines[index].strip() != "---":
                index += 1
            if index < len(lines) and lines[index].strip() == "---":
                index += 1
            continue
        output.append(line)
        index += 1

    return _strip_horizontal_rules("\n".join(output).strip())


def _strip_horizontal_rules(text: str) -> str:
    lines = text.splitlines()
    while lines and lines[0].strip() == "---":
        lines.pop(0)
    while lines and lines[-1].strip() == "---":
        lines.pop()
    return "\n".join(lines).strip()


def _parse_parent_details(
    parent_status: plans.SessionStatus,
    parent_expansion_path: Path,
) -> ParentDetails:
    parent_text = _read_text(parent_status.path)
    before_handoff = _split_before_final_handoff(
        parent_text, parent_status.session.filename
    )
    body = _extract_instruction_body(before_handoff)
    if not body:
        raise ResplitError(
            "cannot-split",
            f"{parent_status.session.filename} has no implementation instructions above handoff",
        )

    return ParentDetails(
        title=_extract_parent_title(parent_status.session_id, before_handoff),
        body=body,
        metadata=_extract_metadata(before_handoff),
        had_expansion=parent_expansion_path.exists(),
        prior_session_file=_extract_previous_session_file(before_handoff),
    )


def _infer_parts(body: str) -> int:
    semantic_units = _semantic_units(body)
    if len(semantic_units) >= 4:
        return min(5, len(semantic_units))
    if len(semantic_units) >= 2:
        return min(3, len(semantic_units))

    paragraphs = [part for part in re.split(r"\n\s*\n", body.strip()) if part.strip()]
    if len(paragraphs) >= 4:
        return 3
    if len(paragraphs) >= 2:
        return 2

    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    if len([sentence for sentence in sentences if sentence.strip()]) >= 4:
        return 3
    return 2


def _semantic_units(body: str) -> list[str]:
    sections = _section_units(body)
    if len(sections) >= 2:
        return sections

    list_items = _list_units(body)
    if len(list_items) >= 2:
        return list_items

    deliverables = _deliverable_units(body)
    if len(deliverables) >= 2:
        return deliverables

    return []


def _section_units(body: str) -> list[str]:
    lines = body.splitlines()
    units: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if re.match(r"^#{2,6}\s+\S", line) and current:
            units.append(current)
            current = [line]
            continue
        current.append(line)

    if current:
        units.append(current)

    if len(units) == 1 and not re.match(r"^#{2,6}\s+\S", units[0][0]):
        return []
    return ["\n".join(unit).strip() for unit in units if "\n".join(unit).strip()]


def _list_units(body: str) -> list[str]:
    units: list[str] = []
    current: list[str] = []
    in_code_fence = False

    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
        is_item = (
            not in_code_fence
            and re.match(r"^\s*(?:[-*+]|\d+[.)])\s+\S", line) is not None
        )
        if is_item:
            if current:
                units.append("\n".join(current).strip())
            current = [line.strip()]
            continue
        if current and (line.startswith((" ", "\t")) or not line.strip()):
            current.append(line.rstrip())
            continue
        if current:
            units.append("\n".join(current).strip())
            current = []

    if current:
        units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _deliverable_units(body: str) -> list[str]:
    paragraphs = [
        re.sub(r"\s+", " ", part).strip()
        for part in re.split(r"\n\s*\n", body.strip())
        if part.strip()
    ]
    for paragraph in paragraphs:
        if "```" in paragraph or ":" not in paragraph:
            continue
        preamble, tail = paragraph.split(":", 1)
        parts = _series_parts(tail)
        if len(parts) < 2:
            continue
        preamble = preamble.strip().rstrip(".")
        return [
            "\n\n".join(
                segment
                for segment in (
                    f"Focus deliverable: {part.rstrip('.')}.",
                    f"Parent boundary goal: {preamble}." if preamble else "",
                )
                if segment
            )
            for part in parts
        ]
    return []


def _series_parts(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    normalized = re.sub(r",?\s+and\s+", ", ", normalized)
    return [part.strip(" .;") for part in normalized.split(",") if part.strip(" .;")]


def _balanced_groups(units: list[str], parts: int) -> list[str]:
    groups: list[list[str]] = [[] for _ in range(parts)]
    for index, unit in enumerate(units):
        group_index = min(parts - 1, index * parts // len(units))
        groups[group_index].append(unit)
    return ["\n\n".join(group).strip() for group in groups]


def _phase_instruction(parent_body: str, index: int, parts: int) -> str:
    focus_by_parts = {
        2: (
            "Inspect the target state, implement the setup and safety checks, "
            "and leave a clear handoff for the remaining behavior.",
            "Complete the remaining implementation, update derived metadata, "
            "verify behavior, and record the final handoff.",
        ),
        3: (
            "Implement input parsing, plan inspection, validation, and refusal behavior.",
            "Implement the core file mutations, including locking, archival, "
            "and child-session generation.",
            "Implement derived ledger updates, stale artifact handling, reporting, "
            "and verification.",
        ),
        4: (
            "Implement input parsing, target inference, and plan inspection.",
            "Implement safety validation and lock handling.",
            "Implement archival and child-session generation.",
            "Implement ledger updates, stale artifact handling, reporting, and verification.",
        ),
        5: (
            "Implement input parsing and target inference.",
            "Implement plan inspection and safety validation.",
            "Implement lock handling and parent archival.",
            "Implement child-session generation.",
            "Implement ledger updates, stale artifact handling, reporting, and verification.",
        ),
    }
    focus = focus_by_parts.get(parts)
    if focus is None:
        focus_text = (
            f"Implement deterministic phase {index} of {parts} from the "
            "archived parent instructions."
        )
    else:
        focus_text = focus[index - 1]

    return "\n".join(
        [
            f"Implement part {index} of {parts} from the archived parent session.",
            "",
            "Parent implementation intent:",
            "",
            parent_body,
            "",
            f"Focus for this child: {focus_text}",
        ]
    )


def _load_child_specs(path: Path | None) -> list[dict[str, str]] | None:
    if path is None:
        return None
    raw = json.loads(_read_text(path))
    children = raw.get("children") if isinstance(raw, dict) else raw
    if not isinstance(children, list):
        raise ResplitError("ambiguous-input", "child spec must be a list or object with children")

    specs: list[dict[str, str]] = []
    for index, item in enumerate(children, start=1):
        if not isinstance(item, dict):
            raise ResplitError(
                "ambiguous-input", f"child spec {index} must be a JSON object"
            )
        title = str(item.get("title", "")).strip()
        instructions = str(item.get("instructions", "")).strip()
        if not title or not instructions:
            raise ResplitError(
                "ambiguous-input",
                f"child spec {index} requires nonempty title and instructions",
            )
        specs.append(
            {
                "title": title,
                "instructions": instructions,
                "owned_write_paths": str(item.get("owned_write_paths", "")).strip(),
            }
        )
    _validate_parts(len(specs))
    return specs


def _build_child_specs(
    parent_id: plans.SessionId,
    parent_details: ParentDetails,
    parts: int,
    explicit_specs: list[dict[str, str]] | None,
    first_previous_session_file: str | None,
) -> list[ChildSpec]:
    parent_required = parent_details.metadata.get("required prior sessions", "none")
    parent_owned = parent_details.metadata.get(
        "owned write paths", "unknown - inherited from parent"
    )

    if explicit_specs is None:
        semantic_units = _semantic_units(parent_details.body)
        if len(semantic_units) >= parts:
            instruction_groups = _balanced_groups(semantic_units, parts)
        else:
            paragraphs = [
                part.strip()
                for part in re.split(r"\n\s*\n", parent_details.body.strip())
                if part.strip()
            ]
            if len(paragraphs) >= parts:
                instruction_groups = _balanced_groups(paragraphs, parts)
            else:
                instruction_groups = [
                    _phase_instruction(parent_details.body, index, parts)
                    for index in range(1, parts + 1)
                ]
        raw_specs = [
            {
                "title": _generated_child_title(
                    parent_details.title, index, instruction_groups[index - 1]
                ),
                "instructions": instruction_groups[index - 1],
                "owned_write_paths": parent_owned,
            }
            for index in range(1, parts + 1)
        ]
    else:
        raw_specs = explicit_specs

    children: list[ChildSpec] = []
    for index, raw in enumerate(raw_specs, start=1):
        child_session_id = _child_id(parent_id, index)
        previous_file = (
            first_previous_session_file
            if index == 1
            else _child_id(parent_id, index - 1).filename
        )
        required = (
            parent_required
            if index == 1
            else f"{_child_id(parent_id, index - 1).value} (generated child sequence)"
        )
        children.append(
            ChildSpec(
                session_id=child_session_id,
                title=raw["title"],
                instructions=raw["instructions"],
                required_prior_sessions=required or "none",
                previous_session_file=previous_file,
                owned_write_paths=raw.get("owned_write_paths") or parent_owned,
            )
        )
    return children


def _generated_child_title(parent_title: str, index: int, instructions: str) -> str:
    focus = _instruction_summary(instructions)
    if focus is None:
        return f"{parent_title}, Part {index}"
    return f"{parent_title}: {focus}"


def _instruction_summary(instructions: str) -> str | None:
    for raw_line in instructions.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        line = re.sub(r"^\s*Focus deliverable:\s*", "", line, flags=re.IGNORECASE)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\s+", " ", line).strip(" .:-")
        if not line:
            continue
        words = line.split()
        if len(words) > 10:
            words = words[:10]
            while words and words[-1].lower() in {"and", "or", "the", "a", "an"}:
                words.pop()
            line = " ".join(words)
        return line[:1].upper() + line[1:]
    return None


def _render_child_session(
    parent_id: plans.SessionId,
    child: ChildSpec,
    archive_relative_path: str,
    parent_details: ParentDetails,
    skill_prefix: str,
) -> str:
    forbidden = parent_details.metadata.get(
        "forbidden/shared paths", "Inherited from parent session; coordinator ownership required"
    )
    serial_blocker = (
        f"Generated child of `session-{parent_id.value}.md`; run serially until "
        "expansion validates narrower ownership."
    )
    lines = [
        f"# Session {child.session_id.value}: {child.title}",
        "",
        "> **Before starting**: Read `context.md` in this directory for shared project context.",
    ]
    if child.previous_session_file is not None:
        lines.append(
            "> **Previous session**: Read the **Handoff** section at the "
            f"bottom of `{child.previous_session_file}`."
        )

    lines.extend(
        [
            "",
            "## Lineage",
            "",
            f"- **Split from**: `session-{parent_id.value}.md`",
            f"- **Parent title**: {parent_details.title}",
            f"- **Child session**: {child.session_id.value}",
            f"- **Parent archive**: `{archive_relative_path}`",
        ]
    )
    if parent_details.had_expansion:
        lines.append(
            "- **Archived parent expansion**: historical context only; rerun "
            f"`{skill_prefix}session-expand` for current facts."
        )

    lines.extend(
        [
            "",
            "## Parallel Metadata",
            "",
            "- **Safety classification**: `serial-required`",
            f"- **Required prior sessions**: {child.required_prior_sessions}",
            "- **Allowed concurrent sessions**: none",
            f"- **Owned write paths**: {child.owned_write_paths}",
            f"- **Forbidden/shared paths**: {forbidden}",
            f"- **Serial blockers**: {serial_blocker}",
            "- **Optional internal tracks**: none",
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
            child.instructions.strip(),
            "",
            "## Handoff",
            "",
            "<!-- Fill this section when the session is complete -->",
            "",
            "- **Status**: pending",
            "- **Files modified**: (none yet)",
            "- **Key decisions**: (none yet)",
            "- **Blockers for next session**: (none)",
            "- **Notes**:",
            "",
        ]
    )
    return "\n".join(lines)


def _preceding_session_file(
    snapshot: plans.PlanSnapshot,
    parent_id: plans.SessionId,
) -> str | None:
    previous: plans.SessionStatus | None = None
    for session in snapshot.sessions:
        if session.session_id == parent_id:
            return previous.session.filename if previous is not None else None
        previous = session
    return None


def _next_session_after(
    snapshot: plans.PlanSnapshot,
    parent_id: plans.SessionId,
) -> plans.SessionStatus | None:
    for index, session in enumerate(snapshot.sessions):
        if session.session_id != parent_id:
            continue
        if index + 1 >= len(snapshot.sessions):
            return None
        return snapshot.sessions[index + 1]
    return None


def _replace_previous_session_reference(
    text: str,
    parent_id: plans.SessionId,
    replacement_id: plans.SessionId,
) -> tuple[str, bool]:
    pattern = re.compile(
        r"(?m)^> \*\*Previous session\*\*: Read the \*\*Handoff\*\* "
        r"section at the bottom of `"
        + re.escape(parent_id.filename)
        + r"`\.$"
    )
    replacement = (
        "> **Previous session**: Read the **Handoff** section at the bottom "
        f"of `{replacement_id.filename}`."
    )
    updated = pattern.sub(replacement, text)
    return updated, updated != text


def _replace_required_prior_metadata(
    text: str,
    parent_id: plans.SessionId,
    replacement_id: plans.SessionId,
) -> tuple[str, bool]:
    lines = text.splitlines()
    changed = False

    for index, line in enumerate(lines):
        match = re.match(r"^(-\s+\*\*Required prior sessions\*\*:\s*)(.*)$", line)
        if match is None:
            continue
        required, required_changed = _replace_session_id_mentions(
            match.group(2),
            parent_id,
            replacement_id,
        )
        if required_changed:
            lines[index] = f"{match.group(1)}{required.strip()}"
            changed = True

    if not changed:
        return text, False
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(lines) + suffix, True


def _repair_next_session_references(
    snapshot: plans.PlanSnapshot,
    parent_id: plans.SessionId,
    children: list[ChildSpec],
) -> list[str]:
    next_session = _next_session_after(snapshot, parent_id)
    if next_session is None:
        return []

    replacement_id = children[-1].session_id
    text = _read_text(next_session.path)
    updated, previous_changed = _replace_previous_session_reference(
        text,
        parent_id,
        replacement_id,
    )
    updated, required_changed = _replace_required_prior_metadata(
        updated,
        parent_id,
        replacement_id,
    )
    if not previous_changed and not required_changed:
        return []

    _write_text(next_session.path, updated)
    return [next_session.session.filename]


def _create_archive_dir(plan_dir: Path, parent_id: plans.SessionId, now: datetime) -> Path:
    archive_root = plan_dir / ARCHIVE_ROOT_NAME
    archive_root.mkdir(exist_ok=True)
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = archive_root / f"session-{parent_id.value}-{timestamp}"
    candidate = base
    suffix = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            candidate = archive_root / f"session-{parent_id.value}-{timestamp}-{suffix}"
            suffix += 1


def _archive_file(path: Path, archive_dir: Path) -> str | None:
    if not path.exists():
        return None
    destination = archive_dir / path.name
    path.rename(destination)
    return path.name


def _split_table_row(line: str) -> tuple[str, list[str]] | None:
    if not line.lstrip().startswith("|"):
        return None
    leading = line[: len(line) - len(line.lstrip())]
    return leading, line.strip().strip("|").split("|")


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def _normalize_header(value: str) -> str:
    cleaned = value.strip().strip("`").lower()
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned)


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _session_id_from_cell(value: str) -> plans.SessionId | None:
    text = value.strip().strip("`")
    match = re.fullmatch(
        rf"(?:session[\s-]*|s)?({plans.SESSION_ID_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return plans.parse_session_id(match.group(1))


def _cell_mentions_session(value: str, session_id: plans.SessionId) -> bool:
    ids = re.findall(rf"(?<![\d.])({plans.SESSION_ID_PATTERN})(?![\d.])", value)
    return session_id.value in ids


def _replace_session_id_mentions(
    value: str,
    parent_id: plans.SessionId,
    replacement_id: plans.SessionId,
) -> tuple[str, bool]:
    pattern = re.compile(
        rf"(?<![\d.])(?:session[\s-]*|s)?{re.escape(parent_id.value)}(?![\d.])",
        re.IGNORECASE,
    )
    updated = pattern.sub(replacement_id.value, value)
    return updated, updated != value


def _find_header(headers: list[str], names: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in names:
            return index
    return None


def _table_block_type(headers: list[str]) -> str | None:
    session_headers = {"#", "session", "session #", "session number", "session id", "id"}
    if _find_header(headers, session_headers) is not None:
        if _find_header(headers, {"status"}) is not None:
            return "status"
        if _find_header(headers, {"classification"}) is not None:
            return "ownership"
    has_wave = _find_header(headers, {"wave"}) is not None
    has_sessions = _find_header(headers, {"sessions"}) is not None
    if has_wave and has_sessions:
        return "wave"
    return None


def _replace_status_table_parent(
    block: list[str],
    parent_id: plans.SessionId,
    children: list[ChildSpec],
) -> tuple[list[str], bool]:
    parsed_header = _split_table_row(block[0])
    assert parsed_header is not None
    headers = [_normalize_header(cell) for cell in parsed_header[1]]
    session_index = _find_header(
        headers, {"#", "session", "session #", "session number", "session id", "id"}
    )
    status_index = _find_header(headers, {"status"})
    title_index = _find_header(headers, {"title"})
    file_index = _find_header(headers, {"file"})
    if session_index is None or status_index is None:
        return block, False

    changed = False
    updated = block[:2]
    for row in block[2:]:
        parsed = _split_table_row(row)
        if parsed is None:
            updated.append(row)
            continue
        leading, cells = parsed
        if max(session_index, status_index) >= len(cells):
            updated.append(row)
            continue
        row_session_id = _session_id_from_cell(cells[session_index])
        if row_session_id != parent_id:
            updated.append(row)
            continue

        for child in children:
            child_cells = list(cells)
            child_cells[session_index] = _replace_cell_content(
                child_cells[session_index], child.session_id.value
            )
            child_cells[status_index] = _replace_cell_content(
                child_cells[status_index], "pending"
            )
            if title_index is not None and title_index < len(child_cells):
                child_cells[title_index] = _replace_cell_content(
                    child_cells[title_index], child.title
                )
            if file_index is not None and file_index < len(child_cells):
                child_cells[file_index] = _replace_cell_content(
                    child_cells[file_index], child.filename
                )
            updated.append(_join_table_row(leading, child_cells))
        changed = True
    return updated, changed


def _replace_wave_table_parent(
    block: list[str],
    parent_id: plans.SessionId,
    children: list[ChildSpec],
) -> tuple[list[str], bool]:
    parsed_header = _split_table_row(block[0])
    assert parsed_header is not None
    headers = [_normalize_header(cell) for cell in parsed_header[1]]
    wave_index = _find_header(headers, {"wave"})
    sessions_index = _find_header(headers, {"sessions"})
    safe_index = _find_header(headers, {"safe concurrent set"})
    notes_index = _find_header(headers, {"notes"})
    if wave_index is None or sessions_index is None:
        return block, False

    changed = False
    updated = block[:2]
    for row in block[2:]:
        parsed = _split_table_row(row)
        if parsed is None:
            updated.append(row)
            continue
        leading, cells = parsed
        if max(wave_index, sessions_index) >= len(cells):
            updated.append(row)
            continue
        if not _cell_mentions_session(cells[sessions_index], parent_id):
            updated.append(row)
            continue

        for child in children:
            child_cells = list(cells)
            child_cells[wave_index] = _replace_cell_content(
                child_cells[wave_index], child.session_id.value
            )
            child_cells[sessions_index] = _replace_cell_content(
                child_cells[sessions_index], child.session_id.value
            )
            if safe_index is not None and safe_index < len(child_cells):
                child_cells[safe_index] = _replace_cell_content(
                    child_cells[safe_index], "none"
                )
            if notes_index is not None and notes_index < len(child_cells):
                note = (
                    f"Generated child of session {parent_id.value}; "
                    "serial-required until expanded and validated"
                )
                child_cells[notes_index] = _replace_cell_content(
                    child_cells[notes_index],
                    note,
                )
            updated.append(_join_table_row(leading, child_cells))
        changed = True
    return updated, changed


def _replace_ownership_table_parent(
    block: list[str],
    parent_id: plans.SessionId,
    children: list[ChildSpec],
) -> tuple[list[str], bool]:
    parsed_header = _split_table_row(block[0])
    assert parsed_header is not None
    headers = [_normalize_header(cell) for cell in parsed_header[1]]
    session_index = _find_header(
        headers, {"session", "session #", "session number", "session id", "id"}
    )
    classification_index = _find_header(headers, {"classification"})
    owned_index = _find_header(headers, {"owned write paths"})
    required_index = _find_header(headers, {"required prior sessions"})
    allowed_index = _find_header(headers, {"allowed concurrent sessions"})
    blockers_index = _find_header(headers, {"serial blockers"})
    if session_index is None or classification_index is None:
        return block, False

    changed = False
    updated = block[:2]
    for row in block[2:]:
        parsed = _split_table_row(row)
        if parsed is None:
            updated.append(row)
            continue
        leading, cells = parsed
        if max(session_index, classification_index) >= len(cells):
            updated.append(row)
            continue
        row_session_id = _session_id_from_cell(cells[session_index])
        if row_session_id != parent_id:
            if (
                row_session_id is not None
                and row_session_id.sort_key > parent_id.sort_key
                and required_index is not None
                and required_index < len(cells)
            ):
                replacement = children[-1].session_id
                required, required_changed = _replace_session_id_mentions(
                    cells[required_index],
                    parent_id,
                    replacement,
                )
                if required_changed:
                    updated_cells = list(cells)
                    updated_cells[required_index] = _replace_cell_content(
                        updated_cells[required_index],
                        required.strip(),
                    )
                    updated.append(_join_table_row(leading, updated_cells))
                    changed = True
                    continue
            updated.append(row)
            continue

        for child in children:
            child_cells = list(cells)
            child_cells[session_index] = _replace_cell_content(
                child_cells[session_index], child.session_id.value
            )
            child_cells[classification_index] = _replace_cell_content(
                child_cells[classification_index], "serial-required"
            )
            if owned_index is not None and owned_index < len(child_cells):
                child_cells[owned_index] = _replace_cell_content(
                    child_cells[owned_index], child.owned_write_paths
                )
            if required_index is not None and required_index < len(child_cells):
                child_cells[required_index] = _replace_cell_content(
                    child_cells[required_index], child.required_prior_sessions
                )
            if allowed_index is not None and allowed_index < len(child_cells):
                child_cells[allowed_index] = _replace_cell_content(
                    child_cells[allowed_index], "none"
                )
            if blockers_index is not None and blockers_index < len(child_cells):
                blocker = (
                    f"Generated child of session {parent_id.value}; run serially "
                    "until expanded and validated"
                )
                child_cells[blockers_index] = _replace_cell_content(
                    child_cells[blockers_index],
                    blocker,
                )
            updated.append(_join_table_row(leading, child_cells))
        changed = True
    return updated, changed


def _update_table_blocks(
    text: str,
    parent_id: plans.SessionId,
    children: list[ChildSpec],
    *,
    manifest: bool,
) -> tuple[str, bool]:
    lines = text.splitlines()
    updated: list[str] = []
    changed = False
    index = 0

    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            updated.append(lines[index])
            index += 1
            continue

        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        parsed_header = _split_table_row(block[0]) if len(block) >= 2 else None
        parsed_separator = _split_table_row(block[1]) if len(block) >= 2 else None
        if (
            parsed_header is None
            or parsed_separator is None
            or not _is_separator_row(parsed_separator[1])
        ):
            updated.extend(block)
            continue

        headers = [_normalize_header(cell) for cell in parsed_header[1]]
        block_type = _table_block_type(headers)
        if block_type == "status":
            new_block, block_changed = _replace_status_table_parent(
                block, parent_id, children
            )
        elif manifest and block_type == "wave":
            new_block, block_changed = _replace_wave_table_parent(
                block, parent_id, children
            )
        elif manifest and block_type == "ownership":
            new_block, block_changed = _replace_ownership_table_parent(
                block, parent_id, children
            )
        else:
            new_block, block_changed = block, False

        updated.extend(new_block)
        changed = changed or block_changed

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(updated) + suffix, changed


def _append_resplit_notes(
    text: str,
    parent_id: plans.SessionId,
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
            "- Parent and stale validation artifacts were archived under "
            f"`{archive_relative_path}`.",
            "- Rerun `session-expand` before continuing execution so expansion "
            "notes and parallel validation match the nested sessions.",
            "",
        ]
    )
    return text.rstrip() + "\n" + note


def _update_manifest(
    manifest_path: Path,
    parent_id: plans.SessionId,
    children: list[ChildSpec],
    total_sessions: int,
    archive_relative_path: str,
) -> bool:
    text = _read_text(manifest_path)
    updated = re.sub(
        r"(?m)^(\*\*Sessions\*\*:[ \t]*)\d+[ \t]*$",
        rf"\g<1>{total_sessions}",
        text,
    )
    updated, tables_changed = _update_table_blocks(
        updated, parent_id, children, manifest=True
    )
    updated = _append_resplit_notes(updated, parent_id, children, archive_relative_path)
    changed = updated != text or tables_changed
    if changed:
        _write_text(manifest_path, updated)
    return changed


def _update_context(
    context_path: Path,
    parent_id: plans.SessionId,
    children: list[ChildSpec],
) -> bool:
    text = _read_text(context_path)
    updated, changed = _update_table_blocks(text, parent_id, children, manifest=False)
    if changed:
        _write_text(context_path, updated)
    return changed


def resplit_session(
    plan_dir: str | Path,
    *,
    session_id: str | None = None,
    parts: int | None = None,
    child_specs: list[dict[str, str]] | None = None,
    state_dir: str | Path | None = None,
    skill_prefix: str = "$",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resplit one pending session and return a JSON-serializable summary."""
    if child_specs is not None:
        parts = _validate_parts(len(child_specs))
    elif parts is not None:
        parts = _validate_parts(parts)

    snapshot = plans.inspect_plan(plan_dir)
    parent_id = _select_parent(snapshot, session_id)
    parent_status = _status_by_id(snapshot).get(parent_id.value)
    if parent_status is None:
        raise ResplitError("ambiguous-input", f"session {parent_id.value} does not exist")

    parent_expansion_path = snapshot.plan_dir / parent_id.expansion_filename
    parent_details = _parse_parent_details(parent_status, parent_expansion_path)
    if parts is None:
        parts = _infer_parts(parent_details.body)
    parts = _validate_parts(parts)

    parallel_validation_text = (
        _read_text(snapshot.parallel_validation_path)
        if snapshot.parallel_validation_path is not None
        else None
    )
    _validate_snapshot(snapshot, parent_id, parts, parallel_validation_text)

    lock_acquired = False
    try:
        state.acquire_lock(snapshot.plan_dir, state_dir, owner="session-resplit")
        lock_acquired = True
    except state.StateError as error:
        raise ResplitError("lock-contention", str(error)) from error

    try:
        locked_snapshot = plans.inspect_plan(snapshot.plan_dir)
        locked_parallel_validation_text = (
            _read_text(locked_snapshot.parallel_validation_path)
            if locked_snapshot.parallel_validation_path is not None
            else None
        )
        locked_parent_status = _validate_snapshot(
            locked_snapshot,
            parent_id,
            parts,
            locked_parallel_validation_text,
        )
        parent_details = _parse_parent_details(
            locked_parent_status, locked_snapshot.plan_dir / parent_id.expansion_filename
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
        archive_dir = _create_archive_dir(
            locked_snapshot.plan_dir, parent_id, now or datetime.now(timezone.utc)
        )
        archive_relative_path = archive_dir.relative_to(locked_snapshot.plan_dir).as_posix()

        archived_files: list[str] = []
        for path in (
            locked_parent_status.path,
            locked_snapshot.plan_dir / parent_id.expansion_filename,
            locked_snapshot.parallel_validation_path,
        ):
            if path is None:
                continue
            archived = _archive_file(path, archive_dir)
            if archived is not None:
                archived_files.append(archived)

        for child in children:
            child_text = _render_child_session(
                parent_id,
                child,
                archive_relative_path,
                parent_details,
                skill_prefix,
            )
            _write_text_exclusive(locked_snapshot.plan_dir / child.filename, child_text)

        reference_updates = _repair_next_session_references(
            locked_snapshot,
            parent_id,
            children,
        )
        total_sessions = len(locked_snapshot.sessions) - 1 + len(children)
        ledger_updates: list[str] = []
        if _update_manifest(
            locked_snapshot.manifest_path,
            parent_id,
            children,
            total_sessions,
            archive_relative_path,
        ):
            ledger_updates.append("manifest.md")
        if _update_context(locked_snapshot.context_path, parent_id, children):
            ledger_updates.append("context.md")

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
            "stale_validation_archived": "parallel-validation.md" in archived_files,
            "next_command": f"{skill_prefix}session-expand {locked_snapshot.plan_dir}/",
        }
    finally:
        if lock_acquired:
            state.release_lock(snapshot.plan_dir, state_dir)


def _walk_up_for_plan_dir(start: Path) -> Path | None:
    current = plans.canonicalize_path(start)
    if current.is_file():
        current = current.parent
    while True:
        if (
            (current / "context.md").is_file()
            and (current / "manifest.md").is_file()
            and any(plans.session_id_from_filename(path.name) for path in current.iterdir())
        ):
            return current
        if current.parent == current:
            return None
        current = current.parent


def _resolve_cli_target(args: argparse.Namespace) -> tuple[Path, str | None]:
    if args.target is None:
        inferred = _walk_up_for_plan_dir(Path.cwd())
        if inferred is None:
            raise ResplitError(
                "ambiguous-input",
                "no plan directory argument and none could be inferred from cwd",
            )
        return inferred, args.session

    target = plans.canonicalize_path(args.target)
    direct_session_id = plans.session_id_from_filename(target.name)
    if direct_session_id is not None:
        if args.session is not None:
            raise ResplitError(
                "ambiguous-input",
                "cannot combine a direct session file with --session",
            )
        if not target.is_file():
            raise ResplitError("ambiguous-input", f"session file does not exist: {target}")
        return target.parent, direct_session_id.value

    if not target.is_dir():
        raise ResplitError("ambiguous-input", f"plan directory does not exist: {target}")
    return target, args.session


def _parse_count(args: argparse.Namespace) -> int | None:
    values = [value for value in (args.count, args.parts) if value is not None]
    if len(values) > 1:
        raise ResplitError("ambiguous-input", "provide child count only once")
    return _validate_parts(values[0]) if values else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resplit one pending session file into nested child sessions."
    )
    parser.add_argument("target", nargs="?", help="Plan directory or session-<id>.md file")
    parser.add_argument("count", nargs="?", type=int, help="Child count, 2 through 20")
    parser.add_argument("--session", help="Explicit session id to resplit")
    parser.add_argument("--parts", type=int, help="Child count, 2 through 20")
    parser.add_argument(
        "--child-spec",
        type=Path,
        help="JSON list or {children: [...]} with title/instructions per child",
    )
    parser.add_argument("--state-dir", help="Override agent-session-loop state directory")
    parser.add_argument(
        "--skill-prefix",
        default="$",
        choices=("$", "/"),
        help="Skill invocation prefix to write into generated guidance",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    return parser


def _print_result(result: dict[str, Any]) -> None:
    print(f"Resplit session {result['parent_id']} into {', '.join(result['children'])}.")
    print(f"Child files: {', '.join(result['child_files'])}")
    print(f"Archive directory: {result['archive_dir']}")
    archived = result["archived_files"] or ["none"]
    print(f"Archived files: {', '.join(archived)}")
    ledgers = result["ledger_updates"] or ["none"]
    print(f"Derived ledgers updated: {', '.join(ledgers)}")
    references = result.get("reference_updates") or ["none"]
    print(f"Downstream references updated: {', '.join(references)}")
    print("Rerun session-expand before continuing execution.")
    print("RESPLIT_STATUS: complete")
    print(f"Next command: {result['next_command']}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        plan_dir, session_id = _resolve_cli_target(args)
        count = _parse_count(args)
        child_specs = _load_child_specs(args.child_spec)
        result = resplit_session(
            plan_dir,
            session_id=session_id,
            parts=count,
            child_specs=child_specs,
            state_dir=args.state_dir,
            skill_prefix=args.skill_prefix,
        )
    except (plans.PlanError, ResplitError) as error:
        reason = error.reason if isinstance(error, ResplitError) else "malformed-plan"
        detail = error.detail if isinstance(error, ResplitError) else str(error)
        if args.json:
            print(json.dumps({"ok": False, "reason": reason, "detail": detail}, sort_keys=True))
        else:
            print("RESPLIT_STATUS: paused")
            print(f"RESPLIT_PAUSE: {reason} - {detail}")
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
