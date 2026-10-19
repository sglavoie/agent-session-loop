from .supervisor_audit_agent_contract import *


def _agent_catalog_entry(
    entry: assurance_projection.CatalogEntry,
) -> AgentCatalogEntry:
    return AgentCatalogEntry(
        issue_id=entry.issue_id,
        state=entry.state,
        severity=entry.severity,
        category=entry.category,
        component=entry.component,
        invariant_key=entry.invariant_key,
        lane=entry.lane,
        summary=_inline(entry.summary, limit=MAX_CATALOG_SUMMARY_CHARS),
    )


def build_agent_catalog(catalog: assurance_projection.IdentityCatalog) -> AgentCatalog:
    return _build_agent_catalog(catalog, maximum_bytes=MAX_AGENT_CATALOG_BYTES)


def _build_agent_catalog(
    catalog: assurance_projection.IdentityCatalog, *, maximum_bytes: int
) -> AgentCatalog:
    """Return the agent-facing form of ``catalog``, bounded and self-describing.

    The bound is spent on the most recently seen issues, because a defect sighted
    lately is the one an auditor is most likely to be looking at again, and the
    survivors are listed in issue order so the listing reads as a catalog rather
    than as a ranking. The digest covers the whole catalog, not the sample: it
    names the identity state a decision was made against, which is what a later
    publisher revalidates, and a sample cannot answer that question.
    """
    number = assurance_models_primitives.issue_id_number
    ranked = sorted(
        catalog.entries,
        key=lambda entry: (entry.last_seen_at, number(entry.issue_id)),
        reverse=True,
    )
    envelope = len(
        assurance_models_primitives.canonical_json_bytes(
            {"digest": catalog.digest, "omitted": len(ranked), "issues": []}
        )
    )
    budget = maximum_bytes - envelope
    kept: list[AgentCatalogEntry] = []
    omitted: list[str] = []
    used = 0
    for entry in ranked:
        supplied = _agent_catalog_entry(entry)
        body = (
            len(assurance_models_primitives.canonical_json_bytes(supplied.to_dict()))
            - 1
        )
        # Every entry after the first also pays for the comma that precedes it.
        cost = body + (1 if kept else 0)
        if omitted or used + cost > budget:
            omitted.append(entry.issue_id)
            continue
        used += cost
        kept.append(supplied)
    return AgentCatalog(
        digest=catalog.digest,
        entries=tuple(sorted(kept, key=lambda entry: number(entry.issue_id))),
        omitted=tuple(sorted(omitted, key=number)),
    )


#: What an auditor is given when the ledger holds no issue, or holds issues
#: nobody can read. Every finding then has to request a new allocation, which is
#: the honest answer when no existing issue can be shown.
EMPTY_AGENT_CATALOG = build_agent_catalog(assurance_projection.IdentityCatalog())


def widen_agent_catalog(
    supplied: AgentCatalog,
    catalog: assurance_projection.IdentityCatalog,
    issue_ids: Sequence[str],
) -> AgentCatalog:
    """Return ``supplied`` with ``issue_ids`` promoted into the listing.

    A candidate the size bound had dropped is worth more to a retry than the
    entry recency ranking kept in its place: proposing an ID the validator would
    then refuse as unselectable would burn the attempt and teach nothing. The
    digest is unchanged because it names the whole inbox rather than the sample,
    so widening what is listed does not restate what the decision is valid for.
    """
    promoted = []
    for issue_id in issue_ids:
        if issue_id in supplied.issue_ids:
            continue
        entry = catalog.entry(issue_id)
        if entry is not None:
            promoted.append(_agent_catalog_entry(entry))
    if not promoted:
        return supplied
    number = assurance_models_primitives.issue_id_number
    added = frozenset(entry.issue_id for entry in promoted)
    return AgentCatalog(
        digest=supplied.digest,
        entries=tuple(
            sorted(
                (*supplied.entries, *promoted), key=lambda entry: number(entry.issue_id)
            )
        ),
        omitted=tuple(item for item in supplied.omitted if item not in added),
    )


def read_identity_catalog(
    state_dir: Path | str | None,
) -> assurance_projection.IdentityCatalog:
    """Return the inbox's identity catalog, or an empty one.

    An unreadable ledger costs the auditor its ability to recognize a recurrence,
    and costs ASL its ability to recognize one on the auditor's behalf. It must
    not also cost the run its audit, so this degrades to the empty catalog: the
    audit still runs, and every finding it reports asks for a new issue instead
    of selecting one nobody could show it.
    """
    try:
        projection = assurance_projection.read_projection(state_dir=state_dir)
    except (
        OSError,
        assurance_ledger.LedgerError,
        assurance_models_primitives.AssuranceError,
    ):
        return assurance_projection.IdentityCatalog()
    return projection.catalog


def read_agent_catalog(state_dir: Path | str | None) -> AgentCatalog:
    """Return the agent-facing catalog to supply for one audit."""
    return build_agent_catalog(read_identity_catalog(state_dir))


__all__ = (
    "_agent_catalog_entry",
    "build_agent_catalog",
    "_build_agent_catalog",
    "EMPTY_AGENT_CATALOG",
    "widen_agent_catalog",
    "read_identity_catalog",
    "read_agent_catalog",
)
