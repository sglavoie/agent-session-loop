"""Verification scope, duplicate command, and command runnability checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import (
    CommandCheck,
    CompletionContract,
)
from .plans import (
    PlanError,
)
from .preflight_models import PreflightFinding, _finding
from .verification import (
    VerificationContext,
    diagnose_command,
    diagnose_command_scope,
)


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
) -> tuple[PreflightFinding, ...]:
    """Flag contract command checks that cannot run in this environment.

    Skipped entirely when ``cwd is None`` (mirrors the environment cwd guard):
    without a repo root there is no interpreter/PATH context to probe against.
    Otherwise every :class:`CommandCheck` is passed to :func:`diagnose_command`
    (fail-open by design), mapping ``executable-not-found`` to a red finding and
    ``module-not-importable`` to a yellow finding. Both block ``--mark-loop-ready``.

    ``which`` and ``module_probe`` are optional injectable seams forwarded to
    :func:`diagnose_command`; when ``None`` the helper's own defaults
    (``shutil.which`` / a bounded import probe) are used.
    """
    if cwd is None:
        return ()

    verification_context = context or VerificationContext.legacy(cwd)
    diagnose_kwargs: dict[str, Any] = {}
    if which is not None:
        diagnose_kwargs["which"] = which
    if module_probe is not None:
        diagnose_kwargs["module_probe"] = module_probe

    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            if not isinstance(check, CommandCheck):
                continue
            try:
                resolved = verification_context.resolve_check(check)
            except PlanError as error:
                findings.append(
                    _finding(
                        "red",
                        "repository-contract",
                        f"session {session_id} check {check.name!r} is invalid",
                        session_id=session_id,
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
            if diagnostic.reason == "executable-not-found":
                findings.append(
                    _finding(
                        "red",
                        "command-not-runnable",
                        (
                            f"session {session_id} check {check.name!r} "
                            "executable is not on PATH"
                        ),
                        session_id=session_id,
                        detail=diagnostic.detail,
                    )
                )
            else:
                findings.append(
                    _finding(
                        "yellow",
                        "command-not-runnable",
                        (
                            f"session {session_id} check {check.name!r} "
                            "module is not importable"
                        ),
                        session_id=session_id,
                        detail=diagnostic.detail,
                    )
                )

    return tuple(findings)
