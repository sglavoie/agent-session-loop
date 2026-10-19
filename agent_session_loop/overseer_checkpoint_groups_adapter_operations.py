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


class _GroupAdapterOperations:
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
