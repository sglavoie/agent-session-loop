"""The ``commit`` operation across a plan's whole repository registry.

:mod:`agent_session_loop.overseer_checkpoint` checkpoints one repository, and
refuses outright when a plan declares more than one. This module is the
multi-repository form of the same operation: it authorizes every participating
repository before it stages anything, records what it established in a durable
group in the overseer ledger, and then commits the repositories one at a time,
rewriting that group at every boundary.

The two properties everything here is arranged around are that a partial group
is *reported* rather than repaired, and that a retry *reconciles* rather than
repeats. A commit that landed is never rolled back and never made twice: the
per-repository :data:`~agent_session_loop.overseer_checkpoint.CHECKPOINT_TRAILER`
identity is derived from the action, so a process that crashed between the
commit and the record it owed can be told what it did by the process that
follows it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    commit_groups,
    commit_message,
    overseer_checkpoint,
    overseer_decisions,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
)

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


@dataclass(frozen=True)
class GroupAdapter:
    """The ``commit`` operation over a registry, behind the dispatcher's guards.

    ``authority`` and ``authorize`` are separate seams because they answer
    separate questions: the ruling says an operator decided this checkpoint
    should happen, and the policy client says AgentMux still permits writing to
    these repositories. Both are asked again before every commit rather than
    once for the group, so a revocation that arrives mid-group stops the next
    repository instead of being noticed after the last one.

    No preparation is memoised in the process. The durable group in the ledger
    *is* the handoff from :meth:`prepare` to :meth:`execute`, which is what lets
    the two run in different processes with a crash in between.
    """

    authority: Authority
    ledger: overseer_store_ledger.Ledger
    state_dir: Path | None = None
    repositories: Repositories = overseer_checkpoint.declared_repositories
    authorize: Authorize = field(default=overseer_policy_client.authorize, repr=False)
    timeout: float = 5.0

    # -- the dispatcher's three entry points -------------------------------

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Authorize the registry, attribute every repository, then stage.

        Attribution of *all* the outstanding repositories happens before any of
        them is staged, so a registry that cannot be checkpointed as a whole is
        refused without having touched an index. Staging itself is still
        repository by repository and can stop partway; when it does, the
        refusal names the repositories already staged rather than leaving the
        operator to discover them.
        """
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Commit):
            return _decline("payload_mismatch", "not a commit")
        ruling = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            ruling,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
        )
        if granted is not None:
            return _decline(*granted)
        if not payload.message.strip():
            return _decline("message_empty", "a checkpoint commits under a message")
        repositories = action.repositories
        if not repositories:
            return _decline("registry_empty", "this action names no repository")

        permission = self._permit(action)
        if permission.grant is None:
            return _decline(
                permission.refusal or "policy_unavailable", permission.detail
            )

        key = group_key(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
        )
        try:
            stored = self.ledger.checkpoint_group(key)
        except overseer_store_models.LedgerError as error:
            return _decline("group_unreadable", str(error))
        ordered = [repository.alias for repository in repositories]
        previous: dict[str, Any] | None = None
        if stored is not None:
            if stored.settled:
                return _decline("group_settled", summarize(stored.record))
            try:
                previous = commit_groups.validate_group(stored.record)
            except commit_groups.CommitGroupError as error:
                return _decline("group_unreadable", str(error))
            if previous["ordered_aliases"] != ordered:
                return _decline(
                    "registry_changed",
                    f"the group was prepared over "
                    f"{', '.join(previous['ordered_aliases'])}",
                )

        outstanding = (
            list(previous["remaining_aliases"]) if previous is not None else ordered
        )
        attributions: dict[str, overseer_checkpoint.Attribution] = {}
        for repository in repositories:
            if repository.alias not in outstanding:
                continue
            attribution = overseer_checkpoint.attribute(
                plan_dir=action.plan_dir,
                run_id=action.run_id,
                repositories=(repository,),
                state_dir=self.state_dir,
            )
            if attribution.refusal == "nothing_to_commit":
                attributions[repository.alias] = attribution
                continue
            if attribution.refusal is not None:
                return _decline(
                    attribution.refusal,
                    f"{repository.alias}: {attribution.detail}",
                )
            attributions[repository.alias] = attribution
        if previous is None and all(
            attribution.clean for attribution in attributions.values()
        ):
            return _decline(
                "nothing_to_commit", "every declared worktree is already clean"
            )

        rows: list[dict[str, Any]] = []
        heads: dict[str, str | None] = {}
        staged_already: list[str] = []
        previous_rows = _target_rows(previous)
        for repository in repositories:
            alias = repository.alias
            if alias not in attributions:
                rows.append(previous_rows[alias])
                heads[alias] = (previous or {}).get("initial_heads", {}).get(alias)
                continue
            attribution = attributions[alias]
            root = Path(attribution.root)
            if attribution.clean:
                snapshot = ""
            else:
                staged = commit_message.stage_paths(root, attribution.owned)
                if staged.returncode != 0:
                    return _decline(
                        "staging_failed",
                        f"{alias}: {staged.stderr.strip() or 'git add failed'}"
                        + (
                            f"; already staged {', '.join(staged_already)}"
                            if staged_already
                            else ""
                        ),
                    )
                staged_already.append(alias)
                try:
                    snapshot = commit_message.pathspec_snapshot(root, attribution.owned)
                except commit_message.CommitMessageError as error:
                    return _decline("repository_unreadable", f"{alias}: {error}")
            row = commit_groups.create_target(
                alias=alias,
                root=root,
                repository_path=attribution.root,
                qualified_paths=attribution.owned,
                staging_paths=attribution.owned,
            )
            row["checkpoint_id"] = repository_identity(
                plan_dir=action.plan_dir,
                incident_id=action.incident_id,
                target=action.target,
                alias=alias,
            )
            row["snapshot"] = snapshot
            row["baseline"] = attribution.baseline or attribution.head
            row["clean"] = attribution.clean
            rows.append(row)
            # The head the *run* recorded, not the one the repository is on, so
            # a repository that is clean because this group already committed
            # in it is recovered rather than skipped. Their current heads are
            # identical -- the checkpoint commit -- and only the recorded
            # baseline still names where the work started. For a repository
            # nothing ever changed in the run recorded nothing, and the
            # fallback leaves the two equal, which is the clean skip.
            heads[alias] = attribution.baseline or attribution.head

        group = self._compose(action, payload, rows, heads, previous, ruling)
        try:
            commit_groups.validate_group(group)
        except commit_groups.CommitGroupError as error:
            return _decline("group_unreadable", str(error))
        try:
            self.ledger.open_checkpoint_group(
                grant=permission.grant,
                plan_dir=action.plan_dir,
                group_key=key,
                target=action.target,
                record=group,
                incident_id=action.incident_id,
                phase="prepared",
            )
        except (
            overseer_store_models.LedgerError,
            overseer_store_models.LedgerRefused,
        ) as error:
            return _decline("ledger_refused", str(error))
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Drive the durable group to completion, or to its first failure.

        Every repository is reconciled against its own trailer before it is
        committed, so a repository this group already committed --- in this
        process or in one that died --- is recorded rather than committed
        again. A failure stops the group where it stands: the successful
        commits stay, the ledger row keeps them, and the receipt reports the
        partial result instead of undoing it.
        """
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Commit):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not a commit")
            )
        key = group_key(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
        )
        try:
            stored = self.ledger.checkpoint_group(key)
        except overseer_store_models.LedgerError as error:
            return overseer_dispatch.Receipt.failed(
                _phrase("group_unreadable", str(error)), locator=key
            )
        if stored is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("not_prepared", "no preparation established this checkpoint"),
                locator=key,
            )
        if stored.settled:
            return overseer_dispatch.Receipt.executed(
                detail=summarize(stored.record), locator=key
            )
        try:
            group = commit_groups.validate_group(stored.record)
        except commit_groups.CommitGroupError as error:
            return overseer_dispatch.Receipt.failed(
                _phrase("group_unreadable", str(error)), locator=key
            )
        current = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            current,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
        )
        if granted is not None:
            return overseer_dispatch.Receipt.failed(_phrase(*granted), locator=key)
        if _decision_identity(current) != group.get("authority"):
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "authority_superseded",
                    f"{_decision_id(group.get('authority'))} was replaced by "
                    f"{_decision_id(_decision_identity(current))}",
                ),
                locator=key,
            )

        final, failed = commit_groups.coordinate_group(
            group,
            is_clean=self._is_clean,
            read_head=self._read_head,
            run_commit=lambda target, _group_id: self._commit(
                action, target, stored=stored
            ),
            persist=lambda record, phase: self._persist(key, record, phase),
            skip_clean=True,
        )
        summary = summarize(final)
        if failed is None:
            return overseer_dispatch.Receipt.executed(
                detail=f"{summary}; verification={_verification_state(final)}",
                locator=key,
            )
        refusal = _refusal_name(failed.get("refusal"))
        return overseer_dispatch.Receipt.failed(
            detail=(
                f"{_phrase(refusal, _text(failed.get('detail')))} "
                f"in {_text(final.get('failed_alias'))}; {summary}"
            ),
            locator=key,
        )

    def reconcile(
        self,
        record: overseer_store_models.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Say what a group a dead process may have made actually committed.

        A read and only a read. What it finds is not written back into the
        durable group, because the place a recovered commit is *recorded* is
        the retry that reconciles it into the group's own outcomes --- writing
        it here as well would give one commit two authors, and one of them no
        surviving evidence of the head it was made on.

        Reading Git history is reading a repository, so this asks AgentMux
        before it opens one, and it asks about the registry the plan declares
        *now*. The stored group's own roots are then required to be in that
        answer. A group prepared over a repository an operator has since
        excluded --- or moved --- would otherwise send reconciliation to a path
        nobody currently permits, on the strength of a record written when they
        did. Anything short of a complete yes is ``uncertain`` rather than
        ``failed``: a checkpoint we may not look for is not a checkpoint we
        know did not happen.
        """
        if record.operation != "commit":
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("payload_mismatch", record.operation)
            )
        key = group_key(
            plan_dir=record.plan_dir,
            incident_id=record.incident_id,
            target=record.target,
        )
        declared = self.repositories(record.plan_dir, state_dir=self.state_dir)
        if not declared:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("registry_empty", "the plan now declares no repository"),
                locator=key,
            )
        permission = self._permit_registry(record.plan_dir, declared)
        if permission.grant is None:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase(
                    permission.refusal or "policy_unavailable", permission.detail
                ),
                locator=key,
            )
        try:
            stored = self.ledger.checkpoint_group(key)
        except overseer_store_models.LedgerError as error:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("group_unreadable", str(error)), locator=key
            )
        if stored is not None and stored.settled:
            return overseer_dispatch.Receipt.executed(
                detail=summarize(stored.record), locator=key
            )
        permitted = {repository.alias: Path(repository.path) for repository in declared}
        aliases = _group_roots(stored) or {
            alias: str(root) for alias, root in permitted.items()
        }
        if not aliases:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("registry_empty", "the plan now declares no repository"),
                locator=key,
            )
        unpermitted = sorted(
            alias
            for alias, path in aliases.items()
            if permitted.get(alias) != Path(path)
        )
        if unpermitted:
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase(
                    "registry_changed",
                    f"the group's roots for {', '.join(unpermitted)} are not the "
                    "ones the run now declares and AgentMux now permits",
                ),
                locator=key,
            )
        landed: list[str] = []
        absent: list[str] = []
        for alias, path in aliases.items():
            identity = repository_identity(
                plan_dir=record.plan_dir,
                incident_id=record.incident_id,
                target=record.target,
                alias=alias,
            )
            found = overseer_checkpoint.find_checkpoint(Path(path), identity)
            if found is None:
                return overseer_dispatch.Receipt.uncertain(
                    detail=_phrase(
                        "repository_unreadable", f"{alias}: {path} could not be read"
                    ),
                    locator=key,
                )
            if found:
                landed.append(f"{alias} {found[:12]}")
            else:
                absent.append(alias)
        if not absent:
            return overseer_dispatch.Receipt.executed(
                detail=", ".join(landed), locator=key
            )
        if not landed:
            return overseer_dispatch.Receipt.failed(
                detail=_phrase(
                    "commit_absent", "no repository carries this group's checkpoint"
                ),
                locator=key,
            )
        return overseer_dispatch.Receipt.failed(
            detail=_phrase(
                "commit_absent",
                f"committed {', '.join(landed)}; outstanding {', '.join(absent)}",
            ),
            locator=key,
        )

    # -- the seams the three entry points are built from -------------------

    def _permit(self, action: overseer_dispatch.Action) -> Permission:
        """Ask AgentMux about the whole registry, and record the answer.

        Called once in preparation and again before each individual commit.
        Repeating it is the point: a grant is an answer about the moment it was
        given, and a group spends several moments.
        """
        return self._permit_registry(action.plan_dir, action.repositories)

    def _permit_registry(
        self,
        plan_dir: str,
        repositories: tuple[overseer_policy_client.Repository, ...],
    ) -> Permission:
        """The same question, asked about a registry rather than about an action.

        Reconciliation has a ledger row and no action, so it has no
        ``action.repositories`` to ask about and has to name the set discovery
        reads now. Both callers arrive here so there is one place the answer is
        interpreted and one place it is recorded.
        """
        decision = self.authorize(
            plan_dir=plan_dir,
            access=overseer_policy_client.ACCESS_ACTION,
            repositories=repositories,
            timeout=self.timeout,
        )
        self.ledger.record_decision(decision)
        if decision.config_fingerprint:
            self.ledger.note_policy(config_fingerprint=decision.config_fingerprint)
        grant = decision.grant()
        if grant is not None:
            return Permission(grant=grant, decision=decision)
        if not decision.available:
            return Permission(
                grant=None,
                refusal="policy_unavailable",
                detail=decision.summary
                or f"AgentMux could not be asked: {decision.reason}",
                decision=decision,
            )
        refused = decision.refused_repositories()
        if refused:
            names = ", ".join(outcome.alias for outcome in refused)
            return Permission(
                grant=None,
                refusal="repository_excluded",
                detail=(
                    f"AgentMux refused {names}: {decision.summary or decision.reason}"
                ),
                decision=decision,
            )
        return Permission(
            grant=None,
            refusal="repository_excluded",
            detail=decision.summary
            or f"AgentMux refused this group: {decision.reason}",
            decision=decision,
        )

    def _is_clean(self, target: Mapping[str, Any]) -> bool:
        """Whether this repository needs no commit *and* can be asked about one.

        Both halves matter. A worktree that is clean because the commit already
        happened and one that is clean because nothing changed are told apart by
        :meth:`_read_head`, but only if the history can be read at all --- so a
        repository git cannot answer for is reported dirty here and refused by
        :meth:`_commit`, rather than skipped as clean.
        """
        root = Path(_text(target.get("root")))
        dirty = overseer_checkpoint.dirty_paths(root)
        if dirty is None or dirty:
            return False
        return (
            overseer_checkpoint.find_checkpoint(
                root, _text(target.get("checkpoint_id"))
            )
            is not None
        )

    def _read_head(self, target: Mapping[str, Any]) -> str | None:
        """The sha this repository's own checkpoint is at, if it has one.

        Answering with the *trailer's* commit rather than with HEAD is what
        keeps a clean repository from being credited with somebody else's
        commit: a run that stopped after committing and a run whose repository
        was advanced by an operator both leave a clean worktree on a moved
        HEAD, and only the first left our identity behind.
        """
        root = Path(_text(target.get("root")))
        found = overseer_checkpoint.find_checkpoint(
            root, _text(target.get("checkpoint_id"))
        )
        return found or None

    def _commit(
        self,
        action: overseer_dispatch.Action,
        target: Mapping[str, Any],
        *,
        stored: overseer_store_models.CheckpointGroupRecord,
    ) -> dict[str, Any]:
        """Recheck permission, reconcile, and commit one repository.

        The order is the order the questions can be answered in: may this still
        be written to, has it already been written to by this group, is it
        still where preparation left it, and only then commit. Reconciling
        before committing is what makes a retry safe --- the second attempt
        finds the first attempt's commit instead of making another.
        """
        alias = _text(target.get("key"))
        root = Path(_text(target.get("root")))
        identity = _text(target.get("checkpoint_id"))
        paths = tuple(str(path) for path in target.get("staging_paths") or ())

        permission = self._permit(action)
        if permission.grant is None:
            return _failure(
                permission.refusal or "policy_unavailable",
                f"{alias}: {permission.detail}",
            )
        if permission.grant.config_fingerprint != stored.config_fingerprint:
            return _failure(
                "policy_superseded",
                f"{alias}: policy moved from {stored.config_fingerprint[:12]} to "
                f"{permission.grant.config_fingerprint[:12]} mid-group",
            )

        found = overseer_checkpoint.find_checkpoint(root, identity)
        if found is None:
            return _failure(
                "repository_unreadable", f"{alias}: {root} could not be read"
            )
        if found:
            return {
                "success": True,
                "commit_sha": found,
                "commit_index": None,
                "recovered": True,
                "status_result": "already-committed",
            }

        head = commit_message.head_sha(root)
        if head != _text(target.get("baseline")):
            return _failure(
                "baseline_moved",
                f"{alias}: HEAD moved from {_text(target.get('baseline'))} to "
                f"{head or '(unreadable)'} while the group was being made",
            )
        try:
            commit_message.assert_pathspec_snapshot(
                root, paths, _text(target.get("snapshot"))
            )
        except commit_message.CommitMessageError as error:
            return _failure(*_drift(alias, error))

        message = overseer_checkpoint.checkpoint_message(
            _text(_verbatim_message(action)), identity
        )
        try:
            result = commit_message.commit_staged(root, message, pathspec=paths)
        except commit_message.CommitMessageError as error:
            raise overseer_checkpoint.CheckpointUncertain(
                f"{alias}: {error}", locator=identity
            ) from error
        landed = overseer_checkpoint.find_checkpoint(root, identity)
        if landed is None:
            raise overseer_checkpoint.CheckpointUncertain(
                f"{alias}: the commit reported {result.returncode} and the history "
                "could not be read",
                locator=identity,
            )
        if landed:
            return {
                "success": True,
                "commit_sha": landed,
                "commit_index": None,
                "recovered": False,
                "status_result": "committed",
            }
        return _failure(
            "commit_failed",
            f"{alias}: {result.stderr.strip() or result.stdout.strip()}",
        )

    def _persist(self, key: str, record: dict[str, Any], phase: str) -> None:
        """Rewrite the durable group at one boundary, or fail the whole group.

        A persistence failure is deliberately not swallowed. A group that goes
        on committing after it stopped being able to record what it committed
        is exactly the run that duplicates commits after a restart.
        """
        self.ledger.save_checkpoint_group(group_key=key, record=record, phase=phase)

    def _compose(
        self,
        action: overseer_dispatch.Action,
        payload: overseer_dispatch.Commit,
        rows: Sequence[Mapping[str, Any]],
        heads: Mapping[str, str | None],
        previous: Mapping[str, Any] | None,
        ruling: overseer_decisions.Ruling | None,
    ) -> dict[str, Any]:
        """Build the durable group, carrying any earlier outcomes forward.

        A retry composes a *new* obligation over the repositories still owed,
        and copies the previous attempt's successes and skips into it verbatim.
        It never recomputes them: the head a commit was made on is a fact about
        a moment that has passed, and re-deriving it from the repository as it
        stands now would quietly relabel what happened.
        """
        group = commit_groups.create_group(
            scope=GROUP_SCOPE,
            session_id=None,
            targets=rows,
            initial_heads=heads,
            verbatim_message=payload.message,
        )
        if previous is not None:
            group["successful_repositories"] = list(
                previous.get("successful_repositories") or ()
            )
            group["skipped_repositories"] = list(
                previous.get("skipped_repositories") or ()
            )
            group["created_at"] = previous.get("created_at", group["created_at"])
            finished = {
                _text(row.get("key"))
                for field_name in ("successful_repositories", "skipped_repositories")
                for row in _rows(group, field_name)
            }
            group["remaining_aliases"] = [
                alias for alias in group["ordered_aliases"] if alias not in finished
            ]
            group["initial_heads"] = {
                alias: (
                    previous.get("initial_heads", {}).get(alias)
                    if alias not in group["remaining_aliases"]
                    else heads.get(alias)
                )
                for alias in group["ordered_aliases"]
            }
        group["plan_dir"] = action.plan_dir
        group["run_id"] = action.run_id
        group["incident_id"] = action.incident_id
        group["target"] = action.target
        group["authority"] = _decision_identity(ruling)
        group["verification"] = overseer_checkpoint.read_verification(
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            state_dir=self.state_dir,
        ).state
        return group


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
