# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F405
"""Parent-session parsing and semantic instruction partitioning."""

from resplit_shared import *  # noqa: F403


def _split_before_final_handoff(text: str, filename: str) -> str:
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return "\n".join(lines[:index]).rstrip() + "\n"
    raise ResplitError("malformed-plan", f"{filename} has no final handoff block")


def _extract_parent_title(
    parent_id: plans_session_ids.SessionId, before_handoff: str
) -> str:
    for line in before_handoff.splitlines():
        match = re.match(r"^#\s+Session\s+\S+\s*:\s*(.+?)\s*$", line)
        if match:
            return match.group(1)
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            title = match.group(1)
            return re.sub(
                rf"^Session\s+{re.escape(parent_id.value)}\s*:?\s*", "", title
            )
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

    while index < len(lines) and (
        not lines[index].strip()
        or lines[index].startswith(plans_session_scope.READS_REPOSITORIES_PREFIX)
        or lines[index].startswith(plans_session_scope.WRITES_REPOSITORIES_PREFIX)
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
    parent_status: plans_models.SessionStatus,
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
            f"{parent_status.session.filename} has no implementation instructions "
            f"above handoff",
        )

    registry = plans_registry.read_repository_registry(parent_status.path.parent)
    try:
        contract = contracts.parse_completion_contract(
            parent_text,
            session_id=parent_status.session_id,
            repository_registry=registry,
        )
    except plans_errors.PlanError as error:
        raise ResplitError(
            "malformed-plan",
            f"{parent_status.session.filename} has a malformed completion contract: "
            f"{error}",
        ) from error

    try:
        scope = (
            plans_session_scope.parse_session_scope(parent_text, registry.aliases)
            if registry is not None
            else None
        )
    except plans_errors.PlanError as error:
        raise ResplitError(
            "malformed-plan",
            f"{parent_status.session.filename} has a malformed repository scope: "
            f"{error}",
        ) from error

    return ParentDetails(
        title=_extract_parent_title(parent_status.session_id, before_handoff),
        body=body,
        had_expansion=parent_expansion_path.exists(),
        prior_session_file=_extract_previous_session_file(before_handoff),
        contract=contract,
        scope=scope,
        registry=registry,
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
            "Implement input parsing, plan inspection, validation, and refusal "
            "behavior.",
            "Implement the core file mutations, including locking, archival, "
            "and child-session generation.",
            "Implement derived ledger updates, stale artifact handling, reporting, "
            "and verification.",
        ),
        4: (
            "Implement input parsing, target inference, and plan inspection.",
            "Implement safety validation and lock handling.",
            "Implement archival and child-session generation.",
            "Implement ledger updates, stale artifact handling, reporting, and "
            "verification.",
        ),
        5: (
            "Implement input parsing and target inference.",
            "Implement plan inspection and safety validation.",
            "Implement lock handling and parent archival.",
            "Implement child-session generation.",
            "Implement ledger updates, stale artifact handling, reporting, and "
            "verification.",
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
