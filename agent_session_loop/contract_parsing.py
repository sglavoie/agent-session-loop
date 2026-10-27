"""Completion-contract recipe parsing, file loading, and baseline budgets."""

from __future__ import annotations

import json
from pathlib import Path

from .contract_checks import _parse_check
from .contract_markdown import (
    _extract_bullets,
    _final_handoff_start,
    _find_heading_index,
    _locate_json_block,
    _section_end,
    completion_contract_heading_indices,
)
from .contract_models import (
    COMPLETION_CONTRACT_HEADING,
    DEFAULT_BASELINE_CHECK_BUDGET_SECONDS,
    DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS,
    DONE_WHEN_HEADING,
    VERIFY_HEADING,
    CompletionContract,
    VerificationCheck,
    VerifyRecipe,
)
from .plans_errors import ContractError, PlanError
from .plans_files import canonicalize_path
from .plans_registry import read_repository_registry
from .plans_session_ids import SessionId, parse_session_id, session_id_to_filename
from .workspaces import RepositoryRegistry


def _parse_verify_payload(
    payload: object,
    *,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> VerifyRecipe:
    if not isinstance(payload, dict):
        raise PlanError(
            f"session {session_id.value} Verify JSON must be a top-level object"
        )

    raw_session = payload.get("session")
    if raw_session is not None:
        try:
            declared_id = parse_session_id(raw_session)
        except ValueError as error:
            raise PlanError(
                f"session {session_id.value} Verify JSON 'session' is not a "
                f"valid session id: {error}"
            ) from error
        if declared_id != session_id:
            raise PlanError(
                f"session {session_id.value} Verify JSON 'session' is "
                f"{declared_id.value!r}; expected {session_id.value!r}"
            )

    raw_parallel = payload.get("parallel", False)
    if not isinstance(raw_parallel, bool):
        raise PlanError(
            f"session {session_id.value} Verify JSON 'parallel' must be a "
            f"boolean; got {raw_parallel!r}"
        )

    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or len(raw_checks) == 0:
        raise PlanError(
            f"session {session_id.value} Verify JSON 'checks' must be a non-empty list"
        )

    checks: list[VerificationCheck] = []
    seen_names: set[str] = set()
    for index, raw_check in enumerate(raw_checks):
        check = _parse_check(
            raw_check,
            index=index,
            session_id=session_id,
            repository_registry=repository_registry,
        )
        # Compared case-folded: each name becomes a failure-artifact stem, and
        # on a case-insensitive filesystem ``Lint`` and ``lint`` share a file.
        if check.name.casefold() in seen_names:
            raise PlanError(
                f"session {session_id.value} contract check name "
                f"{check.name!r} is duplicated"
            )
        seen_names.add(check.name.casefold())
        checks.append(check)

    checks_by_name = {check.name: check for check in checks}
    gate_names = {check.name for check in checks if not check.on_failure_of}
    if not gate_names:
        raise PlanError(
            f"session {session_id.value} Verify JSON must contain at least one "
            "unconditional gate check"
        )
    for check in checks:
        if check.review_replay and check.on_failure_of:
            raise PlanError(
                f"session {session_id.value} contract check {check.name!r} "
                "cannot set 'review_replay' to true on a diagnostic "
                "'on_failure_of' check"
            )
        for dependency in check.on_failure_of:
            if dependency == check.name:
                raise PlanError(
                    f"session {session_id.value} contract check {check.name!r} "
                    "cannot name itself in 'on_failure_of'"
                )
            target = checks_by_name.get(dependency)
            if target is None:
                raise PlanError(
                    f"session {session_id.value} contract check {check.name!r} "
                    f"'on_failure_of' references unknown check {dependency!r}"
                )
            if target.on_failure_of:
                raise PlanError(
                    f"session {session_id.value} contract check {check.name!r} "
                    f"'on_failure_of' target {dependency!r} is diagnostic; "
                    "diagnostics may depend only on unconditional gates"
                )

    return VerifyRecipe(checks=tuple(checks), parallel=raw_parallel)


def parse_completion_contract(
    text: str,
    *,
    session_id: int | str | SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> CompletionContract | None:
    """Parse a session file body and return its completion contract, if any.

    Returns ``None`` if the session text contains no ``## Completion Contract``
    section. Raises :class:`PlanError` for any malformed contract, and for
    duplicate contracts: silently selecting one would run an unintended gate.
    """
    canonical_id = parse_session_id(session_id)
    lines = text.splitlines(keepends=False)

    handoff_start = _final_handoff_start(lines)
    end = handoff_start if handoff_start is not None else len(lines)

    starts = completion_contract_heading_indices(lines, end)
    if not starts:
        return None
    if len(starts) > 1:
        locations = ", ".join(str(index + 1) for index in starts)
        raise PlanError(
            f"session {canonical_id.value} has {len(starts)} "
            f"{COMPLETION_CONTRACT_HEADING!r} sections (lines {locations}); "
            "exactly one is allowed"
        )
    contract_start = starts[0]

    contract_end = _section_end(lines, contract_start, max_level=2, end=end)
    section = lines[contract_start + 1 : contract_end]

    done_start = _find_heading_index(section, DONE_WHEN_HEADING, 0, len(section))
    if done_start is None:
        raise PlanError(
            f"session {canonical_id.value} contract missing "
            f"{DONE_WHEN_HEADING!r} subsection"
        )
    done_end = _section_end(section, done_start, max_level=3, end=len(section))
    done_lines = section[done_start + 1 : done_end]
    done_when = tuple(_extract_bullets(done_lines))
    if not done_when:
        raise PlanError(
            f"session {canonical_id.value} contract "
            f"{DONE_WHEN_HEADING!r} must list at least one bullet"
        )

    verify_start = _find_heading_index(section, VERIFY_HEADING, 0, len(section))
    if verify_start is None:
        raise PlanError(
            f"session {canonical_id.value} contract missing "
            f"{VERIFY_HEADING!r} subsection"
        )
    verify_end = _section_end(section, verify_start, max_level=3, end=len(section))
    verify_lines = section[verify_start + 1 : verify_end]

    json_start, json_end = _locate_json_block(verify_lines, canonical_id)
    json_text = "\n".join(verify_lines[json_start:json_end])
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as error:
        # Reported as a line of ``text`` (the session file), not of the block:
        # the heading, the Verify subsection, and the fence all sit above it.
        line = contract_start + verify_start + json_start + error.lineno + 2
        raise ContractError(
            f"session {canonical_id.value} Verify JSON is invalid: {error.msg} "
            f"at line {line} column {error.colno}",
            line=line,
        ) from error

    recipe = _parse_verify_payload(
        payload,
        session_id=canonical_id,
        repository_registry=repository_registry,
    )
    return CompletionContract(
        session_id=canonical_id,
        done_when=done_when,
        verify=recipe,
    )


def baseline_check_budget_seconds(check: VerificationCheck) -> float:
    """Return the per-check baseline execution budget, in seconds.

    Command checks are clamped to :data:`DEFAULT_BASELINE_CHECK_BUDGET_SECONDS`
    so a generous per-session timeout cannot exceed the baseline budget. File
    and regex checks have no command timeout and use the full check budget.
    """
    timeout = getattr(check, "timeout_seconds", DEFAULT_BASELINE_CHECK_BUDGET_SECONDS)
    return min(float(timeout), DEFAULT_BASELINE_CHECK_BUDGET_SECONDS)


def recipe_total_budget_seconds(recipe: VerifyRecipe) -> float:
    """Return the total baseline budget for a recipe, in seconds.

    The sum of per-check budgets is clamped to
    :data:`DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS`.
    """
    total = sum(baseline_check_budget_seconds(check) for check in recipe.checks)
    return min(float(total), DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS)


def read_completion_contract(
    plan_dir: str | Path, session_id: int | str | SessionId
) -> CompletionContract | None:
    """Read and parse the completion contract for a session in a plan directory."""
    canonical_id = parse_session_id(session_id)
    canonical_dir = canonicalize_path(plan_dir)
    session_path = canonical_dir / session_id_to_filename(canonical_id)
    try:
        text = session_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PlanError(
            f"missing session file for session {canonical_id.value}: {session_path}"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise PlanError(f"cannot read {session_path}: {error}") from error
    registry = read_repository_registry(canonical_dir)
    return parse_completion_contract(
        text,
        session_id=canonical_id,
        repository_registry=registry,
    )
