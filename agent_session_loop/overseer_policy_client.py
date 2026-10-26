"""The ASL side of AgentMux's authorization contract.

AgentMux owns the decision and ASL owns the enforcement. This module is the
only place ASL asks the question: it runs ``amx overseer policy --json``, reads
the schema-v1 envelope, and turns the answer into a :class:`Decision` the rest
of the overseer can act on. It holds no state between calls, keeps nothing on
disk, and has no configuration of its own -- everything it knows about whether
the overseer may act comes back over stdout from the process it just started.

## An answer is evidence for one question

The command already carries the machinery that makes that checkable: the
payload names the contract it was decided under, echoes the plan and access
category it was asked about, lists every repository in the order it was given,
and carries a ``config_fingerprint`` naming the declarations it was decided
against plus a ``request_digest`` naming the question itself. All of it is
verified here rather than trusted, because a caller that accepts whatever came
back is a caller that can be handed an answer to a different, easier question.
A payload whose access category, plan, or repository set does not match what
was asked is :data:`FAILURE_ANSWER_MISMATCH` -- not a refusal by AgentMux, but
an answer this client refuses to read as one.

Two of those checks look redundant and are not. A payload claiming
``allowed`` while one of its own repositories is refused is internally
inconsistent, and a payload claiming ``allowed`` alongside a nonzero exit
status contradicts the command's own contract that a refusal exits nonzero in
both output modes. Either one means the thing on the other end is not the
command this contract describes, so both fail closed.

## Unavailable is never permitted, and never cached

Every way the question can fail to be answered -- no ``amx`` on PATH, a
timeout, a crash, unreadable stdout, an envelope this client does not speak, a
contract version it does not understand -- produces a :class:`Decision` with
``allowed`` false and ``available`` false, under a reason from
:data:`FAILURE_REASONS`. There is no fallback path, because the only thing a
fallback could fall back *to* is an authorization that was granted at some
earlier moment under configuration that has since had every opportunity to
change. The module owns no cache, so the substitution is not merely
unimplemented: there is nothing here to substitute.

``available`` and ``allowed`` are kept separate for the caller's benefit
rather than the decision's. Both refuse. But "AgentMux said no" is an operator
decision to respect and "AgentMux could not be asked" is a fault to report, and
an overseer that cannot tell them apart cannot explain itself.

## The two adapters

:meth:`Decision.grant` is what a permitted decision hands to
``overseer_store_ledger``: the plan, the access category, the request identity, and
the configuration fingerprint the store gates stored evidence on.
:meth:`Decision.authorization` is the same permission in the shape
``overseer_isolation.recover_stale_lock`` already consumes. Both return
``None`` on anything that is not an available, allowed, verified answer, so
neither can manufacture permission out of a failure.

Durable records this boundary writes: **none**. It starts one subprocess and
returns what it said.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence

from . import overseer_discovery
from .overseer_policy_client_answer import _exit_status, _failed, _read_answer
from .overseer_policy_client_models import (
    ACCESS_ACTION,
    ACCESS_CATEGORIES,
    ACCESS_CONTEXT,
    ACCESS_MUTATION,
    AMX_BINARY,
    DEFAULT_TIMEOUT_SECONDS,
    FAILURE_ANSWER_MISMATCH,
    FAILURE_REASONS,
    POLICY_CONTRACT_NAME,
    POLICY_CONTRACT_VERSION,
    POLICY_SCHEMA_VERSION,
    Decision,
    Failure,
    Grant,
    Repository,
    RepositoryOutcome,
)

__all__ = [
    "ACCESS_ACTION",
    "ACCESS_CATEGORIES",
    "ACCESS_CONTEXT",
    "ACCESS_MUTATION",
    "AMX_BINARY",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAILURE_ANSWER_MISMATCH",
    "FAILURE_REASONS",
    "POLICY_CONTRACT_NAME",
    "POLICY_CONTRACT_VERSION",
    "POLICY_SCHEMA_VERSION",
    "Decision",
    "Failure",
    "Grant",
    "Repository",
    "RepositoryOutcome",
    "authorize",
    "policy_argv",
    "repositories_for",
]


def repositories_for(
    declarations: Iterable[overseer_discovery.RepositoryDeclaration],
) -> tuple[Repository, ...]:
    """Turn a discovered run's declared repositories into a request's set.

    The resolved root is the path that goes on the wire, not the configured
    one: AgentMux measures applicability against canonical paths, and a request
    carrying ``~/src/api`` where the run actually works in
    ``/private/var/.../api`` asks about a directory nobody is running in.

    Order is preserved, because the request digest covers the repository set in
    the order it was given.
    """
    return tuple(
        Repository(alias=declaration.alias, path=declaration.resolved_root)
        for declaration in declarations
    )


def policy_argv(
    *,
    amx_path: str,
    plan_dir: str,
    access: str,
    repositories: Sequence[Repository],
) -> list[str]:
    """Build the exact command one authorization question is asked with.

    ``--contract-version`` is sent explicitly rather than left to the command's
    default. The default is whatever the installed ``amx`` believes, which is
    the one value that cannot detect a version disagreement; sending the version
    this client speaks makes a mismatched pair refuse instead of silently
    answering under a contract only one side understands.
    """
    argv = [
        amx_path,
        "overseer",
        "policy",
        "--json",
        "--contract-version",
        str(POLICY_CONTRACT_VERSION),
        "--plan",
        plan_dir,
        "--access",
        access,
    ]
    for repository in repositories:
        argv.extend(["--repository", repository.spec()])
    return argv


def authorize(
    *,
    plan_dir: str,
    access: str,
    repositories: Sequence[Repository],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    resolve_binary: Callable[[str], str | None] = shutil.which,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Decision:
    """Ask AgentMux one authorization question and verify the answer.

    Called before every context read, mutation, and dispatch. It is a question
    each time, never a lease: the command reads configuration on every
    invocation, so asking again is the only way to see a withdrawn opt-in, and
    this client keeps nothing that would let a caller skip the asking.
    """
    repositories = tuple(repositories)
    problem = _unsendable(plan_dir=plan_dir, access=access, repositories=repositories)
    if problem is not None:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="request_unsendable",
            summary=problem,
            exit_status=None,
        )

    amx_path = resolve_binary(AMX_BINARY)
    if not amx_path:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="client_missing",
            summary=(
                f"no {AMX_BINARY} executable is available, so authorization "
                "could not be asked for"
            ),
            exit_status=None,
        )

    argv = policy_argv(
        amx_path=amx_path,
        plan_dir=plan_dir,
        access=access,
        repositories=repositories,
    )
    try:
        completed = run_command(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="client_timeout",
            summary=f"{AMX_BINARY} overseer policy did not answer in {timeout}s",
            exit_status=None,
        )
    except UnicodeDecodeError as error:
        # Decoded strictly, and refused rather than replaced: an answer that
        # is not text is not an answer, however much of it would still parse.
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="response_unreadable",
            summary=f"policy output was not UTF-8: {error}",
            exit_status=None,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return _failed(
            plan_dir=plan_dir,
            access=access,
            repositories=repositories,
            failure="client_unusable",
            summary=f"{AMX_BINARY} overseer policy could not be run: {error}",
            exit_status=None,
        )

    return _read_answer(
        plan_dir=plan_dir,
        access=access,
        repositories=repositories,
        stdout=completed.stdout or "",
        exit_status=_exit_status(completed.returncode),
    )


def _unsendable(
    *,
    plan_dir: str,
    access: str,
    repositories: Sequence[Repository],
) -> str | None:
    """Why this question must not be asked, or ``None`` if it is well formed.

    A malformed request is refused here rather than sent, because the reply to
    a request the command could not parse is a structured error, and reading a
    structured error as anything other than "no answer" is the mistake this
    whole module exists to avoid making.
    """
    if not plan_dir:
        return "no plan directory was named, so there is nothing to authorize"
    if access not in ACCESS_CATEGORIES:
        return (
            f"access category {access!r} is not one of {', '.join(ACCESS_CATEGORIES)}"
        )
    if not repositories:
        return (
            "no repositories were named; a plan participating in nothing "
            "cannot be authorized for anything"
        )
    seen: set[str] = set()
    for repository in repositories:
        if not repository.alias or not repository.path:
            return (
                "every repository must carry both an alias and a resolved path; "
                f"{repository!r} does not"
            )
        if not repository.path.startswith(("/", "~")):
            return (
                f"repository path {repository.path!r} is neither absolute nor "
                "rooted at ~, and would be resolved against the wrong directory"
            )
        if repository.alias in seen:
            return f"repository alias {repository.alias!r} was named twice"
        seen.add(repository.alias)
    return None
