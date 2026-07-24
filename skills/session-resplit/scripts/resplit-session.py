#!/usr/bin/env python3
"""Resplit one pending session into nested child sessions.

This helper performs the filesystem-safe parts of the session-resplit skill:
plan inspection, target validation, lock acquisition, archival, child file
creation, and derived ledger updates. Deterministic rendering, table mutation,
archive-path, readiness, and atomic multi-file writes are delegated to
``agent_session_loop.authoring`` so split and resplit share one implementation.

Child completion contracts are never placeholders. Explicit child specs must
allocate the parent's verification checks by name (``inherit_checks``) or
replace them (``checks``), and must state explicit ``done_when`` criteria;
ambiguous allocation is rejected. Without explicit specs, every generated
child derives its contract from the parent's own completion contract, and a
parent without a contract is rejected instead of receiving a
``python3 -m unittest`` placeholder.

Sequential by design: this script does not read `parallel-validation.md`, does
not emit `## Parallel Metadata` blocks for generated children, and leaves
legacy parallel manifest tables (waves, ownership) untouched. Generated
children include a parseable `## Completion Contract` so `asl verify` /
`asl run` enforce the same gate used elsewhere.
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

from agent_session_loop import (  # noqa: E402
    authoring,
    contracts,
    harnesses,
    plans,
    responses,
    state,
)


MIN_PARTS = 2
MAX_PARTS = 20
HANDOFF_HEADING = "## Handoff"
ARCHIVE_ROOT_NAME = authoring.ARCHIVE_ROOT_NAME
CHILD_SPEC_KEYS = frozenset(
    {"title", "instructions", "done_when", "checks", "inherit_checks"}
)

#: One-line remediation per pause reason so a supervising agent (or human) can
#: recover without re-reading the skill. Keyed by every ``ResplitError.reason``
#: this script raises (plus ``malformed-plan`` from a bubbled ``PlanError``).
PAUSE_REMEDIATION: dict[str, str] = {
    "no-contract": (
        "Add a parseable `## Completion Contract` to the parent session, or "
        "pass --child-spec with explicit 'done_when' and "
        "'checks'/'inherit_checks' per child."
    ),
    "ambiguous-contract": (
        "Fix --child-spec so every parent check is inherited or replaced and "
        "each child states explicit 'done_when' plus checks."
    ),
    "ambiguous-input": (
        "Re-run with an explicit plan directory or session-<id>.md file and a "
        f"child count between {MIN_PARTS} and {MAX_PARTS}."
    ),
    "blocked": (
        "Select a pending session to resplit; the chosen session is already "
        "complete or the plan has no pending session."
    ),
    "unknown-status": (
        "Repair the session's final handoff `- **Status**:` line to a known "
        "value (pending/complete) before resplitting."
    ),
    "malformed-plan": (
        "Repair the malformed session or plan file (missing handoff, contract, "
        "or instructions above the handoff) and re-run."
    ),
    "collision": (
        "Remove or rename the conflicting descendant/child files, then re-run "
        "the resplit."
    ),
    "cannot-split": (
        "Add implementation instructions above the parent handoff, or resplit a "
        "different session."
    ),
    "lock-contention": (
        "Another live runner holds an active lock on this plan (the helper "
        "already self-cleared any stale lock). Wait for that runner to finish, "
        "or investigate and stop it, then re-run the resplit."
    ),
}


def _next_steps(plan_dir: str) -> list[dict[str, Any]]:
    """Per-harness `session-expand` invocations to run after a resplit."""
    step = responses.invocation_step(
        (
            "Rerun session-expand so expansion notes match the nested child "
            "sessions before continuing execution."
        ),
        harnesses.skill_invocations("session-expand", f"{plan_dir}/"),
    )
    return responses.next_steps_payload([step])["next_steps"]


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
    contract: contracts.CompletionContract
    previous_session_id: plans.SessionId | None

    @property
    def filename(self) -> str:
        return self.session_id.filename


@dataclass(frozen=True)
class ParentDetails:
    """Parsed parent session content needed for child generation."""

    title: str
    body: str
    had_expansion: bool
    prior_session_file: str | None
    contract: contracts.CompletionContract | None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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

    return parent_status


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


def _extract_previous_session_file(before_handoff: str) -> str | None:
    match = re.search(
        r"> \*\*Previous session\*\*: Read the \*\*Handoff\*\* section "
        r"at the bottom of `([^`]+)`\.",
        before_handoff,
    )
    return match.group(1) if match else None


def _strip_contract_section(lines: list[str]) -> list[str]:
    start = contracts._find_heading_index(
        lines, contracts.COMPLETION_CONTRACT_HEADING, 0, len(lines)
    )
    if start is None:
        return lines
    end = contracts._section_end(lines, start, max_level=2, end=len(lines))
    return lines[:start] + lines[end:]


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

    output = _strip_contract_section(output)
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

    try:
        contract = contracts.parse_completion_contract(
            parent_text, session_id=parent_status.session_id
        )
    except plans.PlanError as error:
        raise ResplitError(
            "malformed-plan",
            f"{parent_status.session.filename} has a malformed completion contract: {error}",
        ) from error

    return ParentDetails(
        title=_extract_parent_title(parent_status.session_id, before_handoff),
        body=body,
        had_expansion=parent_expansion_path.exists(),
        prior_session_file=_extract_previous_session_file(before_handoff),
        contract=contract,
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


def _require_str_list(value: object, *, label: str, index: int) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ResplitError(
            "ambiguous-contract",
            f"child spec {index} {label!r} must be a list of non-empty strings",
        )
    return [item.strip() for item in value]


def _load_child_specs(path: Path | None) -> list[Any] | None:
    if path is None:
        return None
    raw = json.loads(_read_text(path))
    children = raw.get("children") if isinstance(raw, dict) else raw
    if not isinstance(children, list):
        raise ResplitError("ambiguous-input", "child spec must be a list or object with children")
    return children


def _normalize_child_specs(children: list[Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(children, start=1):
        if not isinstance(item, dict):
            raise ResplitError(
                "ambiguous-input", f"child spec {index} must be a JSON object"
            )
        unknown_keys = sorted(set(item) - CHILD_SPEC_KEYS)
        if unknown_keys:
            raise ResplitError(
                "ambiguous-input",
                f"child spec {index} has unknown keys: {', '.join(unknown_keys)}",
            )
        title = str(item.get("title", "")).strip()
        instructions = str(item.get("instructions", "")).strip()
        if not title or not instructions:
            raise ResplitError(
                "ambiguous-input",
                f"child spec {index} requires nonempty title and instructions",
            )

        if "done_when" not in item:
            raise ResplitError(
                "ambiguous-contract",
                f"child spec {index} must state explicit 'done_when' criteria",
            )
        done_when = _require_str_list(
            item["done_when"], label="done_when", index=index
        )

        raw_checks = item.get("checks", [])
        if not isinstance(raw_checks, list) or not all(
            isinstance(check, dict) for check in raw_checks
        ):
            raise ResplitError(
                "ambiguous-contract",
                f"child spec {index} 'checks' must be a list of check objects",
            )
        inherit_checks = _require_str_list(
            item.get("inherit_checks", []), label="inherit_checks", index=index
        )
        if not raw_checks and not inherit_checks:
            raise ResplitError(
                "ambiguous-contract",
                f"child spec {index} allocates no verification checks; provide "
                "'inherit_checks' naming parent checks or replacement 'checks'",
            )

        specs.append(
            {
                "title": title,
                "instructions": instructions,
                "done_when": done_when,
                "checks": raw_checks,
                "inherit_checks": inherit_checks,
            }
        )
    _validate_parts(len(specs))
    return specs


def _allocate_explicit_contract(
    child_session_id: plans.SessionId,
    spec: dict[str, Any],
    parent_checks: dict[str, contracts.VerificationCheck],
    *,
    index: int,
) -> contracts.CompletionContract:
    check_payloads: list[dict[str, object]] = []
    for name in spec["inherit_checks"]:
        parent_check = parent_checks.get(name)
        if parent_check is None:
            raise ResplitError(
                "ambiguous-contract",
                f"child spec {index} inherits unknown parent check {name!r}",
            )
        check_payloads.append(authoring._check_to_json(parent_check))
    check_payloads.extend(spec["checks"])

    try:
        return authoring.contract_from_payload(
            child_session_id,
            spec["done_when"],
            {"session": child_session_id.value, "checks": check_payloads},
        )
    except plans.PlanError as error:
        raise ResplitError(
            "ambiguous-contract", f"child spec {index} has invalid checks: {error}"
        ) from error


def _validate_parent_check_allocation(
    parent_contract: contracts.CompletionContract | None,
    explicit_specs: list[dict[str, Any]],
) -> None:
    inherited_names = {
        name for spec in explicit_specs for name in spec["inherit_checks"]
    }
    if not inherited_names:
        # Full replacement mode: every child carries its own explicit checks
        # (enforced per spec), so the parent recipe is explicitly superseded.
        return
    if parent_contract is None:
        raise ResplitError(
            "ambiguous-contract",
            "child specs inherit parent checks but the parent session has no "
            "completion contract",
        )
    parent_names = {check.name for check in parent_contract.verify.checks}
    unallocated = sorted(parent_names - inherited_names)
    if unallocated:
        raise ResplitError(
            "ambiguous-contract",
            "parent checks not allocated to any child: "
            f"{', '.join(unallocated)}; inherit each parent check explicitly "
            "or replace all checks with explicit 'checks' entries",
        )


def _derive_fallback_contract(
    child_session_id: plans.SessionId,
    child_title: str,
    parent_contract: contracts.CompletionContract,
    *,
    is_last: bool,
) -> contracts.CompletionContract:
    done_when: tuple[str, ...] = (
        f"{child_title} is implemented as described in the child scope above.",
    )
    if is_last:
        done_when = done_when + parent_contract.done_when
    return contracts.CompletionContract(
        session_id=child_session_id,
        done_when=done_when,
        verify=parent_contract.verify,
    )


def _previous_session_id_from_filename(filename: str | None) -> plans.SessionId | None:
    if filename is None:
        return None
    return plans.session_id_from_filename(filename)


def _build_child_specs(
    parent_id: plans.SessionId,
    parent_details: ParentDetails,
    parts: int,
    explicit_specs: list[dict[str, Any]] | None,
    first_previous_session_file: str | None,
) -> list[ChildSpec]:
    if explicit_specs is None:
        if parent_details.contract is None:
            raise ResplitError(
                "no-contract",
                f"session {parent_id.value} has no completion contract to derive "
                "child contracts from; provide --child-spec with explicit "
                "'done_when' and 'checks'/'inherit_checks' per child",
            )
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
        raw_specs: list[dict[str, Any]] = [
            {
                "title": _generated_child_title(
                    parent_details.title, index, instruction_groups[index - 1]
                ),
                "instructions": instruction_groups[index - 1],
            }
            for index in range(1, parts + 1)
        ]
    else:
        _validate_parent_check_allocation(parent_details.contract, explicit_specs)
        raw_specs = explicit_specs

    parent_checks = (
        {check.name: check for check in parent_details.contract.verify.checks}
        if parent_details.contract is not None
        else {}
    )

    children: list[ChildSpec] = []
    for index, raw in enumerate(raw_specs, start=1):
        child_session_id = _child_id(parent_id, index)
        if explicit_specs is None:
            assert parent_details.contract is not None
            contract = _derive_fallback_contract(
                child_session_id,
                raw["title"],
                parent_details.contract,
                is_last=index == len(raw_specs),
            )
        else:
            contract = _allocate_explicit_contract(
                child_session_id, raw, parent_checks, index=index
            )
        previous_session_id = (
            _previous_session_id_from_filename(first_previous_session_file)
            if index == 1
            else _child_id(parent_id, index - 1)
        )
        children.append(
            ChildSpec(
                session_id=child_session_id,
                title=raw["title"],
                instructions=raw["instructions"],
                contract=contract,
                previous_session_id=previous_session_id,
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


def _child_body_lines(
    parent_id: plans.SessionId,
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
    parent_id: plans.SessionId,
    child: ChildSpec,
    archive_relative_path: str,
    parent_details: ParentDetails,
    skill_prefix: str,
) -> str:
    return authoring.render_session_file(
        session_id=child.session_id,
        title=child.title,
        instruction_lines=_child_body_lines(
            parent_id, child, archive_relative_path, parent_details, skill_prefix
        ),
        is_first=False,
        previous_session_id=child.previous_session_id,
        contract=child.contract,
    )


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
            f"- Parent files were archived under `{archive_relative_path}`.",
            "- Rerun `session-expand` before continuing execution so expansion "
            "notes match the nested sessions.",
            "",
        ]
    )
    return text.rstrip() + "\n" + note


def _manifest_replacements(children: list[ChildSpec]) -> tuple[authoring.ManifestSession, ...]:
    return tuple(
        authoring.ManifestSession(
            session_id=child.session_id, title=child.title, status="pending"
        )
        for child in children
    )


def _updated_manifest_text(
    manifest_path: Path,
    parent_id: plans.SessionId,
    children: list[ChildSpec],
    total_sessions: int,
    archive_relative_path: str,
) -> tuple[str, bool]:
    text = _read_text(manifest_path)
    updated = authoring.replace_manifest_sessions_count(text, total_sessions)
    updated, tables_changed = authoring.replace_session_status_rows(
        updated, parent_id, _manifest_replacements(children)
    )
    updated = _append_resplit_notes(updated, parent_id, children, archive_relative_path)
    return updated, updated != text or tables_changed


def _updated_context_text(
    context_path: Path,
    parent_id: plans.SessionId,
    children: list[ChildSpec],
) -> tuple[str, bool]:
    text = _read_text(context_path)
    return authoring.replace_session_status_rows(
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
        try:
            archived.rename(original)
        except OSError:
            pass
    for directory in (archive_dir, archive_dir.parent):
        try:
            directory.rmdir()
        except OSError:
            pass


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

    _validate_snapshot(snapshot, parent_id, parts)

    lock_acquired = False
    try:
        state.acquire_lock(snapshot.plan_dir, state_dir, owner="session-resplit")
        lock_acquired = True
    except state.StateError as error:
        raise ResplitError("lock-contention", str(error)) from error

    try:
        timestamp = now or datetime.now(timezone.utc)
        locked_snapshot = plans.inspect_plan(snapshot.plan_dir)
        locked_parent_status = _validate_snapshot(locked_snapshot, parent_id, parts)
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

        archive_dir = authoring.create_archive_dir(
            locked_snapshot.plan_dir, parent_id, timestamp
        )
        archive_relative_path = archive_dir.relative_to(locked_snapshot.plan_dir).as_posix()

        # Build every staged write before mutating anything on disk.
        transaction = authoring.PlanTransaction(locked_snapshot.plan_dir)
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
            next_text, next_changed = authoring.replace_previous_session_reference(
                _read_text(next_session.path),
                parent_id,
                children[-1].session_id,
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
        except plans.PlanError:
            _rollback_archive(archive_moves, archive_dir)
            raise

        readiness = authoring.refresh_readiness_metadata(
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
        help=(
            "JSON list or {children: [...]} with title/instructions/done_when "
            "plus checks and/or inherit_checks per child"
        ),
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
    readiness = result.get("readiness") or {}
    if readiness:
        print(f"Readiness metadata refreshed: {readiness.get('state', 'draft')}")
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
        remediation = PAUSE_REMEDIATION.get(reason)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": reason,
                        "detail": detail,
                        "remediation": remediation,
                    },
                    sort_keys=True,
                )
            )
        else:
            print("RESPLIT_STATUS: paused")
            print(f"RESPLIT_PAUSE: {reason} - {detail}")
            if remediation:
                print(f"RESPLIT_REMEDIATION: {remediation}")
        return 1

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
