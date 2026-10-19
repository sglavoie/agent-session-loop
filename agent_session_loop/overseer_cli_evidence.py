"""Overseer cli evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import (
    overseer_cli_claims as claims_module,
)
from . import (
    overseer_cli_common as common,
)
from . import (
    overseer_decisions,
    overseer_discovery,
    overseer_history,
    overseer_policy_client,
)


@dataclass(frozen=True)
class Evidence:
    """Everything a ruling was derived from, and how much of it was readable."""

    scope: overseer_decisions.Scope
    statements: tuple[overseer_decisions.Statement, ...]
    history: overseer_history.History | None


def gather(
    *,
    candidate: overseer_discovery.Candidate,
    grant: overseer_policy_client.Grant | None,
    incident_id: int | None,
    seams: common.Seams,
    state_dir: str | None,
    history: bool,
    generated_classes: Sequence[str],
    timeout: float,
) -> Evidence:
    """Collect the statements a claim may cite, from both evidence boundaries.

    The mailbox is one boundary and the harness transcript is the other, and
    they are not interchangeable. A mailbox record shows that an instruction
    *arrived*, never that a person composed it, so everything it yields is a
    relayed instruction -- readable, citable as corroboration, and never on its
    own an authorization. Only a transcript carries a turn a human actually
    typed. A caller that skips the transcript is therefore choosing a state
    where nothing can be authorized, which is a legitimate thing to want and a
    silent surprise if it happened by default.

    A transcript that cannot be read is not an error here. It comes back as a
    history carrying its own failure, contributes no statements, and is
    reported, so "no authorization was found" and "the evidence could not be
    reached" stay distinguishable.
    """
    scope = claims_module._scope_for(candidate, incident_id=incident_id)
    view = seams.read_mailbox(candidate.plan_dir, state_dir=state_dir)
    statements = list(
        overseer_decisions.statements_from_supervision(
            view.messages, grant=grant, scope=scope
        )
    )
    transcript: overseer_history.History | None = None
    if history:
        transcript = seams.read_history(
            plan_dir=common.plan_dir_of(candidate),
            repositories=overseer_policy_client.repositories_for(
                candidate.repositories
            ),
            timeout=timeout,
        )
        statements.extend(
            overseer_decisions.statements_from_history(
                transcript,
                generated_classes=tuple(generated_classes),
                repositories=claims_module.project_identities(candidate),
            )
        )
    return Evidence(scope=scope, statements=tuple(statements), history=transcript)


def _rule(
    *,
    evidence: Evidence,
    claims: Sequence[overseer_decisions.Claim],
    operation: str,
    raises: Sequence[overseer_decisions.Topic] = (),
    reviewed: str = "",
) -> tuple[overseer_decisions.Ruling, overseer_decisions.Register]:
    """Recompute a ruling from live statements. Never from a cached one.

    ``reviewed`` is the evidence digest the claims were read from, when the
    caller is binding them to one. Re-deriving the ruling proves the claims are
    still in force; the digest is what makes the re-derivation notice a
    statement nobody claimed because nobody had seen it yet.

    ``raises`` is the human questions the caller knows are open. It is a floor
    rather than the whole answer: the register is asked which human topics were
    actually claimed in scope, so a safety hold the host agent read out of the
    conversation stops the operation whether or not the invocation named safety.
    Deriving it here rather than at each leaf is what keeps every authority
    recheck asking the same question as the first one.

    Whether ``reviewed`` was *owed* is not asked here. This function is handed
    claims and cannot tell what the host was shown; :func:`unbound` is the
    answer the leaves give for the case where nothing was named.
    """
    register = overseer_decisions.register(
        statements=evidence.statements, claims=claims
    )
    ruling = overseer_decisions.resolve(
        register=register,
        request=overseer_decisions.Request(
            operation=operation,
            scope=evidence.scope,
            raises=overseer_decisions.dependencies(
                register=register, scope=evidence.scope, raises=raises
            ),
            reviewed=reviewed,
        ),
    )
    return ruling, register


#: The refusals that say the reading itself cannot be used, whatever the claims
#: in it turned out to say. A leaf that answers with one of these publishes no
#: ``evidence_digest``: a digest printed beside "your reading is unusable" is a
#: fresh record's name handed to a caller holding an old record's claims, which
#: is the manufactured binding this boundary exists to refuse. The field is only
#: the obvious route, so the rule is the whole answer rather than one key of it:
#: no refusal from either leaf names the record it observed, in its payload, its
#: summary, its escalation or its printed lines. The name of a record is what
#: reading it produces, and a refusal is not a reading.
UNUSABLE_READING: frozenset[str] = frozenset({"evidence_unbound", "evidence_changed"})


def unbound(
    *, scope: overseer_decisions.Scope, operation: str
) -> overseer_decisions.Ruling:
    """The ruling for claims that name no record they were read from.

    Claims are a reading, and a reading is of something. ``decide`` prints the
    statements and the ``evidence_digest`` that names them together, so a host
    that composed claims was shown a record and can say which one; a claim-
    bearing invocation that names none is either a reading of a record nobody
    can identify or no reading at all, and neither is something to weigh
    decisions against.

    The alternative -- reading the record now and calling *that* what the claims
    were read from -- is the failure this refuses. It cannot fail: the digest is
    taken from the same read the claims are resolved against, so it always
    agrees, and every hold that arrived before the invocation is silently
    absorbed into the record the host is then said to have reviewed.
    """
    return overseer_decisions.Ruling(
        operation=operation,
        scope=scope,
        authorized=False,
        refusal="evidence_unbound",
        summary=(
            "the claims name no reviewed evidence; re-read the record with "
            "`asl overseer decide --list-statements` and pass its "
            "`evidence_digest` as --reviewed-evidence"
        ),
    )
