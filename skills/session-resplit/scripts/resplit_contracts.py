# mypy: ignore-errors
# ruff: noqa
# ruff: noqa: F821
"""Child contract allocation and generated child specifications."""

import resplit_parsing as _parsing
import resplit_shared as _shared

for _module in (_shared, _parsing):
    globals().update(
        {
            name: value
            for name, value in vars(_module).items()
            if not name.startswith("__")
        }
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
        raise ResplitError(
            "ambiguous-input", "child spec must be a list or object with children"
        )
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
        done_when = _require_str_list(item["done_when"], label="done_when", index=index)

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
    child_session_id: plans_session_ids.SessionId,
    spec: dict[str, Any],
    parent_checks: dict[str, contracts.VerificationCheck],
    *,
    index: int,
    repository_registry: RepositoryRegistry | None = None,
) -> contracts.CompletionContract:
    check_payloads: list[dict[str, object]] = []
    for name in spec["inherit_checks"]:
        parent_check = parent_checks.get(name)
        if parent_check is None:
            raise ResplitError(
                "ambiguous-contract",
                f"child spec {index} inherits unknown parent check {name!r}",
            )
        check_payloads.append(
            authoring_render._check_to_json(
                parent_check,
                repository_registry=repository_registry,
                session_id=child_session_id,
                index=len(check_payloads),
            )
        )
    check_payloads.extend(spec["checks"])

    try:
        return authoring_render.contract_from_payload(
            child_session_id,
            spec["done_when"],
            {"session": child_session_id.value, "checks": check_payloads},
            repository_registry=repository_registry,
        )
    except plans_errors.PlanError as error:
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
    child_session_id: plans_session_ids.SessionId,
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


def _previous_session_id_from_filename(
    filename: str | None,
) -> plans_session_ids.SessionId | None:
    if filename is None:
        return None
    return plans_session_ids.session_id_from_filename(filename)


def _build_child_specs(
    parent_id: plans_session_ids.SessionId,
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
                child_session_id,
                raw,
                parent_checks,
                index=index,
                repository_registry=parent_details.registry,
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
