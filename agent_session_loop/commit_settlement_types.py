# mypy: ignore-errors
# ruff: noqa
"""What a run owes in commits, and how it settles the account before it ends.

A run commits more than once — once per accepted session and once more at the
end — and each commit may span several repositories that have to move together
or not at all. This boundary owns that ledger. It decides *which* repositories
a commit group covers, drives the group through
:mod:`agent_session_loop.commit_groups`, records every attempt that failed
without stopping the run, and finally closes the run out.

Four things live here:

* **Commit target resolution.** :func:`_commit_targets` turns a verification
  context into the repository rows a group commits, and the four resolvers
  above it decide which of those rows a particular boundary actually owes:
  :func:`_dirty_commit_targets` for an unregistered workspace,
  :func:`_registered_session_commit_targets` for a session commit whose scope
  is whatever the workspace write guard authorized,
  :func:`_registered_end_commit_targets` for the end-of-run sweep, and
  :func:`_resolve_recovered_commit_targets` for a group a previous run left
  owed.
* **The commit group lifecycle.** :func:`_run_commit_group` builds or resumes a
  group, runs one ``asl commit`` per target through
  :mod:`agent_session_loop.run_commit`, and persists the group at every phase.
  :func:`_commit_failure_record` and :func:`_unresolved_commit_groups` are the
  other half of the same story: what the run admits it failed to commit.
* **The settlement of a paused turn's retained work.**
  :func:`_settle_paused_retained_work` is the one commit this ledger makes for
  work that was *not* accepted: :func:`_paused_work_commit_targets` picks the
  repositories, the group runs under the ``session-paused`` scope, and the
  outcome is recorded under :data:`PAUSED_WORK_COMMIT_KEY`.
* **Run finish.** :func:`_finish_run` writes the run's own ending — its finish
  timestamp, its duration, the cleared live-action fields — appends
  ``run_finished`` and notifies the AgentMux loop.

The durable records this boundary owns are the ``commit_groups`` list in
``state.json`` (through :func:`_upsert_commit_group`), the
:data:`COMMIT_FAILURES_KEY` and :data:`UNRESOLVED_COMMIT_GROUPS_KEY` payload
keys, the three commit-group events, and the ``run_finished`` event. Every one
of them is written through the ``_RunPersistence`` handle the caller supplies
and under that handle's lock; this module opens no file of its own. The commit
subprocess itself is `run_commit`'s, and it deliberately runs *outside* the
lock: the started and finished payloads come back from that boundary and are
appended here, under the lock, once the subprocess is done.

Five invariants survive the move:

* **A commit is only launched against a repository that has something to
  commit.** ``asl commit`` refuses an empty commit and exits nonzero, so a
  target that is already clean would record a failed attempt that the next run
  reads back as ``skipped-clean`` and finalizes as complete (ASI-19). Every
  resolver here filters on :func:`_commit_target_is_clean` before it returns,
  which is what keeps a group's record honest.
* **The write guard's evidence is the session commit's scope, and it is
  validated rather than trusted.** :func:`_registered_session_commit_targets`
  refuses a guard that did not pass, that carries added, removed, unavailable
  or violating evidence, whose authorized aliases are malformed or duplicated,
  or whose authorized aliases disagree with the aliases its own change records
  name. Only :data:`STAGEABLE_CHANGE_CATEGORIES` become pathspecs; a linked
  worktree's paths are relative to that checkout rather than to the root the
  commit runs in, so such an alias falls back to a repository-wide stage
  instead of naming paths git would reject.
* **A commit failure is recorded, never silent.** A group that ended ``failed``
  or ``pending`` is named by its own durable row, and an attempt that failed
  *before* any group existed leaves no row at all — so
  :func:`_unresolved_commit_groups` summarizes that failure in place. Otherwise
  the one commit failure with no durable evidence would also be the only one a
  run could end silent about.
* **Retries are bounded, and the run stops rather than working past the loss.**
  :data:`MAX_COMMIT_GROUP_RETRIES` caps how many times one group is retried
  inside a single run; past that the group is left owed, in ``failed``, for a
  later run to recover. Each attempt is a real harness call that writes a
  commit message, which is why the budget is small. Exhausting the budget ends
  the run: ``run_loop._pause_for_unsettled_commit_group`` pauses at the next
  selection boundary, because a session launched over an uncommitted one builds
  on changes no commit describes and hands them to whichever message is written
  next (ASI-138, ASI-139).
* **A paused run does not announce that it finished.** :func:`_finish_run`
  emits the AgentMux Finished notification only when the payload is not
  paused — a paused run already notified Paused, and Finished would release the
  daemon's run lease and overwrite the paused attention icon.

One more invariant arrived after them, because the ledger now settles a
*paused* turn's retained work as well as an accepted one's, and the two commits
make different claims. An accepted session's commit says the session completed
and its verification passed. The retained-work commit a *recoverable* pause
makes says only that the turn's changes were written down before the process
exited: the session it names is back to ``pending``, nothing verified the work,
and the next attempt may replace all of it. Three things keep that weaker claim
from reading as the stronger one. The group is scoped ``session-paused`` rather
than ``session``, so it is a different identity from the accepted commit the
same session may still earn later and cannot overwrite its row. The message is
rendered by :func:`run_commit_paused._paused_work_commit_message` and committed with
``--message-file`` rather than generated, both because the pause may *be* the
harness failing and because a marking that says the work is unverified must not
be rewordable by a model — and because the group carries that text durably as
``verbatim_message``, a settlement that failed is *recovered* the same way by
whichever run picks it up next (:func:`_recovered_commit_generation`) rather
than falling back to that run's message harness. And the record under
:data:`PAUSED_WORK_COMMIT_KEY`
names, in ``residue``, whatever the commit failed to take with it — the
settlement re-probes every resolved target on every outcome, rather than
assuming a commit it launched left the tree clean or that a commit that
refused left nothing behind. The commit exists so the relaunch starts on a
clean tree, not so the run can claim the work was good.

The sentence a failed settlement records says the same thing the record does.
``result: "failed"`` covers a commit that wrote nothing, a commit whose ``HEAD``
advanced onto a tree that still held something, and a multi-repository group
that committed part of itself before a later repository refused. So
:func:`_paused_work_failure_message` reads two facts — the failing commit's own
classification, and the repositories the group had already committed — and
renders one of three: work that could not be committed at all, work that was
committed with named residue left behind, or work that was committed in some
repositories and refused in the one the group stopped on. It is the ``message``
the durable ``commit_failures`` entry and the supervisor audit bundle carry, and
it is the one part of the settlement's evidence a reader meets before the
structured record — which is why it must not deny a commit the group's own
``successful_repositories`` already records.

Two things the settlement deliberately does not do, recorded here because a
later reader will otherwise read them as gaps:

* **Registered residue is named, not committed.** :func:`_paused_work_commit_targets`
  stages exactly what the accepted path would have staged, which on the
  registered session branch is derived entirely from write-guard evidence, and
  that evidence excludes the run's own plan directory. So a registered run
  whose plan directory is tracked inside a declared repository still meets the
  next process's startup commit gate with a dirty tree — the difference this
  boundary makes is that ``residue`` names the paths instead of the run going
  silent about them. Widening the staging to cover them would mean the run
  committing its own control plane, and the same exclusion already shapes the
  accepted per-session commit, so this is not a limitation the settlement
  introduced.
* **A run with no commit mode is left alone.** :func:`_paused_work_commit_is_owed`
  refuses ``commit_mode is None`` for anything but a repair child, so an
  authored session's recoverable pause still leaves dirt that a later generated
  repair child's own commit preflight refuses. That is the smaller failure.
  Settling it would mean auto-committing on behalf of an operator who asked for
  no auto-commit, and a commit nobody asked for is not recoverable by
  relaunching.

One consequence of the first is worth stating in the same breath: the
``committed-with-residue`` result is not reachable through the real ``asl
commit`` boundary. That boundary re-reads the same ``git status`` and calls a
commit that left its worktree dirty unsuccessful, so the group fails and the
record says ``failed`` before the residue probe can produce the fourth result.
The result is kept rather than removed because the boundary's verdict and this
module's probe are two independent measurements, and the record must not claim
a clean tree the run never confirmed. Which is also why the probe is
unconditional: ``failed`` says the group did not finish, not that the tree is
as the turn left it, and on a partial multi-repository failure the two claims
differ repository by repository. :func:`_commit_target_residue` therefore runs
against every resolved target whatever the group's verdict was, and it answers
without raising — a target whose ``git status`` cannot be read is recorded with
``clean: None`` rather than allowed to turn a pause into a crash.

The module never imports the runner. Everything it needs is either a leaf it
reads at the owner — ``clock``, ``commit_groups``, ``events``,
``turn_execution_executor`` — or a boundary extracted before it: the git shell in
``review_guard``, the run's write handle in ``run_persistence``, the progress
voice in ``run_reporting``, and the commit subprocess in ``run_commit``. The
two things it still needs from the loop enter as callbacks rather than as
imports: the readiness lifecycle :func:`_finish_run` closes, which Session 15
owns and which arrives as the :data:`RunFinalizer` callback, and the commit
soft-fail whose retry budget lives on the run context, which arrives as
:class:`CommitSoftFail`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    commit_groups,
    events,
    pause_policy,
    review_guard,
    review_guard_git,
    run_commit,
    run_commit_paused,
    run_persistence,
    run_reporting,
    run_selection,
    turn_execution_signals,
    verification_models,
)
from .run_errors import RunError

#: The write-guard change categories `git add --` can stage. The other three
#: `repository_evidence` categories cannot: `untracked_removed` names paths that
#: no longer exist, `worktree_ignore` names paths git refuses to add outright,
#: and `ignore_config` names exclusion sources that may live outside the
#: worktree altogether. An allow-list is what keeps a new category from
#: silently becoming stageable.
STAGEABLE_CHANGE_CATEGORIES: tuple[str, ...] = (
    "tracked",
    "untracked_added",
    "untracked_changed",
)

#: Closes the readiness lifecycle a finished run leaves behind. The
#: implementation is Session 15's ``_finalize_readiness_lifecycle``; it enters
#: :func:`_finish_run` as this callback so the commit ledger never has to reach
#: back into the loop for it.
RunFinalizer = Callable[[dict[str, Any]], None]


class CommitSoftFail(Protocol):
    """Absorb a commit failure and keep the run going.

    The loop keeps the implementation — its own ``_soft_fail_commit`` — because
    the retry budget it spends lives on the run context, and because the
    owed-group settlement at the top of the loop shares it with every other
    commit boundary. It arrives here already bound to that context, as
    ``functools.partial(_soft_fail_commit, ctx)``, exactly as it already does
    for ``turn_settlement.SoftFailCommit``; that is what lets this module
    record a loss only the loop can retry without importing the loop.

    It never raises: a commit that cannot be made is a record and a warning,
    not an ending. For the paused-work settlement that matters twice over,
    since the run is already on its way to a pause and a raise here would
    replace a recoverable pause with a crash.
    """

    def __call__(
        self,
        *,
        scope: str,
        session_id: str | None,
        group: Mapping[str, Any] | None,
        commit: Mapping[str, Any] | None,
        error: str | None,
        message: str,
    ) -> None: ...


@dataclasses.dataclass(frozen=True)
class RetainedWorkContext:
    """The paused turn whose retained work the settlement is about to commit.

    Everything the settlement needs to know about the turn, gathered by the
    caller so this module never reads the loop's context. ``session_label`` is
    :func:`plans_payloads.session_label`'s rendering rather than a bare id, because the
    commit message opens a sentence with it. ``turn_payload`` is the live
    dictionary the run mutates in place: the settlement writes its record onto
    it under :data:`PAUSED_WORK_COMMIT_KEY`, and reads ``workspace_write_guard``
    off it for the registered staging scope. ``artifact_dir`` is where the
    deterministic commit message is written, and ``result_path`` is the turn's
    own durable artifact — re-persisting it after the settlement returns
    belongs to the caller, which owns the file writes this module does not
    make.

    The two flags decide whether the pause owes a commit at all. A review
    session is excluded, matching the accepted path, which never commits one
    either. A repair session with no commit mode owns its own commit, so it is
    included where a plain session with no commit mode is not.
    """

    session_id: str
    session_label: str
    turn_payload: dict[str, Any]
    result_path: Path
    artifact_dir: Path
    is_review_session: bool
    is_repair_session: bool
