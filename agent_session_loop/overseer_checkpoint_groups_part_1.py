# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405
from . import overseer_checkpoint_groups_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .overseer_checkpoint_groups_shared import *

__all__ = [
    "GROUP_PHASES",
    "GROUP_SCOPE",
    "REFUSALS",
    "GroupAdapter",
    "Permission",
    "Refusal",
    "adapters",
    "group_key",
    "repository_identity",
    "summarize",
]

Operation = overseer_decisions.Operation

#: The operations this module implements, identical to the single-repository
#: adapter's: this is the same operation over a wider corpus, not a new one.
GROUP_OPERATIONS: tuple[Operation, ...] = ("commit",)

#: The scope every group this module opens is written under.
GROUP_SCOPE = commit_groups.GROUP_SCOPE_OVERSEER

#: The phases a group row can be stopped at, oldest first. ``prepared`` is this
#: module's; the rest are the boundaries
#: :func:`~agent_session_loop.commit_groups.coordinate_group` persists at, named
#: here so an operator reading a stalled row knows which of them it stopped in
#: and a test can restart the run at each of them.
GROUP_PHASES: tuple[str, ...] = (
    "prepared",
    "resumed",
    "recovered-clean-commit",
    "skipped-clean",
    "repository-committed",
    "repository-failed",
    "complete",
)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "message_empty",
    "registry_empty",
    "registry_changed",
    "policy_unavailable",
    "repository_excluded",
    "policy_superseded",
    "repository_unreadable",
    "nothing_to_commit",
    "baseline_unknown",
    "baseline_moved",
    "changes_unattributed",
    "repository_ambiguous",
    "staging_failed",
    "ledger_refused",
    "not_prepared",
    "group_settled",
    "group_unreadable",
    "worktree_drifted",
    "snapshot_mismatch",
    "commit_failed",
    "commit_absent",
]

#: Why a group checkpoint did not happen, or did not finish. A strict superset
#: of :data:`agent_session_loop.overseer_checkpoint.REFUSALS`, because a group
#: reports every repository-level refusal that module's ``attribute`` reaches
#: --- ``repository_ambiguous`` included, which only its single-repository
#: caller can actually provoke and which is carried here so the two adapters
#: speak one vocabulary rather than two overlapping ones. What is added on top
#: are facts about the group rather than about a repository: which repositories
#: were excluded, whether the registry still matches the one prepared, and
#: whether the durable record can be read back at all.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "message_empty",
    "registry_empty",
    "registry_changed",
    "policy_unavailable",
    "repository_excluded",
    "policy_superseded",
    "repository_unreadable",
    "nothing_to_commit",
    "baseline_unknown",
    "baseline_moved",
    "changes_unattributed",
    "repository_ambiguous",
    "staging_failed",
    "ledger_refused",
    "not_prepared",
    "group_settled",
    "group_unreadable",
    "worktree_drifted",
    "snapshot_mismatch",
    "commit_failed",
    "commit_absent",
)

#: The UUID5 namespace a group's per-repository identities are derived in. A
#: namespace of its own, distinct from the single-repository adapter's, because
#: a group commit and a lone checkpoint of the same action are different
#: commits and must never be found by the same trailer.
_ID_NAMESPACE = uuid.UUID("6d1e0a94-52b7-5c3f-9a08-4be27f61d5c2")

Authority = overseer_checkpoint.Authority
Repositories = overseer_checkpoint.Repositories
Authorize = Callable[..., overseer_policy_client.Decision]


def group_key(*, plan_dir: str, incident_id: int | None, target: str) -> str:
    """The durable identity of the group one action owes.

    Derived from the action rather than generated with it, for the same reason
    :func:`~agent_session_loop.overseer_checkpoint.checkpoint_id` is: a retry
    after a crash has the ledger row's fields and nothing else, and it must
    arrive at the row the dead process opened rather than open a second one.
    This is *not*
    :attr:`~agent_session_loop.commit_groups.create_group`'s ``group_id``, which
    hashes only ``{scope, session_id, ordered_aliases}`` and so would be shared
    by every overseer checkpoint of the same repositories.
    """
    return overseer_checkpoint.checkpoint_id(
        plan_dir=plan_dir, incident_id=incident_id, target=target
    )


def repository_identity(
    *,
    plan_dir: str,
    incident_id: int | None,
    target: str,
    alias: str,
) -> str:
    """The trailer identity this group's commit in ``alias`` carries.

    Per repository rather than per group, because reconciliation asks each
    repository a question only that repository can answer: one shared identity
    would make a group that committed in two of three repositories look, from
    either of the two, exactly like one that finished.
    """
    name = "\x00".join(
        (
            plan_dir,
            "" if incident_id is None else str(incident_id),
            "commit-group",
            target,
            alias,
        )
    )
    return str(uuid.uuid5(_ID_NAMESPACE, name))


@dataclass(frozen=True)
class Permission:
    """AgentMux's answer about the registry, as one group step reads it.

    ``grant`` is ``None`` for every answer that is not a complete yes. Mixed
    permission --- some repositories allowed, some refused --- is a refusal
    here and not a smaller commit, because a checkpoint of part of a registry
    is a claim about the run's work that the excluded repository contradicts.
    """

    grant: overseer_policy_client.Grant | None
    refusal: Refusal | None = None
    detail: str = ""
    decision: overseer_policy_client.Decision | None = None

    @property
    def allowed(self) -> bool:
        """Whether every participating repository may be written to."""
        return self.grant is not None


def summarize(record: Mapping[str, Any]) -> str:
    """One line naming what a group has committed and what it still owes."""
    committed = ", ".join(
        f"{_text(row.get('key'))} {_text(row.get('commit_sha'))[:12]}"
        for row in _rows(record, "successful_repositories")
    )
    skipped = ", ".join(
        _text(row.get("key")) for row in _rows(record, "skipped_repositories")
    )
    outstanding = ", ".join(
        str(alias) for alias in record.get("remaining_aliases") or ()
    )
    parts = [f"committed {committed}" if committed else "committed nothing"]
    if skipped:
        parts.append(f"clean {skipped}")
    if outstanding:
        parts.append(f"outstanding {outstanding}")
    return "; ".join(parts)
