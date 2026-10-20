"""Constants, failure vocabulary and value types of the overseer policy client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import overseer_isolation

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
