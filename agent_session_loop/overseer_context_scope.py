"""Overseer context scope."""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import (
    overseer_context_models as models,
)
from . import (
    overseer_discovery,
    overseer_policy_client,
)


def scope_digest(candidate: overseer_discovery.Candidate) -> str:
    """Digest the identity an authorization was issued against.

    Covers the plan, the workspace root, the registry digest and every declared
    repository's alias and resolved root, in declaration order -- the same set
    and the same order the policy request is keyed by. It deliberately does
    *not* cover the run's lifecycle: a run that pauses while a packet is held
    has not changed scope, it has changed state, and conflating the two would
    make every packet stale the moment the thing it describes happens.
    """
    parts: list[str] = [
        candidate.plan_dir or "",
        candidate.workspace_root or "",
        candidate.registry_digest or "",
    ]
    for declaration in candidate.repositories:
        parts.append(declaration.alias)
        parts.append(declaration.resolved_root)
    digest = hashlib.sha256(models._SCOPE_DOMAIN)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def revalidate(
    *,
    packet: models.Packet,
    candidate: overseer_discovery.Candidate | None,
) -> models.Revalidation:
    """Compare a packet against the plan as discovery sees it now.

    Metadata only, and no grant: this reads nothing but the identity discovery
    already publishes without authorization, so a caller can find out that a
    packet is stale without asking AgentMux for permission to look at a run
    whose scope it no longer covers.
    """
    if candidate is None or not candidate.record_observed:
        return models.Revalidation(
            plan_dir=packet.plan_dir,
            fresh=False,
            reason="plan_unobserved",
            detail=(
                "discovery no longer reads a record for this plan, so there is "
                "nothing to revalidate the packet against"
            ),
            recorded_scope=packet.scope,
            current_scope="",
        )
    current = scope_digest(candidate)
    if current != packet.scope:
        return models.Revalidation(
            plan_dir=packet.plan_dir,
            fresh=False,
            reason="scope_changed",
            detail=(
                "the workspace, registry digest or declared repositories moved "
                "since this packet was authorized"
            ),
            recorded_scope=packet.scope,
            current_scope=current,
        )
    if (candidate.run_id or "") != packet.run_id:
        return models.Revalidation(
            plan_dir=packet.plan_dir,
            fresh=False,
            reason="run_replaced",
            detail=(
                f"the plan now runs {candidate.run_id or 'no run'}, not {packet.run_id}"
            ),
            recorded_scope=packet.scope,
            current_scope=current,
        )
    return models.Revalidation(
        plan_dir=packet.plan_dir,
        fresh=True,
        reason=None,
        detail="",
        recorded_scope=packet.scope,
        current_scope=current,
    )


# -- helpers ---------------------------------------------------------------


def _candidate_for(
    plan_dir: str,
    *,
    build_inventory: models.BuildInventory,
    state_dir: str | Path | None,
) -> overseer_discovery.Candidate | None:
    """The plan as discovery sees it now, or ``None`` if it sees it no longer."""
    inventory = build_inventory(state_dir=state_dir)
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return candidate
    return None


def _source_kind(value: str) -> models.SourceKind | None:
    """Read a stored family name, refusing one this build does not know."""
    for kind in models.SOURCE_KINDS:
        if kind == value:
            return kind
    return None


def _request_digest(grant: overseer_policy_client.Grant | None) -> str:
    return grant.request_digest if grant is not None else ""
