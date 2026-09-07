"""The overseer's checkpoint: commit only what this run is provably responsible for.

A checkpoint is the one overseer operation that writes into a *repository*, so
it is the one where "act on the right thing" is not a convenience. Everything
here exists to make one claim checkable before a commit is made: that every
change about to be committed belongs to the run being checkpointed. The claim is
established from the four sources the run itself already published --- the
baseline its own artifacts recorded, the paths its own write guard observed, the
repositories its plan declared writable, and the diff in the worktree right now
--- and any change those four cannot account for stops the checkpoint rather
than being swept in with the rest.

## Ownership is established, never assumed

:func:`attribute` reads the run's ``turn-`` and ``commit-`` artifacts, takes the
newest HEAD they recorded for the declared repository as the *baseline*, and
takes the union of the qualified paths they recorded as the run's *activity*.
Then it looks at the worktree. A dirty path the activity names is ``owned``; a
dirty path it does not name is ``unattributed``, and one unattributed path
refuses the whole checkpoint. That is deliberately strict: a supervisor that
committed a change it could not attribute would be putting somebody else's work
under this run's name, and there is no way to undo that from here.

The baseline is checked as well as read. If HEAD is not where the run's own
evidence left it, something committed into this repository that this run did not
record, and the diff sitting on top of it is measured from a foundation the run
never established --- ``baseline_moved``, and nothing is staged. A run whose
artifacts recorded no head at all is ``baseline_unknown`` for the same reason:
there is nothing to measure from, and guessing "HEAD is fine" would be inventing
the evidence this module exists to check.

Single repository, by refusal rather than by convention. An action naming zero
or several writable repositories is ``repository_ambiguous``. Isolating a change
set across two repositories needs a commit protocol with two-phase properties
that git does not have, so this module refuses the case rather than performing
half of it.

## Authorize, then prepare --- because preparing stages files

``asl commit --prepare-only`` stages before it emits: ``_stage_for_commit``
defaults to ``git add -A``, which would take the whole worktree, unattributed
paths and all. So nothing here goes through that path. :meth:`CheckpointAdapter.prepare`
checks authority as its first act, attributes the changes, and only then stages
--- with :func:`agent_session_loop.commit_message.stage_paths`, which names the
owned paths and touches no other index entry. The dispatcher has already
authorized once before ``prepare`` is called and authorizes again after it; this
module's own check is the third, and it is the one that runs before any file is
staged.

Preparation records what it established in :class:`Prepared`, and
:meth:`CheckpointAdapter.execute` *consumes* that record: an execute with no
preparation to consume commits nothing and fails ``not_prepared``. The snapshot
check is therefore not something a caller can forget to ask for --- there is no
path to a commit that does not go through a preparation that took one.

## The commit is scoped, so unrelated work and staging survive it

:func:`agent_session_loop.commit_message.commit_staged` with a pathspec builds a
throwaway ``GIT_INDEX_FILE`` from HEAD plus the named staged entries and commits
that. The real index is never written, so an entry staged before the checkpoint
is neither committed by it nor unstaged by it, and a worktree change outside the
owned set is left exactly where it was. Between preparation and execution,
:func:`agent_session_loop.commit_message.assert_pathspec_snapshot` reports the
two ways the owned set can move underneath us --- an owned file edited on disk is
``worktree_drifted``, an owned file re-staged is ``snapshot_mismatch`` --- and
both refuse without committing.

## A checkpoint reports; it does not decide

The commit sha and the verification state both go into the receipt, and the
verification state is read out of the run's newest recorded verification rather
than produced here: ``passed`` only when the run recorded ``passed`` true,
``failed`` when it recorded false, ``unresolved`` when it recorded a block that
resolved nothing, ``unrecorded`` when there is no block at all. Nothing in this
module runs a check, and nothing in it writes a session file --- there is no
import here that could --- so "must not mark an incomplete session complete" and
"must not claim failed checks passed" are properties of what this module can
reach rather than rules it remembers to follow. A checkpoint preserves unfinished
work; it does not finish it.

## A crash is answerable, because the identity is derived

Every checkpoint commit carries an ``Asl-Checkpoint-Id`` trailer whose value is
:func:`checkpoint_id`: a UUID5 over the plan directory, the incident, the
operation and the dispatch target --- the four fields carried by both an
:class:`~agent_session_loop.overseer_dispatch.Action` and a stored
:class:`~agent_session_loop.overseer_store.ActionRecord`. Nothing about the
moment goes into it. So :meth:`CheckpointAdapter.reconcile` can ask git whether
that exact commit exists, from the ledger row alone, with no timestamp heuristic
and no risk of reading another supervisor's checkpoint as our own. A repository
that cannot be read answers ``uncertain`` rather than ``failed``, because a
repository nobody could open is not evidence that a commit is absent.

Durable records this boundary writes: **one commit in one repository**, plus the
index entries :func:`agent_session_loop.commit_message.stage_paths` adds for the
owned paths. It writes nothing into any plan's state directory and nothing into
the ledger --- the dispatcher owns that row.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    commit_message,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store,
    state,
)

__all__ = [
    "CHECKPOINT_OPERATIONS",
    "CHECKPOINT_TRAILER",
    "LEGACY_REPOSITORY_KEY",
    "REFUSALS",
    "VERIFICATION_STATES",
    "Activity",
    "Attribution",
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

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other four are somebody else's adapters.
CHECKPOINT_OPERATIONS: tuple[Operation, ...] = ("commit",)

#: The trailer a checkpoint commit carries, and the only thing
#: :func:`find_checkpoint` matches on. A trailer rather than a subject prefix so
#: the operator-facing message is whatever the action asked for.
CHECKPOINT_TRAILER = "Asl-Checkpoint-Id"

#: The repository key a single-repository run's evidence uses instead of an
#: alias. Accepted alongside the alias because the run that wrote the evidence,
#: not this module, decides which vocabulary it used.
LEGACY_REPOSITORY_KEY = "."

VerificationState = Literal["unrecorded", "unresolved", "passed", "failed"]

#: What the run's own records say about its checks, ordered from least to most
#: known. Kept four-valued rather than collapsed to a boolean because "the run
#: recorded no verification" and "the run recorded a failing verification" are
#: opposite things for an operator to read, and neither is "passed".
VERIFICATION_STATES: tuple[VerificationState, ...] = (
    "unrecorded",
    "unresolved",
    "passed",
    "failed",
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
    "repository_ambiguous",
    "repository_unreadable",
    "nothing_to_commit",
    "baseline_unknown",
    "baseline_moved",
    "changes_unattributed",
    "staging_failed",
    "not_prepared",
    "worktree_drifted",
    "snapshot_mismatch",
    "commit_failed",
    "commit_absent",
]

#: Why a checkpoint did not happen. Closed, and deliberately separate from the
#: dispatcher's refusals: these are what an adapter reports about a repository,
#: not what the single door decided about permission.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "message_empty",
    "repository_ambiguous",
    "repository_unreadable",
    "nothing_to_commit",
    "baseline_unknown",
    "baseline_moved",
    "changes_unattributed",
    "staging_failed",
    "not_prepared",
    "worktree_drifted",
    "snapshot_mismatch",
    "commit_failed",
    "commit_absent",
)

#: The UUID5 namespace checkpoint identities are derived in. Fixed, because an
#: id that changed between builds would make every pre-upgrade commit
#: unreconcilable.
_ID_NAMESPACE = uuid.UUID("9c2f5b71-3d84-5f0a-8f6c-1a4b7e2d9053")

#: The evidence keys a turn's write-guard record uses for one repository. Read
#: rather than recomputed: a second capture would be taken at a different
#: instant, so a path that moved in between could be attributed by one reading
#: and disowned by the other.
_WRITE_GUARD_KEY = "workspace_write_guard"
_CHANGED_KEY = "changed"


class CheckpointUncertain(Exception):
    """A commit whose outcome this module refuses to guess.

    Raised only when git could neither report the commit nor be re-read, so "it
    landed" and "it did not" are both unsupported. The dispatcher turns this
    into ``outcome_uncertain`` and leaves the ledger row attempted, which is
    what :meth:`CheckpointAdapter.reconcile` is later able to settle.
    """

    def __init__(self, detail: str, *, locator: str = "") -> None:
        self.detail = detail
        self.locator = locator
        super().__init__(detail)


@dataclass(frozen=True)
class Verification:
    """What the run's newest recorded verification actually says.

    Read out of the run's own artifact and never recomputed, so a checkpoint
    reports the check result the run reached rather than a fresher one nobody
    ran.
    """

    state: VerificationState
    session: str = ""
    reason: str = ""
    failures: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        """Whether the run recorded checks that actually passed."""
        return self.state == "passed"


@dataclass(frozen=True)
class Activity:
    """What one run's own artifacts say it did to one repository.

    ``baseline`` is the newest HEAD those artifacts recorded and ``paths`` is
    every repository-relative path they observed changing, deduplicated in the
    order they were first seen.
    """

    alias: str
    baseline: str
    paths: tuple[str, ...]
    records: int = 0

    @property
    def observed(self) -> bool:
        """Whether the run published anything to attribute a change to."""
        return bool(self.baseline)


@dataclass(frozen=True)
class Attribution:
    """Who owns the changes currently in one repository, and why.

    ``refusal`` ``None`` is the only value that authorizes staging. Every other
    field is reported in all cases, refused or not, so an operator reading a
    refusal sees the evidence it was reached from rather than only its name.
    """

    alias: str
    root: str
    baseline: str
    head: str
    dirty: tuple[str, ...] = ()
    owned: tuple[str, ...] = ()
    unattributed: tuple[str, ...] = ()
    activity: tuple[str, ...] = ()
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def safe(self) -> bool:
        """Whether every change in the worktree belongs to this run."""
        return self.refusal is None

    @property
    def clean(self) -> bool:
        """Whether the repository has nothing to checkpoint at all."""
        return not self.dirty


@dataclass(frozen=True)
class Prepared:
    """What one preparation established, for the execution that consumes it.

    ``ruling`` is held to be *compared*, never to authorize: execution
    establishes authority again through the adapter's seam and requires the two
    to rest on the same unchanged decision. An adapter that authorized from this
    field would be caching a permission, which is the thing the second check
    exists to catch.
    """

    target: str
    alias: str
    root: str
    paths: tuple[str, ...]
    snapshot: str
    baseline: str
    verification: Verification
    ruling: overseer_decisions.Ruling | None


def checkpoint_id(*, plan_dir: str, incident_id: int | None, target: str) -> str:
    """Return the identity one checkpoint commit always carries.

    Derived rather than generated, from the four fields an action and a ledger
    row both carry, so a commit made by a process that then died is findable by
    a process that never saw it.
    """
    name = "\x00".join(
        (plan_dir, "" if incident_id is None else str(incident_id), "commit", target)
    )
    return str(uuid.uuid5(_ID_NAMESPACE, name))


def checkpoint_message(message: str, identity: str) -> str:
    """Attach the reconciliation trailer to an action's commit message."""
    body = commit_message.strip_disallowed_trailers(message).strip()
    return f"{body}\n\n{CHECKPOINT_TRAILER}: {identity}\n"


def find_checkpoint(root: Path, identity: str) -> str | None:
    """The sha of the commit carrying ``identity``, ``""`` if none, or ``None``.

    ``None`` means git could not be asked, which is not evidence that the
    commit is absent. Keeping that apart from ``""`` is what lets a caller
    answer ``uncertain`` instead of inventing a negative.
    """
    result = _git(
        root,
        [
            "log",
            "--max-count=1",
            "--format=%H",
            "--fixed-strings",
            f"--grep={CHECKPOINT_TRAILER}: {identity}",
        ],
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def declared_repositories(
    plan_dir: str,
    *,
    state_dir: str | Path | None = None,
) -> tuple[overseer_policy_client.Repository, ...]:
    """The repositories a plan's own record declares, resolved on this host.

    Read from discovery rather than from a repository the caller names, because
    the case this exists for is reconciliation: the ledger row carries a plan
    directory and nothing about where that plan's code lives.
    """
    inventory = overseer_discovery.build_inventory(state_dir=state_dir)
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return overseer_policy_client.repositories_for(candidate.repositories)
    return ()


def read_activity(
    *,
    plan_dir: str,
    run_id: str,
    alias: str,
    state_dir: str | Path | None = None,
) -> Activity:
    """Read one run's own account of what it did to one repository.

    Both artifact families are read because they record the same fact at
    different moments: a ``commit-`` record names the head the run's last commit
    produced, and a ``turn-`` record's write-guard evidence names the head a turn
    ran against together with the paths that turn changed. Oldest to newest, so
    the last head seen is the newest one recorded.
    """
    baseline = ""
    paths: list[str] = []
    records = 0
    for record in _run_records(plan_dir=plan_dir, run_id=run_id, state_dir=state_dir):
        head, changed = _repository_evidence(record, alias)
        if head == "" and not changed:
            continue
        records += 1
        if head:
            baseline = head
        paths.extend(changed)
    return Activity(
        alias=alias,
        baseline=baseline,
        paths=tuple(dict.fromkeys(paths)),
        records=records,
    )


def read_verification(
    *,
    plan_dir: str,
    run_id: str,
    state_dir: str | Path | None = None,
) -> Verification:
    """Read the run's newest recorded verification, without running anything.

    Newest-recorded rather than newest-turn: a turn that paused before it
    verified writes no verification block, and reading that absence as "no
    verification" would describe the artifact layout rather than the run.
    """
    newest: Mapping[str, Any] | None = None
    for record in _run_records(plan_dir=plan_dir, run_id=run_id, state_dir=state_dir):
        block = record.get("verification")
        if isinstance(block, Mapping):
            newest = block
    if newest is None:
        return Verification(state="unrecorded")
    passed = newest.get("passed")
    state_name: VerificationState = "unresolved"
    if passed is True:
        state_name = "passed"
    elif passed is False:
        state_name = "failed"
    return Verification(
        state=state_name,
        session=_text(newest.get("session")),
        reason=_text(newest.get("reason")),
        failures=_names(newest.get("failures")),
    )


def attribute(
    *,
    plan_dir: str,
    run_id: str,
    repositories: Sequence[overseer_policy_client.Repository],
    state_dir: str | Path | None = None,
) -> Attribution:
    """Decide whether the changes in the declared repository belong to this run.

    The order of the questions is the order they can be answered in: is there
    exactly one repository, can it be read, is there anything to commit at all,
    does the run's evidence establish a baseline, is HEAD still on it, and only
    then is every dirty path accounted for. A clean repository is answered
    before ownership because a checkpoint of nothing cannot be a checkpoint of
    somebody else's work.
    """
    if len(repositories) != 1:
        return Attribution(
            alias="",
            root="",
            baseline="",
            head="",
            refusal="repository_ambiguous",
            detail=(
                f"a checkpoint isolates one repository; this action names "
                f"{len(repositories)}"
            ),
        )
    repository = repositories[0]
    root = Path(repository.path)
    head = commit_message.head_sha(root)
    dirty = dirty_paths(root)
    if not head or dirty is None:
        return Attribution(
            alias=repository.alias,
            root=repository.path,
            baseline="",
            head=head,
            refusal="repository_unreadable",
            detail=f"{repository.path} could not be read as a git worktree",
        )
    activity = read_activity(
        plan_dir=plan_dir,
        run_id=run_id,
        alias=repository.alias,
        state_dir=state_dir,
    )
    partial = Attribution(
        alias=repository.alias,
        root=repository.path,
        baseline=activity.baseline,
        head=head,
        dirty=dirty,
        activity=activity.paths,
    )
    if not dirty:
        return _refuse(partial, "nothing_to_commit", "the worktree is already clean")
    if not activity.observed:
        return _refuse(
            partial,
            "baseline_unknown",
            "this run's records name no head to measure the worktree from",
        )
    if activity.baseline != head:
        return _refuse(
            partial,
            "baseline_moved",
            f"the run recorded {activity.baseline} but HEAD is {head}",
        )
    owned = tuple(path for path in dirty if path in set(activity.paths))
    unattributed = tuple(path for path in dirty if path not in set(activity.paths))
    accounted = Attribution(
        alias=repository.alias,
        root=repository.path,
        baseline=activity.baseline,
        head=head,
        dirty=dirty,
        owned=owned,
        unattributed=unattributed,
        activity=activity.paths,
    )
    if unattributed:
        return _refuse(
            accounted,
            "changes_unattributed",
            "this run's records do not account for "
            + ", ".join(sorted(unattributed)[:_NAMED_LIMIT]),
        )
    return accounted


def authority_refusal(
    ruling: overseer_decisions.Ruling | None,
    *,
    plan_dir: str,
    run_id: str,
    incident_id: int,
) -> tuple[Refusal, str] | None:
    """Return why this ruling does not authorize this checkpoint, or ``None``.

    Scope is checked field by field against what the caller is about to do
    rather than by :meth:`~agent_session_loop.overseer_decisions.Scope.covers`,
    because covers answers "may this decision reach that target" and the
    question here is the stricter one: was the ruling made about *this* plan,
    run and incident. A ruling that named none of them is a ruling that would
    equally authorize a commit in the next plan's repository.
    """
    if ruling is None:
        return ("authority_absent", "no ruling was established")
    if ruling.operation != "commit":
        return (
            "authority_silent",
            f"the ruling is about {ruling.operation!r}, not 'commit'",
        )
    if ruling.refusal is not None:
        return ("authority_refused", ruling.refusal)
    if not ruling.authorized:
        return ("authority_refused", ruling.summary or "not authorized")
    if ruling.unresolved:
        return ("authority_held", ", ".join(ruling.unresolved))
    decision = ruling.decision
    if decision is None:
        return ("authority_absent", "the ruling names no governing decision")
    if not decision.speaks_to("commit"):
        return (
            "authority_silent",
            f"{decision.decision_id} says nothing about commits",
        )
    if decision.stance != "allow":
        return ("authority_held", f"{decision.decision_id} is a {decision.stance}")
    scope = ruling.scope
    if not scope.project:
        return ("authority_unscoped", "the ruling names no project")
    named = (
        ("plan", scope.plan_dir, plan_dir),
        ("run", scope.run_id, run_id),
        ("incident", scope.incident, str(incident_id)),
    )
    for named_field, ruled, wanted in named:
        if not ruled or ruled != wanted:
            return (
                "authority_unscoped",
                f"the ruling's {named_field} is {ruled!r}, not {wanted!r}",
            )
    if not decision.scope.covers(scope):
        return (
            "authority_unscoped",
            f"{decision.decision_id} was not made about this scope",
        )
    return None


Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]
Repositories = Callable[..., tuple[overseer_policy_client.Repository, ...]]


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
        record: overseer_store.ActionRecord,
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


# -- internals -------------------------------------------------------------

#: How many paths a refusal names before it stops. A refusal is a sentence an
#: operator reads, not the change set it is about.
_NAMED_LIMIT = 5


def _git(root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``root``, reporting a failure rather than raising."""
    argv = ["git", *args]
    try:
        return subprocess.run(
            argv,
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(error))


def dirty_paths(root: Path) -> tuple[str, ...] | None:
    """Every path the worktree currently differs from HEAD on, or ``None``.

    ``None`` is a status that could not be taken, which is not the same fact as
    a clean worktree and must never be read as one.
    """
    result = _git(root, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if result.returncode != 0:
        return None
    return _porcelain_paths(result.stdout)


def _porcelain_paths(payload: str) -> tuple[str, ...]:
    """Every path a ``status --porcelain -z`` listing names, renames included.

    A rename or copy record is ``XY <new>\\0<old>\\0``: the original path
    follows as its own NUL-terminated field, so it is consumed here rather than
    read as a malformed entry. Both sides are reported, because a checkpoint
    that owns one end of a rename and not the other owns neither.
    """
    fields = payload.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if ("R" in status or "C" in status) and index < len(fields):
            paths.append(fields[index])
            index += 1
    return tuple(dict.fromkeys(path for path in paths if path))


def _run_records(
    *,
    plan_dir: str,
    run_id: str,
    state_dir: str | Path | None,
) -> tuple[dict[str, Any], ...]:
    """Every artifact record this run published, oldest first.

    Sorted by directory name, which orders them by the zero-padded index the
    runner writes, so "newest" needs no timestamps and no ``stat`` call.
    """
    run_dir = state.plan_state_dir(plan_dir, state_dir) / state.RUNS_DIR_NAME / run_id
    try:
        entries = sorted(
            (entry for entry in run_dir.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError:
        return ()
    prefixes = (overseer_context.TURN_PREFIX, overseer_context.COMMIT_PREFIX)
    records: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.name.startswith(prefixes):
            continue
        record = _read_record(entry / state.RESULT_FILE_NAME)
        if record is not None:
            records.append(record)
    return tuple(records)


def _read_record(path: Path) -> dict[str, Any] | None:
    """One JSON object a run published, or ``None`` when it cannot be read.

    Unreadable and absent are the same answer here on purpose: both mean this
    artifact contributes no evidence, and neither may contribute a *default*.
    An artifact that cannot be read simply does not attribute anything, which
    leaves the checkpoint refusing for want of evidence rather than proceeding
    on an assumption.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _repository_evidence(
    record: Mapping[str, Any], alias: str
) -> tuple[str, tuple[str, ...]]:
    """One record's head and changed paths for one repository, or nothing.

    Two shapes because the run writes two: a commit record names the repository
    it committed in at the top level, and a turn record names every repository
    its write guard compared inside the guard's ``changed`` map.
    """
    if "commit_index" in record:
        if not _names_repository(record.get("repository_alias"), alias):
            return ("", ())
        head = _text(record.get("head_after")) or _text(record.get("head_before"))
        return (head, _relative_paths(record.get("qualified_paths"), alias))
    guard = record.get(_WRITE_GUARD_KEY)
    if not isinstance(guard, Mapping):
        return ("", ())
    changed = guard.get(_CHANGED_KEY)
    if not isinstance(changed, Mapping):
        return ("", ())
    head = ""
    paths: list[str] = []
    for key, entry in changed.items():
        if not _names_repository(key, alias) or not isinstance(entry, Mapping):
            continue
        head = _text(entry.get("head_after")) or _text(entry.get("head_before")) or head
        paths.extend(_relative_paths(entry.get("qualified_paths"), alias))
    return (head, tuple(dict.fromkeys(paths)))


def _names_repository(key: object, alias: str) -> bool:
    """Whether an evidence key is about the repository ``alias`` names."""
    return isinstance(key, str) and key in (alias, LEGACY_REPOSITORY_KEY)


def _relative_paths(value: object, alias: str) -> tuple[str, ...]:
    """Strip the qualifying prefix from every entry that names this repository.

    Evidence is written as ``<alias-or-dot>:<path>``. An entry qualified by some
    other repository is dropped rather than dequalified, because a path that
    belongs to a repository this checkpoint is not committing is not part of
    what it may own.
    """
    if not isinstance(value, list):
        return ()
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        prefix, separator, remainder = item.partition(":")
        if not separator:
            paths.append(item)
            continue
        if _names_repository(prefix, alias) and remainder:
            paths.append(remainder)
    return tuple(dict.fromkeys(paths))


def _refuse(attribution: Attribution, refusal: Refusal, detail: str) -> Attribution:
    """Return the same attribution, refused, so the evidence survives the answer."""
    return Attribution(
        alias=attribution.alias,
        root=attribution.root,
        baseline=attribution.baseline,
        head=attribution.head,
        dirty=attribution.dirty,
        owned=attribution.owned,
        unattributed=attribution.unattributed,
        activity=attribution.activity,
        refusal=refusal,
        detail=detail,
    )


def _drift(error: commit_message.CommitMessageError) -> tuple[Refusal, str]:
    """Tell an edited worktree apart from a re-staged index.

    Both arrive as ``snapshot-drifted`` from
    :func:`~agent_session_loop.commit_message.assert_pathspec_snapshot`, and the
    difference matters to whoever reads the refusal: one means somebody is still
    editing, the other means somebody staged over the set this checkpoint was
    about to commit.
    """
    details = error.details if isinstance(error.details, Mapping) else {}
    if details.get("paths"):
        return ("worktree_drifted", str(error))
    return ("snapshot_mismatch", str(error))


def _key(plan_dir: str, incident_id: int, target: str) -> str:
    """The memo key one action's preparation and execution agree on."""
    return "\x00".join((plan_dir, str(incident_id), target))


def _phrase(refusal: Refusal, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


def _decision_id(ruling: overseer_decisions.Ruling | None) -> str:
    if ruling is None or ruling.decision is None:
        return "no decision"
    return ruling.decision.decision_id


def _same_decision(
    first: overseer_decisions.Ruling | None,
    second: overseer_decisions.Ruling | None,
) -> bool:
    """Whether two rulings rest on the same governing decision, unchanged.

    The stance and the moment are compared as well as the identity, because a
    decision re-read from moved evidence can keep its id and mean the opposite.
    """
    if first is None or second is None:
        return False
    left, right = first.decision, second.decision
    if left is None or right is None:
        return False
    return (
        left.decision_id == right.decision_id
        and left.stance == right.stance
        and left.at == right.at
        and left.operations == right.operations
    )


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""
