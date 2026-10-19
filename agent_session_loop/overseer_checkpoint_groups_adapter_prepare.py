# mypy: disable-error-code="name-defined,attr-defined"
# ruff: noqa: F403, F405
from . import overseer_checkpoint_groups_part_1 as _part_1
from . import overseer_checkpoint_groups_shared as _shared
from .overseer_checkpoint_groups_shared import *

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
globals().update(
    {name: value for name, value in vars(_part_1).items() if not name.startswith("__")}
)


class _GroupAdapterPreparation:
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
