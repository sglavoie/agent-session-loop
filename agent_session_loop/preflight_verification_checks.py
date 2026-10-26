"""Verification scope, duplicate command, and command runnability checks."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import (
    CommandCheck,
    CompletionContract,
)
from .plans_errors import PlanError
from .preflight_models import PreflightFinding, PreflightSeverity, _finding
from .verification_command_diagnosis import diagnose_command, diagnose_command_scope
from .verification_models import VerificationContext


def _check_verification_scope(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            diagnostic = diagnose_command_scope(check)
            if diagnostic is None:
                continue
            findings.append(
                _finding(
                    "red",
                    "verification-scope",
                    (
                        f"session {session_id} check {check.name!r} "
                        "verification command is overly broad"
                    ),
                    session_id=session_id,
                    detail=diagnostic.detail,
                )
            )

    return tuple(findings)


def _check_duplicate_verification_commands(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    """Flag identical command argv/cwd pairs within one session recipe."""

    findings: list[PreflightFinding] = []
    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        first_by_identity: dict[
            tuple[str | None, tuple[str, ...], str], CommandCheck
        ] = {}
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            identity = (check.repo, check.argv, check.cwd or ".")
            first = first_by_identity.get(identity)
            if first is None:
                first_by_identity[identity] = check
                continue
            cwd = check.cwd or "."
            findings.append(
                _finding(
                    "yellow",
                    "verification-duplicate",
                    (
                        f"session {session_id} checks {first.name!r} and "
                        f"{check.name!r} run the same command"
                    ),
                    session_id=session_id,
                    detail=(f"argv: {list(check.argv)!r}; cwd: {cwd!r}"),
                )
            )
    return tuple(findings)


def _check_command_runnability(
    contracts_by_session: dict[str, CompletionContract],
    cwd: str | Path | None,
    *,
    context: VerificationContext | None = None,
    which: Any | None = None,
    module_probe: Any | None = None,
    session_paths: Mapping[str, str] | None = None,
    footprint_escapes: frozenset[str] = frozenset(),
) -> tuple[PreflightFinding, ...]:
    """Flag contract command checks that cannot run in this environment.

    Skipped entirely when ``cwd is None`` (mirrors the environment cwd guard):
    without a repo root there is no interpreter/PATH context to probe against.
    Otherwise every :class:`CommandCheck` is passed to :func:`diagnose_command`
    (fail-open by design), mapping ``cwd-not-found`` and ``executable-not-found``
    to red findings and ``module-not-importable`` to a yellow finding. All block
    ``--mark-loop-ready``.

    A check whose location cannot be resolved is a ``repository-contract``
    finding in a registered plan. In a legacy plan it is a contract escaping
    its bound root, so it is ``malformed-contract``, and it is left out when
    its ``cwd`` is in ``footprint_escapes``: the ``repository-footprint``
    finding already reports that very target.

    ``session_paths`` maps a session id to its file, which every finding names.
    ``which`` and ``module_probe`` are optional injectable seams forwarded to
    :func:`diagnose_command`; when ``None`` the helper's own defaults
    (``shutil.which`` / a bounded import probe) are used.
    """
    if cwd is None:
        return ()

    verification_context = context or VerificationContext.legacy(cwd)
    registered = verification_context.workspace is not None
    paths = session_paths or {}
    diagnose_kwargs: dict[str, Any] = {}
    if which is not None:
        diagnose_kwargs["which"] = which
    if module_probe is not None:
        diagnose_kwargs["module_probe"] = module_probe

    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        path = paths.get(session_id)
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            try:
                resolved = verification_context.resolve_check(check)
            except PlanError as error:
                if not registered and (check.cwd or ".") in footprint_escapes:
                    continue
                findings.append(
                    _finding(
                        "red",
                        "repository-contract" if registered else "malformed-contract",
                        f"session {session_id} check {check.name!r} is invalid",
                        session_id=session_id,
                        path=path,
                        detail=str(error),
                    )
                )
                continue
            diagnostic = diagnose_command(
                check,
                repo_root=resolved.resolved_root,
                **diagnose_kwargs,
            )
            if diagnostic is None:
                continue
            if diagnostic.reason == "cwd-not-found":
                severity: PreflightSeverity = "red"
                summary = "cwd does not exist"
            elif diagnostic.reason == "executable-not-found":
                severity = "red"
                summary = "executable is not on PATH"
            else:
                severity = "yellow"
                summary = "module is not importable"
            findings.append(
                _finding(
                    severity,
                    "command-not-runnable",
                    f"session {session_id} check {check.name!r} {summary}",
                    session_id=session_id,
                    path=path,
                    detail=diagnostic.detail,
                )
            )

    return tuple(findings)
