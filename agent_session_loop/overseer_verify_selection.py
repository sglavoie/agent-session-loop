# ruff: noqa: F403, F405, I001
"""Resolve a declared verification check within its authorized workspace."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .overseer_verify_shared import *
from .overseer_verify_context import _context


def session_under_verification(facts: intervention.RunFacts) -> str:
    """The session this run is on, as a session id, or ``""``.

    Read from the run rather than taken from the caller. A verify that could be
    pointed at any session would be answering about one session's contract with
    another session's working tree, and the result would look like a verdict.
    """
    turn = facts.runner_state.get("active_turn")
    named = turn.get("session_id") if isinstance(turn, Mapping) else None
    if not isinstance(named, str) or not named.strip():
        return ""
    try:
        return plans_session_ids.parse_session_id(named).value
    except ValueError:
        return ""


def select(
    *,
    plan_dir: str,
    check: str,
    facts: intervention.RunFacts,
    repositories: Sequence[overseer_policy_client.Repository],
    state_dir: str | Path | None = None,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    read_contract: ReadContract = contracts.read_completion_contract,
) -> Selection:
    """Look one check name up in the contract of the session the run is on.

    Every step can only narrow: the session comes from the run, the contract
    comes from the session, the check comes from the contract, and the root the
    check resolves against comes from what discovery recorded and what the
    action was authorized for. There is no step at which a value the caller
    supplied becomes part of what runs.
    """
    named = check.strip()
    if not named:
        return Selection(
            session_id="",
            declared=(),
            refusal="check_unnamed",
            detail="a verify names the check to run; this action names none",
        )
    session_id = session_under_verification(facts)
    if not session_id:
        return Selection(
            session_id="",
            declared=(),
            refusal="session_unknown",
            detail=f"{plan_dir} records no session id for its active turn, so "
            "there is no contract to read the check out of",
        )

    try:
        contract = read_contract(plan_dir, session_id)
    except plans_errors.PlanError as error:
        return Selection(
            session_id=session_id,
            declared=(),
            refusal="contract_unreadable",
            detail=str(error),
        )
    if contract is None:
        return Selection(
            session_id=session_id,
            declared=(),
            refusal="contract_absent",
            detail=f"session {session_id} declares no completion contract, so it "
            "declares no checks",
        )

    declared = tuple(item.name for item in contract.verify.checks)
    found = next((item for item in contract.verify.checks if item.name == named), None)
    if found is None:
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal="check_undeclared",
            detail=f"session {session_id} declares no check named {named!r}; it "
            f"declares {', '.join(declared) or '(none)'}",
        )

    context = _context(
        plan_dir=plan_dir,
        repositories=repositories,
        state_dir=state_dir,
        build_inventory=build_inventory,
    )
    if isinstance(context, tuple):
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal=context[0],
            detail=context[1],
        )

    try:
        resolved = context.resolve_check(found)
    except plans_errors.PlanError as error:
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal="check_unresolvable",
            detail=str(error),
        )

    root = plans_files.canonicalize_path(resolved.resolved_root)
    permitted = {plans_files.canonicalize_path(item.path) for item in repositories}
    if root not in permitted:
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal="scope_unauthorized",
            detail=f"check {named!r} resolves into {root}, which is not one of "
            f"the repositories this action was authorized for: "
            f"{', '.join(sorted(str(item) for item in permitted)) or '(none)'}",
        )
    return Selection(
        session_id=session_id,
        declared=declared,
        check=found,
        context=context,
        resolved_root=str(root),
    )
