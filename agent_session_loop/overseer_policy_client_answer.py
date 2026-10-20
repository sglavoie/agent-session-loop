"""Reading and verifying one ``amx overseer policy`` answer."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .overseer_policy_client_models import (
    FAILURE_ANSWER_MISMATCH,
    POLICY_CONTRACT_NAME,
    POLICY_CONTRACT_VERSION,
    POLICY_SCHEMA_VERSION,
    Decision,
    Failure,
    Repository,
    RepositoryOutcome,
)


def _read_answer(
    *,
    plan_dir: str,
    access: str,
    repositories: tuple[Repository, ...],
    stdout: str,
    exit_status: int | None,
) -> Decision:
    """Parse and verify one policy answer, refusing anything unrecognizable."""
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as error:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="response_unreadable",
            summary=f"policy output was not JSON: {error}",
            exit_status=exit_status,
        )
    if not isinstance(parsed, dict):
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="response_malformed",
            summary="policy output was not a JSON object",
            exit_status=exit_status,
        )

    schema_version = parsed.get("schema_version")
    if schema_version != POLICY_SCHEMA_VERSION:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="unsupported_envelope",
            summary=(
                f"policy envelope schema {schema_version!r} is not the "
                f"schema {POLICY_SCHEMA_VERSION} this client speaks"
            ),
            exit_status=exit_status,
        )

    error_payload = parsed.get("error")
    if isinstance(error_payload, dict):
        code = _text(error_payload.get("code")) or "unknown"
        message = _text(error_payload.get("message")) or "no message"
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="request_rejected",
            summary=f"policy request was rejected ({code}): {message}",
            exit_status=exit_status,
        )

    data = parsed.get("data")
    if not isinstance(data, dict):
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="response_malformed",
            summary="policy envelope carried no data object",
            exit_status=exit_status,
        )

    contract = data.get("contract")
    if not isinstance(contract, dict):
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="unsupported_contract",
            summary="policy payload named no contract",
            exit_status=exit_status,
        )
    contract_version = _version(contract.get("version"))
    if (
        _text(contract.get("name")) != POLICY_CONTRACT_NAME
        or contract_version != POLICY_CONTRACT_VERSION
    ):
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="unsupported_contract",
            summary=(
                f"policy payload is {_text(contract.get('name'))!r} version "
                f"{contract_version!r}, not {POLICY_CONTRACT_NAME} version "
                f"{POLICY_CONTRACT_VERSION}"
            ),
            exit_status=exit_status,
            contract_version=contract_version,
        )

    allowed = data.get("allowed")
    fingerprint = _text(data.get("config_fingerprint"))
    digest = _text(data.get("request_digest"))
    if not isinstance(allowed, bool) or not fingerprint or not digest:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="response_malformed",
            summary=(
                "policy payload is missing the decision, the configuration "
                "fingerprint, or the request digest"
            ),
            exit_status=exit_status,
            contract_version=contract_version,
        )

    outcomes, outcomes_malformed = _read_outcomes(data.get("repositories"))
    if outcomes_malformed:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="response_malformed",
            summary="policy payload carried an unreadable repository outcome",
            exit_status=exit_status,
            contract_version=contract_version,
        )

    mismatch = _mismatch(
        data=data,
        plan_dir=plan_dir,
        access=access,
        repositories=repositories,
        allowed=allowed,
        outcomes=outcomes,
        exit_status=exit_status,
    )
    if mismatch is not None:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure=FAILURE_ANSWER_MISMATCH,
            summary=mismatch,
            exit_status=exit_status,
            contract_version=contract_version,
        )

    return Decision(
        plan_dir=plan_dir,
        access=access,
        repositories=repositories,
        allowed=allowed,
        available=True,
        reason=_text(data.get("reason")) or "",
        failure=None,
        summary=_text(data.get("summary")) or "",
        config_fingerprint=fingerprint,
        request_digest=digest,
        contract_version=contract_version,
        exit_status=exit_status,
        outcomes=outcomes,
    )


def _mismatch(
    *,
    data: dict[str, Any],
    plan_dir: str,
    access: str,
    repositories: tuple[Repository, ...],
    allowed: bool,
    outcomes: tuple[RepositoryOutcome, ...],
    exit_status: int | None,
) -> str | None:
    """Why this answer is not an answer to the question that was asked.

    The first three clauses catch a reply about another plan, another access
    category, or another repository set -- the three things the request digest
    binds and therefore the three ways an answer could be carried somewhere it
    does not belong. The last two catch an answer that disagrees with itself,
    which means the process on the other end is not the command this contract
    describes.
    """
    if _text(data.get("plan")) != plan_dir:
        return (
            f"policy answered about plan {_text(data.get('plan'))!r}, not {plan_dir!r}"
        )
    if _text(data.get("access")) != access:
        return (
            f"policy answered about access {_text(data.get('access'))!r}, not "
            f"{access!r}"
        )
    answered = tuple((outcome.alias, outcome.path) for outcome in outcomes)
    asked = tuple((repository.alias, repository.path) for repository in repositories)
    if answered != asked:
        return (
            "policy answered about a different repository set: asked about "
            f"{list(asked)}, answered about {list(answered)}"
        )
    if allowed and any(not outcome.allowed for outcome in outcomes):
        return "policy allowed the plan while refusing one of its repositories"
    if allowed and exit_status not in (0, None):
        return (
            f"policy allowed the plan and exited {exit_status}; a permitted "
            "decision exits zero"
        )
    return None


def _read_outcomes(
    value: Any,
) -> tuple[tuple[RepositoryOutcome, ...], bool]:
    """Parse the per-repository outcomes, reporting whether any is unusable.

    A malformed entry raises the flag rather than being dropped. Dropping one
    would shrink the answered set, and a shrunken set is exactly what the
    comparison against the asked set exists to catch.
    """
    if not isinstance(value, list):
        return (), True
    outcomes: list[RepositoryOutcome] = []
    for entry in value:
        if not isinstance(entry, dict):
            return (), True
        allowed = entry.get("allowed")
        path = _text(entry.get("path"))
        if not isinstance(allowed, bool) or not path:
            return (), True
        outcomes.append(
            RepositoryOutcome(
                alias=_text(entry.get("alias")) or "",
                path=path,
                resolved_path=_text(entry.get("resolved_path")) or "",
                allowed=allowed,
                reason=_text(entry.get("reason")) or "",
                detail=_text(entry.get("detail")) or "",
            )
        )
    return tuple(outcomes), False


def _failed(
    *,
    plan_dir: str,
    access: str,
    repositories: Sequence[Repository],
    failure: Failure,
    summary: str,
    exit_status: int | None,
    contract_version: int | None = None,
) -> Decision:
    """The decision every unanswered question produces.

    One constructor for all of them, so no failure path can accidentally build
    a decision with ``allowed`` or ``available`` set, or carry an identity it
    never verified.
    """
    return Decision(
        plan_dir=plan_dir,
        access=access,
        repositories=tuple(repositories),
        allowed=False,
        available=False,
        reason="",
        failure=failure,
        summary=summary,
        config_fingerprint="",
        request_digest="",
        contract_version=contract_version,
        exit_status=exit_status,
        outcomes=(),
    )


def _exit_status(value: Any) -> int | None:
    """The process status, or ``None`` when the runner did not report one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _version(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
