# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F401, F403, F405, I001
from . import overseer_checkpoint_groups_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .overseer_checkpoint_groups_shared import *
from . import overseer_checkpoint_groups_part_1 as _part_1
from . import overseer_checkpoint_groups_part_2 as _part_2

globals().update(
    {
        name: value
        for part in (_part_1, _part_2)
        for name, value in vars(part).items()
        if not name.startswith("__")
    }
)


def adapters(
    *,
    authority: Authority,
    ledger: overseer_store_ledger.Ledger,
    state_dir: Path | None = None,
    repositories: Repositories = overseer_checkpoint.declared_repositories,
    authorize: Authorize = overseer_policy_client.authorize,
    timeout: float = 5.0,
) -> overseer_dispatch.Registry:
    """Build a dispatcher registry holding exactly the group adapter."""
    return overseer_dispatch.registry(
        {
            "commit": GroupAdapter(
                authority=authority,
                ledger=ledger,
                state_dir=state_dir,
                repositories=repositories,
                authorize=authorize,
                timeout=timeout,
            )
        }
    )


# -- internals -------------------------------------------------------------


def _phrase(refusal: str, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


def _decline(refusal: Refusal, detail: str) -> overseer_dispatch.Preparation:
    return overseer_dispatch.Preparation.declined(_phrase(refusal, detail))


def _failure(refusal: Refusal, detail: str) -> dict[str, Any]:
    """One repository's refusal, in the shape ``coordinate_group`` reads."""
    return {
        "success": False,
        "commit_sha": None,
        "commit_index": None,
        "refusal": refusal,
        "detail": detail,
        "status_result": _phrase(refusal, detail),
    }


def _drift(alias: str, error: commit_message.CommitMessageError) -> tuple[Refusal, str]:
    """Tell an edited worktree apart from a re-staged index, as session 13 does.

    Read from the error's own ``paths`` detail rather than from its prose, so
    the two stay distinguishable if the sentence is ever reworded.
    """
    details = error.details if isinstance(error.details, Mapping) else {}
    if details.get("paths"):
        return ("worktree_drifted", f"{alias}: {error}")
    return ("snapshot_mismatch", f"{alias}: {error}")


def _decision_identity(
    ruling: overseer_decisions.Ruling | None,
) -> dict[str, Any] | None:
    """The governing decision, reduced to what makes it the same decision.

    Stored in the group rather than compared against a held ruling, because the
    execution that must find the decision unchanged may be in a process that
    never saw the preparation. Stance and moment travel with the id for the
    reason :func:`agent_session_loop.overseer_checkpoint` compares them too: a
    decision re-read from moved evidence can keep its id and mean the opposite.
    """
    if ruling is None or ruling.decision is None:
        return None
    decision = ruling.decision
    return {
        "decision_id": decision.decision_id,
        "stance": decision.stance,
        "at": decision.at,
        "operations": list(decision.operations),
    }


def _decision_id(identity: object) -> str:
    if not isinstance(identity, Mapping):
        return "no decision"
    return _text(identity.get("decision_id")) or "no decision"


def _refusal_name(value: object) -> str:
    return value if isinstance(value, str) and value in REFUSALS else "commit_failed"


def _rows(record: Mapping[str, Any], field_name: str) -> tuple[Mapping[str, Any], ...]:
    rows = record.get(field_name)
    if not isinstance(rows, list):
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


def _target_rows(group: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if group is None:
        return {}
    return {
        _text(row.get("key")): dict(row)
        for row in _rows(group, "repositories")
        if _text(row.get("key"))
    }


def _group_roots(
    stored: overseer_store_models.CheckpointGroupRecord | None,
) -> dict[str, str]:
    """Where a stored group's repositories were, as that group recorded them.

    Preferred over re-reading discovery, because reconciliation asks about the
    commits *this group* may have made and the group knows the paths it was
    prepared over. Discovery is the fallback for a group that was never opened.
    """
    if stored is None:
        return {}
    return {
        _text(row.get("key")): _text(row.get("root"))
        for row in _rows(stored.record, "repositories")
        if _text(row.get("key")) and _text(row.get("root"))
    }


def _verification_state(group: Mapping[str, Any]) -> str:
    return _text(group.get("verification")) or "unrecorded"


def _verbatim_message(action: overseer_dispatch.Action) -> str:
    payload = action.payload
    return payload.message if isinstance(payload, overseer_dispatch.Commit) else ""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
