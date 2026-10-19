# ruff: noqa: F401, F403, F405, I001
"""The overseer's verify: run a check the plan already declared, and no other.

Every other operation in the scope moves words or commits. This one runs a
program, which makes it the single place in the overseer where a supervisor
could turn into a shell. So the payload it is reached through,
:class:`agent_session_loop.overseer_dispatch.Verify`, carries a *name* --- and
this module is what that name is looked up in.

## The contract is the menu

The argv is never in the action. :func:`select` reads the session the run is
actually stopped on out of the run's own state, reads that session's
``## Completion Contract`` with
:func:`agent_session_loop.contracts.read_completion_contract`, and takes the
check whose ``name`` matches. A name the contract does not declare is
``check_undeclared``, and the refusal lists the names it does declare rather
than inviting another guess.

The consequence is the point: changing what a verify runs means changing a
plan file, which lives in a repository, under the write rules that govern
repositories --- a reviewed edit rather than a string in a supervisor's
argument list. Nothing in this module composes a command, and there is no
parameter through which one could be passed.

Which session is likewise not a choice. It is
``runner_state["active_turn"]["session_id"]``, the session the run is on;
naming one here would let a caller verify session 3's contract against session
9's working tree and read the result as though it meant something.

## The check runs where the action was authorized

:func:`agent_session_loop.verification_models.VerificationContext` already refuses a
check whose location escapes its repository. What it cannot know is which
repositories *this action* was permitted in, because AgentMux answered that
about the action and not about the plan. So the resolved root is checked
against the action's own authorized set, and a check that resolves outside it
is ``scope_unauthorized`` --- the case being a registered plan whose contract
names an alias the authorization did not cover.

The directory the context is built from is read back rather than chosen: a
registered plan resolves against the workspace root discovery recorded for the
run, and a legacy plan against the one repository the action names. A plan with
no registry and more than one repository has no unambiguous root, and that is
``workspace_unrecoverable`` rather than a repository picked by position.

## The whole registry is the scope, not the selected check

A context is built from the plan's *live* manifest, and a manifest is a file in
a repository that a turn can rewrite. Checking only the selected check's
resolved root would therefore admit a plan that grew a participant between the
authorization and now: the named check still resolves under an allowed
repository, and the added one is simply never mentioned. That is a changed plan
being verified under permission granted for a smaller one.

So the *complete* current registry is bound before anything is run. It has to
agree with what discovery persisted for this run --- same registry digest, same
aliases, same resolved roots --- or the participating set the authorization was
issued against is stale and the answer is ``registry_moved``: re-run discovery
and re-read the record, rather than retry. And the participants it declares
have to be exactly the ones the action carries, or an added, moved or removed
repository is ``scope_unauthorized`` even when the selected check resolves
somewhere allowed.

The declared side is compared *before* the workspace is resolved, so an
unpermitted participant is refused without Git ever being run inside it, and
the resolved side is compared afterwards, so a path that only becomes another
repository once symlinks are followed is caught too. Both run in
:func:`select`, which preparation and execution each call in full --- the
window a manifest can be rewritten in is exactly the window between them, so
the check that runs is one the registry still agreed with a moment earlier.

## Which mode a run is in is read from the record, not from the plan

Binding the registry is only a binding if there is a registry to bind. Deciding
that from the live plan alone --- manifest, therefore registered; no manifest,
therefore legacy --- makes *deleting* the manifest the way out of it: the
comparisons above never run, a single-repository action picks its one
repository as the root, and a plan that was authorized as a registered
workspace is verified as though it had never been one.

So the mode comes from the run's own record. A registered run's record carries
the registry it ran under: ``registry_digest`` and the repositories it was
computed from are written together, from one registry, so the record attests to
its own registry and can be checked against itself. Rebuild the registry from
the declarations and its digest is the digest recorded beside them. A record
whose declarations are malformed or do not reproduce the recorded digest has
invalid registry evidence, not legacy provenance. It is refused before either
mode is selected. Only an observation with no workspace root, digest, or
repository declarations represents a legacy run; ordinary legacy verification
remains supported, while overseer discovery excludes those undeclared runs.

A run the record attests as registered, whose plan now declares no registry, is
therefore ``registry_moved`` like any other registry drift, and for the same
reason: the participating set the authorization was issued against is no longer
the plan's own. That the contract may also have become valid for legacy mode is
not a second opinion --- it is the same edit seen from the other side.

## A failing check is a fact, not a refusal

``Receipt.failed`` here means the check ran and did not pass, and the detail
carries the real reason the check reported. That reads out of the dispatcher as
``performed: false`` with no ``refusal``, which is exactly right: nothing
refused the verify, and the answer is no. Reporting a red check as
``executed`` because the operation completed would close an incident on a
verification that failed.

Nothing is retried and nothing is interpreted. One check is named, one check
runs, and its own ``status`` is the answer.

## Reconciliation is decidable, uniquely

A verification has no side effect to look for afterwards, which would normally
make a row a dead end. It is the opposite: because nothing was changed, a row
whose result nobody observed is fully settled by saying so. There is no world
to re-read, no commit to search for, and no honest reason to leave the plan
blocked --- ``result_unobserved`` says the answer was lost, and the remedy is
to ask again.

Durable records this boundary writes: **none**. ``artifact_dir`` is not passed,
so a failed command's streams stay in the bounded preview the result carries and
nothing is retained on disk; the dispatcher owns the ledger row.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import (
    contracts,
    intervention,
    overseer_checkpoint,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store_models,
    plans_errors,
    plans_files,
    plans_registry,
    plans_session_ids,
    verification_checks,
    verification_models,
    workspaces,
)

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other four are somebody else's adapters.
VERIFY_OPERATIONS: tuple[Operation, ...] = ("verify",)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "check_unnamed",
    "run_unreadable",
    "session_unknown",
    "contract_absent",
    "contract_unreadable",
    "check_undeclared",
    "workspace_unrecoverable",
    "check_unresolvable",
    "registry_moved",
    "scope_unauthorized",
    "not_prepared",
    "run_moved",
    "contract_moved",
    "check_failed",
    "check_errored",
    "result_unobserved",
]

#: Why a verification did not happen, or did not pass. Closed, and a consumer
#: contract: operator surfaces read these words, so a spelling change is a
#: contract change. The first five are
#: :data:`~agent_session_loop.overseer_checkpoint.AUTHORITY_REFUSALS` unchanged,
#: so the authority check is one implementation rather than two.
#:
#: * ``payload_mismatch`` -- the action is not a verify.
#: * ``check_unnamed`` -- the payload names no check, so there is nothing to
#:   look up. Distinct from a name that was looked up and not found.
#: * ``run_unreadable`` -- the plan's own state could not be read, so which
#:   session it is on cannot be established.
#: * ``session_unknown`` -- the run records no session, or one that is not a
#:   session id. A contract cannot be read without knowing whose it is.
#: * ``contract_absent`` -- that session declares no completion contract, so it
#:   declares no checks.
#: * ``contract_unreadable`` -- the session file exists and could not be parsed.
#:   Escalates: a malformed contract is an authoring problem, not a retry.
#: * ``check_undeclared`` -- the contract does not declare this name. The
#:   detail lists the names it does declare.
#: * ``workspace_unrecoverable`` -- the directory the checks resolve against
#:   could not be established from the run's own record. Escalates.
#: * ``check_unresolvable`` -- the declared check's own location does not
#:   resolve under its repository. That is the contract's problem, reported
#:   rather than worked around.
#: * ``registry_moved`` -- the plan's repository registry is no longer the one
#:   discovery observed for this run, so the participating set the
#:   authorization was issued against is stale. Covers a registry that changed
#:   and a registry that is gone: a run the record attests as registered whose
#:   plan now declares none has moved just as far, and a legacy reading of it
#:   would be no binding rather than a weaker one. Escalates: the remedy is
#:   fresh discovery and a fresh reading of the record, not another attempt.
#: * ``scope_unauthorized`` -- a repository this action was not authorized for
#:   participates: either the check resolves into one, or the plan's registry
#:   declares or resolves one the action does not carry. Escalates.
#: * ``not_prepared`` -- execution found no preparation to consume.
#: * ``run_moved`` -- the run left the situation the verify was prepared in, so
#:   the check would be answering about a different state than the one asked
#:   about.
#: * ``contract_moved`` -- the contract changed between preparation and
#:   execution, so the name no longer denotes the check that was authorized.
#: * ``check_failed`` -- the check ran and did not pass. Not a refusal of the
#:   operation: the operation happened and the answer is no.
#: * ``check_errored`` -- the check could not be run to a verdict at all.
#: * ``result_unobserved`` -- reconciliation found a row whose result nobody
#:   recorded. A verification changes nothing, so the only thing lost is the
#:   answer, and asking again is the whole remedy.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "check_unnamed",
    "run_unreadable",
    "session_unknown",
    "contract_absent",
    "contract_unreadable",
    "check_undeclared",
    "workspace_unrecoverable",
    "check_unresolvable",
    "registry_moved",
    "scope_unauthorized",
    "not_prepared",
    "run_moved",
    "contract_moved",
    "check_failed",
    "check_errored",
    "result_unobserved",
)

#: The refusals a retry cannot clear. Published rather than acted on: this
#: module holds no ledger and settles no incident, so escalation is something it
#: reports and the dispatcher's caller performs. Each of these is a statement
#: about the plan or the permission rather than about now, and looking again
#: later would produce the same answer.
ESCALATING_REFUSALS: tuple[Refusal, ...] = (
    "contract_unreadable",
    "workspace_unrecoverable",
    "registry_moved",
    "scope_unauthorized",
)

Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]
BuildInventory = Callable[..., overseer_discovery.Inventory]
InspectRun = Callable[..., intervention.RunFacts]
ReadContract = Callable[..., contracts.CompletionContract | None]
RunCheck = Callable[..., verification_models.CheckResult]


@dataclass(frozen=True)
class Selection:
    """The check a name resolved to, or why that name resolved to nothing.

    ``declared`` is carried whether or not the lookup succeeded, because the
    useful thing to tell an operator who named a check that does not exist is
    which ones do.
    """

    session_id: str
    declared: tuple[str, ...]
    check: contracts.VerificationCheck | None = None
    context: verification_models.VerificationContext | None = None
    resolved_root: str = ""
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Whether this is a runnable check rather than a reason there is none."""
        return self.refusal is None


@dataclass(frozen=True)
class Prepared:
    """What preparation established, handed to the execution that consumes it."""

    target: str
    selection: Selection
    progress: str
    ruling: overseer_decisions.Ruling | None
