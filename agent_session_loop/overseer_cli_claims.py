"""Overseer cli claims."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, cast

from . import (
    overseer_cli_common as common,
)
from . import (
    overseer_decisions,
    overseer_discovery,
    overseer_policy_client,
)


def _resolve_candidate(
    inventory: overseer_discovery.Inventory, plan_dir: str
) -> overseer_discovery.Candidate:
    """The one record that speaks for ``plan_dir``.

    A state directory copied aside still names the plan it was copied from, so
    several records can claim one plan. Only a record whose directory is the
    plan's own hash is that plan's; a stray copy sorting earlier must not
    shadow it. When no record proves its identity the first claimant is
    returned, and its ``plan_identity_mismatch`` refuses it downstream. Two
    records that both prove it are ambiguous, and that fails closed.
    """
    wanted = common.canonical_plan(plan_dir)
    claimants = [
        candidate
        for candidate in inventory.candidates
        if common.canonical_plan(candidate.plan_dir) == wanted
    ]
    if not claimants:
        raise common.OverseerCliError(
            "plan_unobserved",
            f"discovery has no record of {wanted}; run `asl overseer scan` first",
        )
    proven = [
        candidate
        for candidate in claimants
        if "plan_identity_mismatch" not in candidate.ineligible_reasons
    ]
    if len(proven) > 1:
        raise common.OverseerCliError(
            "plan_identity_ambiguous",
            f"{len(proven)} state records each claim {wanted}: "
            + ", ".join(str(candidate.state_dir) for candidate in proven),
        )
    return proven[0] if proven else claimants[0]


def _independent_plans(
    inventory: overseer_discovery.Inventory, *, excluding: str
) -> tuple[str, ...]:
    """Eligible plans other than this one: work a blocked host may still do."""
    blocked = common.canonical_plan(excluding)
    return tuple(
        common.plan_dir_of(candidate)
        for candidate in inventory.eligible()
        if common.canonical_plan(candidate.plan_dir) != blocked
    )


def _grant_for(
    candidate: overseer_discovery.Candidate,
    *,
    access: str,
    seams: common.Seams,
    timeout: float,
) -> tuple[overseer_policy_client.Decision, overseer_policy_client.Grant | None]:
    """Ask AgentMux now. Never reuse an answer from an earlier leaf.

    The plan directory goes on the wire in :func:`plan_dir_of`'s spelling, which
    is the one every other comparison in this module uses. A grant naming a
    different spelling than the caller passes on -- ``None`` against ``""``, for
    the truncated record discovery still reports -- is a grant the dispatcher
    would refuse to match, and the refusal would read as a policy problem.
    """
    decision = seams.authorize(
        plan_dir=common.plan_dir_of(candidate),
        access=access,
        repositories=overseer_policy_client.repositories_for(candidate.repositories),
        timeout=timeout,
    )
    return decision, decision.grant()


def project_identities(
    candidate: overseer_discovery.Candidate,
) -> tuple[str, ...]:
    """Every project a plan participates in, in the registry's own spelling.

    A project is a repository root, because that is the identity AgentMux
    authorizes a conversation under. The workspace directory those repositories
    sit under is not one of them: no conversation is ever admitted under it, so
    a request naming it could be matched by no evidence at all and a repository
    approval would be rejected ``scope_widened`` for naming somewhere else.

    The roots are passed through exactly as discovery resolved them, since the
    packet digests, the policy question and the dispatcher all compare that same
    spelling; a second normalization here would be a second answer to what one
    repository is.
    """
    return tuple(
        dict.fromkeys(
            declaration.resolved_root
            for declaration in candidate.repositories
            if declaration.resolved_root
        )
    )


def _scope_for(
    candidate: overseer_discovery.Candidate, *, incident_id: int | None
) -> overseer_decisions.Scope:
    """The scope one request is about: this plan, in its own repositories.

    A plan that declares no repository names no project, which refuses
    ``target_unscoped`` rather than falling back to the workspace -- an
    unmatched identity would read as a permission problem, and an unscoped
    request reads as what it is.
    """
    return overseer_decisions.Scope(
        projects=project_identities(candidate),
        plan_dir=common.plan_dir_of(candidate),
        run_id=candidate.run_id or "",
        incident="" if incident_id is None else str(incident_id),
    )


def _claim_projects(
    named: dict[str, Any], position: int, *, scope: overseer_decisions.Scope
) -> tuple[str, ...]:
    """The projects one claim's scope names, defaulting to the request's.

    A claim scope that mentions neither ``project`` nor ``projects`` inherits
    every project the request is about, which is the narrowest reading for a
    plan in one repository and the only honest one for a plan in several. A
    claim that names either of them is taken at its word and checked against
    the evidence, which is where naming a project nobody spoke in is refused.
    """
    if "projects" not in named:
        return () if "project" in named else scope.named_projects
    listed = named["projects"]
    if not isinstance(listed, list):
        raise common.OverseerCliError(
            "claims_invalid", f"claim {position} has a non-list scope.projects"
        )
    return tuple(str(item) for item in listed)


def _string_list(entry: dict[str, Any], key: str, position: int) -> tuple[str, ...]:
    """An optional list of strings, refused when it is any other shape.

    Iterating whatever arrived would split a bare string into characters and
    raise on a number, so a claim of the wrong shape is reported as one.
    """
    listed = entry.get(key, [])
    if not isinstance(listed, list) or not all(
        isinstance(item, str) for item in listed
    ):
        raise common.OverseerCliError(
            "claims_invalid",
            f"claim {position} has a {key} that is not a list of strings",
        )
    return tuple(listed)


def parse_claims(
    text: str, *, scope: overseer_decisions.Scope
) -> tuple[overseer_decisions.Claim, ...]:
    """Read host-composed claims, defaulting each one's scope to the request's.

    The host writes claims because reading what a human meant is judgement, and
    judgement is the host's job. Everything after this point is mechanical: the
    register decides which claims survive, and it can reject every one of them.

    An omitted ``scope`` means "exactly the scope being asked about", the
    narrowest reading available. A claim may still name a wider one and be
    rejected for it -- that rejection is reported, not silently dropped.

    Topics, stances and operations are passed through unchecked rather than
    validated here. The register owns those vocabularies and already reports
    an unrecognised one as an ``unknown_topic`` or ``unknown_operation``
    rejection against the claim that carried it. Rejecting them at the parser
    instead would turn a reportable finding into a usage error and lose which
    claim was at fault.
    """
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as error:
        raise common.OverseerCliError(
            "claims_invalid", f"claims are not JSON: {error}"
        ) from error
    except RecursionError as error:
        # Not a ValueError: pathologically nested JSON raises it from the
        # decoder instead of a decode error.
        raise common.OverseerCliError(
            "claims_invalid", "claims are nested too deeply to decode"
        ) from error
    if not isinstance(loaded, list):
        raise common.OverseerCliError(
            "claims_invalid", "claims must be a JSON array of claim objects"
        )
    claims: list[overseer_decisions.Claim] = []
    for position, entry in enumerate(loaded):
        if not isinstance(entry, dict):
            raise common.OverseerCliError(
                "claims_invalid", f"claim {position} is not an object"
            )
        required = ("statement_id", "topic", "stance")
        missing = [key for key in required if key not in entry]
        if missing:
            raise common.OverseerCliError(
                "claims_invalid",
                f"claim {position} omits {', '.join(missing)}",
            )
        named = entry.get("scope")
        if named is None:
            claim_scope = scope
        elif isinstance(named, dict):
            claim_scope = overseer_decisions.Scope(
                project=str(named.get("project", "")),
                projects=_claim_projects(named, position, scope=scope),
                plan_dir=str(named.get("plan_dir", scope.plan_dir)),
                run_id=str(named.get("run_id", scope.run_id)),
                incident=str(named.get("incident", scope.incident)),
            )
        else:
            raise common.OverseerCliError(
                "claims_invalid", f"claim {position} has a non-object scope"
            )
        claims.append(
            overseer_decisions.Claim(
                statement_id=str(entry["statement_id"]),
                topic=cast(overseer_decisions.Topic, str(entry["topic"])),
                stance=cast(overseer_decisions.Stance, str(entry["stance"])),
                scope=claim_scope,
                operations=cast(
                    tuple[overseer_decisions.Operation, ...],
                    _string_list(entry, "operations", position),
                ),
                corroborating=_string_list(entry, "corroborating", position),
                reading=str(entry.get("reading", "")),
            )
        )
    return tuple(claims)


def _claims_text(args: argparse.Namespace) -> str:
    path = getattr(args, "claims_file", None)
    if getattr(args, "claims_stdin", False):
        if path:
            raise common.OverseerCliError(
                "claims_invalid", "--claims-file and --claims-stdin are exclusive"
            )
        try:
            return sys.stdin.read()
        except (OSError, UnicodeDecodeError) as error:
            raise common.OverseerCliError(
                "claims_invalid", f"cannot read claims from stdin: {error}"
            ) from error
    if not path:
        return "[]"
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise common.OverseerCliError(
            "claims_invalid", f"cannot read {path}: {error}"
        ) from error


def _raises(args: argparse.Namespace) -> tuple[overseer_decisions.Topic, ...]:
    """The human questions the invocation says this operation depends on.

    ``--raises`` is additive to what the claims already show: a topic named here
    is raised whether or not any decision was claimed on it, which is how a host
    agent says "this needs acceptance and nobody has given it" rather than only
    "someone answered acceptance". The parser restricts the values to
    :data:`overseer_decisions.HUMAN_TOPICS`, so the cast records a vocabulary the
    argument list already enforced.
    """
    named = tuple(getattr(args, "raises", None) or ())
    return tuple(cast(overseer_decisions.Topic, str(item)) for item in named)
