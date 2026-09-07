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
``overseer_store``: the plan, the access category, the request identity, and
the configuration fingerprint the store gates stored evidence on.
:meth:`Decision.authorization` is the same permission in the shape
``overseer_isolation.recover_stale_lock`` already consumes. Both return
``None`` on anything that is not an available, allowed, verified answer, so
neither can manufacture permission out of a failure.

Durable records this boundary writes: **none**. It starts one subprocess and
returns what it said.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from . import overseer_discovery, overseer_isolation

__all__ = [
    "ACCESS_ACTION",
    "ACCESS_CATEGORIES",
    "ACCESS_CONTEXT",
    "ACCESS_MUTATION",
    "AMX_BINARY",
    "DEFAULT_TIMEOUT_SECONDS",
    "FAILURE_REASONS",
    "POLICY_CONTRACT_NAME",
    "POLICY_CONTRACT_VERSION",
    "POLICY_SCHEMA_VERSION",
    "Decision",
    "Grant",
    "Repository",
    "RepositoryOutcome",
    "authorize",
    "policy_argv",
    "repositories_for",
]

#: The contract ``amx overseer policy`` publishes inside the shared schema-v1
#: envelope. It is versioned independently of that envelope, so this pair is
#: checked on its own rather than inferred from the envelope version.
POLICY_CONTRACT_NAME = "overseer_policy"
POLICY_CONTRACT_VERSION = 1

#: The envelope version the command wraps its payload in. A different one is a
#: different wire format, not a newer field set.
POLICY_SCHEMA_VERSION = 1

ACCESS_CONTEXT = "context"
ACCESS_MUTATION = "mutation"
ACCESS_ACTION = "action"

#: The three points the shared requirements say permission is rechecked at:
#: before reading project or conversation content, before preparing a mutation,
#: and before dispatching an action. Naming one does not currently change the
#: decision; it changes what the answer is an answer *to*, and the request
#: digest binds it.
ACCESS_CATEGORIES: tuple[str, ...] = (ACCESS_CONTEXT, ACCESS_MUTATION, ACCESS_ACTION)

#: The executable that answers the question. Resolved through the injected
#: ``resolve_binary`` seam, never assumed to be present.
AMX_BINARY = "amx"

#: How long one authorization question may take. Bounded because the overseer
#: asks before every context read, mutation, and dispatch: a hung answer must
#: become a refusal rather than a stalled overseer.
DEFAULT_TIMEOUT_SECONDS = 10.0

Failure = Literal[
    "request_unsendable",
    "client_missing",
    "client_timeout",
    "client_unusable",
    "response_unreadable",
    "response_malformed",
    "unsupported_envelope",
    "unsupported_contract",
    "request_rejected",
    "answer_mismatch",
]

#: The one refusal that is not a failure: AgentMux answered, and the answer was
#: no. It is not in :data:`FAILURE_REASONS` because nothing went wrong.
FAILURE_ANSWER_MISMATCH: Failure = "answer_mismatch"

#: Every way this client can fail to obtain an answer. Treat them as a consumer
#: contract: an operator surface and the incident ledger both record them, so a
#: spelling change is a contract change.
#:
#: * ``request_unsendable`` -- the question was malformed here and was never
#:   asked. Refusing to ask is cheaper than asking badly and reading the reply
#:   optimistically.
#: * ``client_missing`` -- no ``amx`` executable could be resolved.
#: * ``client_timeout`` -- the command did not answer inside the deadline.
#: * ``client_unusable`` -- the command could not be started, or died on a
#:   signal.
#: * ``response_unreadable`` -- stdout was not JSON.
#: * ``response_malformed`` -- stdout was JSON of the wrong shape, or a field
#:   this contract requires was absent or of the wrong type.
#: * ``unsupported_envelope`` -- the schema-v1 wrapper was a different version.
#: * ``unsupported_contract`` -- the payload named another contract, or a
#:   version of it this client does not speak.
#: * ``request_rejected`` -- AgentMux returned the structured error envelope:
#:   the question itself was refused, before any decision was reached.
#: * ``answer_mismatch`` -- a well-formed answer to a different question, or one
#:   that contradicts itself.
FAILURE_REASONS: tuple[Failure, ...] = (
    "request_unsendable",
    "client_missing",
    "client_timeout",
    "client_unusable",
    "response_unreadable",
    "response_malformed",
    "unsupported_envelope",
    "unsupported_contract",
    "request_rejected",
    "answer_mismatch",
)


@dataclass(frozen=True)
class Repository:
    """One repository of a plan, as the authorization request names it.

    ``alias`` is the plan's own vocabulary and ``path`` is the resolved root on
    this host. Both are required: an aliasless entry would make a refusal name
    a path an operator has to map back to a plan entry themselves, and a
    pathless one names nothing that can be authorized at all.
    """

    alias: str
    path: str

    def spec(self) -> str:
        """The ``alias=path`` form ``--repository`` accepts."""
        return f"{self.alias}={self.path}"


@dataclass(frozen=True)
class RepositoryOutcome:
    """What the decision said about one repository.

    Every repository is reported whether it was permitted or not, so a refusal
    names all of its causes rather than the first one AgentMux hit.
    """

    alias: str
    path: str
    resolved_path: str
    allowed: bool
    reason: str
    detail: str


@dataclass(frozen=True)
class Grant:
    """Permission for one plan, under one access category, at one moment.

    Deliberately not durable and deliberately not reconstructible: this type is
    only ever built from a live, verified, allowed answer. ``config_fingerprint``
    is what the ledger gates stored project evidence on, so evidence gathered
    under a policy that has since moved cannot be read back under a grant issued
    against the new one without an explicit re-authorization.
    """

    plan_dir: str
    access: str
    request_digest: str
    config_fingerprint: str


@dataclass(frozen=True)
class Decision:
    """One authorization answer, verified against the question that was asked.

    ``allowed`` is the whole answer; ``available`` says whether there was an
    answer at all. A caller that only branches on ``allowed`` is already fail
    closed, because every failure sets it false.
    """

    plan_dir: str
    access: str
    repositories: tuple[Repository, ...]
    allowed: bool
    available: bool
    reason: str
    failure: Failure | None
    summary: str
    config_fingerprint: str
    request_digest: str
    contract_version: int | None
    exit_status: int | None
    outcomes: tuple[RepositoryOutcome, ...]

    def refused_repositories(self) -> tuple[RepositoryOutcome, ...]:
        """Every repository the decision named as a cause of refusal."""
        return tuple(outcome for outcome in self.outcomes if not outcome.allowed)

    def grant(self) -> Grant | None:
        """The permission this decision confers, or ``None`` if it confers none.

        Requires an available, allowed answer that carried both identities. A
        decision missing either identity is not permission this client will
        hand on: the fingerprint is what the ledger gates evidence on, and the
        digest is what proves which question was asked.
        """
        if not self.available or not self.allowed:
            return None
        if not self.request_digest or not self.config_fingerprint:
            return None
        return Grant(
            plan_dir=self.plan_dir,
            access=self.access,
            request_digest=self.request_digest,
            config_fingerprint=self.config_fingerprint,
        )

    def authorization(self) -> overseer_isolation.Authorization | None:
        """The same permission in the shape lock recovery already consumes.

        ``overseer_isolation.Authorization.permits`` re-checks the access
        category and the plan itself, so this adapter narrows nothing and adds
        nothing -- it only stops the caller from assembling one by hand out of
        fields it liked the look of.
        """
        grant = self.grant()
        if grant is None:
            return None
        return overseer_isolation.Authorization(
            plan_dir=grant.plan_dir,
            access=grant.access,
            allowed=True,
            request_digest=grant.request_digest,
        )


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
