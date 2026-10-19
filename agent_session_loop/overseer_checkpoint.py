"""Guarded checkpoint adapter and public checkpoint API.

Evidence, Git inspection, policy, and records live in focused sibling modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import commit_message, overseer_dispatch, overseer_store_models
from .overseer_checkpoint_evidence import (
    attribute,
    declared_repositories,
    read_verification,
)
from .overseer_checkpoint_evidence import read_activity as read_activity
from .overseer_checkpoint_git import checkpoint_id, checkpoint_message, find_checkpoint
from .overseer_checkpoint_git import dirty_paths as dirty_paths
from .overseer_checkpoint_models import AUTHORITY_REFUSALS as AUTHORITY_REFUSALS
from .overseer_checkpoint_models import CHECKPOINT_OPERATIONS as CHECKPOINT_OPERATIONS
from .overseer_checkpoint_models import CHECKPOINT_TRAILER as CHECKPOINT_TRAILER
from .overseer_checkpoint_models import LEGACY_REPOSITORY_KEY as LEGACY_REPOSITORY_KEY
from .overseer_checkpoint_models import REFUSALS as REFUSALS
from .overseer_checkpoint_models import VERIFICATION_STATES as VERIFICATION_STATES
from .overseer_checkpoint_models import Activity as Activity
from .overseer_checkpoint_models import Attribution as Attribution
from .overseer_checkpoint_models import (
    Authority,
    CheckpointUncertain,
    Prepared,
    Repositories,
)
from .overseer_checkpoint_models import AuthorityRefusal as AuthorityRefusal
from .overseer_checkpoint_models import Operation as Operation
from .overseer_checkpoint_models import Refusal as Refusal
from .overseer_checkpoint_models import Verification as Verification
from .overseer_checkpoint_models import VerificationState as VerificationState
from .overseer_checkpoint_policy import (
    _decision_id,
    _drift,
    _key,
    _phrase,
    _same_decision,
    authority_refusal,
)

__all__ = [
    "AUTHORITY_REFUSALS",
    "CHECKPOINT_OPERATIONS",
    "CHECKPOINT_TRAILER",
    "LEGACY_REPOSITORY_KEY",
    "REFUSALS",
    "VERIFICATION_STATES",
    "Activity",
    "Attribution",
    "AuthorityRefusal",
    "CheckpointAdapter",
    "CheckpointUncertain",
    "Prepared",
    "Verification",
    "adapters",
    "attribute",
    "authority_refusal",
    "checkpoint_id",
    "checkpoint_message",
    "declared_repositories",
    "dirty_paths",
    "find_checkpoint",
    "read_activity",
    "read_verification",
]


@dataclass(frozen=True)
class CheckpointAdapter:
    """The ``commit`` operation, behind the dispatcher's guards.

    ``authority`` is asked for a fresh ruling every time rather than handed one
    at construction, because an adapter holding a ruling is a cached permission
    with extra steps. ``prepared`` carries one preparation forward to the
    execution that consumes it, keyed on the plan, incident and target the
    action names, and is emptied by that execution --- so the memo is a handoff
    between two calls about one action rather than state this module keeps.
    """

    authority: Authority
    state_dir: Path | None = None
    repositories: Repositories = declared_repositories
    prepared: dict[str, Prepared] = field(
        default_factory=dict, repr=False, compare=False
    )

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Check authority, attribute the changes, then stage only what is owned."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Commit):
            return overseer_dispatch.Preparation.declined(
                _phrase("payload_mismatch", "not a commit")
            )
        ruling = self.authority(action)
        granted = authority_refusal(
            ruling,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
        )
        if granted is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*granted))
        if not payload.message.strip():
            return overseer_dispatch.Preparation.declined(
                _phrase("message_empty", "a checkpoint commits under a message")
            )
        attribution = attribute(
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            repositories=action.repositories,
            state_dir=self.state_dir,
        )
        if attribution.refusal is not None:
            return overseer_dispatch.Preparation.declined(
                _phrase(attribution.refusal, attribution.detail)
            )
        root = Path(attribution.root)
        staged = commit_message.stage_paths(root, attribution.owned)
        if staged.returncode != 0:
            return overseer_dispatch.Preparation.declined(
                _phrase("staging_failed", staged.stderr.strip() or "git add failed")
            )
        try:
            snapshot = commit_message.pathspec_snapshot(root, attribution.owned)
        except commit_message.CommitMessageError as error:
            return overseer_dispatch.Preparation.declined(
                _phrase("repository_unreadable", str(error))
            )
        self.prepared[_key(action.plan_dir, action.incident_id, action.target)] = (
            Prepared(
                target=action.target,
                alias=attribution.alias,
                root=attribution.root,
                paths=attribution.owned,
                snapshot=snapshot,
                baseline=attribution.baseline,
                verification=read_verification(
                    plan_dir=action.plan_dir,
                    run_id=action.run_id,
                    state_dir=self.state_dir,
                ),
                ruling=ruling,
            )
        )
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Re-establish authority, re-check the snapshot, then commit the owned set."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Commit):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not a commit")
            )
        prepared = self.prepared.pop(
            _key(action.plan_dir, action.incident_id, action.target), None
        )
        if prepared is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("not_prepared", "no preparation established this checkpoint")
            )
        current = self.authority(action)
        granted = authority_refusal(
            current,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
        )
        if granted is not None:
            return overseer_dispatch.Receipt.failed(_phrase(*granted))
        if not _same_decision(prepared.ruling, current):
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "authority_superseded",
                    f"{_decision_id(prepared.ruling)} was replaced by "
                    f"{_decision_id(current)}",
                )
            )
        root = Path(prepared.root)
        head = commit_message.head_sha(root)
        if head != prepared.baseline:
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "baseline_moved",
                    f"HEAD moved from {prepared.baseline} to {head or '(unreadable)'} "
                    "while the checkpoint was being prepared",
                )
            )
        try:
            commit_message.assert_pathspec_snapshot(
                root, prepared.paths, prepared.snapshot
            )
        except commit_message.CommitMessageError as error:
            return overseer_dispatch.Receipt.failed(_phrase(*_drift(error)))
        return self._commit(action, prepared)

    def reconcile(
        self,
        record: overseer_store_models.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Say whether a checkpoint a dead process may have made is in the history."""
        if record.operation != "commit":
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("payload_mismatch", record.operation)
            )
        identity = checkpoint_id(
            plan_dir=record.plan_dir,
            incident_id=record.incident_id,
            target=record.target,
        )
        repositories = self.repositories(record.plan_dir, state_dir=self.state_dir)
        if len(repositories) != 1:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase(
                    "repository_ambiguous",
                    f"the plan now declares {len(repositories)} repositories",
                ),
                locator=identity,
            )
        found = find_checkpoint(Path(repositories[0].path), identity)
        if found is None:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase(
                    "repository_unreadable", f"{repositories[0].path} could not be read"
                ),
                locator=identity,
            )
        if found:
            return overseer_dispatch.Receipt.executed(
                detail=f"{repositories[0].alias} {found}", locator=found
            )
        return overseer_dispatch.Receipt.failed(
            detail=_phrase("commit_absent", "no commit carries this checkpoint id"),
            locator=identity,
        )

    def _commit(
        self,
        action: overseer_dispatch.Action,
        prepared: Prepared,
    ) -> overseer_dispatch.Receipt:
        """Make the scoped commit and read back what became of it.

        git's exit code is not taken as the answer on its own. A commit that
        landed and then failed to be reported is indistinguishable at the exit
        code from one that never happened, so the history is asked either way
        and only a history that cannot be read is uncertain.
        """
        root = Path(prepared.root)
        identity = checkpoint_id(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
        )
        payload = action.payload
        message = checkpoint_message(
            payload.message if isinstance(payload, overseer_dispatch.Commit) else "",
            identity,
        )
        try:
            result = commit_message.commit_staged(
                root, message, pathspec=prepared.paths
            )
        except commit_message.CommitMessageError as error:
            raise CheckpointUncertain(str(error), locator=identity) from error
        found = find_checkpoint(root, identity)
        if found is None:
            raise CheckpointUncertain(
                f"the commit reported {result.returncode} and the history could "
                "not be read",
                locator=identity,
            )
        if found:
            return overseer_dispatch.Receipt.executed(
                detail=(
                    f"{prepared.alias} {found} on {prepared.baseline}; "
                    f"{len(prepared.paths)} "
                    f"{'path' if len(prepared.paths) == 1 else 'paths'}; "
                    f"verification={prepared.verification.state}"
                ),
                locator=found,
            )
        return overseer_dispatch.Receipt.failed(
            detail=_phrase(
                "commit_failed", result.stderr.strip() or result.stdout.strip()
            ),
            locator=identity,
        )


def adapters(
    *,
    authority: Authority,
    state_dir: Path | None = None,
    repositories: Repositories = declared_repositories,
) -> overseer_dispatch.Registry:
    """Build a dispatcher registry holding exactly the checkpoint adapter.

    The other four operations are absent rather than stubbed, because
    :meth:`~agent_session_loop.overseer_dispatch.Registry.supports` is what the
    single door consults and a stub that refuses is indistinguishable from an
    adapter that failed.
    """
    return overseer_dispatch.registry(
        {
            "commit": CheckpointAdapter(
                authority=authority,
                state_dir=state_dir,
                repositories=repositories,
            )
        }
    )
